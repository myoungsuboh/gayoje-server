"""
PaddleSubscription 영속화 — 웹훅이 받은 구독 스냅샷 저장/조회.

[용도]
1. 고객포털 세션 생성 — Paddle API 가 customer_id 를 요구한다.
2. FE /pricing 구독현황 표시 — status / current_period_end.

[모델]
(:User {email})-[:HAS_PADDLE_SUBSCRIPTION]->(:PaddleSubscription)
사용자당 1개(MERGE) — Paddle 구독이 바뀌면 같은 노드를 갱신한다.
진실원천은 Paddle 이고 이건 스냅샷 — 표시/포털 진입용이지 entitlement 판단용이 아니다
(entitlement 는 User.subscription_type, admin_repository.change_subscription 이 관리).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from app.clients import neo4j_client

_UPSERT_CYPHER = """\
MATCH (u:User {email: $email})
MERGE (u)-[:HAS_PADDLE_SUBSCRIPTION]->(s:PaddleSubscription)
// [out-of-order 방어] 웹훅은 순서 보장이 없다 — 이미 더 최신(occurred_at)이 반영돼 있으면
// 낡은 이벤트로 덮어쓰지 않는다(apply=false). occurred_at 미제공('')이면 무조건 갱신(정보 없음).
// occurred_at 은 _norm_occurred_at 으로 고정 포맷 정규화 → 사전순 비교 = 시간순.
WITH s, ($occurred_at = '' OR coalesce(s.occurred_at, '') = '' OR s.occurred_at <= $occurred_at) AS apply
FOREACH (_ IN CASE WHEN apply THEN [1] ELSE [] END |
  SET s.subscription_id = $subscription_id,
      s.customer_id = $customer_id,
      s.status = $status,
      s.price_id = $price_id,
      s.current_period_end = $current_period_end,
      s.occurred_at = $occurred_at,
      s.updated_at = datetime()
)
RETURN s {.subscription_id, .customer_id, .status, .price_id, .current_period_end} AS s, apply
"""

_GET_CYPHER = """\
MATCH (u:User {email: $email})-[:HAS_PADDLE_SUBSCRIPTION]->(s:PaddleSubscription)
RETURN s {.subscription_id, .customer_id, .status, .price_id, .current_period_end} AS s
"""


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _norm_occurred_at(ts: str) -> str:
    """ISO8601 → 고정 포맷(UTC, 마이크로초 6자리) 정규화 — 사전순 비교가 시간순과 일치하도록.
    ('…00Z' vs '…00.000Z' 처럼 밀리초 자릿수가 섞이면 사전순이 역전된다.)
    파싱 불가 문자열은 '' (순서 정보 없음 = 무조건 갱신)."""
    raw = (ts or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def upsert(
    *,
    email: str,
    subscription_id: str,
    customer_id: str,
    status: str,
    price_id: Optional[str],
    current_period_end: Optional[str],
    occurred_at: str = "",
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """구독 스냅샷 upsert.

    Returns:
        (snapshot, applied)
        - snapshot=None, applied=False : 대상 User 없음
        - applied=False (snapshot 존재): stale 이벤트 — 이미 더 최신 상태가 반영돼 있어 skip.
          호출자(웹훅)는 이 경우 entitlement 변경도 건너뛰어야 한다 (옛 이벤트의 등급 부활 방지).
        - applied=True : 정상 반영
    """
    rows = await neo4j_client.run_cypher(
        _UPSERT_CYPHER,
        {
            "email": _norm_email(email),
            "subscription_id": subscription_id or "",
            "customer_id": customer_id or "",
            "status": status or "",
            "price_id": price_id or "",
            "current_period_end": current_period_end or "",
            "occurred_at": _norm_occurred_at(occurred_at),
        },
    )
    if not rows:
        return None, False
    return rows[0]["s"], bool(rows[0].get("apply"))


async def get_by_email(email: str) -> Optional[Dict[str, Any]]:
    """사용자의 Paddle 구독 스냅샷 — 없으면 None."""
    rows = await neo4j_client.run_cypher(_GET_CYPHER, {"email": _norm_email(email)})
    return rows[0]["s"] if rows else None


_SET_CUSTOMER_ID_CYPHER = """\
MATCH (u:User {email: $email})-[:HAS_PADDLE_SUBSCRIPTION]->(s:PaddleSubscription)
SET s.customer_id = $customer_id, s.updated_at = datetime()
RETURN s.customer_id AS customer_id
"""


async def set_customer_id(email: str, customer_id: str) -> bool:
    """저장된 customer_id 만 보정(self-heal). 포털 세션 때 stale/환경 불일치 id 를 Paddle
    에서 이메일로 재조회해 교체할 때 쓴다 — occurred_at 게이트와 무관한 '정정'이라 별도 경로.
    구독 노드가 없으면(웹훅 미수신) no-op(False) — 포털은 재조회 id 로 계속 진행한다."""
    rows = await neo4j_client.run_cypher(
        _SET_CUSTOMER_ID_CYPHER,
        {"email": _norm_email(email), "customer_id": customer_id or ""},
    )
    return bool(rows)
