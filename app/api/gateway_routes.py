"""
Gateway 도메인 라우트 — frontend 가 historically 사용한 wrapped 응답 형태.

[구조]
모든 라우트가 backend 의 repository / pipeline 함수를 직접 호출. 응답은
`ApiResponse({status, data: {result: ...}})` 로 통일.

[권장]
새 클라이언트는 가능하면 `/api/v2/*` 직접 호출 (Pydantic 타입화 + 정확한
응답 스키마). 이 라우터는 기존 프론트와의 호환 유지가 주 목적.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.api._quota_helpers import tracked_pipeline_context
from app.clients import neo4j_client
from app.core import quota
from app.core.limiter import limiter
from app.core.security import get_current_user
from app.pipelines.base import PipelineContext
from app.pipelines.create_md_pipeline import CreateMdInput, run_create_md_pipeline
from app.pipelines.cps_pipeline import CpsInput, run_cps_pipeline
from app.pipelines.delete_pipeline import (
    DeleteMeetingInput,
    delete_project,
    run_delete_meeting_pipeline,
)
from app.pipelines.design_pipeline import (
    DesignInput,
    DesignPipelineCancelled,
    DesignPrecheckFailed,
    run_design_pipeline,
)
from app.pipelines.fix_spec_pipeline import FixSpecInput, run_fix_spec_pipeline
from app.pipelines.lineage_pipeline import LineageInput, run_lineage_pipeline
from app.pipelines.lint_pipeline import LintInput, run_lint_pipeline
from app.pipelines.prd_pipeline import PrdInput, run_prd_pipeline
from app.pipelines.skill_recommend_pipeline import (
    CatalogEntry,
    RecommendInput,
    run_skill_recommend_pipeline,
)
from app.schemas import ApiResponse
from app.service import (
    lineage_repository,
    lint_repository,
    ownership_repository,
    query_repository,
    repo_repository,
    skill_repository,
)
from app.service.skill_repository import SkillInput
from app.service.user_repository import UserPublic

router = APIRouter(prefix="/gateway", tags=["Gateway Domain Routes"])


# ─── Helpers ────────────────────────────────────────────────────


def _ok(data: Any) -> ApiResponse:
    """공통 응답 wrapper: { status, data: { result: ... } }."""
    return ApiResponse(status="success", data={"result": data})


def _to_dict(x: Any) -> Any:
    """Pydantic 모델은 dump, 리스트는 각각 dump."""
    if x is None:
        return None
    if isinstance(x, list):
        return [getattr(i, "model_dump", lambda: i)() for i in x]
    if hasattr(x, "model_dump"):
        return x.model_dump()
    return x


# 공통 어댑터 — pipelines.base.Neo4jClientProxy (run_cypher + run_in_transaction 노출)
from app.pipelines.base import Neo4jClientProxy as _Neo4jProxy


# [2026-05-19] 미사용 _ctx() (raw GeminiClient) 제거. 모든 LLM 호출은
# tracked_pipeline_context 로 토큰 누적. LLM 미사용 라우트만 _ctx_no_llm 사용.


class _NullGemini:
    """LLM 안 쓰는 라우트용."""

    async def generate(self, *a, **kw):  # pragma: no cover
        raise RuntimeError("이 라우트는 Gemini 를 호출하지 않습니다.")


def _ctx_no_llm(user_email: str = "") -> PipelineContext:
    """delete/lineage 등 LLM 미사용 라우트용.

    [Phase 2D] user_email — delete_pipeline 의 _derive_ids /
    _DELETE_PROJECT_NODE_CYPHER 가 ctx.user_email 기반으로 멀티테넌시 격리.
    """
    return PipelineContext(
        gemini=_NullGemini(),
        neo4j=_Neo4jProxy(),
        idempotency_key="gateway",
        user_email=user_email or "",
    )


# ─── Ownership helpers ────────────────────────────────────────


async def _record_owner(user: UserPublic, project: Optional[str], team_id: Optional[str] = None) -> None:
    """
    CREATE 진입점에서 호출 — 신규 프로젝트 claim.
    team_id 지정 시 팀 프로젝트로 claim (멤버십 + 유료 플랜 게이트).
    다른 유저가 이미 소유한 이름이면 409 Conflict.
    """
    if not project:
        return
    try:
        await ownership_repository.claim(user.email, project, team_id)
    except ownership_repository.ProjectOwnershipConflict as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{e.project}' 는 이미 다른 사용자가 사용 중인 프로젝트 이름입니다. 다른 이름을 사용하세요.",
        ) from e


async def _assert_owner(user: UserPublic, project: Optional[str], team_id: Optional[str] = None) -> None:
    """read/access 진입점에서 호출 — owner / 팀 멤버 아니면 403."""
    await ownership_repository.assert_access(user.email, project or "", team_id)


# ===== 0) 임의 외부 webhook 프록시는 폐기됨 =====


@router.post("/proxy", response_model=ApiResponse)
async def arbitrary_proxy_deprecated(
    payload: Dict[str, Any] = Body(...),
    current_user: UserPublic = Depends(get_current_user),
) -> ApiResponse:
    """
    이전 빌드에는 외부 webhook 으로 임의 forward 하는 라우트가 있었으나 폐기됨.
    410 Gone 으로 명확히 차단.
    """
    raise HTTPException(
        status_code=410,
        detail="외부 webhook 프록시는 폐기되었습니다. 도메인별 라우트 또는 /api/v2/* 사용.",
    )


# ===== 1) Skill 도메인 =====


@router.post("/skills", response_model=ApiResponse)
async def post_skill(
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    """postSkill — bulk skill upsert. payload: { projectName, skills: [...] }"""
    project_name = payload.get("projectName") or payload.get("project_name") or "harness"
    await _record_owner(current_user, project_name, payload.get("team_id"))
    raw_skills = payload.get("skills") or []
    skills: List[SkillInput] = []
    for s in raw_skills:
        if not isinstance(s, dict) or not s.get("id") or not s.get("name"):
            continue
        skills.append(
            SkillInput(
                id=s["id"],
                name=s["name"],
                scope=s.get("scope", ""),
                priority=s.get("priority", "Medium"),
                trigger_condition=s.get("trigger_condition", ""),
                instructions=s.get("instructions") or [],
                tags=s.get("tags") or [],
            )
        )
    out = await skill_repository.create_skills(project_name, skills)
    return _ok(out)


@router.get("/skills", response_model=ApiResponse)
async def get_all_skills(
    projectName: str = Query(...), current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    items = await skill_repository.get_all_skills(projectName)
    return _ok(_to_dict(items))


@router.get("/skills/{skill_id}", response_model=ApiResponse)
async def get_skill(
    skill_id: str,
    projectName: str = Query(...),
    current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    out = await skill_repository.get_skill(projectName, skill_id)
    return _ok(_to_dict(out))


@router.delete("/skills", response_model=ApiResponse)
async def delete_skill(
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    project = payload.get("projectName") or payload.get("project_name") or "harness"
    await _assert_owner(current_user, project, payload.get("team_id"))
    skill_id = payload.get("id") or payload.get("skill_id")
    if not skill_id:
        raise HTTPException(status_code=422, detail="id 필수")
    ok = await skill_repository.delete_skill(project, skill_id)
    return _ok({"deleted": ok, "id": skill_id})


@router.post("/skills/recommend", response_model=ApiResponse)
async def recommend_skills(
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    project = payload.get("projectName") or payload.get("project_name") or "harness"
    await _assert_owner(current_user, project, payload.get("team_id"))
    await quota.assert_tokens_within_limit(current_user.email)
    catalog = payload.get("skillCatalog") or payload.get("skill_catalog") or []
    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key="gateway",
    ) as ctx:
        result = await run_skill_recommend_pipeline(
            ctx,
            RecommendInput(
                project_name=project,
                skill_catalog=[
                    CatalogEntry(
                        id=c.get("id", ""),
                        name=c.get("name", ""),
                        description=c.get("description", ""),
                        category=c.get("category", ""),
                    )
                    for c in catalog
                    if isinstance(c, dict)
                ],
                allowed_categories=payload.get("allowedCategories") or [],
            ),
        )
    return _ok(
        {
            "recommended": [
                {"id": r.id, "reason": r.reason, "confidence": r.confidence}
                for r in result.recommended
            ],
            "meta": result.meta,
        }
    )


@router.post("/skills/duplicate", response_model=ApiResponse)
async def get_duplicate_skills(
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    project = payload.get("projectName") or payload.get("project_name") or "harness"
    await _assert_owner(current_user, project, payload.get("team_id"))
    name = payload.get("newSkillName") or payload.get("name")
    if not name:
        raise HTTPException(status_code=422, detail="newSkillName 필수")
    out = await skill_repository.find_duplicate_skill(project, name)
    return _ok(out)


# ===== 2) Plan 도메인 =====


@router.get("/cps", response_model=ApiResponse)
async def get_cps(
    projectName: str = Query(...), current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    out = await query_repository.get_master_cps(projectName, team_id=team_id or "")
    return _ok(_to_dict(out))


@router.get("/prd", response_model=ApiResponse)
async def get_prd(
    projectName: str = Query(...), current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    out = await query_repository.get_master_prd(projectName, team_id=team_id or "")
    return _ok(_to_dict(out))


@router.get("/ddd", response_model=ApiResponse)
async def get_ddd(
    projectName: str = Query(...), current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    out = await query_repository.get_ddd_graph(projectName, team_id=team_id or "")
    return _ok(_to_dict(out))


@router.get("/architecture", response_model=ApiResponse)
async def get_architecture(
    projectName: str = Query(...), current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    out = await query_repository.get_architecture_graph(projectName, team_id=team_id or "")
    return _ok(_to_dict(out))


@router.get("/spack", response_model=ApiResponse)
async def get_spack(
    projectName: str = Query(...), current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    out = await query_repository.get_spack_graph(projectName, team_id=team_id or "")
    return _ok(_to_dict(out))


@router.post("/spack", response_model=ApiResponse)
@limiter.limit("3/minute")
async def create_spack(
    request: Request,
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    """createSpack — PRD 마스터 → Spack/DDD/Architecture (createDesign 의 별칭).

    Rate limit: IP 당 분당 3회. Design 3종 (Spack/DDD/Architecture) 동시 생성으로
    LLM 호출이 많아 비용 보호 + 중복 호출 차단.
    """
    project = payload.get("projectName") or payload.get("project_name") or "harness"
    team_id = payload.get("team_id") or ""
    await _assert_owner(current_user, project, payload.get("team_id"))
    await quota.assert_tokens_within_limit(current_user.email)
    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key="gateway",
        team_id=team_id,
    ) as ctx:
        try:
            result = await run_design_pipeline(
                ctx,
                DesignInput(project_name=project),
                # 클라이언트가 axios.abort 등으로 연결 끊으면 stage 사이마다 감지 →
                # 최종 commit 전에 빠져나가므로 기존 Spack/DDD/Architecture 보존.
                check_cancel=request.is_disconnected,
            )
        except DesignPipelineCancelled as cancelled_at:
            return _ok(
                {
                    "result": "cancelled",
                    "stage": str(cancelled_at) or "unknown",
                    "message": "client disconnected — previous design preserved",
                }
            )
        except DesignPrecheckFailed as precheck:
            # 누더기+거대 PRD fail-fast — 즉시 명확한 안내. 기존 설계 보존(트랜잭션 전).
            return _ok(
                {
                    "result": "precheck_failed",
                    "project_name": project,
                    "message": str(precheck),
                    "diagnostic": getattr(precheck, "diagnostic", {}),
                }
            )
    return _ok(
        {
            "project_name": result.project_name,
            "master_prd_id": result.master_prd_id,
            "spack": result.spack,
            "ddd": result.ddd,
            "architecture": result.architecture,
            "diagnostic": result.diagnostic,
        }
    )


# ===== 3) Meeting 도메인 =====


@router.post("/meetings", response_model=ApiResponse)
@limiter.limit("3/minute")
async def post_meeting(
    request: Request,
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    """
    postMeeting — CPS + PRD 체인 (v2 /pipelines/post_meeting 과 동일).

    흐름: CPS Agent → Save CPS → Code_CPS_Parser → PRD Agent1 → PRD Agent2 →
    Save PRD. 도메인 라우트에서도 동일 체인을 실행.

    Rate limit: IP 당 분당 3회. CPS+PRD 가 3~6분 걸리는 무거운 작업이라
    분당 3회는 동일 사용자가 충분히 반복할 수 없는 한도. 더블클릭/새로고침
    중복 호출 차단 + Gemini API 비용 보호.
    """
    project_name = (
        payload.get("project_name") or payload.get("projectName") or "harness"
    )
    team_id = payload.get("team_id") or ""
    await _record_owner(current_user, project_name, payload.get("team_id"))
    version = payload.get("version") or "v1"
    meeting_content = (
        payload.get("meeting_content") or payload.get("meetingContent") or ""
    )
    # 등급별 한도 체크 — 토큰(cheap) → 글자수 → 미팅 카운트(atomic +1) 순.
    # 한도 초과 시 HTTPException(402) 으로 즉시 차단 (LLM 비용 0).
    await quota.assert_tokens_within_limit(current_user.email)
    await quota.assert_summary_within_limit(current_user.email, meeting_content)
    await quota.acquire_meeting_quota(current_user.email)
    # quota 토큰 자동 누적 — CPS+PRD 두 단계 LLM 사용량을 한 ctx 로 합산해 적재.
    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key="gateway",
        team_id=team_id,
    ) as ctx:
        cps_result = await run_cps_pipeline(
            ctx,
            CpsInput(
                project_name=project_name,
                version=version,
                date=payload.get("date") or "",
                meeting_content=meeting_content,
                previous_cps_id=payload.get("previous_cps_id")
                or payload.get("previousCpsId"),
                team_id=team_id,
            ),
        )
        prd_result = await run_prd_pipeline(
            ctx,
            PrdInput(
                project_name=project_name,
                version=version,
                cps_graph=cps_result.cps_graph,
                previous_prd_id=payload.get("previous_prd_id")
                or payload.get("previousPrdId"),
                team_id=team_id,
                # [2026-06-04] CPS delta 가 비어도 PRD 가 회의록으로 생성되도록 raw fallback.
                meeting_content=meeting_content,
            ),
        )
    return _ok(
        {
            "cps": {
                "meeting_log_id": cps_result.meeting_log_id,
                "delta_cps_id": cps_result.delta_cps_id,
                "master_cps_id": cps_result.master_cps_id,
                "mode": cps_result.mode,
                # [2026-05-25] CPS Agent 추출 모드 — FE 가 사용자 안내 표시.
                "extraction_mode": cps_result.extraction_mode,
                "extraction_warning": cps_result.extraction_warning,
            },
            "prd": {
                "delta_prd_id": prd_result.delta_prd_id,
                "master_prd_id": prd_result.master_prd_id,
                "mode": prd_result.mode,
            },
        }
    )


@router.get("/meetings", response_model=ApiResponse)
async def get_meeting_logs(
    projectName: str = Query(...),
    version: str = Query(...),
    current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    out = await query_repository.get_meeting_log(projectName, version, team_id=team_id or "")
    return _ok(_to_dict(out))


@router.get("/meetings/versions", response_model=ApiResponse)
async def get_meeting_versions(
    projectName: str = Query(...), current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    items = await query_repository.get_meeting_versions(projectName, team_id=team_id or "")
    return _ok(_to_dict(items))


@router.delete("/meetings", response_model=ApiResponse)
async def delete_meeting(
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    project = payload.get("project_name") or payload.get("projectName") or "harness"
    team_id = payload.get("team_id") or ""
    await _assert_owner(current_user, project, payload.get("team_id"))
    version = payload.get("version") or ""
    if not version:
        raise HTTPException(status_code=422, detail="version 필수")
    # delete_pipeline 도 Master CPS/PRD rebuild 시 LLM 호출 — 가드 + tracked 필수.
    await quota.assert_tokens_within_limit(current_user.email)
    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key="gateway",
        team_id=team_id,
    ) as ctx:
        result = await run_delete_meeting_pipeline(
            ctx,
            DeleteMeetingInput(project_name=project, version=version, team_id=team_id),
        )
    return _ok(
        {
            "status": result.status,
            "message": result.message,
            "project_name": result.project_name,
            "deleted_version": result.deleted_version,
            "remaining_cps_count": result.remaining_cps_count,
            "remaining_prd_count": result.remaining_prd_count,
            "cps_master_rebuilt": result.cps_master_rebuilt,
            "prd_master_rebuilt": result.prd_master_rebuilt,
        }
    )


# ===== 4) Project / Repo / Lint / Lineage =====


@router.delete("/projects", response_model=ApiResponse)
async def delete_project_route(
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    project = payload.get("projectName") or payload.get("project_name")
    if not project:
        raise HTTPException(status_code=422, detail="projectName 필수")
    team_id = payload.get("team_id") or ""
    await _assert_owner(current_user, project, payload.get("team_id"))
    # [Phase 2D Security] 이전엔 body.get("email") 로 받았으나 (a) NameError —
    # body 변수 미정의, (b) tenant 사칭 시도 위험. 인증된 current_user.email 만 사용.
    out = await delete_project(
        _ctx_no_llm(user_email=current_user.email), project, team_id
    )
    return _ok(out)


@router.post("/projects/repos", response_model=ApiResponse)
async def add_project_repo(
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    project = payload.get("projectName") or payload.get("project_name") or "harness"
    await _record_owner(current_user, project, payload.get("team_id"))
    url = payload.get("url") or ""
    if not url:
        raise HTTPException(status_code=422, detail="url 필수")
    out = await repo_repository.add_repo(
        repo_repository.RepoIn(
            project_name=project,
            url=url,
            role=payload.get("role", "primary"),
            label=payload.get("label", ""),
            team_id=payload.get("team_id"),
        )
    )
    return _ok(_to_dict(out))


@router.get("/projects/repos", response_model=ApiResponse)
async def get_project_repos(
    projectName: str = Query(...), current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    repos = await repo_repository.get_repos(projectName, team_id=team_id or "")
    return _ok({"repos": _to_dict(repos), "count": len(repos)})


@router.delete("/projects/repos", response_model=ApiResponse)
async def delete_project_repo(
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    project = payload.get("projectName") or payload.get("project_name") or "harness"
    await _assert_owner(current_user, project, payload.get("team_id"))
    url = payload.get("url") or ""
    if not url:
        raise HTTPException(status_code=422, detail="url 필수")
    await repo_repository.delete_repo(project, url, team_id=payload.get("team_id") or "")
    return _ok({"ok": True, "project_name": project, "url": url})


@router.post("/lint/run", response_model=ApiResponse)
@limiter.limit("5/minute")
async def run_lint(
    request: Request,
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    """Lint 실행 — Rate limit IP 당 분당 5회 (LLM + GitHub API 호출)."""
    project = payload.get("projectName") or payload.get("project_name") or "harness"
    await _assert_owner(current_user, project, payload.get("team_id"))
    github_url = payload.get("githubUrl") or payload.get("github_url") or ""
    if not github_url:
        raise HTTPException(status_code=422, detail="githubUrl 필수")
    await quota.assert_tokens_within_limit(current_user.email)
    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key="gateway",
    ) as ctx:
        result = await run_lint_pipeline(
            ctx, LintInput(project_name=project, github_url=github_url)
        )
    return _ok(result.model_dump())


@router.get("/lint/last", response_model=ApiResponse)
async def get_last_lint(
    projectName: str = Query(...),
    githubUrl: str = Query(...),
    current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    out = await lint_repository.get_last_lint_result(projectName, githubUrl, team_id=team_id or "")
    return _ok(_to_dict(out))


@router.post("/lint/fix-spec", response_model=ApiResponse)
@limiter.limit("3/minute")
async def generate_fix_spec(
    request: Request,
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    """generateFixSpec — Lint 결과 → 수정 스펙 MD. Rate limit IP 당 분당 3회 (LLM 호출)."""
    project = payload.get("projectName") or payload.get("project_name") or "harness"
    await _assert_owner(current_user, project, payload.get("team_id"))
    github_url = payload.get("githubUrl") or payload.get("github_url") or ""
    lint_result = payload.get("lintResult") or payload.get("lint_result") or {}
    await quota.assert_tokens_within_limit(current_user.email)
    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key="gateway",
    ) as ctx:
        result = await run_fix_spec_pipeline(
            ctx,
            FixSpecInput(
                project_name=project, github_url=github_url, lint_result=lint_result
            ),
        )
    return _ok(
        {
            "success": result.success,
            "markdown": result.markdown,
            "filename": result.filename,
            "message": result.message,
            "metadata": result.metadata,
        }
    )


@router.post("/lineage/analyze", response_model=ApiResponse)
@limiter.limit("5/minute")
async def analyze_lineage(
    request: Request,
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    """Lineage 분석 — Rate limit IP 당 분당 5회 (deterministic 매칭, LLM 미사용이라 가벼움)."""
    project = payload.get("projectName") or payload.get("project_name") or "harness"
    await _assert_owner(current_user, project, payload.get("team_id"))
    result = await run_lineage_pipeline(
        _ctx_no_llm(), LineageInput(project_name=project)
    )
    return _ok(result.model_dump())


@router.get("/lineage/last", response_model=ApiResponse)
async def get_last_lineage(
    projectName: str = Query(...), current_user: UserPublic = Depends(get_current_user),
    team_id: Optional[str] = None,
) -> ApiResponse:
    await _assert_owner(current_user, projectName, team_id)
    out = await lineage_repository.get_last_lineage(projectName, team_id=team_id or "")
    return _ok(_to_dict(out))


# ===== 5) Doc 변환 =====


@router.post("/docs/markdown", response_model=ApiResponse)
@limiter.limit("3/minute")
async def create_markdown(
    request: Request,
    payload: Dict[str, Any], current_user: UserPublic = Depends(get_current_user)
) -> ApiResponse:
    """createMD — Spack/DDD/Architecture → MD 3종.

    Rate limit: IP 당 분당 3회. LLM 3회 병렬 호출.
    """
    project = payload.get("projectName") or payload.get("project_name") or "harness"
    await _assert_owner(current_user, project, payload.get("team_id"))
    await quota.assert_tokens_within_limit(current_user.email)
    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key="gateway",
    ) as ctx:
        result = await run_create_md_pipeline(ctx, CreateMdInput(project_name=project))
    return _ok(
        {
            "project_name": result.project_name,
            "spack_md": result.spack_md,
            "ddd_md": result.ddd_md,
            "arch_md": result.arch_md,
            "orchestrator_md": result.orchestrator_md,
            "checklist_md": result.checklist_md,
            "diagnostic": result.diagnostic,
        }
    )
