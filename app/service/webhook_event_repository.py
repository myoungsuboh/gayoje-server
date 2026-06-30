"""
WebhookEvent — PG webhook 멱등성 보장 + 처리 이력.

[배경]
PG (토스) 가 결제 성공/실패/환불 이벤트를 우리 webhook 으로 push. 네트워크
재시도로 같은 이벤트가 2회+ 도착할 수 있음 (at-least-once 전달). 같은 이벤트
중복 처리 시 결제 이중 인정, 환불 이중 처리 등 위험 → 멱등성 가드 필수.

[전략]
1. webhook 도착 → pg_event_id (또는 paymentKey+status 조합) 로 노드 조회
2. 이미 있고 status='processed' 면 즉시 200 OK 반환 (중복)
3. 없으면 노드 생성 (status='pending') → 처리 → 성공 시 'processed' 로 update
4. 처리 실패 시 'failed' + retry_count 증가. 일정 횟수 후 admin 알림.

[스키마]
(:WebhookEvent {
    id: uuid,
    pg_event_id: str,           # PG 의 이벤트 식별자 — 멱등성 키
    event_type: str,            # 'payment.paid' | 'payment.failed' | ...
    payload: str,               # 원본 JSON (분쟁 evidence)
    status: 'pending' | 'processed' | 'failed' | 'duplicate',
    related_payment_id: str?,   # 연결된 Payment 노드 (있을 때)
    retry_count: int,
    error_message: str?,
    received_at: datetime,
    processed_at: datetime?,
    updated_at: datetime
})
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# ===== 상수 =====

STATUS_PENDING = "pending"
STATUS_PROCESSED = "processed"
STATUS_FAILED = "failed"
STATUS_DUPLICATE = "duplicate"


# ===== 도메인 모델 =====


@dataclass(frozen=True)
class WebhookEvent:
    id: str
    pg_event_id: str
    event_type: str
    payload: str
    status: str
    related_payment_id: Optional[str] = None
    retry_count: int = 0
    error_message: Optional[str] = None
    received_at: Optional[str] = None
    processed_at: Optional[str] = None
    updated_at: Optional[str] = None


# ===== Cypher =====


_ENSURE_PG_EVENT_ID_CYPHER = """\
CREATE CONSTRAINT webhook_event_pg_id_unique IF NOT EXISTS
FOR (w:WebhookEvent) REQUIRE w.pg_event_id IS UNIQUE
"""


_ENSURE_RECEIVED_AT_INDEX_CYPHER = """\
CREATE INDEX webhook_event_received_at IF NOT EXISTS
FOR (w:WebhookEvent) ON (w.received_at)
"""


# pg_event_id 가 이미 있으면 중복 — RETURN status 만.
_TRY_INSERT_CYPHER = """\
MERGE (w:WebhookEvent {pg_event_id: $pg_event_id})
ON CREATE SET
    w.id = $id,
    w.event_type = $event_type,
    w.payload = $payload,
    w.status = 'pending',
    w.retry_count = 0,
    w.received_at = datetime(),
    w.updated_at = datetime()
RETURN w.id AS id, w.status AS status
"""


_MARK_PROCESSED_CYPHER = """\
MATCH (w:WebhookEvent {pg_event_id: $pg_event_id})
SET w.status = 'processed',
    w.related_payment_id = $related_payment_id,
    w.processed_at = datetime(),
    w.updated_at = datetime()
RETURN w.id AS id
"""


_MARK_FAILED_CYPHER = """\
MATCH (w:WebhookEvent {pg_event_id: $pg_event_id})
SET w.status = 'failed',
    w.error_message = $error_message,
    w.retry_count = COALESCE(w.retry_count, 0) + 1,
    w.updated_at = datetime()
RETURN w.id AS id, w.retry_count AS retry_count
"""


_GET_BY_PG_EVENT_ID_CYPHER = """\
MATCH (w:WebhookEvent {pg_event_id: $pg_event_id})
RETURN w {.*,
    received_at: toString(w.received_at),
    processed_at: toString(w.processed_at),
    updated_at: toString(w.updated_at)
} AS event
"""


# ===== 부팅 헬퍼 =====


async def ensure_webhook_constraints() -> None:
    for cypher, label in [
        (_ENSURE_PG_EVENT_ID_CYPHER, "pg_event_id UNIQUE"),
        (_ENSURE_RECEIVED_AT_INDEX_CYPHER, "received_at INDEX"),
    ]:
        try:
            await neo4j_client.run_cypher(cypher)
            logger.info("webhook_event: %s ensure 완료", label)
        except Exception as e:  # noqa: BLE001
            logger.warning("webhook_event: %s 실패 (%s)", label, e)


# ===== 함수 =====


async def try_insert(
    *, pg_event_id: str, event_type: str, payload: Any
) -> tuple[str, bool]:
    """
    webhook 도착 시 첫 호출 — MERGE 로 멱등.

    Returns: (status, is_new)
        - is_new=True : 신규 이벤트, 호출자가 처리 진행
        - is_new=False : 이미 처리 중 또는 완료 → 200 OK 반환 후 호출자 skip
    """
    payload_str = (
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    )
    new_id = uuid.uuid4().hex
    records = await neo4j_client.run_cypher(
        _TRY_INSERT_CYPHER,
        {
            "id": new_id,
            "pg_event_id": pg_event_id,
            "event_type": event_type,
            "payload": payload_str,
        },
    )
    if not records:
        return ("unknown", False)
    row = records[0]
    status = row.get("status") or STATUS_PENDING
    # MERGE 가 새로 생성했으면 id 가 우리가 발급한 new_id 와 일치
    is_new = row.get("id") == new_id
    return (status, is_new)


async def mark_processed(
    pg_event_id: str, related_payment_id: Optional[str] = None
) -> bool:
    records = await neo4j_client.run_cypher(
        _MARK_PROCESSED_CYPHER,
        {"pg_event_id": pg_event_id, "related_payment_id": related_payment_id or ""},
    )
    return bool(records and records[0].get("id"))


async def mark_failed(pg_event_id: str, error_message: str) -> Optional[int]:
    """실패 + retry_count 증가. retry_count 반환."""
    records = await neo4j_client.run_cypher(
        _MARK_FAILED_CYPHER,
        {"pg_event_id": pg_event_id, "error_message": error_message or ""},
    )
    if not records:
        return None
    return int(records[0].get("retry_count") or 0)


async def get_by_pg_event_id(pg_event_id: str) -> Optional[WebhookEvent]:
    records = await neo4j_client.run_cypher(
        _GET_BY_PG_EVENT_ID_CYPHER, {"pg_event_id": pg_event_id}
    )
    if not records:
        return None
    e = records[0].get("event") or {}
    return WebhookEvent(
        id=e.get("id") or "",
        pg_event_id=e.get("pg_event_id") or "",
        event_type=e.get("event_type") or "",
        payload=e.get("payload") or "",
        status=e.get("status") or "",
        related_payment_id=e.get("related_payment_id"),
        retry_count=int(e.get("retry_count") or 0),
        error_message=e.get("error_message"),
        received_at=e.get("received_at"),
        processed_at=e.get("processed_at"),
        updated_at=e.get("updated_at"),
    )
