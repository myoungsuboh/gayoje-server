"""
AuditLog — 관리자가 수행한 결정적 액션을 영구 기록.

[설계 의도]
- logger.info 는 stdout 으로만 남아 컨테이너 재시작/로그 회전 시 손실됨.
- 결제와 연결되는 액션 (구독 변경, 관리자 권한 부여) 은 분쟁/감사 시 증거가 필요.
- (:AuditLog) 노드로 영구 저장, 관리자 화면에서 조회 가능.

[스키마]
(:AuditLog {
    id: uuid,
    actor_email: str,         # 액션을 수행한 어드민
    action: str,              # 'subscription_change' | 'admin_grant' | 'admin_revoke' | ...
    target_email: str,        # 영향받은 사용자 email (없으면 빈 문자열)
    payload: str (JSON),      # 액션별 상세 (e.g. from/to/reason)
    created_at: datetime
})

[기록 정책]
- best-effort: 기록 실패가 본 액션을 막지는 않도록 wrap (try/except).
- 단, 정상 운영에서는 항상 성공해야 함 — 실패 시 warning 로그.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# ===== 액션 상수 (typo 방지 + grep 용이) =====
ACTION_SUBSCRIPTION_CHANGE = "subscription_change"
ACTION_ADMIN_GRANT = "admin_grant"
ACTION_ADMIN_REVOKE = "admin_revoke"
# admin 이 사용자 사용량 카운터를 수동 리셋 (이번 cycle 카운터만 0,
# reset_at 은 건드리지 않음 — abuse 방지).
ACTION_USAGE_RESET = "usage_reset"
# 사용자 자진 탈퇴 — User 노드 + SubscriptionChange 가 모두 삭제되므로
# 결제 분쟁 시 흔적이 없음. AuditLog 만 영구 보존되어 추적 가능.
ACTION_USER_SELF_DELETE = "user_self_delete"
# 부팅 시 또는 신규 가입 시 ADMIN_EMAILS 자동 승격 (actor='SYSTEM').
ACTION_SYSTEM_ADMIN_GRANT = "system_admin_grant"
# Lineage 정답 라벨 (truth) — 모델 평가의 ground truth. 데이터 정합성/
# 무결성 검토를 위해 변경 이력 기록 (admin 패널의 추적성과 동일 가치).
ACTION_LINEAGE_TRUTH_SAVE = "lineage_truth_save"
ACTION_LINEAGE_TRUTH_DELETE = "lineage_truth_delete"
ACTION_LINEAGE_TRUTH_IMPORT = "lineage_truth_import"
# 가격/할인율 변경 — 매출 직결, 분쟁 시 추적성 필수.
ACTION_PRICING_UPDATE = "pricing_update"
# 한도 (meeting_logs/total_tokens/...) 변경 — 사용자 사용량 가드 직결, 분쟁 시 추적성 필수.
ACTION_QUOTA_CONFIG_UPDATE = "quota_config_update"
# 인프라 비용 — admin 이 수동 입력, 수익 대시보드 원가 계산 입력값.
ACTION_INFRA_COST_UPDATE = "infra_cost_update"

# ─── 관리자 계정 운영 (2026-05-18) ───
# 정지/해제: reversible 일시 정지. 결제 분쟁/abuse 차단 시 발동.
ACTION_USER_SUSPEND = "user_suspend"
ACTION_USER_UNSUSPEND = "user_unsuspend"
# 비밀번호 초기화 메일 발송 — 관리자가 사용자 대신 forgot-password 흐름 트리거.
# 관리자는 비밀번호를 알지 못함 (사용자가 메일 링크로 직접 설정).
ACTION_PASSWORD_RESET_SENT = "password_reset_sent"

# ─── 결제/구독 (2026-05-18) ───
# 모든 결제 이벤트는 영구 기록 — 환불 분쟁/세무 대응/내부 감사용.
ACTION_BILLING_METHOD_REGISTER = "billing_method_register"
ACTION_BILLING_METHOD_DEACTIVATE = "billing_method_deactivate"
ACTION_SUBSCRIPTION_CREATE = "subscription_create"
ACTION_SUBSCRIPTION_UPGRADE = "subscription_upgrade"
ACTION_SUBSCRIPTION_CANCEL_SCHEDULE = "subscription_cancel_schedule"
ACTION_SUBSCRIPTION_RESUME = "subscription_resume"
ACTION_SUBSCRIPTION_TERMINATE = "subscription_terminate"
ACTION_PAYMENT_SUCCESS = "payment_success"
ACTION_PAYMENT_FAILED = "payment_failed"
ACTION_PAYMENT_REFUND = "payment_refund"
ACTION_WEBHOOK_RECEIVED = "webhook_received"

# [2026-05] 쿠폰 시스템 — 베타 신청자에게 무료 기간 제공.
ACTION_COUPON_CREATE = "coupon_create"
ACTION_COUPON_REDEEM = "coupon_redeem"
ACTION_COUPON_REVOKE = "coupon_revoke"


SYSTEM_ACTOR = "SYSTEM:ADMIN_EMAILS"


# ===== 도메인 모델 =====


class AuditLogRow(BaseModel):
    id: str
    actor_email: str
    action: str
    target_email: Optional[str] = None
    payload: Dict[str, Any] = {}
    created_at: Optional[str] = None


# ===== Cypher =====


_ENSURE_INDEX_CYPHER = """\
// 최신순 정렬 + actor/target 별 검색용 인덱스.
CREATE INDEX audit_log_created_at IF NOT EXISTS
FOR (a:AuditLog) ON (a.created_at)
"""

_ENSURE_ACTOR_INDEX_CYPHER = """\
CREATE INDEX audit_log_actor IF NOT EXISTS
FOR (a:AuditLog) ON (a.actor_email)
"""

_ENSURE_TARGET_INDEX_CYPHER = """\
CREATE INDEX audit_log_target IF NOT EXISTS
FOR (a:AuditLog) ON (a.target_email)
"""

_WRITE_AUDIT_CYPHER = """\
CREATE (a:AuditLog {
    id: randomUUID(),
    actor_email: $actor_email,
    action: $action,
    target_email: $target_email,
    payload: $payload,
    created_at: datetime()
})
RETURN a.id AS id
"""

_LIST_AUDIT_CYPHER = """\
// 최신순 + 검색. q 가 비면 전체.
MATCH (a:AuditLog)
WHERE $q = ''
   OR toLower(a.actor_email) CONTAINS $q
   OR toLower(COALESCE(a.target_email, '')) CONTAINS $q
   OR toLower(a.action) CONTAINS $q
WITH a
ORDER BY a.created_at DESC
SKIP $offset
LIMIT $limit
RETURN {
    id: a.id,
    actor_email: a.actor_email,
    action: a.action,
    target_email: COALESCE(a.target_email, ''),
    payload: COALESCE(a.payload, '{}'),
    created_at: toString(a.created_at)
} AS log
"""

_COUNT_AUDIT_CYPHER = """\
MATCH (a:AuditLog)
WHERE $q = ''
   OR toLower(a.actor_email) CONTAINS $q
   OR toLower(COALESCE(a.target_email, '')) CONTAINS $q
   OR toLower(a.action) CONTAINS $q
RETURN count(a) AS total
"""


# ===== 함수 =====


async def ensure_audit_indexes() -> None:
    """부팅 시 1회. 실패해도 부팅 막지 않음."""
    try:
        await neo4j_client.run_cypher(_ENSURE_INDEX_CYPHER)
        await neo4j_client.run_cypher(_ENSURE_ACTOR_INDEX_CYPHER)
        await neo4j_client.run_cypher(_ENSURE_TARGET_INDEX_CYPHER)
        logger.info("audit_repository: AuditLog 인덱스 ensure 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("audit_repository: 인덱스 생성 실패: %s", e)


async def write(
    *,
    actor_email: str,
    action: str,
    target_email: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    감사 로그 1건 기록. best-effort — 실패 시 None + warning 로그.
    호출자는 반환값을 보지 않아도 무방.
    """
    try:
        rows = await neo4j_client.run_cypher(
            _WRITE_AUDIT_CYPHER,
            {
                "actor_email": actor_email,
                "action": action,
                "target_email": target_email or "",
                "payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
            },
        )
        if not rows:
            return None
        return (rows[0] or {}).get("id")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "audit_repository: write 실패 — action=%s actor=%s target=%s err=%s",
            action, actor_email, target_email, e,
        )
        return None


def _row_to_log(row: Dict[str, Any]) -> Optional[AuditLogRow]:
    if not row or not row.get("id"):
        return None
    raw_payload = row.get("payload") or "{}"
    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else (raw_payload or {})
    except (ValueError, TypeError):
        payload = {"_raw": raw_payload}
    return AuditLogRow(
        id=row.get("id", ""),
        actor_email=row.get("actor_email", ""),
        action=row.get("action", ""),
        target_email=row.get("target_email") or None,
        payload=payload,
        created_at=row.get("created_at"),
    )


async def list_logs(
    q: str = "", limit: int = 50, offset: int = 0
) -> Dict[str, Any]:
    q_norm = (q or "").strip().lower()
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    rows = await neo4j_client.run_cypher(
        _LIST_AUDIT_CYPHER, {"q": q_norm, "limit": limit, "offset": offset}
    )
    logs: List[AuditLogRow] = []
    for r in rows or []:
        log = _row_to_log(r.get("log") or {})
        if log:
            logs.append(log)

    count_rows = await neo4j_client.run_cypher(_COUNT_AUDIT_CYPHER, {"q": q_norm})
    total = int(((count_rows or [{}])[0]).get("total") or 0)

    return {"logs": logs, "total": total, "limit": limit, "offset": offset}
