"""
createMD 라우트 (PR11) — Spack/DDD/Architecture 그래프 → 바이브코딩용 MD 3종.

- POST /api/v2/pipelines/create_md                  → createMD (default async)
- POST /api/v2/pipelines/create_md?wait=true        → 동기 실행
- GET  /api/v2/pipelines/create_md/status/{task_id} → 비동기 결과 조회
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
from app.pipelines.create_md_pipeline import (
    CreateMdInput,
    run_create_md_pipeline,
)
from app.queue.client import enqueue_create_md
from app.queue.status_guard import get_job_status_for_user
from app.service import ownership_repository
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["createMD (MD Export)"])


# [2026-05-19] 미사용 _build_context() 제거. 모든 LLM 호출은 tracked_pipeline_
# context 로 wrap (토큰 자동 누적). raw GeminiClient PipelineContext 가 필요한
# 곳 없음.


class CreateMdRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None


class CreateMdResponse(BaseModel):
    status: str
    task_id: str
    project_name: Optional[str] = None
    spack_md: Optional[str] = None
    ddd_md: Optional[str] = None
    arch_md: Optional[str] = None
    orchestrator_md: Optional[str] = None
    checklist_md: Optional[str] = None
    diagnostic: Optional[Dict[str, Any]] = None


@router.post(
    "/pipelines/create_md",
    response_model=CreateMdResponse,
    summary="createMD — Spack/DDD/Architecture 그래프 → MD 3종 (LLM × 3 병렬)",
)
@limiter.limit("3/minute")
async def create_md_route(
    request: Request,
    payload: CreateMdRequest,
    wait: bool = Query(False),
    current_user: UserPublic = Depends(get_current_user),
) -> CreateMdResponse:
    # 타인 Spack/DDD/Architecture 로 MD 생성 시 응답에 타인 설계 전문 포함 +
    # 본인 quota 로 LLM 3회 호출. 가장 큰 단일 소비 중 하나라 abuse 영향 큼.
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    task_id = str(uuid.uuid4())
    # 토큰 한도 가드 — createMD 는 LLM 3회 병렬 호출 (가장 큰 단일 소비 중 하나).
    await quota.assert_tokens_within_limit(current_user.email)

    if wait:
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
        ) as ctx:
            try:
                result = await run_create_md_pipeline(
                    ctx, CreateMdInput(project_name=payload.project_name)
                )
            except GeminiError as e:
                logger.exception("create_md gemini error (task=%s)", task_id)
                raise gemini_error_to_http(e) from e
            except ValueError as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
                ) from e
        return CreateMdResponse(
            status="success",
            task_id=task_id,
            project_name=result.project_name,
            spack_md=result.spack_md,
            ddd_md=result.ddd_md,
            arch_md=result.arch_md,
            orchestrator_md=result.orchestrator_md,
            checklist_md=result.checklist_md,
            diagnostic=result.diagnostic,
        )

    try:
        await enqueue_create_md(
            task_id=task_id,
            project_name=payload.project_name,
            user_email=current_user.email,  # quota 토큰 누적용
        )
    except HTTPException:
        raise  # [2026-06] 동시성 429 등 의도된 HTTP 에러는 503 으로 가리지 말고 그대로 전파
    except Exception as e:  # noqa: BLE001
        logger.exception("enqueue create_md failed (task=%s)", task_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {e}",
        ) from e
    return CreateMdResponse(status="accepted", task_id=task_id)


@router.get(
    "/pipelines/create_md/status/{task_id}",
    response_model=PipelineStatusResponse,
    summary="createMD 작업 상태 조회",
)
@limiter.limit("60/minute")
async def create_md_status_route(
    request: Request,
    task_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> PipelineStatusResponse:
    # ownership 검증 후 status 반환 (Sprint 8 P0).
    info = await get_job_status_for_user(task_id, current_user.email)
    return PipelineStatusResponse(**info)
