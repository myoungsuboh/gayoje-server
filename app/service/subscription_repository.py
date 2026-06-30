"""
Subscription — 사용자 구독 (Pro/Pro+/Pro Max) 상태 + 결제 주기 관리.

[배경 — 2026-05-18]
이전엔 User.subscription_type 만으로 등급 표시했지만 (단순 라벨), 실제 결제
도입 시:
  - 다음 결제일 (next_billing_at) 추적
  - 결제 실패 시 grace period (3일)
  - 해지 예약 (현재 주기 끝까지 유지 후 만료)
  - 업그레이드 프로레이션 (일할 차액)

이 모든 상태가 별도 노드로 명시되어야 분쟁 추적/매출 집계/cron 갱신 가능.

[스키마]
(:User)-[:HAS_SUBSCRIPTION]->(:Subscription {
    id: uuid,
    plan: 'pro' | 'pro_plus' | 'pro_max',
    status: 'active' | 'pending_cancel' | 'canceled' | 'past_due' | 'grace',
    current_period_start: datetime,
    current_period_end: datetime,
    next_billing_at: datetime,
    started_at: datetime,
    canceled_at: datetime?,           # 해지 요청 시각
    cancel_at_period_end: bool,       # true: 만료일까지 유지 / false: 즉시 종료
    grace_until: datetime?,           # 결제 실패 후 grace 만료 시각
    prorated_credit: int,             # 다운/업그레이드 차액 적립 (KRW)
    pg_customer_key: str,             # 토스 customerKey (BillingMethod 와 동일)
    created_at: datetime,
    updated_at: datetime
})

[상태 전이]
- active → pending_cancel (사용자 해지 요청, current_period_end 까지 등급 유지)
- active → past_due (결제 실패 1회, grace_until 설정)
- past_due → grace (결제 재시도 실패, grace 진입)
- grace → canceled (grace 만료, 등급 강등)
- pending_cancel → canceled (current_period_end 도달)
- active → active (업그레이드: 같은 노드의 plan 만 변경 + prorated 처리)

[1 user = 활성 sub 0 or 1 정책]
이전 sub 가 canceled 인 후 새 sub 생성. 다중 active 불가.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# ===== 상수 =====

STATUS_ACTIVE = "active"
STATUS_PENDING_CANCEL = "pending_cancel"
STATUS_CANCELED = "canceled"
STATUS_PAST_DUE = "past_due"
STATUS_GRACE = "grace"

ALL_STATUSES = (
    STATUS_ACTIVE,
    STATUS_PENDING_CANCEL,
    STATUS_CANCELED,
    STATUS_PAST_DUE,
    STATUS_GRACE,
)

# 현재 등급 유지가 인정되는 status (한도 가드 / 모델 선택에서 paid 로 판정).
EFFECTIVELY_PAID_STATUSES = (STATUS_ACTIVE, STATUS_PENDING_CANCEL, STATUS_PAST_DUE, STATUS_GRACE)


# ===== 도메인 모델 =====


@dataclass(frozen=True)
class Subscription:
    id: str
    user_email: str
    plan: str
    status: str
    current_period_start: Optional[str] = None
    current_period_end: Optional[str] = None
    next_billing_at: Optional[str] = None
    started_at: Optional[str] = None
    canceled_at: Optional[str] = None
    cancel_at_period_end: bool = False
    grace_until: Optional[str] = None
    prorated_credit: int = 0
    pg_customer_key: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_email": self.user_email,
            "plan": self.plan,
            "status": self.status,
            "current_period_start": self.current_period_start,
            "current_period_end": self.current_period_end,
            "next_billing_at": self.next_billing_at,
            "started_at": self.started_at,
            "canceled_at": self.canceled_at,
            "cancel_at_period_end": self.cancel_at_period_end,
            "grace_until": self.grace_until,
            "prorated_credit": self.prorated_credit,
            "pg_customer_key": self.pg_customer_key,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ===== Cypher =====


_ENSURE_CONSTRAINT_CYPHER = """\
CREATE CONSTRAINT subscription_id_unique IF NOT EXISTS
FOR (s:Subscription) REQUIRE s.id IS UNIQUE
"""


_ENSURE_USER_EMAIL_INDEX_CYPHER = """\
// user → subscription 빠른 조회용
CREATE INDEX subscription_user_email IF NOT EXISTS
FOR (s:Subscription) ON (s.user_email)
"""


_CREATE_SUBSCRIPTION_CYPHER = """\
MATCH (u:User {email: $email})
CREATE (s:Subscription {
    id: $id,
    user_email: $email,
    plan: $plan,
    status: $status,
    current_period_start: datetime($current_period_start),
    current_period_end: datetime($current_period_end),
    next_billing_at: datetime($next_billing_at),
    started_at: datetime(),
    cancel_at_period_end: false,
    prorated_credit: 0,
    pg_customer_key: $pg_customer_key,
    created_at: datetime(),
    updated_at: datetime()
})
CREATE (u)-[:HAS_SUBSCRIPTION]->(s)
RETURN s {.*,
    current_period_start: toString(s.current_period_start),
    current_period_end: toString(s.current_period_end),
    next_billing_at: toString(s.next_billing_at),
    started_at: toString(s.started_at),
    created_at: toString(s.created_at),
    updated_at: toString(s.updated_at)
} AS sub
"""


# 사용자의 최신 (active 우선, 없으면 가장 최근) 구독 조회.
_GET_LATEST_SUBSCRIPTION_CYPHER = """\
MATCH (u:User {email: $email})-[:HAS_SUBSCRIPTION]->(s:Subscription)
WITH s
ORDER BY
  CASE s.status
    WHEN 'active' THEN 0
    WHEN 'past_due' THEN 1
    WHEN 'grace' THEN 2
    WHEN 'pending_cancel' THEN 3
    WHEN 'canceled' THEN 4
    ELSE 99
  END,
  s.created_at DESC
LIMIT 1
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


_GET_BY_ID_CYPHER = """\
MATCH (s:Subscription {id: $id})
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


# 갱신용 패치 — 호출자가 변경할 필드만 dict 로 넘김. None 인 필드는 SET 안 함.
# 보안: status 등 핵심 필드는 별도 함수로 캡슐화해서 임의 변경 금지.
_UPDATE_STATUS_CYPHER = """\
MATCH (s:Subscription {id: $id})
SET s.status = $status,
    s.updated_at = datetime()
RETURN s.id AS id
"""


_RENEW_PERIOD_CYPHER = """\
// 정기결제 성공 후 다음 주기로 이동.
MATCH (s:Subscription {id: $id})
SET s.current_period_start = datetime($current_period_start),
    s.current_period_end = datetime($current_period_end),
    s.next_billing_at = datetime($next_billing_at),
    s.status = 'active',
    s.grace_until = null,
    s.updated_at = datetime()
RETURN s.id AS id
"""


_SCHEDULE_CANCEL_CYPHER = """\
// 사용자 해지 요청 — 현재 주기 끝까지 등급 유지.
MATCH (s:Subscription {id: $id})
SET s.cancel_at_period_end = true,
    s.canceled_at = datetime(),
    s.status = 'pending_cancel',
    s.updated_at = datetime()
RETURN s.id AS id
"""


_RESUME_CYPHER = """\
// pending_cancel 상태에서 해지 취소 — active 로 복귀.
MATCH (s:Subscription {id: $id})
WHERE s.status = 'pending_cancel'
SET s.cancel_at_period_end = false,
    s.canceled_at = null,
    s.status = 'active',
    s.updated_at = datetime()
RETURN s.id AS id
"""


_TERMINATE_CYPHER = """\
// 즉시 종료 (만료 도달 / grace 만료 / admin 강제 해지).
MATCH (s:Subscription {id: $id})
SET s.status = 'canceled',
    s.canceled_at = COALESCE(s.canceled_at, datetime()),
    s.cancel_at_period_end = false,
    s.updated_at = datetime()
RETURN s.id AS id
"""


_MARK_PAST_DUE_CYPHER = """\
// 결제 실패 → grace 진입.
//
// [2026-05-18 critical fix]
// 이전: grace_until 을 매번 새 값으로 set → 사용자가 카드 안 고치고 grace 안에서
// 재시도 실패할 때마다 grace_until 이 미래로 계속 연장 → 영원히 강등 안 됨.
// 수정: 이미 grace 상태이고 grace_until 이 있으면 그 값 유지 (첫 진입 시만 set).
// active → grace 처음 진입 시는 새 grace_until 적용.
MATCH (s:Subscription {id: $id})
SET s.status = 'grace',
    s.grace_until = CASE
        WHEN s.grace_until IS NULL THEN datetime($grace_until)
        ELSE s.grace_until
    END,
    s.updated_at = datetime()
RETURN s.id AS id,
       toString(s.grace_until) AS grace_until
"""


_TOUCH_RENEWAL_ATTEMPT_CYPHER = """\
// cron 이 renewal 시도 직전 호출 — last_renewal_attempt_at 갱신으로 같은 cycle 안
// 중복 시도 차단. status 변경 없음.
MATCH (s:Subscription {id: $id})
SET s.last_renewal_attempt_at = datetime(),
    s.updated_at = datetime()
RETURN s.id AS id
"""


# [2026-05-18] 이메일 알림 멱등성 마커 — 같은 사용자에게 같은 알림 반복 전송 방지.
# notification_kind: 'grace_entered' | 'expiring_soon' (강등 후 안내는 별도 1회성)
_MARK_NOTIFIED_CYPHER = """\
MATCH (s:Subscription {id: $id})
SET s[$field] = datetime(),
    s.updated_at = datetime()
RETURN s.id AS id
"""


_CLEAR_NOTIFIED_CYPHER = """\
// 사용자가 재결제 성공 등으로 grace 탈출 시 알림 마커 cleanup → 다음 grace 진입 시
// 다시 알림 받을 수 있게.
MATCH (s:Subscription {id: $id})
REMOVE s.notified_grace_at, s.notified_expiring_at
SET s.updated_at = datetime()
RETURN s.id AS id
"""


_UPGRADE_PLAN_CYPHER = """\
// 업그레이드 — plan 변경 + prorated_credit 갱신.
MATCH (s:Subscription {id: $id})
SET s.plan = $plan,
    s.prorated_credit = $prorated_credit,
    s.updated_at = datetime()
RETURN s.id AS id
"""


# ===== 부팅 헬퍼 =====


async def ensure_subscription_constraints() -> None:
    for cypher, label in [
        (_ENSURE_CONSTRAINT_CYPHER, "id UNIQUE"),
        (_ENSURE_USER_EMAIL_INDEX_CYPHER, "user_email INDEX"),
    ]:
        try:
            await neo4j_client.run_cypher(cypher)
            logger.info("subscription: %s 제약/인덱스 ensure 완료", label)
        except Exception as e:  # noqa: BLE001
            logger.warning("subscription: %s 실패 (%s)", label, e)


# ===== 함수 =====


def _row_to_obj(row: dict) -> Subscription:
    s = row.get("sub") or row
    return Subscription(
        id=s.get("id") or "",
        user_email=s.get("user_email") or "",
        plan=s.get("plan") or "",
        status=s.get("status") or "",
        current_period_start=s.get("current_period_start"),
        current_period_end=s.get("current_period_end"),
        next_billing_at=s.get("next_billing_at"),
        started_at=s.get("started_at"),
        canceled_at=s.get("canceled_at"),
        cancel_at_period_end=bool(s.get("cancel_at_period_end")),
        grace_until=s.get("grace_until"),
        prorated_credit=int(s.get("prorated_credit") or 0),
        pg_customer_key=s.get("pg_customer_key"),
        created_at=s.get("created_at"),
        updated_at=s.get("updated_at"),
    )


async def create_subscription(
    *,
    user_email: str,
    plan: str,
    pg_customer_key: str,
    current_period_start: datetime,
    current_period_end: datetime,
    next_billing_at: datetime,
) -> Optional[Subscription]:
    """첫 결제 성공 후 호출. status='active'."""
    new_id = uuid.uuid4().hex
    records = await neo4j_client.run_cypher(
        _CREATE_SUBSCRIPTION_CYPHER,
        {
            "id": new_id,
            "email": user_email,
            "plan": plan,
            "status": STATUS_ACTIVE,
            "current_period_start": current_period_start.isoformat(),
            "current_period_end": current_period_end.isoformat(),
            "next_billing_at": next_billing_at.isoformat(),
            "pg_customer_key": pg_customer_key or "",
        },
    )
    if not records:
        return None
    return _row_to_obj(records[0])


async def get_latest_subscription(user_email: str) -> Optional[Subscription]:
    """사용자의 가장 최신 구독 (active 우선)."""
    records = await neo4j_client.run_cypher(
        _GET_LATEST_SUBSCRIPTION_CYPHER, {"email": user_email}
    )
    if not records:
        return None
    return _row_to_obj(records[0])


async def get_subscription_by_id(sub_id: str) -> Optional[Subscription]:
    records = await neo4j_client.run_cypher(_GET_BY_ID_CYPHER, {"id": sub_id})
    if not records:
        return None
    return _row_to_obj(records[0])


async def renew_period(
    sub_id: str,
    *,
    current_period_start: datetime,
    current_period_end: datetime,
    next_billing_at: datetime,
) -> bool:
    """정기결제 성공 후 다음 주기로 이동. status→active, grace_until→null."""
    records = await neo4j_client.run_cypher(
        _RENEW_PERIOD_CYPHER,
        {
            "id": sub_id,
            "current_period_start": current_period_start.isoformat(),
            "current_period_end": current_period_end.isoformat(),
            "next_billing_at": next_billing_at.isoformat(),
        },
    )
    return bool(records and records[0].get("id"))


async def schedule_cancel(sub_id: str) -> bool:
    """사용자 해지 요청 — 현재 주기 끝까지 등급 유지."""
    records = await neo4j_client.run_cypher(_SCHEDULE_CANCEL_CYPHER, {"id": sub_id})
    return bool(records and records[0].get("id"))


async def resume(sub_id: str) -> bool:
    """pending_cancel 에서 해지 취소 → active."""
    records = await neo4j_client.run_cypher(_RESUME_CYPHER, {"id": sub_id})
    return bool(records and records[0].get("id"))


async def terminate(sub_id: str) -> bool:
    """즉시 종료 (만료 도달 / grace 만료 / admin 강제). 등급 강등 처리는 호출자."""
    records = await neo4j_client.run_cypher(_TERMINATE_CYPHER, {"id": sub_id})
    return bool(records and records[0].get("id"))


async def mark_grace(sub_id: str, grace_until: datetime) -> bool:
    """결제 실패 → grace 진입. grace_until 이후 cron 이 terminate."""
    records = await neo4j_client.run_cypher(
        _MARK_PAST_DUE_CYPHER,
        {"id": sub_id, "grace_until": grace_until.isoformat()},
    )
    return bool(records and records[0].get("id"))


async def touch_renewal_attempt(sub_id: str) -> bool:
    """cron 이 renewal 시도 직전 호출 — last_renewal_attempt_at 갱신. status 변경 없음."""
    records = await neo4j_client.run_cypher(
        _TOUCH_RENEWAL_ATTEMPT_CYPHER, {"id": sub_id},
    )
    return bool(records and records[0].get("id"))


# [2026-05-18] 이메일 알림 멱등성
NOTIF_GRACE_ENTERED = "notified_grace_at"      # 결제 실패 → grace 첫 진입 시 1회
NOTIF_EXPIRING_SOON = "notified_expiring_at"   # 강등 24시간 전 1회


async def mark_notified(sub_id: str, field: str) -> bool:
    """이메일 발송 후 마커 set — 같은 종류 알림 중복 발송 방지."""
    if field not in (NOTIF_GRACE_ENTERED, NOTIF_EXPIRING_SOON):
        raise ValueError(f"invalid notif field: {field}")
    records = await neo4j_client.run_cypher(
        _MARK_NOTIFIED_CYPHER, {"id": sub_id, "field": field},
    )
    return bool(records and records[0].get("id"))


async def clear_notification_markers(sub_id: str) -> bool:
    """재결제 성공 등으로 grace 탈출 시 호출 — 다음 grace 진입 때 다시 알림 가능."""
    records = await neo4j_client.run_cypher(
        _CLEAR_NOTIFIED_CYPHER, {"id": sub_id},
    )
    return bool(records and records[0].get("id"))


async def upgrade_plan(sub_id: str, *, plan: str, prorated_credit: int) -> bool:
    """업그레이드 — plan 변경 + prorated_credit 적립 (다음 결제일 차감용)."""
    records = await neo4j_client.run_cypher(
        _UPGRADE_PLAN_CYPHER,
        {"id": sub_id, "plan": plan, "prorated_credit": int(prorated_credit)},
    )
    return bool(records and records[0].get("id"))


async def update_status(sub_id: str, status: str) -> bool:
    """status 직접 변경 — 일반 흐름은 위 캡슐화된 함수 사용 권장."""
    if status not in ALL_STATUSES:
        raise ValueError(f"invalid status: {status}")
    records = await neo4j_client.run_cypher(
        _UPDATE_STATUS_CYPHER, {"id": sub_id, "status": status}
    )
    return bool(records and records[0].get("id"))
