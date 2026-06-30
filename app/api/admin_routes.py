"""
Admin 라우트 — 사용자 목록/검색/상세, 구독 변경, admin 토글, 감사 로그 조회.

모든 라우트는 `get_admin_user` 로 보호. 미인증/비어드민은 401/403.
모든 라우트에 slowapi rate limit 적용 (어드민 키로 brute force / 봇 방어).

[엔드포인트]
- GET    /api/admin/users?q=&limit=&offset=        → 목록 + 검색
- GET    /api/admin/users/{email}                  → 상세 (통계 + 구독 이력)
- PATCH  /api/admin/users/{email}/subscription     → 구독 변경 + 이력 + 감사로그
- PATCH  /api/admin/users/{email}/admin            → admin 토글 + 감사로그
   · 본인 강등 차단
   · last-admin 보호 (cypher atomic)
- GET    /api/admin/audit-logs?q=&limit=&offset=   → 감사 로그 조회
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.limiter import limiter
from app.core.security import get_admin_user
from app.service import admin_repository, audit_repository, usage_repository, user_repository
from app.service.audit_repository import (
    ACTION_ADMIN_GRANT,
    ACTION_ADMIN_REVOKE,
    ACTION_SUBSCRIPTION_CHANGE,
    ACTION_USAGE_RESET,
    ACTION_USER_SUSPEND,
    ACTION_USER_UNSUSPEND,
)
from app.service.user_repository import (
    SUBSCRIPTION_TYPES,
    UserPublic,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["Admin"])


# ===== Request DTOs =====


class ChangeSubscriptionRequest(BaseModel):
    type: str = Field(..., description="free 또는 pro")
    reason: Optional[str] = Field(default=None, max_length=500)
    # [2026-06] 기간제 부여 — N개월 후 free 자동 강등. null = 영구. 기본 1개월.
    #   type=free 면 무시(만료 없음). Paddle 경로는 이 라우트를 안 거치므로 영향 없음.
    duration_months: Optional[int] = Field(default=1, ge=1, le=60)


class SetAdminRequest(BaseModel):
    is_admin: bool


class SuspendUserRequest(BaseModel):
    reason: Optional[str] = Field(
        default=None, max_length=500,
        description="정지 사유. 비어 있으면 사용자에게 메시지 노출 안 함.",
    )


# ===== Routes =====


@router.get("/users")
@limiter.limit("60/minute")
async def list_users_route(
    request: Request,
    q: str = Query("", description="email/name/github_username 부분검색"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    return await admin_repository.list_users(q=q, limit=limit, offset=offset)


@router.get("/users/{email}")
@limiter.limit("60/minute")
async def get_user_detail_route(
    request: Request,
    email: str,
    _admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    detail = await admin_repository.get_user_detail(email)
    if not detail:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자를 찾을 수 없습니다.",
        )
    return detail.model_dump()


@router.patch("/users/{email}/subscription")
@limiter.limit("20/minute")
async def change_subscription_route(
    request: Request,
    email: str,
    payload: ChangeSubscriptionRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    if payload.type not in SUBSCRIPTION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"subscription type 은 {SUBSCRIPTION_TYPES} 중 하나여야 합니다.",
        )
    result = await admin_repository.change_subscription(
        target_email=email,
        to_type=payload.type,
        reason=payload.reason,
        changed_by_email=admin.email,
        duration_months=payload.duration_months,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자를 찾을 수 없습니다.",
        )
    # 감사 로그 — change 노드와는 별도로, "어떤 어드민이" 변경했는지 영구 기록.
    await audit_repository.write(
        actor_email=admin.email,
        action=ACTION_SUBSCRIPTION_CHANGE,
        target_email=email,
        payload={
            "from_type": result["change"].from_type,
            "to_type": result["change"].to_type,
            "reason": payload.reason,
        },
    )
    logger.info(
        "admin: %s changed %s subscription -> %s (reason=%r)",
        admin.email, email, payload.type, payload.reason,
    )
    return {
        "user": result["user"].model_dump(),
        "change": result["change"].model_dump(),
    }


@router.patch("/users/{email}/admin")
@limiter.limit("10/minute")
async def set_admin_route(
    request: Request,
    email: str,
    payload: SetAdminRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    # 본인 강등 차단 (실수로 본인 권한 잃는 것 방지). UX 차원의 가드.
    if email.lower() == admin.email.lower() and not payload.is_admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="자기 자신의 관리자 권한은 해제할 수 없습니다.",
        )
    # last-admin 보호는 cypher 안에서 atomic 처리 (race condition 안전).
    result = await admin_repository.set_admin(
        target_email=email, is_admin=payload.is_admin
    )
    status_str = result.get("status")
    if status_str == "last_admin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("message") or "마지막 관리자입니다.",
        )
    if status_str != "ok":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자를 찾을 수 없습니다.",
        )
    user = result["user"]
    # 감사 로그
    await audit_repository.write(
        actor_email=admin.email,
        action=ACTION_ADMIN_GRANT if payload.is_admin else ACTION_ADMIN_REVOKE,
        target_email=email,
        payload={"is_admin": payload.is_admin},
    )
    logger.info(
        "admin: %s set %s.is_admin = %s", admin.email, email, payload.is_admin
    )
    return user.model_dump()


class ResetUsageRequest(BaseModel):
    reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="감사 로그 본문에 기록할 사유 (예: 'CS 처리 — 결제 오류 보상')",
    )


@router.post("/users/{email}/reset-usage")
@limiter.limit("10/minute")
async def reset_user_usage_route(
    request: Request,
    email: str,
    payload: ResetUsageRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    """
    사용자 사용량 카운터 수동 리셋.

    [동작]
    - usage_meeting_count / usage_total_tokens / usage_total_chars = 0
    - usage_reset_at 은 건드리지 않음 (현재 cycle 유지 정책 — abuse 방지)
    - 새 cycle 부여하고 싶으면 등급 변경 (PATCH /subscription) 사용

    [응답]
    success=True/False + email + reason.
    """
    ok = await usage_repository.reset_usage(email)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자를 찾을 수 없습니다.",
        )
    # 감사 로그 — 누가 누구의 사용량을 왜 리셋했는지 영구 기록.
    await audit_repository.write(
        actor_email=admin.email,
        action=ACTION_USAGE_RESET,
        target_email=email,
        payload={"reason": payload.reason},
    )
    logger.info(
        "admin: %s reset usage for %s (reason=%r)",
        admin.email, email, payload.reason,
    )
    return {"success": True, "email": email, "reason": payload.reason}


@router.patch("/users/{email}/suspend")
@limiter.limit("10/minute")
async def suspend_user_route(
    request: Request,
    email: str,
    payload: SuspendUserRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    """
    사용자 계정 정지. 본인 정지 + last-admin 정지는 차단.

    동작:
    - is_suspended=true, suspended_at=datetime() SET
    - 모든 활성 토큰(access/refresh) 즉시 무효화 (iat < suspended_at)
    - 다음 로그인 시 명시 메시지 표시
    """
    if email.lower() == admin.email.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="자기 자신의 계정은 정지할 수 없습니다.",
        )

    reason = (payload.reason or "").strip()
    result = await admin_repository.suspend_user(
        target_email=email, reason=reason, by_admin_email=admin.email,
    )
    s = result.get("status")
    if s == "last_admin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("message") or "마지막 관리자입니다.",
        )
    if s != "ok":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자를 찾을 수 없습니다.",
        )

    await audit_repository.write(
        actor_email=admin.email,
        action=ACTION_USER_SUSPEND,
        target_email=email,
        payload={"reason": reason or None},
    )
    logger.info("admin: %s suspended %s (reason=%r)", admin.email, email, reason)
    return {"user": result["user"].model_dump()}


@router.patch("/users/{email}/unsuspend")
@limiter.limit("10/minute")
async def unsuspend_user_route(
    request: Request,
    email: str,
    admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    """사용자 정지 해제. suspended_reason / suspended_by_email 은 보존 (이력 참고용)."""
    result = await admin_repository.unsuspend_user(target_email=email)
    if result.get("status") != "ok":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자를 찾을 수 없습니다.",
        )
    await audit_repository.write(
        actor_email=admin.email,
        action=ACTION_USER_UNSUSPEND,
        target_email=email,
        payload={},
    )
    logger.info("admin: %s unsuspended %s", admin.email, email)
    return {"user": result["user"].model_dump()}


# [2026-06 OAuth 전용] admin 비밀번호 재설정 메일 발송(send-password-reset) 제거.
# 이메일/비번 인증 폐지로 재설정 대상이 없다. OAuth 미연결 기존 사용자는
# 동일 이메일로 Google/GitHub 재로그인 시 자동 연결.


@router.get("/stats")
@limiter.limit("60/minute")
async def get_stats_route(
    request: Request,
    _admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    """DAU / WAU / MAU + 전체 사용자 수 반환."""
    stats = await admin_repository.get_active_stats()
    return stats.model_dump()


@router.get("/audit-logs")
@limiter.limit("60/minute")
async def list_audit_logs_route(
    request: Request,
    q: str = Query("", description="actor_email/target_email/action 부분검색"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    """감사 로그 (최신순)."""
    return await audit_repository.list_logs(q=q, limit=limit, offset=offset)


@router.get("/queue/stats")
@limiter.limit("60/minute")
async def queue_stats_route(
    request: Request,
    _admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    """
    [2026-05 운영 가시성 #2] 큐 깊이 + 워커 health.

    Returns:
      {
        "queues": {
          "<queue_name>": {"pending": int|null, "health": str|null},
          ...
        },
        "default": "<QUEUE_NAME>"
      }

    - pending: arq 의 zcard 결과 (대기 작업 수).
    - health: 워커가 publish 한 마지막 health 라인 (없으면 워커 미가동).
    """
    from app.queue.client import get_queue_stats
    return await get_queue_stats()


@router.post("/queue/flush")
@limiter.limit("5/minute")
async def queue_flush_route(
    request: Request,
    admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    """
    모든 큐(pending + in-progress 마커 + stage 키)를 비운다.

    [사용 시점]
    - 워커 장애 재시작 후 잔류 stuck jobs 제거.
    - 배치 오류 후 큐를 완전히 초기화하고 다시 시작할 때.

    [주의] 현재 워커 프로세스가 실행 중인 job 은 중단되지 않는다.
    worker 재시작 후 호출하면 깔끔하게 정리된다.

    Returns: flushed_queues, pending_removed, inprogress_removed,
             stage_keys_removed, job_hash_removed
    """
    from app.queue.client import flush_queues
    result = await flush_queues()
    await audit_repository.write(
        actor_email=admin.email,
        action="queue_flush",
        target_email=None,
        payload=result,
    )
    logger.info(
        "admin: %s flushed queues — pending=%d inprogress=%d",
        admin.email, result["pending_removed"], result["inprogress_removed"],
    )
    return result


# ─── PRD 일괄 cleanup (2026-05-26) ─────────────────────────────


_LIST_MASTER_PRDS_CYPHER = """\
MATCH (m:PRD_Document {type: 'Master', is_latest: true})
RETURN m.project AS project_name,
       m.full_markdown AS full_markdown,
       m.owner_email   AS owner_email
"""


@router.post("/cleanup-dirty-prd")
@limiter.limit("2/minute")
async def cleanup_dirty_prd_route(
    request: Request,
    admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    """모든 누더기 master PRD 일괄 cleanup enqueue (admin 1회성 마이그레이션).

    [목적]
    PR #52 (post_meeting 자동 cleanup) 이전에 누적된 기존 누더기 PRD 들을
    한 번에 정리. detection 트립 (size>=30KB 또는 Epic ID 중복) 된 프로젝트만
    enqueue → LLM 비용 절약 + 깔끔한 프로젝트는 건드리지 않음.

    [동작]
    1. 모든 PRD_Document Master 노드 fetch
    2. 각각 `_should_trigger_cleanup` 적용
    3. trip 된 프로젝트만 `cleanup_master_prd_job` enqueue (deterministic task_id)
    4. arq dedup — lazy trigger 와 중복 enqueue 자동 차단

    Returns: { scanned, triggered, skipped_clean, errored, projects }
    """
    from app.clients import neo4j_client
    from app.queue.client import enqueue_cleanup_master_prd
    from app.queue.jobs import _deterministic_cleanup_task_id, _should_trigger_cleanup

    rows = await neo4j_client.run_cypher(_LIST_MASTER_PRDS_CYPHER, {}) or []

    triggered: List[Dict[str, Any]] = []
    skipped_clean = 0
    errored: List[Dict[str, Any]] = []

    for row in rows:
        project_name = row.get("project_name")
        md = row.get("full_markdown") or ""
        owner_email = row.get("owner_email")
        if not project_name:
            continue
        try:
            trigger, reason = _should_trigger_cleanup(md)
            if not trigger:
                skipped_clean += 1
                continue
            task_id = _deterministic_cleanup_task_id(project_name, md)
            await enqueue_cleanup_master_prd(
                task_id=task_id,
                project_name=project_name,
                dry_run=False,
                # owner_email 누락 케이스 — admin 본인 email 로 토큰 누적 (백필 비용은 admin 부담).
                user_email=owner_email or admin.email,
            )
            triggered.append({
                "project_name": project_name,
                "task_id": task_id,
                "reason": reason,
            })
        except Exception as e:  # noqa: BLE001
            errored.append({"project_name": project_name, "error": str(e)})

    result = {
        "scanned": len(rows),
        "triggered": len(triggered),
        "skipped_clean": skipped_clean,
        "errored": len(errored),
        "projects": triggered[:50],   # 응답 크기 제한 — 처음 50개만 detail
        "errors": errored[:20],
    }
    await audit_repository.write(
        actor_email=admin.email,
        action="cleanup_dirty_prd",
        target_email=None,
        payload={
            "scanned": result["scanned"],
            "triggered": result["triggered"],
            "skipped_clean": result["skipped_clean"],
            "errored": result["errored"],
        },
    )
    logger.info(
        "admin: %s cleanup-dirty-prd scanned=%d triggered=%d skipped=%d errored=%d",
        admin.email, result["scanned"], result["triggered"],
        result["skipped_clean"], result["errored"],
    )
    return result