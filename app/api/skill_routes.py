"""
Skill (Rule Generator) 라우트 — PR6.

[엔드포인트]
- POST   /api/v2/skills                       → postSkill (bulk upsert)
- GET    /api/v2/skills?project_name=X        → getAllSkill
- GET    /api/v2/skills/duplicate?...         → getDuplicateSkill
- GET    /api/v2/skills/{id}?project_name=X   → getSkill
- DELETE /api/v2/skills/{id}?project_name=X   → deleteSkill
- POST   /api/v2/pipelines/recommend_skills   → recommendSkillsByAI (LLM, async + ?wait=true)

CRUD 는 동기 (Cypher 만), recommend 는 v2 패턴 따라 비동기 기본 + ?wait=true 지원.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from app.api._quota_helpers import tracked_pipeline_context
from app.api._schemas import PipelineStatusResponse
from app.core import quota

from app.clients.gemini_client import GeminiError, gemini_error_to_http
from app.clients import neo4j_client
from app.core.limiter import limiter
from app.core.security import get_current_user
from app.pipelines.skill_recommend_pipeline import (
    CatalogEntry,
    RecommendInput,
    run_skill_recommend_pipeline,
)
from app.pipelines.skill_trigger_fill_pipeline import (
    SkillTriggerInput,
    TriggerFillInput,
    run_trigger_fill_pipeline,
)
from app.pipelines.skill_improve_pipeline import (
    SkillImproveInput,
    run_skill_improve_pipeline,
)
from app.queue.client import enqueue_recommend_skills
from app.queue.status_guard import get_job_status_for_user
from app.service import ownership_repository
from app.service import skill_repository as skills
from app.service.skill_repository import (
    SkillInput,
    SkillOut,
    SkillSummary,
)
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["Skills (Rule Generator)"])


# [2026-05-19] 미사용 _build_context() 제거. 모든 LLM 호출은 tracked_pipeline_
# context 로 wrap (토큰 자동 누적).


# ─── Schemas ───────────────────────────────────────────────────


class PostSkillsRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    skills: List[SkillInput] = Field(..., min_length=1)


class PostSkillsResponse(BaseModel):
    status: str = "ok"
    ids: List[str]


class DuplicateResponse(BaseModel):
    is_duplicate: bool
    existing_ids: List[str]


class DeleteResponse(BaseModel):
    status: str
    deleted_id: Optional[str] = None


class RecommendCatalogItem(BaseModel):
    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    description: str = ""
    category: str = ""


class RecommendSkillsRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    skill_catalog: List[RecommendCatalogItem] = Field(..., min_length=1)
    allowed_categories: List[str] = []


class RecommendedItemResponse(BaseModel):
    id: str
    reason: str = ""
    confidence: Optional[float] = None


class RecommendSkillsResponse(BaseModel):
    status: str
    task_id: str
    recommended: Optional[List[RecommendedItemResponse]] = None
    meta: Optional[Dict[str, Any]] = None


# ─── fillSkillTriggers schemas ─────────────────────────────────


class FillTriggerSkillItem(BaseModel):
    """trigger 생성 대상 한 항목 (명시 입력 시)."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    scope: str = ""
    trigger_condition: str = ""
    instructions: List[str] = []
    tags: List[str] = []


class FillSkillTriggersRequest(BaseModel):
    """project_name 만 주면 해당 프로젝트의 빈-trigger skill 을 서버가 조회.
    skills 를 직접 주면 그 목록만 처리 (프론트가 미저장 draft 를 보낼 때)."""

    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    skills: Optional[List[FillTriggerSkillItem]] = None


class FilledTriggerResponse(BaseModel):
    id: str
    trigger_condition: str
    generated: bool


class FillSkillTriggersResponse(BaseModel):
    status: str
    skills: List[FilledTriggerResponse] = []
    meta: Optional[Dict[str, Any]] = None


# ─── Skill CRUD ────────────────────────────────────────────────


@router.post(
    "/skills",
    response_model=PostSkillsResponse,
    summary="postSkill — Skill 일괄 upsert (ArchService.tech_stack 매칭으로 자동 GOVERNED_BY)",
)
@limiter.limit("5/minute")
async def post_skills(
    request: Request,
    payload: PostSkillsRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> PostSkillsResponse:
    # CREATE 패턴 — gateway_compat 의 _OWNERSHIP_CREATE 와 동일 정책.
    # 본인 소유면 멱등, 다른 유저 소유면 409 (ProjectOwnershipConflict).
    try:
        await ownership_repository.claim(current_user.email, payload.project_name, payload.team_id)
    except ownership_repository.ProjectOwnershipConflict as e:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{e.project}' 는 이미 다른 사용자가 사용 중인 프로젝트 "
                f"이름입니다. 다른 이름을 사용하세요."
            ),
        ) from e
    out = await skills.create_skills(payload.project_name, payload.skills)
    return PostSkillsResponse(status="ok", ids=out["ids"])


@router.get(
    "/skills",
    response_model=List[SkillSummary],
    summary="getAllSkill — 프로젝트의 모든 Skill 요약 목록",
)
@limiter.limit("60/minute")
async def get_all_skills_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> List[SkillSummary]:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    return await skills.get_all_skills(project_name)


@router.get(
    "/skills/duplicate",
    response_model=DuplicateResponse,
    summary="getDuplicateSkill — 동일 이름 존재 여부",
)
@limiter.limit("60/minute")
async def duplicate_skill_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    name: str = Query(..., min_length=1),
    current_user: UserPublic = Depends(get_current_user),
) -> DuplicateResponse:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    result = await skills.find_duplicate_skill(project_name, name)
    return DuplicateResponse(**result)


@router.get(
    "/skills/{skill_id}",
    response_model=SkillOut,
    summary="getSkill — 단일 Skill 상세",
)
@limiter.limit("60/minute")
async def get_skill_route(
    request: Request,
    skill_id: str,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> SkillOut:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    out = await skills.get_skill(project_name, skill_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Skill 을 찾을 수 없습니다.")
    return out


@router.delete(
    "/skills/{skill_id}",
    response_model=DeleteResponse,
    summary="deleteSkill — Skill 노드 + 관계 모두 삭제",
)
@limiter.limit("10/minute")
async def delete_skill_route(
    request: Request,
    skill_id: str,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> DeleteResponse:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    ok = await skills.delete_skill(project_name, skill_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Skill 을 찾을 수 없습니다.")
    return DeleteResponse(status="deleted", deleted_id=skill_id)


# ─── Recommend pipeline ────────────────────────────────────────


@router.post(
    "/pipelines/recommend_skills",
    response_model=RecommendSkillsResponse,
    summary="recommendSkillsByAI — CPS/PRD 기반 카탈로그에서 필요 스킬 추천",
)
@limiter.limit("3/minute")
async def recommend_skills_route(
    request: Request,
    payload: RecommendSkillsRequest,
    wait: bool = Query(False, description="true 시 동기 대기 (큐 미사용)"),
    current_user: UserPublic = Depends(get_current_user),
) -> RecommendSkillsResponse:
    # 본인 소유 프로젝트만 — 타인 프로젝트의 CPS/PRD 로 LLM 호출하면
    # 응답 markdown 에 타인 데이터 포함 + 본인 quota 소진 abuse.
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    task_id = str(uuid.uuid4())
    # 토큰 한도 가드 — LLM 호출 전 차단.
    await quota.assert_tokens_within_limit(current_user.email)

    if wait:
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
            team_id=payload.team_id or "",
        ) as ctx:
            try:
                result = await run_skill_recommend_pipeline(
                    ctx,
                    RecommendInput(
                        project_name=payload.project_name,
                        skill_catalog=[
                            CatalogEntry(
                                id=c.id,
                                name=c.name,
                                description=c.description,
                                category=c.category,
                            )
                            for c in payload.skill_catalog
                        ],
                        allowed_categories=payload.allowed_categories,
                    ),
                )
            except GeminiError as e:
                logger.exception("recommend_skills gemini error (task=%s)", task_id)
                raise gemini_error_to_http(e) from e
            except ValueError as e:
                logger.exception("recommend_skills value error (task=%s)", task_id)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
                ) from e
        return RecommendSkillsResponse(
            status="success",
            task_id=task_id,
            recommended=[
                RecommendedItemResponse(
                    id=r.id, reason=r.reason, confidence=r.confidence
                )
                for r in result.recommended
            ],
            meta=result.meta,
        )

    try:
        await enqueue_recommend_skills(
            task_id=task_id,
            project_name=payload.project_name,
            skill_catalog=[c.model_dump() for c in payload.skill_catalog],
            allowed_categories=payload.allowed_categories,
            user_email=current_user.email,  # quota 토큰 누적용
            team_id=payload.team_id or "",
        )
    except HTTPException:
        raise  # [2026-06] 동시성 429 등 의도된 HTTP 에러는 503 으로 가리지 말고 그대로 전파
    except Exception as e:  # noqa: BLE001
        logger.exception("enqueue failed (task=%s)", task_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {e}",
        ) from e

    return RecommendSkillsResponse(status="accepted", task_id=task_id)


@router.get(
    "/pipelines/recommend_skills/status/{task_id}",
    response_model=PipelineStatusResponse,
    summary="recommendSkillsByAI 작업 상태 조회 (legacy 경로 — /pipelines/status/{task_id} 사용 가능)",
)
@limiter.limit("60/minute")
async def recommend_skills_status(
    request: Request,
    task_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> PipelineStatusResponse:
    # ownership 검증 후 status 반환 (Sprint 8 P0).
    info = await get_job_status_for_user(task_id, current_user.email)
    return PipelineStatusResponse(**info)


# ─── Fill skill triggers pipeline ──────────────────────────────


@router.post(
    "/pipelines/fill_skill_triggers",
    response_model=FillSkillTriggersResponse,
    summary="fillSkillTriggers — trigger_condition 이 빈 Skill 들에 LLM 으로 적용 조건 자동 생성 (병렬)",
)
@limiter.limit("3/minute")
async def fill_skill_triggers_route(
    request: Request,
    payload: FillSkillTriggersRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> FillSkillTriggersResponse:
    # 본인 소유 프로젝트만 — 타인 skill 로 LLM 호출 + 본인 quota 소진 abuse 방어.
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    task_id = str(uuid.uuid4())
    # 토큰 한도 가드 — LLM 호출 전 차단.
    await quota.assert_tokens_within_limit(current_user.email)

    # skills 명시 입력 우선, 없으면 프로젝트의 전체 skill 을 서버가 조회.
    # (파이프라인이 빈-trigger 만 골라 LLM 호출하므로 전체를 넘겨도 안전·저렴.)
    if payload.skills is not None:
        items = [
            SkillTriggerInput(
                id=s.id,
                name=s.name,
                scope=s.scope,
                trigger_condition=s.trigger_condition,
                instructions=s.instructions,
                tags=s.tags,
            )
            for s in payload.skills
        ]
    else:
        rows = await skills.get_skills_for_trigger_fill(payload.project_name)
        items = [
            SkillTriggerInput(
                id=r["id"],
                name=r["name"],
                scope=r.get("scope", ""),
                trigger_condition=r.get("trigger_condition", ""),
                instructions=r.get("instructions", []),
                tags=r.get("tags", []),
            )
            for r in rows
        ]

    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key=task_id,
    ) as ctx:
        try:
            result = await run_trigger_fill_pipeline(
                ctx, TriggerFillInput(skills=items),
            )
        except GeminiError as e:
            logger.exception("fill_skill_triggers gemini error (task=%s)", task_id)
            raise gemini_error_to_http(e) from e
        except ValueError as e:
            logger.exception("fill_skill_triggers value error (task=%s)", task_id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
            ) from e

    return FillSkillTriggersResponse(
        status="success",
        skills=[
            FilledTriggerResponse(
                id=f.id,
                trigger_condition=f.trigger_condition,
                generated=f.generated,
            )
            for f in result.skills
        ],
        meta=result.meta,
    )


# ─── improveSkill schemas + pipeline ───────────────────────────


class ImproveSkillRequest(BaseModel):
    """편집 중인 규칙 1개의 초안 — 프로젝트 무관(라이브러리/프로젝트 공용)."""

    name: str = Field(..., min_length=1)
    scope: str = ""
    trigger_condition: str = ""
    instructions: List[str] = []
    tags: List[str] = []


class ImproveSkillResponse(BaseModel):
    status: str
    improved: bool
    name: str
    scope: str
    trigger_condition: str
    instructions: List[str]
    explanation: str = ""
    meta: Optional[Dict[str, Any]] = None


@router.post(
    "/pipelines/improve_skill",
    response_model=ImproveSkillResponse,
    summary="improveSkill — 사용자가 대충 적은 규칙 초안을 AI 가 구체적 규칙으로 다듬기 (단건, 동기)",
)
@limiter.limit("10/minute")
async def improve_skill_route(
    request: Request,
    payload: ImproveSkillRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> ImproveSkillResponse:
    # 단건 규칙 개선 — 프로젝트 데이터 접근이 아니므로 인증 + 토큰 quota 만으로 충분.
    await quota.assert_tokens_within_limit(current_user.email)
    task_id = str(uuid.uuid4())

    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key=task_id,
    ) as ctx:
        try:
            result = await run_skill_improve_pipeline(
                ctx,
                SkillImproveInput(
                    name=payload.name,
                    scope=payload.scope,
                    trigger_condition=payload.trigger_condition,
                    instructions=payload.instructions,
                    tags=payload.tags,
                ),
            )
        except GeminiError as e:
            logger.exception("improve_skill gemini error (task=%s)", task_id)
            raise gemini_error_to_http(e) from e
        except ValueError as e:
            logger.exception("improve_skill value error (task=%s)", task_id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
            ) from e

    return ImproveSkillResponse(
        status="success",
        improved=result.improved,
        name=result.name,
        scope=result.scope,
        trigger_condition=result.trigger_condition,
        instructions=result.instructions,
        explanation=result.explanation,
        meta=result.meta,
    )
