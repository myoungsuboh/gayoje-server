"""
NotificationLog — 이메일 발송 이력 추적 (audit + 분쟁 evidence).

[배경 — 2026-05-18]
사용자가 결제 실패 메일을 못 받았다고 주장 / 환불 사실 몰랐다고 주장 시
admin 이 "발송 시각" 으로 증거 제시 가능해야 함. audit_repository 는 액션 (결제/
환불/등급변경) 만 기록 — 이메일 발송은 별개 통신 행위라 분리.

[스키마]
(:NotificationLog {
    id: str,             # uuid
    user_email: str,     # 수신자
    kind: str,           # 'payment_success' | 'payment_failed' | 'refund' |
                         # 'subscription_canceled' | 'expiring_soon' | 'admin_alert' |
                         # 'password_reset' | 'inquiry_reply' | ...
    subject: str,        # 발송된 이메일 제목 (truncated)
    status: str,         # 'sent' | 'failed' | 'disabled'
    provider: str,       # 'resend' (현재 유일)
    provider_message_id: str?,  # Resend id (재추적용)
    error_message: str?, # 실패 시 사유
    context: str?,       # 추가 메타 JSON (선택)
    sent_at: datetime    # 시도 시각 (성공/실패 무관)
})

[조회]
- 사용자별 최근 N건 — admin/billing 에서 분쟁 확인
- kind 별 통계 — 운영 대시보드 (Phase 후속)
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# ===== 상수 — kind 정의 (typo 방지) =====

KIND_PAYMENT_SUCCESS = "payment_success"
KIND_PAYMENT_FAILED = "payment_failed"
KIND_REFUND = "refund"
KIND_SUBSCRIPTION_CANCELED = "subscription_canceled"
KIND_EXPIRING_SOON = "expiring_soon"
KIND_ADMIN_ALERT = "admin_alert"
KIND_PASSWORD_RESET = "password_reset"
KIND_INQUIRY_REPLY = "inquiry_reply"
KIND_OTHER = "other"


# ===== 도메인 모델 =====


@dataclass(frozen=True)
class NotificationLog:
    id: str
    user_email: str
    kind: str
    subject: str
    status: str
    provider: str
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None
    context: Optional[str] = None
    sent_at: Optional[str] = None


# ===== Cypher =====


_ENSURE_ID_CYPHER = """\
CREATE CONSTRAINT notification_log_id_unique IF NOT EXISTS
FOR (n:NotificationLog) REQUIRE n.id IS UNIQUE
"""


_ENSURE_USER_EMAIL_INDEX_CYPHER = """\
CREATE INDEX notification_log_user_email IF NOT EXISTS
FOR (n:NotificationLog) ON (n.user_email)
"""


_ENSURE_SENT_AT_INDEX_CYPHER = """\
CREATE INDEX notification_log_sent_at IF NOT EXISTS
FOR (n:NotificationLog) ON (n.sent_at)
"""


_CREATE_LOG_CYPHER = """\
CREATE (n:NotificationLog {
    id: $id,
    user_email: $user_email,
    kind: $kind,
    subject: $subject,
    status: $status,
    provider: $provider,
    provider_message_id: $provider_message_id,
    error_message: $error_message,
    context: $context,
    sent_at: datetime()
})
RETURN n.id AS id
"""


_LIST_BY_USER_CYPHER = """\
MATCH (n:NotificationLog {user_email: $email})
WITH n
ORDER BY n.sent_at DESC
LIMIT $limit
RETURN n {.*,
    sent_at: toString(n.sent_at)
} AS log
"""


# ===== 부팅 헬퍼 =====


async def ensure_notification_log_constraints() -> None:
    for cypher, label in [
        (_ENSURE_ID_CYPHER, "notification_log.id UNIQUE"),
        (_ENSURE_USER_EMAIL_INDEX_CYPHER, "user_email INDEX"),
        (_ENSURE_SENT_AT_INDEX_CYPHER, "sent_at INDEX"),
    ]:
        try:
            await neo4j_client.run_cypher(cypher)
            logger.info("notification_log: %s ensure 완료", label)
        except Exception as e:  # noqa: BLE001
            logger.warning("notification_log: %s 실패 (%s)", label, e)


# ===== 함수 =====


async def record(
    *,
    user_email: str,
    kind: str,
    subject: str,
    status: str,                                  # 'sent' | 'failed' | 'disabled'
    provider: str = "resend",
    provider_message_id: Optional[str] = None,
    error_message: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    이메일 발송 결과 기록. send_email 호출 직후 자동.

    Returns: NotificationLog.id (실패 시 None — DB 장애 등)
    """
    new_id = uuid.uuid4().hex
    ctx_str = None
    if context:
        try:
            ctx_str = json.dumps(context, ensure_ascii=False)[:2000]
        except Exception:  # noqa: BLE001
            ctx_str = None
    # subject / error_message 길이 제한 — Neo4j payload 비대 방지
    safe_subject = (subject or "")[:300]
    safe_error = (error_message or "")[:500] if error_message else None
    try:
        records = await neo4j_client.run_cypher(
            _CREATE_LOG_CYPHER,
            {
                "id": new_id,
                "user_email": user_email or "",
                "kind": kind or KIND_OTHER,
                "subject": safe_subject,
                "status": status or "sent",
                "provider": provider or "resend",
                "provider_message_id": provider_message_id or "",
                "error_message": safe_error,
                "context": ctx_str,
            },
        )
        if records and records[0].get("id"):
            return new_id
    except Exception as e:  # noqa: BLE001
        # 기록 실패가 메일 발송 자체를 막으면 안 됨 — log 만.
        logger.warning("notification_log record 실패 user=%s kind=%s err=%s",
                       user_email, kind, e)
    return None


async def list_logs_for_user(user_email: str, limit: int = 50) -> List[NotificationLog]:
    """사용자별 최근 N건 — admin/billing 분쟁 확인용."""
    records = await neo4j_client.run_cypher(
        _LIST_BY_USER_CYPHER, {"email": user_email, "limit": int(limit)},
    )
    out: List[NotificationLog] = []
    for r in records:
        log = r.get("log") or {}
        if not log.get("id"):
            continue
        out.append(NotificationLog(
            id=log.get("id") or "",
            user_email=log.get("user_email") or "",
            kind=log.get("kind") or "",
            subject=log.get("subject") or "",
            status=log.get("status") or "",
            provider=log.get("provider") or "",
            provider_message_id=log.get("provider_message_id"),
            error_message=log.get("error_message"),
            context=log.get("context"),
            sent_at=log.get("sent_at"),
        ))
    return out
