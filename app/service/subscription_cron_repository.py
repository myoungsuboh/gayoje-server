"""
Subscription cron 조회 — 갱신 대상 / 만료 대상 추출.

[배경]
정기결제 cron 이 매시간 실행되어:
  1) next_billing_at <= now 인 active 구독 → 결제 시도
  2) grace_until <= now 인 grace 구독 → terminate + 강등
  3) cancel_at_period_end=true && current_period_end <= now → terminate + 강등

이 세 가지 시점을 정확히 잡아야 매출/UX 가 일치.
"""
from __future__ import annotations

import logging
from typing import List

from app.clients import neo4j_client
from app.service.subscription_repository import (
    STATUS_ACTIVE,
    STATUS_GRACE,
    STATUS_PAST_DUE,
    STATUS_PENDING_CANCEL,
    Subscription,
    _row_to_obj,
)

logger = logging.getLogger(__name__)


_RENEWAL_DUE_CYPHER = """\
// 갱신 대상 두 종류:
//   1) active + next_billing_at <= now : 정상 정기결제 시점 도래
//   2) grace  + grace_until > now      : 결제 실패 후 재시도 (grace 만료 전)
// 단 같은 sub 가 한 cycle 안 반복 호출 안 되도록 최근 12시간 안 시도 한 건 제외
// (last_renewal_attempt_at 가 없거나 12시간 전 이전).
MATCH (s:Subscription)
WHERE (
    (s.status = 'active'
     AND s.next_billing_at IS NOT NULL
     AND s.next_billing_at <= datetime())
    OR
    (s.status = 'grace'
     AND s.grace_until IS NOT NULL
     AND s.grace_until > datetime())
  )
  AND (
    s.last_renewal_attempt_at IS NULL
    OR s.last_renewal_attempt_at <= datetime() - duration({hours: 12})
  )
WITH s
ORDER BY
  // grace 가 만료 직전이면 먼저 시도 (마지막 기회), active 는 next_billing_at 순
  CASE s.status WHEN 'grace' THEN 0 ELSE 1 END,
  s.next_billing_at ASC
LIMIT $limit
RETURN s {.*,
    current_period_start: toString(s.current_period_start),
    current_period_end: toString(s.current_period_end),
    next_billing_at: toString(s.next_billing_at),
    started_at: toString(s.started_at),
    canceled_at: toString(s.canceled_at),
    grace_until: toString(s.grace_until),
    last_renewal_attempt_at: toString(s.last_renewal_attempt_at),
    created_at: toString(s.created_at),
    updated_at: toString(s.updated_at)
} AS sub
"""


_GRACE_EXPIRED_CYPHER = """\
MATCH (s:Subscription {status: 'grace'})
WHERE s.grace_until IS NOT NULL
  AND s.grace_until <= datetime()
WITH s
LIMIT $limit
RETURN s {.*,
    current_period_start: toString(s.current_period_start),
    current_period_end: toString(s.current_period_end),
    next_billing_at: toString(s.next_billing_at),
    started_at: toString(s.started_at),
    canceled_at: toString(s.canceled_at),
    grace_until: toString(s.grace_until),
    created_at: toString(s.created_at),
    updated_at: toString(s.updated_at)
} AS sub
"""


_PENDING_CANCEL_DUE_CYPHER = """\
MATCH (s:Subscription {status: 'pending_cancel'})
WHERE s.current_period_end IS NOT NULL
  AND s.current_period_end <= datetime()
WITH s
LIMIT $limit
RETURN s {.*,
    current_period_start: toString(s.current_period_start),
    current_period_end: toString(s.current_period_end),
    next_billing_at: toString(s.next_billing_at),
    started_at: toString(s.started_at),
    canceled_at: toString(s.canceled_at),
    grace_until: toString(s.grace_until),
    created_at: toString(s.created_at),
    updated_at: toString(s.updated_at)
} AS sub
"""


async def find_renewal_due(limit: int = 100) -> List[Subscription]:
    """next_billing_at 도래 — active + past_due 모두 포함 (past_due 는 재시도)."""
    records = await neo4j_client.run_cypher(_RENEWAL_DUE_CYPHER, {"limit": int(limit)})
    return [_row_to_obj(r) for r in records if (r.get("sub") or {}).get("id")]


async def find_grace_expired(limit: int = 100) -> List[Subscription]:
    """grace_until 만료 → terminate 대상."""
    records = await neo4j_client.run_cypher(_GRACE_EXPIRED_CYPHER, {"limit": int(limit)})
    return [_row_to_obj(r) for r in records if (r.get("sub") or {}).get("id")]


async def find_pending_cancel_due(limit: int = 100) -> List[Subscription]:
    """해지 예약 + 만료 → terminate 대상."""
    records = await neo4j_client.run_cypher(_PENDING_CANCEL_DUE_CYPHER, {"limit": int(limit)})
    return [_row_to_obj(r) for r in records if (r.get("sub") or {}).get("id")]


# [2026-05-18] 강등 임박 알림 대상 — grace 만료 24시간 이내 + 아직 알림 미발송.
_EXPIRING_SOON_CYPHER = """\
MATCH (s:Subscription {status: 'grace'})
WHERE s.grace_until IS NOT NULL
  AND s.grace_until > datetime()
  AND s.grace_until <= datetime() + duration({hours: 24})
  AND s.notified_expiring_at IS NULL
WITH s
LIMIT $limit
RETURN s {.*,
    current_period_start: toString(s.current_period_start),
    current_period_end: toString(s.current_period_end),
    next_billing_at: toString(s.next_billing_at),
    started_at: toString(s.started_at),
    canceled_at: toString(s.canceled_at),
    grace_until: toString(s.grace_until),
    created_at: toString(s.created_at),
    updated_at: toString(s.updated_at)
} AS sub
"""


async def find_expiring_soon(limit: int = 200) -> List[Subscription]:
    """강등 24시간 전 — '마지막 안내' 이메일 발송 대상.

    grace_until > now AND grace_until <= now + 24h AND notified_expiring_at IS NULL
    """
    records = await neo4j_client.run_cypher(_EXPIRING_SOON_CYPHER, {"limit": int(limit)})
    return [_row_to_obj(r) for r in records if (r.get("sub") or {}).get("id")]
