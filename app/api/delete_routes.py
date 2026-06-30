"""
deleteProject + deleteMeeting 라우트 (PR10).

- DELETE /api/v2/projects/{project_name}                  → deleteProject
- POST   /api/v2/pipelines/delete_meeting                 → deleteMeeting (rebuild 포함)
- POST   /api/v2/pipelines/delete_meeting?wait=true       → 동기 실행
- GET    /api/v2/pipelines/delete_meeting/status/{id}     → 비동기 결과 조회

deleteProject 는 단순 Cypher 1개라 항상 동기 (큐 미사용).
deleteMeeting 은 LLM rebuild 포함이라 v2 패턴 (default async + ?wait=true).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from app.api._quota_helpers import tracked_pipeline_context
from app.api._schemas import PipelineStatusResponse
from app.core import quota

from app.clients.gemini_client import GeminiError, gemini_error_to_http
from app.clients import neo4j_client
from app.core.limiter import limiter
from app.core.security import get_current_user
from app.pipelines.base import PipelineContext
from app.pipelines.delete_pipeline import (
    DeleteMeetingInput,
    delete_project,
    run_delete_meeting_pipeline,
    run_rebuild_master_pipeline,
)
from app.queue.client import enqueue_delete_meeting
from app.queue.status_guard import get_job_status_for_user
from app.service import ownership_repository
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["Delete / Rebuild"])


# 공통 어댑터 — pipelines.base.Neo4jClientProxy (run_cypher + run_in_transaction 노출)
from app.pipelines.base import Neo4jClientProxy as _Neo4jProxy


# [2026-05-19] delete_project 는 LLM 안 씀 (Neo4j DETACH DELETE 만). raw
# GeminiClient 인스턴스화는 낭비 + 누가 실수로 LLM 호출 시 토큰 누수 위험.
# NullGemini 로 가드 — generate() 호출되면 즉시 RuntimeError.
class _NullGemini:
    """LLM 안 쓰는 라우트용 가드."""

    async def generate(self, *a, **kw):  # pragma: no cover
        raise RuntimeError("delete_project 라우트는 Gemini 를 호출하지 않습니다.")


def _build_context(
    idempotency_key: str, user_email: str = "", team_id: str = ""
) -> PipelineContext:
    """[Phase 2D] user_email 필수 — delete_pipeline 의 _derive_ids /
    _DELETE_PROJECT_NODE_CYPHER 가 ctx.user_email 기반으로 동작 (멀티테넌시 ID 격리)."""
    return PipelineContext(
        gemini=_NullGemini(),
        neo4j=_Neo4jProxy(),
        idempotency_key=idempotency_key,
        user_email=user_email or "",
        team_id=team_id or "",
    )


# ─── Schemas ───────────────────────────────────────────────────


class DeleteProjectResponse(BaseModel):
    status: str
    project_name: str
    child_count: int


class DeleteMeetingRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    team_id: Optional[str] = None


class DeleteMeetingResponseSync(BaseModel):
    status: str
    task_id: str
    message: Optional[str] = None
    project_name: Optional[str] = None
    deleted_version: Optional[str] = None
    remaining_cps_count: Optional[int] = None
    remaining_prd_count: Optional[int] = None
    cps_master_rebuilt: Optional[bool] = None
    prd_master_rebuilt: Optional[bool] = None


# ─── deleteProject ─────────────────────────────────────────────


@router.delete(
    "/projects/{project_name}",
    response_model=DeleteProjectResponse,
    summary="deleteProject — 프로젝트 전체 노드 삭제 (5-hop)",
)
@limiter.limit("5/minute")
async def delete_project_route(
    request: Request,
    project_name: str,
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> DeleteProjectResponse:
    # IDOR 방어 — 본인 소유 / 팀 멤버만 삭제 가능. DETACH DELETE 가 5-hop 으로
    # CPS/PRD/Skill/Meeting 등 전부 영구 삭제하므로 누락 시 데이터 영구 손실 위험.
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    ctx = _build_context(
        idempotency_key=f"dp-{uuid.uuid4().hex[:8]}",
        user_email=current_user.email,
        team_id=team_id or "",
    )
    try:
        out = await delete_project(ctx, project_name, team_id or "")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return DeleteProjectResponse(**out)


# ─── deleteMeeting (rebuild 포함) ─────────────────────────────


@router.post(
    "/pipelines/delete_meeting",
    response_model=DeleteMeetingResponseSync,
    summary="deleteMeeting — 미팅 삭제 + Master CPS/PRD 재구성 (남은 Delta 있을 때)",
)
@limiter.limit("3/minute")
async def delete_meeting_route(
    request: Request,
    payload: DeleteMeetingRequest,
    wait: bool = Query(False, description="true 시 rebuild 까지 동기 대기"),
    current_user: UserPublic = Depends(get_current_user),
) -> DeleteMeetingResponseSync:
    # 본인 소유 / 팀 멤버만 — 타인 프로젝트의 미팅 삭제 + LLM 비용 강제 차단.
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    task_id = str(uuid.uuid4())
    # delete_pipeline 도 Master CPS/PRD rebuild 시 LLM 호출 (2회) — 토큰 가드 필수.
    await quota.assert_tokens_within_limit(current_user.email)

    if wait:
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
            team_id=payload.team_id or "",
        ) as ctx:
            try:
                result = await run_delete_meeting_pipeline(
                    ctx,
                    DeleteMeetingInput(
                        project_name=payload.project_name, version=payload.version,
                        team_id=payload.team_id or "",
                    ),
                )
            except GeminiError as e:
                logger.exception("delete_meeting gemini error (task=%s)", task_id)
                raise gemini_error_to_http(e) from e
            except ValueError as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
                ) from e
        return DeleteMeetingResponseSync(
            status="success",
            task_id=task_id,
            message=result.message,
            project_name=result.project_name,
            deleted_version=result.deleted_version,
            remaining_cps_count=result.remaining_cps_count,
            remaining_prd_count=result.remaining_prd_count,
            cps_master_rebuilt=result.cps_master_rebuilt,
            prd_master_rebuilt=result.prd_master_rebuilt,
        )

    try:
        await enqueue_delete_meeting(
            task_id=task_id,
            project_name=payload.project_name,
            version=payload.version,
            user_email=current_user.email,  # quota 토큰 누적용
            team_id=payload.team_id or "",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("enqueue delete_meeting failed (task=%s)", task_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {e}",
        ) from e
    return DeleteMeetingResponseSync(status="accepted", task_id=task_id)


@router.get(
    "/pipelines/rebuild_master",
    summary="Master CPS/PRD 강제 재구성 — Delta 소실 없이 Master 만 복구",
)
@limiter.limit("10/minute")
async def rebuild_master_route(
    request: Request,
    project_name: str = Query(..., description="재구성 대상 프로젝트명"),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    기존 CPS/PRD Delta 를 병합해 Master 문서를 재구성한다.

    deleteMeeting 파이프라인에서 LLM 빈 응답으로 Master 가 소실된 경우 복구용.
    Delta 는 삭제하지 않으며 Master 노드만 MERGE(upsert) 한다.
    """
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    task_id = str(uuid.uuid4())
    # [2026-05-26 fix] tracked_pipeline_context 시그니처는 user_email + idempotency_key 만.
    # 이전 코드의 task_id/project_name/pipeline_name 인자는 TypeError 발생시켰음.
    async with tracked_pipeline_context(
        user_email=current_user.email,
        idempotency_key=task_id,
        team_id=team_id or "",
    ) as ctx:
        try:
            result = await run_rebuild_master_pipeline(ctx, project_name, team_id or "")
            return {
                "status": result.status,
                "project_name": result.project_name,
                "cps_rebuilt": result.cps_rebuilt,
                "prd_rebuilt": result.prd_rebuilt,
                "cps_delta_count": result.cps_delta_count,
                "prd_delta_count": result.prd_delta_count,
            }
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except GeminiError as e:
            raise gemini_error_to_http(e) from e


@router.get(
    "/pipelines/delete_meeting/status/{task_id}",
    response_model=PipelineStatusResponse,
    summary="deleteMeeting 작업 상태 조회",
)
@limiter.limit("60/minute")
async def delete_meeting_status_route(
    request: Request,
    task_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> PipelineStatusResponse:
    # ownership 검증 후 status 반환 (Sprint 8 P0).
    info = await get_job_status_for_user(task_id, current_user.email)
    return PipelineStatusResponse(**info)
