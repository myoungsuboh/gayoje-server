"""
v2 파이프라인 라우트 — backend 내부 파이프라인 비동기/동기 실행.

[비동기 (기본)]
  - POST /api/v2/pipelines/* — arq enqueue. task_id 가 곧 arq job_id.
  - GET  /api/v2/pipelines/{stage}/status/{task_id} — 큐/완료 상태 + 결과 조회.

[동기 (`?wait=true`)]
  - 디버깅 / 통합 테스트용. 파이프라인 종료까지 HTTP 응답 대기.

[엔드포인트]
  - POST /api/v2/pipelines/post_meeting — CPS + PRD 체이닝.
  - POST /api/v2/pipelines/cps          — CPS 단독.
  - POST /api/v2/pipelines/prd          — PRD 단독 (cps_graph 직접 입력).
  - POST /api/v2/pipelines/design       — createDesign (Spack/DDD/Architecture).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from app.api._schemas import PipelineStatusResponse
from app.core.meeting_validation import (
    MeetingContentTooShort,
    assert_meeting_content_substantial,
)
from app.api._quota_helpers import tracked_pipeline_context
from app.clients.gemini_client import GeminiError, gemini_error_to_http
from app.clients import neo4j_client
from app.core import quota
from app.core.config import settings
from app.core.limiter import limiter
from app.core.wait_guard import guard_wait_mode
from app.core.security import get_current_user
from app.clients.github_client import GitHubClient, GitHubError
from app.pipelines.cps_pipeline import CpsInput, run_cps_pipeline
from app.pipelines.design_pipeline import DesignInput, run_design_pipeline
from app.pipelines.github_onboard_pipeline import (
    GithubOnboardInput,
    run_github_onboard_pipeline,
)
from app.pipelines.prd_pipeline import PrdInput, run_prd_pipeline
from app.queue.client import (
    enqueue_cps,
    enqueue_design,
    enqueue_github_onboard,
    enqueue_post_meeting,
    enqueue_prd,
)
from app.queue.status_guard import get_job_status_for_user
from app.service import ownership_repository, query_repository
from app.service.ownership_repository import ProjectOwnershipConflict
from app.service.user_repository import UserPublic


async def _claim_or_409(email: str, project: str, team_id: Optional[str] = None) -> None:
    """프로젝트 claim — 다른 유저 소유 시 409 Conflict 로 매핑.

    team_id 지정 시 팀 프로젝트로 claim (멤버십 + 유료 플랜 게이트).
    """
    try:
        await ownership_repository.claim(email, project, team_id)
    except ProjectOwnershipConflict as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{e.project}' 는 이미 다른 사용자가 사용 중인 프로젝트 이름입니다. 다른 이름을 사용하세요.",
        ) from e


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["Pipelines v2"])


# [2026-05-19] 미사용 _build_context() + _Neo4jProxy import 제거.
# 모든 LLM 호출은 tracked_pipeline_context 로 wrap (토큰 자동 누적) —
# raw PipelineContext 가 필요한 곳 없음.


# ─── Schemas ───────────────────────────────────────────────────


class CpsRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    date: str = ""
    # [2026-05 DoS 방어] 8MB cap — 미들웨어 10MB body 한도와 짝.
    # 일반 회의록 텍스트는 100KB 이하, 8MB 면 충분히 여유 + 거대 페이로드 거부.
    # [2026-05-18 환각 차단] 의미적 최소치 검증은 _validate_substantial 에서.
    # 빈 문자열만 Pydantic 거부, 짧은 입력은 validator 가 친근한 에러 메시지로.
    meeting_content: str = Field(..., min_length=1, max_length=8 * 1024 * 1024)
    previous_cps_id: Optional[str] = None
    # 팀 프로젝트로 작업 시 지정 — 없으면 개인 프로젝트 (기존 동작).
    team_id: Optional[str] = None

    @field_validator("meeting_content")
    @classmethod
    def _validate_substantial(cls, v: str) -> str:
        # 너무 짧으면 MeetingContentTooShort raise → 422 응답에 detail 포함.
        # 라우트가 catch 해서 400 으로 매핑.
        assert_meeting_content_substantial(v)
        return v


class CpsResponse(BaseModel):
    status: str
    task_id: str
    mode: Optional[str] = None
    master_cps_id: Optional[str] = None
    delta_cps_id: Optional[str] = None
    meeting_log_id: Optional[str] = None
    diagnostic: Optional[Dict[str, Any]] = None


class PostMeetingRequest(BaseModel):
    """postMeeting — CPS + PRD 둘 다 생성."""

    project_name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    date: str = ""
    # [2026-05 DoS 방어] 8MB cap — 미들웨어 10MB body 한도와 짝.
    # 일반 회의록 텍스트는 100KB 이하, 8MB 면 충분히 여유 + 거대 페이로드 거부.
    # [2026-05-18 환각 차단] _validate_substantial 가 의미적 최소치 검증.
    meeting_content: str = Field(..., min_length=1, max_length=8 * 1024 * 1024)
    previous_cps_id: Optional[str] = None
    previous_prd_id: Optional[str] = None
    team_id: Optional[str] = None

    @field_validator("meeting_content")
    @classmethod
    def _validate_substantial(cls, v: str) -> str:
        assert_meeting_content_substantial(v)
        return v


class PostMeetingResponse(BaseModel):
    status: str
    task_id: str
    cps: Optional[Dict[str, Any]] = None
    prd: Optional[Dict[str, Any]] = None


class PrdRequest(BaseModel):
    """PRD 단독 재실행. cps_graph 를 직접 입력으로 받음 (수동 모드)."""

    project_name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    cps_graph: Dict[str, Any] = Field(..., description="CPS Agent 가 만든 그래프 JSON")
    previous_prd_id: Optional[str] = None
    team_id: Optional[str] = None


class PrdResponse(BaseModel):
    status: str
    task_id: str
    mode: Optional[str] = None
    master_prd_id: Optional[str] = None
    delta_prd_id: Optional[str] = None
    diagnostic: Optional[Dict[str, Any]] = None


class OnboardFromGithubRequest(BaseModel):
    """GitHub URL → V1 + CPS 자동 생성 (Vibe Coding entry — 2026-05-26)."""

    project_name: str = Field(..., min_length=1, max_length=100)
    github_url: str = Field(..., min_length=1, max_length=500)
    team_id: Optional[str] = None


class OnboardFromGithubResponse(BaseModel):
    status: str
    task_id: str
    # wait=true 일 때만 채워짐 — async 응답엔 task_id 만.
    project_name: Optional[str] = None
    repo_full_name: Optional[str] = None
    v1_markdown_size: Optional[int] = None
    sampled_file_count: Optional[int] = None
    cps_master_id: Optional[str] = None
    cps_delta_id: Optional[str] = None
    cps_mode: Optional[str] = None
    prd_master_id: Optional[str] = None
    prd_delta_id: Optional[str] = None
    prd_mode: Optional[str] = None
    diagnostic: Optional[Dict[str, Any]] = None


class DesignRequest(BaseModel):
    """createDesign — PRD 마스터를 가져와 Spack/DDD/Architecture 생성."""

    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None


class DesignResponse(BaseModel):
    status: str
    task_id: str
    project_name: Optional[str] = None
    master_prd_id: Optional[str] = None
    spack: Optional[Dict[str, Any]] = None
    ddd: Optional[Dict[str, Any]] = None
    architecture: Optional[Dict[str, Any]] = None
    # [2026-05] top-level health — cross-stage 정합성 위반을 FE 가 즉시 표시.
    # has_errors=true 면 빨간 배지 (Spack ↔ DDD ↔ Architecture 이름 불일치 등).
    health: Optional[Dict[str, Any]] = None
    diagnostic: Optional[Dict[str, Any]] = None


# ─── Routes: CPS-only (기존 PR1+PR2) ────────────────────────────


@router.post(
    "/pipelines/cps",
    response_model=CpsResponse,
    summary="postMeeting → CPS (CPS 단독 실행, 디버그/수동용)",
)
@limiter.limit("3/minute")
async def run_cps(
    request: Request,
    payload: CpsRequest,
    wait: bool = Query(False, description="true 시 파이프라인 종료까지 동기 대기 (큐 미사용)"),
    current_user: UserPublic = Depends(get_current_user),
) -> CpsResponse:
    """Rate limit: IP 당 분당 3회 (CPS LLM 호출 보호)."""
    guard_wait_mode(wait, current_user)
    task_id = str(uuid.uuid4())
    # CPS 단독 실행 — 신규 프로젝트 claim 또는 본인 소유 확인 (다른 owner 시 409)
    await _claim_or_409(current_user.email, payload.project_name, payload.team_id)
    # [2026-05-18 Phase 1 동시접속] (project, version) 사전 체크 — quota 차감 전.
    # PC + 모바일 동시 저장 시 같은 v1.1 두 번 → 409 응답 (사용자 손해 0).
    if await query_repository.meeting_log_exists(payload.project_name, payload.version, team_id=payload.team_id or ""):
        raise HTTPException(
            status_code=409,
            detail=(
                f"이미 {payload.version} 미팅 로그가 존재합니다 — 다른 디바이스에서 먼저 "
                f"저장됐을 수 있습니다. 새로고침 후 다시 확인해주세요."
            ),
        )
    # 등급별 한도 체크 (순서 의도):
    #   1) 토큰 한도 (cheap: get_usage 1회) — LLM 비용 보호
    #   2) 회의록 글자수 (cheap) — 사용자가 즉시 인지 가능한 에러
    #   3) 미팅 카운트 (atomic +1) — 1·2 통과 후 차감
    # 초과 시 HTTPException(402, detail.code='QUOTA_EXCEEDED').
    await quota.assert_tokens_within_limit(current_user.email)
    await quota.assert_summary_within_limit(current_user.email, payload.meeting_content)
    await quota.acquire_meeting_quota(current_user.email)

    if wait:
        cps_input = CpsInput(
            project_name=payload.project_name,
            version=payload.version,
            date=payload.date,
            meeting_content=payload.meeting_content,
            previous_cps_id=payload.previous_cps_id,
            team_id=payload.team_id or "",
        )
        # quota 토큰 자동 누적 — 성공/실패 양쪽 라우트 종료 시 add_tokens.
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
            team_id=payload.team_id or "",
        ) as ctx:
            try:
                result = await run_cps_pipeline(ctx, cps_input)
            except GeminiError as e:
                logger.exception("cps pipeline gemini error (task=%s)", task_id)
                raise gemini_error_to_http(e) from e
            except ValueError as e:
                logger.exception("cps pipeline value error (task=%s)", task_id)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
                ) from e
        return CpsResponse(
            status="success",
            task_id=task_id,
            mode=result.mode,
            master_cps_id=result.master_cps_id,
            delta_cps_id=result.delta_cps_id,
            meeting_log_id=result.meeting_log_id,
            diagnostic=result.diagnostic,
        )

    try:
        await enqueue_cps(
            task_id=task_id,
            project_name=payload.project_name,
            version=payload.version,
            date=payload.date,
            meeting_content=payload.meeting_content,
            previous_cps_id=payload.previous_cps_id,
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

    return CpsResponse(status="accepted", task_id=task_id)


# ─── Routes: post_meeting (CPS + PRD 체이닝) ─────────────────────


@router.post(
    "/pipelines/post_meeting",
    response_model=PostMeetingResponse,
    summary="postMeeting (CPS + PRD 동시 생성)",
)
@limiter.limit("3/minute")
async def run_post_meeting(
    request: Request,
    payload: PostMeetingRequest,
    wait: bool = Query(False, description="true 시 두 파이프라인 종료까지 동기 대기"),
    current_user: UserPublic = Depends(get_current_user),
) -> PostMeetingResponse:
    """Rate limit: IP 당 분당 3회 (CPS+PRD 체인 — 가장 무거운 작업)."""
    guard_wait_mode(wait, current_user)
    task_id = str(uuid.uuid4())
    # postMeeting 은 새 프로젝트 시작점도 됨 → claim (충돌 시 409)
    await _claim_or_409(current_user.email, payload.project_name, payload.team_id)
    # [2026-05-18 Phase 1 동시접속] (project, version) 사전 체크 — quota 차감 전.
    if await query_repository.meeting_log_exists(payload.project_name, payload.version, team_id=payload.team_id or ""):
        raise HTTPException(
            status_code=409,
            detail=(
                f"이미 {payload.version} 미팅 로그가 존재합니다 — 다른 디바이스에서 먼저 "
                f"저장됐을 수 있습니다. 새로고침 후 다시 확인해주세요."
            ),
        )
    # 등급별 한도 체크 — wait/enqueue 양 경로 공통 (가드 통과 후 분기).
    # CPS+PRD 체인이라 LLM 2회 호출 — 토큰/글자수/미팅 한도 모두 검사 후에야 진입.
    await quota.assert_tokens_within_limit(current_user.email)
    await quota.assert_summary_within_limit(current_user.email, payload.meeting_content)
    await quota.acquire_meeting_quota(current_user.email)

    if wait:
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
            team_id=payload.team_id or "",
        ) as ctx:
            try:
                cps_result = await run_cps_pipeline(
                    ctx,
                    CpsInput(
                        project_name=payload.project_name,
                        version=payload.version,
                        date=payload.date,
                        meeting_content=payload.meeting_content,
                        previous_cps_id=payload.previous_cps_id,
                        team_id=payload.team_id or "",
                    ),
                )
                prd_result = await run_prd_pipeline(
                    ctx,
                    PrdInput(
                        project_name=payload.project_name,
                        version=payload.version,
                        cps_graph=cps_result.cps_graph,
                        previous_prd_id=payload.previous_prd_id,
                        team_id=payload.team_id or "",
                        # [2026-06-04] CPS delta 가 비어도 PRD 가 회의록으로 생성되도록 raw fallback.
                        meeting_content=payload.meeting_content,
                    ),
                )
            except GeminiError as e:
                logger.exception("post_meeting gemini error (task=%s)", task_id)
                raise gemini_error_to_http(e) from e
            except ValueError as e:
                logger.exception("post_meeting value error (task=%s)", task_id)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
                ) from e
        return PostMeetingResponse(
            status="success",
            task_id=task_id,
            cps={
                "mode": cps_result.mode,
                "master_cps_id": cps_result.master_cps_id,
                "delta_cps_id": cps_result.delta_cps_id,
                "meeting_log_id": cps_result.meeting_log_id,
                "diagnostic": cps_result.diagnostic,
            },
            prd={
                "mode": prd_result.mode,
                "master_prd_id": prd_result.master_prd_id,
                "delta_prd_id": prd_result.delta_prd_id,
                "diagnostic": prd_result.diagnostic,
            },
        )

    try:
        await enqueue_post_meeting(
            task_id=task_id,
            project_name=payload.project_name,
            version=payload.version,
            date=payload.date,
            meeting_content=payload.meeting_content,
            previous_cps_id=payload.previous_cps_id,
            previous_prd_id=payload.previous_prd_id,
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

    return PostMeetingResponse(status="accepted", task_id=task_id)


# ─── Routes: PRD 단독 (수동 재실행) ─────────────────────────────


@router.post(
    "/pipelines/prd",
    response_model=PrdResponse,
    summary="PRD 단독 실행 (cps_graph 를 직접 입력)",
)
@limiter.limit("3/minute")
async def run_prd(
    request: Request,
    payload: PrdRequest,
    wait: bool = Query(False, description="true 시 파이프라인 종료까지 동기 대기"),
    current_user: UserPublic = Depends(get_current_user),
) -> PrdResponse:
    """Rate limit: IP 당 분당 3회 (PRD LLM 호출 보호)."""
    guard_wait_mode(wait, current_user)
    task_id = str(uuid.uuid4())
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    # 토큰 한도 가드 — PRD 도 LLM 호출 (cps_graph 기반 PRD agent + master 병합).
    await quota.assert_tokens_within_limit(current_user.email)

    if wait:
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
            team_id=payload.team_id or "",
        ) as ctx:
            try:
                result = await run_prd_pipeline(
                    ctx,
                    PrdInput(
                        project_name=payload.project_name,
                        version=payload.version,
                        cps_graph=payload.cps_graph,
                        previous_prd_id=payload.previous_prd_id,
                        team_id=payload.team_id or "",
                    ),
                )
            except GeminiError as e:
                logger.exception("prd pipeline gemini error (task=%s)", task_id)
                raise gemini_error_to_http(e) from e
            except ValueError as e:
                logger.exception("prd pipeline value error (task=%s)", task_id)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
                ) from e
        return PrdResponse(
            status="success",
            task_id=task_id,
            mode=result.mode,
            master_prd_id=result.master_prd_id,
            delta_prd_id=result.delta_prd_id,
            diagnostic=result.diagnostic,
        )

    try:
        await enqueue_prd(
            task_id=task_id,
            project_name=payload.project_name,
            version=payload.version,
            cps_graph=payload.cps_graph,
            previous_prd_id=payload.previous_prd_id,
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

    return PrdResponse(status="accepted", task_id=task_id)


# ─── Routes: GitHub Onboard (Vibe Coding entry — 2026-05-26) ───


def _github_error_to_http(e: GitHubError) -> HTTPException:
    """GitHubError → 사용자 친화 HTTPException 매핑.

    - 404: repo 없음 또는 미공개.
    - 401/403: 권한 부족 (OAuth 미연결 / token 만료 / private repo).
    - 그 외: 422 (URL 파싱 실패 등).
    """
    status_code = getattr(e, "status", None)
    if status_code == 404:
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "GITHUB_REPO_NOT_FOUND",
                "message": "GitHub 저장소를 찾을 수 없습니다. URL 을 확인하거나 private repo 인 경우 프로필에서 GitHub 계정을 연결해주세요.",
            },
        )
    if status_code in (401, 403):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "GITHUB_REPO_PRIVATE_NEEDS_AUTH",
                "message": "GitHub 권한이 부족합니다. 프로필 → 연결된 계정 → GitHub 에서 계정을 연결해주세요.",
            },
        )
    # URL 파싱 실패 등 일반 422
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": "INVALID_GITHUB_URL", "message": str(e)},
    )


@router.post(
    "/pipelines/onboard_from_github",
    response_model=OnboardFromGithubResponse,
    summary="GitHub URL → V1 + CPS 자동 생성 (회의록 없이 시작)",
)
@limiter.limit("3/minute")
async def run_onboard_from_github(
    request: Request,
    payload: OnboardFromGithubRequest,
    wait: bool = Query(False, description="true 시 30-60초 동기 대기 (dev 디버깅용)"),
    current_user: UserPublic = Depends(get_current_user),
) -> OnboardFromGithubResponse:
    """
    Vibe Coding entry — 회의록 없이 GitHub URL 한 줄로 V1 markdown 생성 후 기존
    CPS pipeline 합류. 결과는 동일 사용자의 plan.vue 에서 V1 표시 + CPS 검토 흐름.

    [Rate limit] 3/minute — Lint + Design 과 동일.
    [Ownership] 신규 프로젝트 claim. 동일 사용자가 같은 project_name 보유 시 409.
    """
    guard_wait_mode(wait, current_user)
    task_id = str(uuid.uuid4())

    # 1) ownership claim — 같은 사용자가 동일 project_name 보유 시 409.
    await _claim_or_409(current_user.email, payload.project_name, payload.team_id)

    # 2) 토큰 한도 가드 — onboard pipeline 의 LLM × 1 (V1) + CPS pipeline 의 LLM ×
    # 2~3 (extract + impact + merge) 합산 시 일반 미팅 5건 수준 (~110K 토큰).
    await quota.assert_tokens_within_limit(current_user.email)
    # 3) 미팅 카운트 + 1 — onboard 도 미팅 1건으로 계수 (free 한도 보호).
    await quota.acquire_meeting_quota(current_user.email)

    # 4) GitHub OAuth access_token 조회 — private repo 접근에 필요. None 이면 anonymous.
    from app.service import user_repository
    user_token = await user_repository.get_github_access_token(current_user.email)

    if wait:
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
            team_id=payload.team_id or "",
        ) as ctx:
            try:
                github = (
                    GitHubClient(user_token=user_token) if user_token else GitHubClient()
                )
                result = await run_github_onboard_pipeline(
                    ctx,
                    GithubOnboardInput(
                        project_name=payload.project_name,
                        github_url=payload.github_url,
                        user_email=current_user.email,
                        team_id=payload.team_id or "",
                    ),
                    github_client=github,
                )
            except GitHubError as e:
                logger.warning(
                    "onboard github error (task=%s url=%s): %s",
                    task_id, payload.github_url, e,
                )
                raise _github_error_to_http(e) from e
            except GeminiError as e:
                logger.exception("onboard gemini error (task=%s)", task_id)
                raise gemini_error_to_http(e) from e
            except ValueError as e:
                logger.exception("onboard value error (task=%s)", task_id)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e),
                ) from e
        cps = result.cps_result
        prd = result.prd_result
        return OnboardFromGithubResponse(
            status="success",
            task_id=task_id,
            project_name=result.project_name,
            repo_full_name=result.repo_full_name,
            v1_markdown_size=result.v1_markdown_size,
            sampled_file_count=result.sampled_file_count,
            cps_master_id=cps.master_cps_id if cps else None,
            cps_delta_id=cps.delta_cps_id if cps else None,
            cps_mode=cps.mode if cps else None,
            prd_master_id=prd.master_prd_id if prd else None,
            prd_delta_id=prd.delta_prd_id if prd else None,
            prd_mode=prd.mode if prd else None,
            diagnostic=result.diagnostic,
        )

    try:
        await enqueue_github_onboard(
            task_id=task_id,
            project_name=payload.project_name,
            github_url=payload.github_url,
            user_token=user_token,
            user_email=current_user.email,
            team_id=payload.team_id or "",
        )
    except HTTPException:
        raise  # [2026-06] 동시성 429 등 의도된 HTTP 에러는 503 으로 가리지 말고 그대로 전파
    except Exception as e:  # noqa: BLE001
        logger.exception("enqueue onboard failed (task=%s)", task_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {e}",
        ) from e
    return OnboardFromGithubResponse(status="accepted", task_id=task_id)


@router.get(
    "/pipelines/onboard_from_github/status/{task_id}",
    response_model=PipelineStatusResponse,
    summary="GitHub onboard 작업 상태 조회",
)
@limiter.limit("60/minute")
async def onboard_from_github_status(
    request: Request,
    task_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> PipelineStatusResponse:
    """ownership 검증 후 status 반환 (Sprint 8 P0)."""
    info = await get_job_status_for_user(task_id, current_user.email)
    return PipelineStatusResponse(**info)


# ─── Routes: Design (createDesign — Spack/DDD/Architecture) ────


@router.post(
    "/pipelines/design",
    response_model=DesignResponse,
    summary="createDesign — PRD 마스터 기반 Spack/DDD/Architecture 생성",
)
@limiter.limit("3/minute")
async def run_design(
    request: Request,
    payload: DesignRequest,
    wait: bool = Query(False, description="true 시 3개 Agent 종료까지 동기 대기"),
    current_user: UserPublic = Depends(get_current_user),
) -> DesignResponse:
    """Rate limit: IP 당 분당 3회 (Design 3종 LLM 호출 보호)."""
    guard_wait_mode(wait, current_user)
    task_id = str(uuid.uuid4())
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    # 토큰 한도 가드 — Spack/DDD/Arch 3종 LLM 동시 호출 (가장 큰 단일 소비).
    await quota.assert_tokens_within_limit(current_user.email)

    if wait:
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
            team_id=payload.team_id or "",
        ) as ctx:
            try:
                result = await run_design_pipeline(
                    ctx,
                    DesignInput(project_name=payload.project_name),
                )
            except GeminiError as e:
                logger.exception("design pipeline gemini error (task=%s)", task_id)
                raise gemini_error_to_http(e) from e
            except ValueError as e:
                logger.exception("design pipeline value error (task=%s)", task_id)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
                ) from e
        return DesignResponse(
            status="success",
            task_id=task_id,
            project_name=result.project_name,
            master_prd_id=result.master_prd_id,
            spack=result.spack,
            ddd=result.ddd,
            architecture=result.architecture,
            health=result.health,
            diagnostic=result.diagnostic,
        )

    try:
        await enqueue_design(
            task_id=task_id,
            project_name=payload.project_name,
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

    return DesignResponse(status="accepted", task_id=task_id)


# ─── Status (모든 파이프라인 공용 — task_id 만 있으면 동작) ────


@router.get(
    "/pipelines/cps/status/{task_id}",
    response_model=PipelineStatusResponse,
    summary="CPS / post_meeting / PRD 작업 상태 조회 (legacy 경로)",
)
@limiter.limit("60/minute")
async def cps_status(
    request: Request,
    task_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> PipelineStatusResponse:
    """
    arq Job 상태 조회. PR1 호환을 위해 `/cps/status/{task_id}` 경로 유지.
    PR3 에선 `/status/{task_id}` 에서도 동일하게 동작.

    ownership 검증 — Sprint 8 P0 (다른 사용자 task_id 우회 차단).
    """
    info = await get_job_status_for_user(task_id, current_user.email)
    return PipelineStatusResponse(**info)


@router.get(
    "/pipelines/status/{task_id}",
    response_model=PipelineStatusResponse,
    summary="모든 파이프라인 (CPS / post_meeting / PRD) 작업 상태 조회",
)
@limiter.limit("60/minute")
async def pipeline_status(
    request: Request,
    task_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> PipelineStatusResponse:
    # ownership 검증 후 status 반환 (Sprint 8 P0).
    info = await get_job_status_for_user(task_id, current_user.email)
    return PipelineStatusResponse(**info)
