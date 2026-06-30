"""
Payment — 실제 결제 시도 기록 (성공/실패/환불).

[배경]
PG 응답 / 환불 / 분쟁 시 evidence 보관용. 매월 정기결제 1회 + 단건 (업그레이드
차액 등) 호출마다 별도 노드 생성. 매출 대시보드도 이 노드 기반으로 정확화.

[스키마]
(:Subscription)-[:HAS_PAYMENT]->(:Payment {
    id: uuid,
    subscription_id: str,
    user_email: str,                # denorm — 빠른 조회용
    amount: int,                    # 결제 금액 (KRW, 정수)
    currency: str,                  # 'KRW'
    status: 'paid' | 'failed' | 'refunded' | 'partial_refund' | 'pending',
    purpose: 'initial' | 'renewal' | 'upgrade_proration' | 'manual',
    pg_payment_key: str,            # 토스 paymentKey
    pg_order_id: str,               # 우리 orderId (UNIQUE — 멱등성)
    method: 'card' | 'kakao' | 'naver' | 'toss',
    paid_at: datetime?,
    failed_at: datetime?,
    fail_reason: str?,
    refunded_at: datetime?,
    refund_amount: int,             # 누적 환불 금액
    refund_reason: str?,
    raw_response: str,              # PG 응답 JSON 원본 (str, 분쟁 evidence)
    created_at: datetime,
    updated_at: datetime
})

[멱등성]
pg_order_id UNIQUE — 같은 주문 번호 두 번 결제 못 함.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# ===== 상수 =====

STATUS_PENDING = "pending"
STATUS_PAID = "paid"
STATUS_FAILED = "failed"
STATUS_REFUNDED = "refunded"
STATUS_PARTIAL_REFUND = "partial_refund"

PURPOSE_INITIAL = "initial"
PURPOSE_RENEWAL = "renewal"
PURPOSE_UPGRADE_PRORATION = "upgrade_proration"
PURPOSE_MANUAL = "manual"
# [2026-05] 쿠폰 무료 적용 — PG 호출 안 하고 0원 기록만. cron 이 free_until 지나면 정상 결제.
PURPOSE_COUPON_FREE = "coupon_free"


# ===== 도메인 모델 =====


@dataclass(frozen=True)
class Payment:
    id: str
    subscription_id: str
    user_email: str
    amount: int
    currency: str
    status: str
    purpose: str
    pg_payment_key: Optional[str] = None
    pg_order_id: Optional[str] = None
    method: Optional[str] = None
    paid_at: Optional[str] = None
    failed_at: Optional[str] = None
    fail_reason: Optional[str] = None
    refunded_at: Optional[str] = None
    refund_amount: int = 0
    refund_reason: Optional[str] = None
    raw_response: Optional[str] = None
    receipt_url: Optional[str] = None   # [2026-05-18] 토스 영수증 URL — 사용자 노출용
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "subscription_id": self.subscription_id,
            "user_email": self.user_email,
            "amount": self.amount,
            "currency": self.currency,
            "status": self.status,
            "purpose": self.purpose,
            "pg_payment_key": self.pg_payment_key,
            "pg_order_id": self.pg_order_id,
            "method": self.method,
            "paid_at": self.paid_at,
            "failed_at": self.failed_at,
            "fail_reason": self.fail_reason,
            "refunded_at": self.refunded_at,
            "refund_amount": self.refund_amount,
            "refund_reason": self.refund_reason,
            "receipt_url": self.receipt_url,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            # raw_response 는 admin view 전용 — 일반 dict 변환에선 제외
        }


# ===== Cypher =====


_ENSURE_PAYMENT_ID_CYPHER = """\
CREATE CONSTRAINT payment_id_unique IF NOT EXISTS
FOR (p:Payment) REQUIRE p.id IS UNIQUE
"""


_ENSURE_PG_ORDER_ID_CYPHER = """\
// 멱등성 — 같은 주문 두 번 결제 차단
CREATE CONSTRAINT payment_pg_order_id_unique IF NOT EXISTS
FOR (p:Payment) REQUIRE p.pg_order_id IS UNIQUE
"""


_ENSURE_USER_EMAIL_INDEX_CYPHER = """\
CREATE INDEX payment_user_email IF NOT EXISTS
FOR (p:Payment) ON (p.user_email)
"""


_ENSURE_CREATED_AT_INDEX_CYPHER = """\
// 매출 집계용 (월/연 시간 윈도우)
CREATE INDEX payment_created_at IF NOT EXISTS
FOR (p:Payment) ON (p.created_at)
"""


_CREATE_PAYMENT_PENDING_CYPHER = """\
MATCH (s:Subscription {id: $subscription_id})
CREATE (p:Payment {
    id: $id,
    subscription_id: $subscription_id,
    user_email: $user_email,
    amount: $amount,
    currency: 'KRW',
    status: 'pending',
    purpose: $purpose,
    pg_order_id: $pg_order_id,
    method: $method,
    refund_amount: 0,
    created_at: datetime(),
    updated_at: datetime()
})
CREATE (s)-[:HAS_PAYMENT]->(p)
RETURN p.id AS id
"""


_MARK_PAID_CYPHER = """\
MATCH (p:Payment {id: $id})
SET p.status = 'paid',
    p.pg_payment_key = $pg_payment_key,
    p.paid_at = datetime(),
    p.raw_response = $raw_response,
    p.receipt_url = $receipt_url,
    p.updated_at = datetime()
RETURN p.id AS id
"""


_MARK_FAILED_CYPHER = """\
MATCH (p:Payment {id: $id})
SET p.status = 'failed',
    p.failed_at = datetime(),
    p.fail_reason = $fail_reason,
    p.raw_response = $raw_response,
    p.updated_at = datetime()
RETURN p.id AS id
"""


_REFUND_CYPHER = """\
// [2026-05-18] 환불 1건 = RefundRecord 노드 1개. 부분환불 N번이면 N개 추가.
// Payment.refund_amount 는 누적 합산 (요약용). 정확한 이력은 RefundRecord 노드.
MATCH (p:Payment {id: $id})
SET p.refund_amount = COALESCE(p.refund_amount, 0) + $refund_amount,
    p.refunded_at = COALESCE(p.refunded_at, datetime()),  // 첫 환불 시각 유지
    p.refund_reason = $refund_reason,                      // 가장 최근 사유 (요약)
    p.status = CASE
        WHEN COALESCE(p.refund_amount, 0) + $refund_amount >= p.amount THEN 'refunded'
        ELSE 'partial_refund'
    END,
    p.raw_response = COALESCE($raw_response, p.raw_response),
    p.updated_at = datetime()
CREATE (r:RefundRecord {
    id: $refund_id,
    payment_id: p.id,
    user_email: p.user_email,
    amount: $refund_amount,
    reason: $refund_reason,
    raw_response: $raw_response,
    created_at: datetime()
})
CREATE (p)-[:HAS_REFUND]->(r)
RETURN p.id AS id,
       p.status AS status,
       p.refund_amount AS refund_amount,
       r.id AS refund_id,
       toString(r.created_at) AS refund_created_at
"""


_ENSURE_REFUND_RECORD_ID_CYPHER = """\
CREATE CONSTRAINT refund_record_id_unique IF NOT EXISTS
FOR (r:RefundRecord) REQUIRE r.id IS UNIQUE
"""


_ENSURE_REFUND_USER_EMAIL_INDEX_CYPHER = """\
CREATE INDEX refund_record_user_email IF NOT EXISTS
FOR (r:RefundRecord) ON (r.user_email)
"""


_LIST_REFUNDS_BY_PAYMENT_CYPHER = """\
MATCH (p:Payment {id: $payment_id})-[:HAS_REFUND]->(r:RefundRecord)
RETURN r {.*, created_at: toString(r.created_at)} AS refund
ORDER BY r.created_at ASC
"""


_GET_BY_ID_CYPHER = """\
MATCH (p:Payment {id: $id})
RETURN p {.*,
    paid_at: toString(p.paid_at),
    failed_at: toString(p.failed_at),
    refunded_at: toString(p.refunded_at),
    created_at: toString(p.created_at),
    updated_at: toString(p.updated_at)
} AS payment
"""


_GET_BY_PG_ORDER_ID_CYPHER = """\
MATCH (p:Payment {pg_order_id: $pg_order_id})
RETURN p {.*,
    paid_at: toString(p.paid_at),
    failed_at: toString(p.failed_at),
    refunded_at: toString(p.refunded_at),
    created_at: toString(p.created_at),
    updated_at: toString(p.updated_at)
} AS payment
"""


_LIST_BY_USER_CYPHER = """\
MATCH (p:Payment {user_email: $email})
WITH p
ORDER BY p.created_at DESC
LIMIT $limit
RETURN p {.*,
    paid_at: toString(p.paid_at),
    failed_at: toString(p.failed_at),
    refunded_at: toString(p.refunded_at),
    created_at: toString(p.created_at),
    updated_at: toString(p.updated_at)
} AS payment
"""

# 리컨실리에이션 대상 — status=pending 이면서 created_at 이 [now-max_age, now-min_age]
# 범위. min_age(older_than) 로 아직 진행 중일 수 있는 갓 생성 결제와의 race 를 피하고,
# max_age 로 너무 오래된 건(이미 별도 처리/만료) 제외.
_FIND_STALE_PENDING_CYPHER = """\
MATCH (p:Payment {status: 'pending'})
WHERE p.created_at <= datetime() - duration({seconds: $older_than_seconds})
  AND p.created_at >= datetime() - duration({days: $max_age_days})
WITH p
ORDER BY p.created_at ASC
LIMIT $limit
RETURN p {.*,
    paid_at: toString(p.paid_at),
    failed_at: toString(p.failed_at),
    refunded_at: toString(p.refunded_at),
    created_at: toString(p.created_at),
    updated_at: toString(p.updated_at)
} AS payment
"""


_LIST_BY_SUBSCRIPTION_CYPHER = """\
MATCH (p:Payment {subscription_id: $subscription_id})
WITH p
ORDER BY p.created_at DESC
RETURN p {.*,
    paid_at: toString(p.paid_at),
    failed_at: toString(p.failed_at),
    refunded_at: toString(p.refunded_at),
    created_at: toString(p.created_at),
    updated_at: toString(p.updated_at)
} AS payment
"""


# ===== 부팅 헬퍼 =====


async def ensure_payment_constraints() -> None:
    for cypher, label in [
        (_ENSURE_PAYMENT_ID_CYPHER, "payment.id UNIQUE"),
        (_ENSURE_PG_ORDER_ID_CYPHER, "payment.pg_order_id UNIQUE"),
        (_ENSURE_USER_EMAIL_INDEX_CYPHER, "payment.user_email INDEX"),
        (_ENSURE_CREATED_AT_INDEX_CYPHER, "payment.created_at INDEX"),
        # [2026-05-18] RefundRecord — 부분환불 evidence
        (_ENSURE_REFUND_RECORD_ID_CYPHER, "refund_record.id UNIQUE"),
        (_ENSURE_REFUND_USER_EMAIL_INDEX_CYPHER, "refund_record.user_email INDEX"),
    ]:
        try:
            await neo4j_client.run_cypher(cypher)
            logger.info("payment: %s ensure 완료", label)
        except Exception as e:  # noqa: BLE001
            logger.warning("payment: %s 실패 (%s)", label, e)


# ===== 함수 =====


def _row_to_obj(row: dict) -> Payment:
    p = row.get("payment") or row
    return Payment(
        id=p.get("id") or "",
        subscription_id=p.get("subscription_id") or "",
        user_email=p.get("user_email") or "",
        amount=int(p.get("amount") or 0),
        currency=p.get("currency") or "KRW",
        status=p.get("status") or "pending",
        purpose=p.get("purpose") or "manual",
        pg_payment_key=p.get("pg_payment_key"),
        pg_order_id=p.get("pg_order_id"),
        method=p.get("method"),
        paid_at=p.get("paid_at"),
        failed_at=p.get("failed_at"),
        fail_reason=p.get("fail_reason"),
        refunded_at=p.get("refunded_at"),
        refund_amount=int(p.get("refund_amount") or 0),
        refund_reason=p.get("refund_reason"),
        raw_response=p.get("raw_response"),
        receipt_url=p.get("receipt_url"),
        created_at=p.get("created_at"),
        updated_at=p.get("updated_at"),
    )


def generate_order_id(user_email: str) -> str:
    """우리 pg_order_id 생성 — 토스 권장 30~64 자 영숫자/_-."""
    # email 의 앞 4자만 prefix (PII 최소) + 시간 + uuid
    prefix = "".join(c for c in (user_email or "x")[:4].lower() if c.isalnum()) or "x"
    return f"hns_{prefix}_{uuid.uuid4().hex}"


async def create_pending_payment(
    *,
    subscription_id: str,
    user_email: str,
    amount: int,
    purpose: str,
    pg_order_id: str,
    method: str = "card",
) -> Optional[Payment]:
    """결제 시도 직전에 pending 상태로 노드 생성. PG 호출 후 mark_paid/failed 로 update."""
    new_id = uuid.uuid4().hex
    records = await neo4j_client.run_cypher(
        _CREATE_PAYMENT_PENDING_CYPHER,
        {
            "id": new_id,
            "subscription_id": subscription_id,
            "user_email": user_email,
            "amount": int(amount),
            "purpose": purpose,
            "pg_order_id": pg_order_id,
            "method": method,
        },
    )
    if not records:
        return None
    return await get_payment_by_id(new_id)


async def mark_paid(
    payment_id: str, *, pg_payment_key: str, raw_response: Any
) -> bool:
    """결제 성공 — PG 응답 raw + receipt URL 저장.

    [2026-05-18] 토스 응답의 receipt.url 자동 추출 (영수증 페이지 링크).
    """
    raw_dict = None
    if isinstance(raw_response, dict):
        raw_dict = raw_response
    raw_str = (
        raw_response if isinstance(raw_response, str) else json.dumps(raw_response, ensure_ascii=False)
    )
    # 토스 응답에서 receipt URL 추출 — 없으면 빈 string
    receipt_url = ""
    if raw_dict:
        receipt = raw_dict.get("receipt") or {}
        receipt_url = receipt.get("url") or ""
    records = await neo4j_client.run_cypher(
        _MARK_PAID_CYPHER,
        {
            "id": payment_id,
            "pg_payment_key": pg_payment_key,
            "raw_response": raw_str,
            "receipt_url": receipt_url,
        },
    )
    return bool(records and records[0].get("id"))


async def mark_failed(payment_id: str, *, fail_reason: str, raw_response: Any) -> bool:
    """결제 실패 — fail_reason + PG raw."""
    raw_str = (
        raw_response if isinstance(raw_response, str) else json.dumps(raw_response, ensure_ascii=False)
    )
    records = await neo4j_client.run_cypher(
        _MARK_FAILED_CYPHER,
        {"id": payment_id, "fail_reason": fail_reason or "", "raw_response": raw_str},
    )
    return bool(records and records[0].get("id"))


async def refund(
    payment_id: str,
    *,
    refund_amount: int,
    refund_reason: str,
    raw_response: Any = None,
) -> Optional[dict]:
    """부분/전액 환불. 누적 환불 금액 자동 합산 + status 자동 전이.

    [2026-05-18] 환불 1건마다 RefundRecord 노드 1개 생성 (분쟁 evidence).
    Payment.refunded_at 은 첫 환불 시각 유지 (마지막 환불 시각으로 덮어쓰지 않음).
    개별 환불 이력은 list_refund_records_for_payment() 로 조회.
    """
    raw_str = None
    if raw_response is not None:
        raw_str = (
            raw_response if isinstance(raw_response, str)
            else json.dumps(raw_response, ensure_ascii=False)
        )
    refund_id = uuid.uuid4().hex
    records = await neo4j_client.run_cypher(
        _REFUND_CYPHER,
        {
            "id": payment_id,
            "refund_id": refund_id,
            "refund_amount": int(refund_amount),
            "refund_reason": refund_reason or "",
            "raw_response": raw_str,
        },
    )
    if not records:
        return None
    row = records[0]
    return {
        "id": row.get("id"),
        "status": row.get("status"),
        "refund_amount": int(row.get("refund_amount") or 0),
        "refund_id": row.get("refund_id"),
        "refund_created_at": row.get("refund_created_at"),
    }


async def list_refund_records_for_payment(payment_id: str) -> list[dict]:
    """Payment 의 RefundRecord 이력 (생성 순). admin/분쟁 evidence 용."""
    records = await neo4j_client.run_cypher(
        _LIST_REFUNDS_BY_PAYMENT_CYPHER, {"payment_id": payment_id},
    )
    return [r.get("refund") or {} for r in records]


async def get_payment_by_id(payment_id: str) -> Optional[Payment]:
    records = await neo4j_client.run_cypher(_GET_BY_ID_CYPHER, {"id": payment_id})
    if not records:
        return None
    return _row_to_obj(records[0])


async def get_payment_by_order_id(pg_order_id: str) -> Optional[Payment]:
    records = await neo4j_client.run_cypher(
        _GET_BY_PG_ORDER_ID_CYPHER, {"pg_order_id": pg_order_id}
    )
    if not records:
        return None
    return _row_to_obj(records[0])


async def list_payments_by_user(
    user_email: str, limit: int = 50
) -> List[Payment]:
    records = await neo4j_client.run_cypher(
        _LIST_BY_USER_CYPHER, {"email": user_email, "limit": int(limit)}
    )
    return [_row_to_obj(r) for r in records if (r.get("payment") or {}).get("id")]


async def list_payments_by_subscription(subscription_id: str) -> List[Payment]:
    records = await neo4j_client.run_cypher(
        _LIST_BY_SUBSCRIPTION_CYPHER, {"subscription_id": subscription_id}
    )
    return [_row_to_obj(r) for r in records if (r.get("payment") or {}).get("id")]


async def find_stale_pending_payments(
    *,
    older_than_seconds: int = 120,
    max_age_days: int = 3,
    limit: int = 100,
) -> List[Payment]:
    """리컨실리에이션 대상 — 오래 pending 인 결제(불확정 보류분 / 누락 웹훅).

    older_than_seconds: 갓 생성돼 아직 결제 진행 중일 수 있는 건 제외(race 회피).
    max_age_days: 너무 오래된 건 제외(이미 만료/별도 처리).
    """
    records = await neo4j_client.run_cypher(
        _FIND_STALE_PENDING_CYPHER,
        {
            "older_than_seconds": int(older_than_seconds),
            "max_age_days": int(max_age_days),
            "limit": int(limit),
        },
    )
    return [_row_to_obj(r) for r in records if (r.get("payment") or {}).get("id")]
