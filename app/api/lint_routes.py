"""
Lint + Repo 라우트 (PR7).

[엔드포인트]
- POST   /api/v2/pipelines/lint                          → runLint
- POST   /api/v2/pipelines/lint?wait=true                → 동기 실행
- GET    /api/v2/pipelines/lint/status/{task_id}         → 비동기 결과 조회
- GET    /api/v2/pipelines/lint/last?project_name=&github_url=  → getLastLintResult
- POST   /api/v2/pipelines/generate_fix_spec             → generateFixSpec
- POST   /api/v2/projects/repos                          → addProjectRepo
- GET    /api/v2/projects/repos?project_name=X           → getProjectRepos
- DELETE /api/v2/projects/repos                          → deleteProjectRepo
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from app.api._quota_helpers import tracked_pipeline_context
from app.api._schemas import PipelineStatusResponse
from app.core import quota

from app.clients.gemini_client import GeminiError, gemini_error_to_http
from app.clients import neo4j_client
from app.core.limiter import limiter
from app.core.security import get_current_user
from app.pipelines.fix_spec_pipeline import (
    FixSpecInput,
    run_fix_spec_pipeline,
)
from app.pipelines.lint_pipeline import LintInput, run_lint_pipeline
from app.queue.client import (
    enqueue_generate_fix_spec,
    enqueue_run_lint,
)
from app.queue.status_guard import get_job_status_for_user
from app.service import lint_repository, ownership_repository, repo_repository
from app.service.lint_repository import LintResult
from app.service.repo_repository import RepoIn, RepoOut
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["Lint / Repo"])


# [2026-05-19] 미사용 _build_context() 제거. 모든 LLM 호출은 tracked_pipeline_
# context 로 wrap (토큰 자동 누적).


# ─── Schemas ───────────────────────────────────────────────────


class LintRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    github_url: str = Field(..., min_length=1)


class LintResponse(BaseModel):
    status: str
    task_id: str
    result: Optional[LintResult] = None


class FixSpecRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    github_url: str = Field(..., min_length=1)
    lint_result: Dict[str, Any]


class FixSpecResponse(BaseModel):
    status: str
    task_id: str
    success: Optional[bool] = None
    markdown: Optional[str] = None
    filename: Optional[str] = None
    message: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ReposListResponse(BaseModel):
    repos: List[RepoOut]
    count: int


class RepoDeleteRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    url: str = Field(..., min_length=1)


class GenericOkResponse(BaseModel):
    ok: bool = True
    project_name: Optional[str] = None
    url: Optional[str] = None


# ─── Lint pipeline ─────────────────────────────────────────────


@router.post(
    "/pipelines/lint",
    response_model=LintResponse,
    summary="runLint — Spack/DDD/Arch/Skill/기획(Story·Screen) 명세 대비 GitHub 코드 적용률 분석",
)
@limiter.limit("3/minute")
async def run_lint_route(
    request: Request,
    payload: LintRequest,
    wait: bool = Query(False, description="true 시 동기 대기"),
    current_user: UserPublic = Depends(get_current_user),
) -> LintResponse:
    # IDOR — 타인 프로젝트 명세로 lint 돌리면 응답에 타인 PRD/Spack/Skill 내용 포함 +
    # 본인 OAuth 토큰으로 임의 GitHub URL 접근 + 본인 quota 소진.
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    task_id = str(uuid.uuid4())
    # 토큰 한도 가드 — Lint 의 Phase B (LLM residual) 가 큰 토큰 소비.
    await quota.assert_tokens_within_limit(current_user.email)

    # 사용자 OAuth access_token — private repo / rate-limit 확장에 필요.
    # 미연결 사용자는 None → 파이프라인이 anonymous 로 호출 (public repo 만).
    from app.service import user_repository
    user_token = await user_repository.get_github_access_token(current_user.email)

    if wait:
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
        ) as ctx:
            try:
                result = await run_lint_pipeline(
                    ctx,
                    LintInput(
                        project_name=payload.project_name,
                        github_url=payload.github_url,
                        team_id=payload.team_id or "",
                    ),
                    user_token=user_token,
                )
            except GeminiError as e:
                logger.exception("lint pipeline gemini error (task=%s)", task_id)
                raise gemini_error_to_http(e) from e
            except ValueError as e:
                logger.exception("lint pipeline value error (task=%s)", task_id)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
                ) from e
        return LintResponse(status="success", task_id=task_id, result=result)

    try:
        await enqueue_run_lint(
            task_id=task_id,
            project_name=payload.project_name,
            github_url=payload.github_url,
            user_token=user_token,
            user_email=current_user.email,  # quota 토큰 누적용
            team_id=payload.team_id or "",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("enqueue lint failed (task=%s)", task_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {e}",
        ) from e
    return LintResponse(status="accepted", task_id=task_id)


@router.get(
    "/pipelines/lint/status/{task_id}",
    response_model=PipelineStatusResponse,
    summary="runLint 작업 상태 조회",
)
@limiter.limit("60/minute")
async def lint_status_route(
    request: Request,
    task_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> PipelineStatusResponse:
    # ownership 검증 후 status 반환 (Sprint 8 P0).
    info = await get_job_status_for_user(task_id, current_user.email)
    return PipelineStatusResponse(**info)


@router.get(
    "/pipelines/lint/last",
    response_model=LintResult,
    summary="getLastLintResult — 동일 (project, githubUrl) 의 가장 최근 Lint 결과",
)
@limiter.limit("60/minute")
async def get_last_lint_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    github_url: str = Query(..., min_length=1),
    current_user: UserPublic = Depends(get_current_user),
) -> LintResult:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    result = await lint_repository.get_last_lint_result(project_name, github_url, team_id=team_id or "")
    if result is None:
        raise HTTPException(
            status_code=404, detail="Lint 결과를 찾을 수 없습니다."
        )
    return result


@router.post(
    "/pipelines/generate_fix_spec",
    response_model=FixSpecResponse,
    summary="generateFixSpec — Lint 실패 항목 + 명세 → 한국어 마크다운 수정 지시서",
)
@limiter.limit("3/minute")
async def generate_fix_spec_route(
    request: Request,
    payload: FixSpecRequest,
    wait: bool = Query(False),
    current_user: UserPublic = Depends(get_current_user),
) -> FixSpecResponse:
    # 타인 lint 결과로 fix_spec markdown 생성 시 응답에 타인 명세 + 코드 포함.
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    task_id = str(uuid.uuid4())
    # 토큰 한도 가드.
    await quota.assert_tokens_within_limit(current_user.email)

    if wait:
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
        ) as ctx:
            try:
                result = await run_fix_spec_pipeline(
                    ctx,
                    FixSpecInput(
                        project_name=payload.project_name,
                        github_url=payload.github_url,
                        lint_result=payload.lint_result,
                    ),
                )
            except GeminiError as e:
                raise gemini_error_to_http(e) from e
        return FixSpecResponse(
            status="success",
            task_id=task_id,
            success=result.success,
            markdown=result.markdown,
            filename=result.filename,
            message=result.message,
            metadata=result.metadata,
        )

    try:
        await enqueue_generate_fix_spec(
            task_id=task_id,
            project_name=payload.project_name,
            github_url=payload.github_url,
            lint_result=payload.lint_result,
            user_email=current_user.email,  # quota 토큰 누적용
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("enqueue fix_spec failed (task=%s)", task_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {e}",
        ) from e
    return FixSpecResponse(status="accepted", task_id=task_id)


# ─── Project Repo CRUD ─────────────────────────────────────────


@router.post(
    "/projects/repos",
    response_model=RepoOut,
    summary="addProjectRepo — Project + Repo 노드 upsert",
)
@limiter.limit("5/minute")
async def add_project_repo_route(
    request: Request,
    payload: RepoIn,
    current_user: UserPublic = Depends(get_current_user),
) -> RepoOut:
    # CREATE 패턴 — 본인 소유면 멱등, 다른 owner 면 409.
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
    return await repo_repository.add_repo(payload)


@router.get(
    "/projects/repos",
    response_model=ReposListResponse,
    summary="getProjectRepos — 프로젝트의 Repo 목록",
)
@limiter.limit("60/minute")
async def get_project_repos_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> ReposListResponse:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    repos = await repo_repository.get_repos(project_name, team_id=team_id or "")
    return ReposListResponse(repos=repos, count=len(repos))


@router.delete(
    "/projects/repos",
    response_model=GenericOkResponse,
    summary="deleteProjectRepo — Repo 삭제 (url 매칭)",
)
@limiter.limit("5/minute")
async def delete_project_repo_route(
    request: Request,
    payload: RepoDeleteRequest = Body(...),
    current_user: UserPublic = Depends(get_current_user),
) -> GenericOkResponse:
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    await repo_repository.delete_repo(payload.project_name, payload.url, team_id=payload.team_id or "")
    return GenericOkResponse(
        ok=True, project_name=payload.project_name, url=payload.url
    )
