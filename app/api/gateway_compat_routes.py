"""
Gateway compat dispatcher — `/api/gateway/{action}` 단일 wildcard 라우트.

[배경]
초기 빌드에서 frontend 가 wildcard 형태로 다양한 action 을 호출하던 경로.
action 별 dispatch table 을 통해 repository / pipeline 함수로 라우팅.

[설계]
- 알려진 action → 핸들러 dispatch
- 미지원 action → 410 Gone
- 응답 shape: `{ "result": ... }` 형태로 wrap (frontend 호환)
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.api._quota_helpers import tracked_pipeline_context
from app.queue.client import enqueue_cps, enqueue_post_meeting, get_pool
from app.queue.status_guard import get_job_status_for_user
from app.clients.gemini_audio import transcribe_audio
from app.clients.gemini_client import GeminiError
from app.clients import neo4j_client
from app.core import quota
from app.core.meeting_validation import (
    MeetingContentTooShort,
    assert_meeting_content_substantial,
)
from app.core.limiter import limiter
from app.core.security import get_current_user
from app.pipelines.base import PipelineContext
from app.pipelines.create_md_pipeline import CreateMdInput, run_create_md_pipeline
from app.pipelines.cps_pipeline import CpsInput, run_cps_pipeline
from app.pipelines.prd_pipeline import PrdInput, run_prd_pipeline
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
from app.pipelines.skill_improve_pipeline import (
    SkillImproveInput,
    run_skill_improve_pipeline,
)
from app.service import (
    lineage_repository,
    lint_repository,
    ownership_repository,
    query_repository,
    repo_repository,
    skill_repository,
    usage_repository,
    user_repository,
)
from app.service.skill_repository import SkillInput
from app.service.user_repository import UserPublic

logger = logging.getLogger("gateway_compat")

router = APIRouter(
    prefix="/api/gateway",
    tags=["Gateway compat (/api/gateway/*)"],
    dependencies=[Depends(get_current_user)],
)


# 공통 어댑터 — pipelines.base.Neo4jClientProxy (run_cypher + run_in_transaction 노출)
from app.pipelines.base import Neo4jClientProxy as _Neo4jProxy


class _NullGemini:
    async def generate(self, *a, **kw):  # pragma: no cover
        raise RuntimeError("이 dispatch 경로는 Gemini 를 호출하지 않습니다.")


# [2026-05-19] use_llm=True path 가 실제로 한 번도 안 호출됨 (모든 LLM 은
# tracked_pipeline_context 로 wrap). use_llm 인자 제거 — 항상 NullGemini.
def _ctx() -> PipelineContext:
    return PipelineContext(
        gemini=_NullGemini(),
        neo4j=_Neo4jProxy(),
        idempotency_key="gateway-compat",
    )


def _dump(x: Any) -> Any:
    if x is None:
        return None
    if isinstance(x, list):
        return [getattr(i, "model_dump", lambda: i)() for i in x]
    if hasattr(x, "model_dump"):
        return x.model_dump()
    return x


def _wrap(data: Any) -> Dict[str, Any]:
    """공통 응답 wrapper: { "result": ... }."""
    return {"result": _dump(data)}


# ─── 개별 handler ───────────────────────────────────────────────


async def _h_post_meeting(
    body: Dict[str, Any], _q: Dict[str, str], *, user_email: str
) -> Dict[str, Any]:
    """
    postMeeting — CPS + PRD 체인 비동기 큐잉.

    [2026-05] 동기 실행 → 비동기 전환.
    이전엔 LLM 호출 2회(CPS+PRD)를 동기로 기다려 응답까지 1~4분 소요. Cloudflare
    프록시 ~100s timeout 으로 배치 처리(V2/V3)에서 빈번히 실패. arq 큐에 enqueue
    하고 즉시 task_id 반환 → 클라이언트가 폴링(`GET /api/gateway/getJobStatus`).

    응답 shape:
      { result: { status: 'accepted', task_id: '...' } }

    [quota]
    dispatcher 가 진입 전 토큰/글자수/미팅 카운트 가드 호출 (LLM_HANDLERS_MEETING).
    여기서는 enqueue 만 수행 — 실제 LLM 호출은 worker 에서 일어나고 worker 가
    내부적으로 tracked_pipeline_context 로 토큰 누적 (v2 /pipelines/post_meeting
    과 동일).
    """
    project = body.get("project_name") or body.get("projectName") or "harness"
    version = body.get("version") or "v1"
    task_id = uuid.uuid4().hex

    # [2026-05-18 Phase 1 동시접속] (project, version) 사전 체크.
    # PC + 모바일 동시 접속 시 같은 v1.1 두 번 저장 → log_id 충돌 + LLM 2배.
    # quota 차감 / arq enqueue 전 차단해 사용자 손해 방지.
    if await query_repository.meeting_log_exists(project, version, team_id=_extract_team_id(body, _q) or ""):
        raise HTTPException(
            status_code=409,
            detail=(
                f"이미 {version} 미팅 로그가 존재합니다 — 다른 디바이스에서 먼저 "
                f"저장됐을 수 있습니다. 새로고침 후 다시 확인해주세요."
            ),
        )

    try:
        # [batch 파이프라이닝] FE 가 다음 항목을 함께 보내면 selectively 선반입(prefetch).
        # 단일 업로드/Notion 등 next_meeting 없는 경로는 None → 선반입 없음 (기존 동작).
        next_meeting = body.get("next_meeting") or body.get("nextMeeting")
        if not isinstance(next_meeting, dict):
            next_meeting = None

        await enqueue_post_meeting(
            task_id=task_id,
            project_name=project,
            version=version,
            date=body.get("date") or "",
            meeting_content=body.get("meeting_content")
            or body.get("meetingContent")
            or "",
            previous_cps_id=body.get("previous_cps_id") or body.get("previousCpsId"),
            previous_prd_id=body.get("previous_prd_id") or body.get("previousPrdId"),
            user_email=user_email,
            next_meeting=next_meeting,
            team_id=_extract_team_id(body, _q) or "",
        )
    except HTTPException:
        raise  # [2026-06] 동시성 429 등 의도된 HTTP 에러는 503 으로 가리지 말고 그대로 전파
    except Exception as e:  # noqa: BLE001
        logger.exception("enqueue post_meeting failed (task=%s)", task_id)
        raise HTTPException(
            status_code=503,
            detail=f"queue unavailable: {e}",
        ) from e

    return _wrap({"status": "accepted", "task_id": task_id})


async def _h_create_cps(
    body: Dict[str, Any], _q: Dict[str, str], *, user_email: str
) -> Dict[str, Any]:
    """
    cps — CPS 단독 비동기 큐잉. PRD 는 만들지 않음.

    [배경 — 2026-05-18]
    frontend 의 검수 모드 (auto_progress=false) 가 사용. autoProgress=true 면
    `/postMeeting` 으로 CPS+PRD 체인을 한 번에 돌리지만, 검수 모드는 CPS 만 먼저
    만들고 사용자가 검토한 뒤 명시적으로 PRD 트리거 (별도 `/createPRD` 액션).
    이전엔 dispatch table 에 `cps` 핸들러가 빠져 있어 검수 모드 사용자가
    "Request failed with status code 410" 으로 막혔던 버그를 수정.

    응답 shape (postMeeting 과 동일):
      { result: { status: 'accepted', task_id: '...' } }
    """
    project = body.get("project_name") or body.get("projectName") or "harness"
    version = body.get("version") or "v1"
    task_id = uuid.uuid4().hex

    # [2026-05-18 Phase 1 동시접속] (project, version) 사전 체크 — _h_post_meeting 와 동일 정책.
    if await query_repository.meeting_log_exists(project, version, team_id=_extract_team_id(body, _q) or ""):
        raise HTTPException(
            status_code=409,
            detail=(
                f"이미 {version} 미팅 로그가 존재합니다 — 다른 디바이스에서 먼저 "
                f"저장됐을 수 있습니다. 새로고침 후 다시 확인해주세요."
            ),
        )

    try:
        await enqueue_cps(
            task_id=task_id,
            project_name=project,
            version=version,
            date=body.get("date") or "",
            meeting_content=body.get("meeting_content")
            or body.get("meetingContent")
            or "",
            previous_cps_id=body.get("previous_cps_id") or body.get("previousCpsId"),
            user_email=user_email,
            team_id=_extract_team_id(body, _q) or "",
        )
    except HTTPException:
        raise  # [2026-06] 동시성 429 등 의도된 HTTP 에러는 503 으로 가리지 말고 그대로 전파
    except Exception as e:  # noqa: BLE001
        logger.exception("enqueue cps failed (task=%s)", task_id)
        raise HTTPException(
            status_code=503,
            detail=f"queue unavailable: {e}",
        ) from e

    return _wrap({"status": "accepted", "task_id": task_id})


# ─── getJobStatus — 비동기 작업 폴링용 ───────────────────────────
async def _h_get_job_status(
    _b: Dict[str, Any], q: Dict[str, str], *, user_email: str
) -> Dict[str, Any]:
    """
    arq Job status 조회 — postMeeting 비동기 전환 (2026-05) 의 폴링 엔드포인트.

    [멀티테넌트 격리]
    status_guard.get_job_status_for_user 가 task_id → kwargs.project_name 회수 →
    ownership_repository.assert_owns 로 검증. 다른 사용자 task_id 조회 시 403.
    task_id 자체가 못 찾으면 404 (정보 누설 방지).

    응답 shape (PipelineStatusResponse 와 동일):
      {
        result: {
          task_id, project_name, status: 'queued' | 'in_progress' | 'complete' | ...,
          result: {...} | null,
          error: str | null,
          enqueue_time, finish_time
        }
      }
    """
    task_id = q.get("task_id") or q.get("taskId") or ""
    if not task_id:
        raise HTTPException(status_code=422, detail="task_id 필수")
    info = await get_job_status_for_user(task_id, user_email)
    return _wrap(info)


# ─── getProjectBusy — 다른 기기/탭의 진행 중 작업 감지 (2026-06) ──────
async def _h_get_project_busy(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    """프로젝트에 inflight master 쓰기 잡(post_meeting/cps/prd)이 있는지 조회.

    [용도] 멀티디바이스 이중작업 — 배치는 FE 주도라 다른 기기는 진행 상태를 모름.
    FE 가 plan 페이지 진입/포커스 시 이걸로 "다른 기기에서 처리 중" 배너 + 버튼
    비활성. 실제 차단은 enqueue 의 409 PROJECT_BUSY 게이트가 담당 — 이건 표시용.
    """
    from app.core import concurrency
    from app.core.project_scope import scoped_project

    project = q.get("projectName") or q.get("project_name") or ""
    if not project:
        raise HTTPException(status_code=422, detail="projectName 필수")
    project_key = scoped_project(project, _extract_team_id({}, q) or None)
    pool = await get_pool()
    busy = await concurrency.is_project_busy(pool, project_key)
    return _wrap({"project_name": project, "busy": busy})


# ─── cancelJob — 진행 중 작업 중지 (Redis cancel flag) ───────────────
async def _h_cancel_job(
    _b: Dict[str, Any], q: Dict[str, str], *, user_email: str
) -> Dict[str, Any]:
    """
    진행 중인 비동기 작업 중지 요청 — Redis 에 job_cancel:{task_id} flag 를 set.

    [2026-05-27] 비동기 큐 전환 후 design 중지가 worker 로 전달 안 되던 버그 수정.
    worker(design_pipeline_job)가 stage 사이마다 이 flag 를 확인해 graceful 종료
    (최종 Neo4j 트랜잭션 전 bail → 기존 SPACK/DDD/Architecture 데이터 보존).

    [멀티테넌트 격리] getJobStatus 와 동일하게 get_job_status_for_user 로 task_id →
    project_name 회수 → ownership 검증 (못 찾으면 404, 타인 task 면 403).
    """
    task_id = q.get("task_id") or q.get("taskId") or ""
    if not task_id:
        raise HTTPException(status_code=422, detail="task_id 필수")
    # 권한 확인 — 본인 task 가 아니면 여기서 403/404.
    await get_job_status_for_user(task_id, user_email)
    pool = await get_pool()
    # TTL 15분 — worker job_timeout(300s) 보다 길게 잡아 중지 신호가 살아있게.
    await pool.set(f"job_cancel:{task_id}", "1", ex=900)
    return _wrap({"task_id": task_id, "status": "cancel_requested"})


def _as_array(out: Any) -> list:
    """단일 dict → [dict] 로 wrap. None → []. frontend 가 result 를 list 로 받기 위함."""
    if out is None:
        return []
    dumped = _dump(out)
    return [dumped] if isinstance(dumped, dict) else (dumped if isinstance(dumped, list) else [dumped])


async def _h_get_cps(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    out = await query_repository.get_master_cps(q.get("projectName", ""), team_id=_extract_team_id({}, q) or "")
    return {"result": _as_array(out)}


async def _h_get_prd(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    out = await query_repository.get_master_prd(q.get("projectName", ""), team_id=_extract_team_id({}, q) or "")
    return {"result": _as_array(out)}


async def _h_get_ddd(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    out = await query_repository.get_ddd_graph(q.get("projectName", ""), team_id=_extract_team_id({}, q) or "")
    return {"result": _as_array(out)}


async def _h_get_spack(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    out = await query_repository.get_spack_graph(q.get("projectName", ""), team_id=_extract_team_id({}, q) or "")
    return {"result": _as_array(out)}


async def _h_get_architecture(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    out = await query_repository.get_architecture_graph(q.get("projectName", ""), team_id=_extract_team_id({}, q) or "")
    return {"result": _as_array(out)}


async def _h_get_meeting_logs(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    out = await query_repository.get_meeting_log(
        q.get("projectName", ""), q.get("version", ""), team_id=_extract_team_id({}, q) or ""
    )
    return {"result": _as_array(out)}


async def _h_get_meeting_versions(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    items = await query_repository.get_meeting_versions(q.get("projectName", ""), team_id=_extract_team_id({}, q) or "")
    return {"result": _dump(items) or []}


async def _h_get_project_timeline(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    """Deliverables Hero strip — last N일 이벤트. frontend 는 top-level shape 기대."""
    project = q.get("projectName") or ""
    try:
        days = max(1, min(90, int(q.get("days") or 7)))
    except ValueError:
        days = 7
    try:
        limit = max(1, min(100, int(q.get("limit") or 30)))
    except ValueError:
        limit = 30
    import time
    since_ms = int(time.time() * 1000) - days * 24 * 60 * 60 * 1000
    out = await query_repository.get_project_timeline(
        project, since_ms=since_ms, limit=limit, team_id=_extract_team_id({}, q) or ""
    )
    return out.model_dump()


async def _h_delete_meeting(
    body: Dict[str, Any], _q: Dict[str, str], *, user_email: str
) -> Dict[str, Any]:
    project = body.get("project_name") or body.get("projectName") or "harness"
    version = body.get("version") or ""
    team_id = _extract_team_id(body, _q) or ""
    if not version:
        raise HTTPException(status_code=422, detail="version 필수")
    # delete_pipeline 도 Master rebuild 시 LLM 호출 — tracked 적재 필요.
    # [2026-06 감사 G1] delete 의 master rebuild 가 다른 기기의 merge 잡과 동시
    # 실행되면 lost update — FE 배치 pre-cleanup 이 정확히 이 경로를 호출한다.
    # 워커 잡들과 같은 프로젝트 락으로 직렬화. sync HTTP 라 대기는 15s 로 짧게,
    # 초과 시 409 PROJECT_BUSY (enqueue 게이트와 같은 코드 — FE 분기 재사용).
    from app.core.master_lock import MasterLockTimeout, master_write_lock
    from app.core.project_scope import scoped_project

    # Redis 불가 시 pool=None → master_write_lock 이 fail-open (잠금 없이 진행).
    # 락 인프라 장애가 delete 자체를 막으면 안 됨 — concurrency.py 와 동일 철학.
    try:
        pool = await get_pool()
    except Exception:  # noqa: BLE001
        pool = None
    try:
        async with master_write_lock(
            pool, scoped_project(project, team_id or None), f"sync-delete-{version}",
            wait_timeout=15.0,
        ):
            async with tracked_pipeline_context(
                user_email=user_email, idempotency_key="gateway-compat",
                team_id=team_id,
            ) as ctx:
                result = await run_delete_meeting_pipeline(
                    ctx, DeleteMeetingInput(project_name=project, version=version, team_id=team_id)
                )
    except MasterLockTimeout:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PROJECT_BUSY",
                "message": "이 프로젝트는 다른 기기 또는 탭에서 처리 중이에요. "
                           "진행 중인 작업이 끝난 뒤 다시 시도해 주세요.",
                "project_name": project,
            },
        )
    return _wrap({
        "status": result.status,
        "message": result.message,
        "project_name": result.project_name,
        "deleted_version": result.deleted_version,
        "remaining_cps_count": result.remaining_cps_count,
        "remaining_prd_count": result.remaining_prd_count,
        "cps_master_rebuilt": result.cps_master_rebuilt,
        "prd_master_rebuilt": result.prd_master_rebuilt,
    })


async def _h_delete_project(
    body: Dict[str, Any], _q: Dict[str, str], *, user_email: str
) -> Dict[str, Any]:
    """[Phase 2D] user_email — Project 노드 매칭에 사용. dispatcher 가 인증된
    current_user.email 를 주입. body.email 절대 신뢰 X (tenant 사칭 위험)."""
    project = body.get("projectName") or body.get("project_name")
    if not project:
        raise HTTPException(status_code=422, detail="projectName 필수")
    team_id = _extract_team_id(body, _q) or ""
    ctx = PipelineContext(
        gemini=_NullGemini(),
        neo4j=_Neo4jProxy(),
        idempotency_key="gateway-compat-delete",
        user_email=user_email,
        team_id=team_id,
    )
    out = await delete_project(ctx, project, team_id)
    return _wrap(out)


async def _h_post_skill(body: Dict[str, Any], _q: Dict[str, str]) -> Dict[str, Any]:
    project = body.get("projectName") or body.get("project_name") or "harness"
    raw_skills = body.get("skills") or []
    skills = []
    for s in raw_skills:
        if not isinstance(s, dict) or not s.get("id") or not s.get("name"):
            continue
        skills.append(SkillInput(
            id=s["id"], name=s["name"],
            scope=s.get("scope", ""), priority=s.get("priority", "Medium"),
            trigger_condition=s.get("trigger_condition", ""),
            instructions=s.get("instructions") or [],
            tags=s.get("tags") or [],
        ))
    out = await skill_repository.create_skills(project, skills)
    return _wrap(out)


async def _h_get_skill(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    skill_id = q.get("id") or ""
    project = q.get("projectName") or "harness"
    if not skill_id:
        raise HTTPException(status_code=422, detail="id 필수")
    out = await skill_repository.get_skill(project, skill_id)
    return _wrap(out)


async def _h_get_all_skill(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    items = await skill_repository.get_all_skills(q.get("projectName", "harness"))
    return _wrap(items)


async def _h_get_all_skill_detail(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    # 규칙 본문(instructions)·trigger 까지 포함 — 바이브 zip 의 skills/*.md 생성용.
    items = await skill_repository.get_all_skills_full(q.get("projectName", "harness"))
    return _wrap(items)


async def _h_delete_skill(body: Dict[str, Any], _q: Dict[str, str]) -> Dict[str, Any]:
    project = body.get("projectName") or body.get("project_name") or "harness"
    skill_id = body.get("id")
    if not skill_id:
        raise HTTPException(status_code=422, detail="id 필수")
    ok = await skill_repository.delete_skill(project, skill_id)
    return _wrap({"deleted": ok, "id": skill_id})


async def _h_get_duplicate_skill(body: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    """
    frontend(RuleGeneratorTab)는 GET `/getDuplicateSkill?projectName=X&newSkillId=Y` 로 호출.
    body 가 비어있으므로 query 에서도 읽어야 함.

    응답 shape: camelCase `{isDuplicate, existingIds}` (frontend 가 `.isDuplicate` 체크).
    프론트가 보내는 키는 `newSkillId` (= 스킬 ID 중복 여부).
    """
    body = body or {}
    project = (
        body.get("projectName") or body.get("project_name")
        or q.get("projectName") or q.get("project_name") or "harness"
    )
    skill_id = (
        body.get("newSkillId") or body.get("skillId") or body.get("id")
        or q.get("newSkillId") or q.get("skillId") or q.get("id")
    )
    skill_name = (
        body.get("newSkillName") or body.get("name")
        or q.get("newSkillName") or q.get("name")
    )

    if skill_id:
        out = await skill_repository.find_duplicate_skill_by_id(project, skill_id)
    else:
        out = await skill_repository.find_duplicate_skill(project, skill_name or "")

    # camelCase 변환 (frontend 호환)
    return {
        "isDuplicate": bool(out.get("is_duplicate")),
        "existingIds": list(out.get("existing_ids") or []),
    }


async def _h_recommend_skills(
    body: Dict[str, Any], _q: Dict[str, str], *, user_email: str
) -> Dict[str, Any]:
    project = body.get("projectName") or body.get("project_name") or "harness"
    catalog = body.get("skillCatalog") or body.get("skill_catalog") or []
    async with tracked_pipeline_context(
        user_email=user_email, idempotency_key="gateway-compat",
    ) as ctx:
        result = await run_skill_recommend_pipeline(
            ctx,
            RecommendInput(
                project_name=project,
                skill_catalog=[
                    CatalogEntry(
                        id=c.get("id", ""), name=c.get("name", ""),
                        description=c.get("description", ""), category=c.get("category", ""),
                    )
                    for c in catalog if isinstance(c, dict)
                ],
                allowed_categories=body.get("allowedCategories") or [],
            ),
        )
    return _wrap({
        "recommended": [
            {"id": r.id, "reason": r.reason, "confidence": r.confidence}
            for r in result.recommended
        ],
        "meta": result.meta,
    })


async def _h_improve_skill(
    body: Dict[str, Any], _q: Dict[str, str], *, user_email: str
) -> Dict[str, Any]:
    """improveSkill — 편집 중인 규칙 1개의 초안을 AI 가 구체적 규칙으로 다듬기 (단건, 동기)."""
    async with tracked_pipeline_context(
        user_email=user_email, idempotency_key="gateway-compat",
    ) as ctx:
        result = await run_skill_improve_pipeline(
            ctx,
            SkillImproveInput(
                name=body.get("name", ""),
                scope=body.get("scope", ""),
                trigger_condition=(
                    body.get("trigger_condition") or body.get("triggerCondition") or ""
                ),
                instructions=body.get("instructions") or [],
                tags=body.get("tags") or [],
            ),
        )
    return _wrap({
        "improved": result.improved,
        "name": result.name,
        "scope": result.scope,
        "trigger_condition": result.trigger_condition,
        "instructions": result.instructions,
        "explanation": result.explanation,
        "meta": result.meta,
    })


async def _h_create_prd(
    body: Dict[str, Any], q: Dict[str, str], *, user_email: str,
    request: Optional[Request] = None,
) -> Dict[str, Any]:
    """
    createPRD — 검수 모드의 [PRD 생성] + prd.mode='error' 강등 후 재생성 경로 (P0-6).

    [배경] FE(plan.vue)는 CPS 검토 후 POST /createPRD {project_name, version} 을
    호출하지만 dispatch 에 핸들러가 없어 검수 모드가 다음 단계로 갈 수 없었다
    (과거 `cps` 핸들러 누락 410 버그와 동일 패턴). 저장된 해당 버전 delta CPS 의
    full_markdown 으로 CPS_Document 1-노드 graph 를 합성해 PRD 파이프라인을 단독
    실행한다 (postMeeting 의 PRD 단계와 동일 코드 경로 — MERGE 기반이라 재실행 멱등).
    """
    project = (
        (body or {}).get("project_name") or (body or {}).get("projectName")
        or q.get("project_name") or q.get("projectName")
    )
    version = (body or {}).get("version") or q.get("version")
    if not project or not version:
        raise HTTPException(status_code=400, detail="project_name 과 version 이 필요합니다.")
    team_id = _extract_team_id(body, q) or ""

    # LLM 2~3회 호출 경로 — postMeeting 과 동일하게 진입점 토큰 가드.
    await quota.assert_tokens_within_limit(user_email)

    md = await query_repository.get_cps_delta_markdown(project, version, team_id=team_id)
    if not md:
        raise HTTPException(
            status_code=404,
            detail=f"{version} 의 CPS 가 없습니다 — 회의록을 먼저 저장해 CPS 를 생성해주세요.",
        )

    cps_graph = {
        "nodes": [{
            "id": f"doc_cps_{project}_{str(version).replace('.', '_')}",
            "label": "CPS_Document",
            "properties": {
                "project": project, "version": version,
                "is_latest": True, "full_markdown": md,
            },
        }],
        "relationships": [],
    }
    async with tracked_pipeline_context(
        user_email=user_email, idempotency_key=f"create-prd-{version}", team_id=team_id,
    ) as ctx:
        try:
            result = await run_prd_pipeline(
                ctx,
                PrdInput(
                    project_name=project, version=str(version),
                    cps_graph=cps_graph, team_id=team_id,
                ),
            )
        except (ValueError, RuntimeError) as e:
            # 결정적 실패(orphan 잔재·빈 병합 등) — 사용자에게 원인 그대로 (actionable).
            raise HTTPException(status_code=422, detail=str(e))
    return {
        "result": {
            "status": "success",
            "delta_prd_id": result.delta_prd_id,
            "master_prd_id": result.master_prd_id,
            "mode": result.mode,
            "diagnostic": result.diagnostic,
        }
    }


async def _h_create_design(
    body: Dict[str, Any], q: Dict[str, str], *, user_email: str,
    request: Optional[Request] = None,
) -> Dict[str, Any]:
    """
    createDesign (action 명 `createSpack` 으로 노출 — 역사적 alias).

    frontend(design.vue)는 POST `/createSpack?projectName=X` 를 body=null 로 호출 →
    body 에 projectName 이 없으므로 query 에서도 읽어야 함.
    응답 shape: `{result: "success"}` (frontend 가 `response.data.result === 'success'` 체크).

    [중지 지원 — 2026-05-18]
    request.is_disconnected 를 pipeline 에 전달 → stage 사이마다 감지 →
    최종 Neo4j commit 전에 종료. 기존 Spack/DDD/Architecture 데이터 보존.
    응답 shape: `{result: "cancelled"}`. 단, abort 한 클라이언트는 이미 연결을
    끊었으므로 응답을 받지 못한다 — 로그/디버깅용.
    """
    project = (
        (body or {}).get("projectName")
        or (body or {}).get("project_name")
        or q.get("projectName")
        or q.get("project_name")
        or "harness"
    )
    team_id = _extract_team_id(body, q) or ""
    check_cancel = request.is_disconnected if request is not None else None
    # [2026-06 멀티디바이스] FE 는 2026-05-26 부터 비동기 큐(/api/v2/pipelines/design,
    # 게이트+락 적용)를 쓰지만, 이 legacy sync 경로는 구버전 번들/외부 호출이 여전히
    # 도달 가능 — 락 없이 설계 그래프를 Wipe-and-Redraw 해 워커 design 잡과 겹치면
    # stage 혼합. sync delete(감사 G1)와 동일하게 짧은 대기 후 409.
    from app.core.master_lock import MasterLockTimeout, master_write_lock
    from app.core.project_scope import scoped_project

    # Redis 불가 시 pool=None → master_write_lock 이 fail-open (잠금 없이 진행).
    # 락 인프라 장애가 design 생성 자체를 막으면 안 됨 — concurrency.py 와 동일 철학.
    try:
        pool = await get_pool()
    except Exception:  # noqa: BLE001
        pool = None
    try:
        async with master_write_lock(
            pool, scoped_project(project, team_id or None),
            f"sync-design-{uuid.uuid4().hex[:8]}", wait_timeout=15.0,
        ):
            async with tracked_pipeline_context(
                user_email=user_email, idempotency_key="gateway-compat",
                team_id=team_id,
            ) as ctx:
                try:
                    result = await run_design_pipeline(
                        ctx, DesignInput(project_name=project),
                        check_cancel=check_cancel,
                    )
                except DesignPipelineCancelled as cancelled_at:
                    return {
                        "result": "cancelled",
                        "project_name": project,
                        "stage": str(cancelled_at) or "unknown",
                        "message": "client disconnected — previous design preserved",
                    }
                except DesignPrecheckFailed as precheck:
                    # 누더기+거대 PRD fail-fast — 즉시 명확한 안내. 기존 설계 보존(트랜잭션 전).
                    return {
                        "result": "precheck_failed",
                        "project_name": project,
                        "message": str(precheck),
                        "diagnostic": getattr(precheck, "diagnostic", {}),
                    }
    except MasterLockTimeout:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PROJECT_BUSY",
                "message": "이 프로젝트는 다른 기기 또는 탭에서 처리 중이에요. "
                           "진행 중인 작업이 끝난 뒤 다시 시도해 주세요.",
                "project_name": project,
            },
        )
    return {
        "result": "success",
        "project_name": result.project_name,
        "master_prd_id": result.master_prd_id,
        "spack": _dump(result.spack), "ddd": _dump(result.ddd),
        "architecture": _dump(result.architecture),
        # [2026-05] top-level health — FE 가 cross-stage 위반 즉시 표시.
        "health": result.health,
        "diagnostic": result.diagnostic,
    }


async def _h_create_md(
    body: Dict[str, Any], q: Dict[str, str], *, user_email: str
) -> Dict[str, Any]:
    """
    frontend(ArchitectureTab)는 GET `/createMD?projectName=X` 로 호출.
    body 가 비어있으므로 query 에서 projectName 을 읽어야 함.
    응답 shape: flat `{spack_md, ddd_md, arch_md, project_name}` (wrap 없음).
    """
    project = (
        (body or {}).get("projectName")
        or (body or {}).get("project_name")
        or q.get("projectName")
        or q.get("project_name")
        or "harness"
    )
    async with tracked_pipeline_context(
        user_email=user_email, idempotency_key="gateway-compat",
    ) as ctx:
        result = await run_create_md_pipeline(ctx, CreateMdInput(project_name=project))
    return {
        "project_name": result.project_name,
        "spack_md": result.spack_md,
        "ddd_md": result.ddd_md,
        "arch_md": result.arch_md,
        "orchestrator_md": result.orchestrator_md,
        "checklist_md": result.checklist_md,
        "diagnostic": result.diagnostic,
    }


async def _h_add_project_repo(body: Dict[str, Any], _q: Dict[str, str]) -> Dict[str, Any]:
    project = body.get("projectName") or body.get("project_name") or "harness"
    url = body.get("url") or ""
    if not url:
        raise HTTPException(status_code=422, detail="url 필수")
    out = await repo_repository.add_repo(
        repo_repository.RepoIn(
            project_name=project, url=url,
            role=body.get("role", "primary"), label=body.get("label", ""),
            team_id=body.get("team_id"),
        )
    )
    return _wrap(out)


async def _h_get_project_repos(body: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    """getProjectRepos — frontend 는 response.data.repos 를 직접 읽음 (wrap 없음)."""
    project = (body or {}).get("projectName") or q.get("projectName") or "harness"
    repos = await repo_repository.get_repos(project, team_id=_extract_team_id(body, q) or "")
    return {"repos": _dump(repos), "count": len(repos)}


async def _h_delete_project_repo(body: Dict[str, Any], _q: Dict[str, str]) -> Dict[str, Any]:
    project = body.get("projectName") or body.get("project_name") or "harness"
    url = body.get("url") or ""
    if not url:
        raise HTTPException(status_code=422, detail="url 필수")
    await repo_repository.delete_repo(project, url, team_id=body.get("team_id") or "")
    return _wrap({"ok": True, "project_name": project, "url": url})


async def _h_run_lint(
    body: Dict[str, Any],
    _q: Dict[str, str],
    *,
    user_token: str | None = None,
    user_email: str,
) -> Dict[str, Any]:
    """user_token: 사용자 OAuth access_token. dispatcher 가 주입.
    private repo / rate-limit 5000/hr 접근에 필요. None 이면 anonymous → public repo 만.
    user_email: quota 토큰 누적용."""
    project = body.get("projectName") or body.get("project_name") or "harness"
    github_url = body.get("githubUrl") or body.get("github_url") or ""
    if not github_url:
        raise HTTPException(status_code=422, detail="githubUrl 필수")
    async with tracked_pipeline_context(
        user_email=user_email, idempotency_key="gateway-compat",
    ) as ctx:
        result = await run_lint_pipeline(
            ctx,
            LintInput(project_name=project, github_url=github_url),
            user_token=user_token,
        )
    return _wrap(result.model_dump())


async def _h_get_last_lint(_b: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    """frontend 는 {found, result, savedAt} top-level shape 를 기대."""
    out = await lint_repository.get_last_lint_result(
        q.get("projectName", ""), q.get("githubUrl", ""), team_id=_extract_team_id({}, q) or ""
    )
    if out is None:
        return {"found": False, "result": None, "savedAt": None}
    dumped = _dump(out)
    saved_at = dumped.get("saved_at") if isinstance(dumped, dict) else None
    return {"found": True, "result": dumped, "savedAt": saved_at}


async def _h_generate_fix_spec(
    body: Dict[str, Any], _q: Dict[str, str], *, user_email: str
) -> Dict[str, Any]:
    project = body.get("projectName") or body.get("project_name") or "harness"
    github_url = body.get("githubUrl") or body.get("github_url") or ""
    lint_result = body.get("lintResult") or body.get("lint_result") or {}
    async with tracked_pipeline_context(
        user_email=user_email, idempotency_key="gateway-compat",
    ) as ctx:
        result = await run_fix_spec_pipeline(
            ctx,
            FixSpecInput(project_name=project, github_url=github_url, lint_result=lint_result),
        )
    return _wrap({
        "success": result.success, "markdown": result.markdown,
        "filename": result.filename, "message": result.message, "metadata": result.metadata,
    })


async def _h_analyze_lineage(
    body: Dict[str, Any], _q: Dict[str, str], *, user_token: str | None = None
) -> Dict[str, Any]:
    """user_token: 사용자 OAuth access_token — private repo tree fetch 용."""
    project = body.get("projectName") or body.get("project_name") or "harness"
    result = await run_lineage_pipeline(
        _ctx(),
        LineageInput(project_name=project, team_id=_extract_team_id(body, _q) or ""),
        user_token=user_token,
    )
    return _wrap(result.model_dump())


async def _h_get_last_lineage(body: Dict[str, Any], q: Dict[str, str]) -> Dict[str, Any]:
    """frontend 는 {found, result, savedAt} top-level shape 를 기대."""
    project = (body or {}).get("projectName") or q.get("projectName") or "harness"
    out = await lineage_repository.get_last_lineage(project, team_id=_extract_team_id(body, q) or "")
    if out is None:
        return {"found": False, "result": None, "savedAt": None}
    dumped = _dump(out)
    saved_at = dumped.get("saved_at") if isinstance(dumped, dict) else None
    return {"found": True, "result": dumped, "savedAt": saved_at}


async def _h_setup_user_constraints(_b: Dict[str, Any], _q: Dict[str, str]) -> Dict[str, Any]:
    await user_repository.ensure_user_constraints()
    return _wrap({"status": "ok", "constraint": "user_email_unique"})


async def _h_create_project(_b: Dict[str, Any], _q: Dict[str, str]) -> Dict[str, Any]:
    """projectName 만 받아 빈 프로젝트를 즉시 등록(claim).

    [2026-06] "+ 새 프로젝트" 가 미팅 로그 없이도 프로젝트를 정식 등록하도록 추가.
    소유 등록(OWNS) + max_projects 쿼터 가드(402) + 동명 타 유저 409 충돌은 dispatcher
    의 _OWNERSHIP_CREATE 분기(ownership_repository.claim)가 이미 수행한다 — 여기선 그 뒤
    확인 응답만 반환한다. 이로써 신규 프로젝트가 pre-claim 상태를 거치지 않아 read 403
    노이즈가 근본적으로 사라진다(claim 직후부터 모든 read 통과).
    """
    project = _b.get("projectName") or _b.get("project_name") or _q.get("projectName") or ""
    if not project or not project.strip():
        raise HTTPException(status_code=422, detail="projectName 필수")
    return _wrap({"project_name": project.strip(), "created": True})


# ─── Dispatch table ────────────────────────────────────────────


Handler = Callable[[Dict[str, Any], Dict[str, str]], Awaitable[Dict[str, Any]]]

_DISPATCH: Dict[str, Handler] = {
    # CPS/PRD/Design 조회
    "getCPS": _h_get_cps,
    "getPRD": _h_get_prd,
    "getDDD": _h_get_ddd,
    "getSpack": _h_get_spack,
    "getArchitecture": _h_get_architecture,
    # Meeting
    "postMeeting": _h_post_meeting,
    # CPS 단독 (검수 모드) — postMeeting 의 PRD-skip 버전. 검수 모드 사용자가
    # CPS 만 먼저 검토 후 명시적으로 PRD 트리거하는 흐름.
    "cps": _h_create_cps,
    "getJobStatus": _h_get_job_status,
    "getProjectBusy": _h_get_project_busy,
    "cancelJob": _h_cancel_job,
    "getMeetingLogs": _h_get_meeting_logs,
    "getMeetingVersions": _h_get_meeting_versions,
    "deleteMeeting": _h_delete_meeting,
    # Project status
    "getProjectTimeline": _h_get_project_timeline,
    # Project
    "deleteProject": _h_delete_project,
    "createProject": _h_create_project,
    # Skill
    "postSkill": _h_post_skill,
    "getSkill": _h_get_skill,
    "getAllSkill": _h_get_all_skill,
    "getAllSkillDetail": _h_get_all_skill_detail,
    "deleteSkill": _h_delete_skill,
    "getDuplicateSkill": _h_get_duplicate_skill,
    "recommendSkillsByAI": _h_recommend_skills,
    "improveSkill": _h_improve_skill,
    # Design / MD
    "createDesign": _h_create_design,
    "createSpack": _h_create_design,  # 역사적 alias
    "createPRD": _h_create_prd,       # 검수 모드 PRD 단독 + error 복구 (P0-6)
    "createMD": _h_create_md,
    # Repo
    "addProjectRepo": _h_add_project_repo,
    "getProjectRepos": _h_get_project_repos,
    "deleteProjectRepo": _h_delete_project_repo,
    # Lint
    "runLint": _h_run_lint,
    "getLastLintResult": _h_get_last_lint,
    "generateFixSpec": _h_generate_fix_spec,
    # Lineage
    "analyzeLineage": _h_analyze_lineage,
    "getLastLineage": _h_get_last_lineage,
    # Setup
    "setupUserConstraints": _h_setup_user_constraints,
}


# ─── Ownership 분류 ─────────────────────────────────────────────
#
# action 을 (a) create — ownership 멱등 등록, (b) access — ownership 검증,
# (c) free — 인증만 통과하면 OK 로 분류. project 이름은 body 또는 query 의
# `projectName` / `project_name` 에서 추출.

# GitHub API 를 호출하는 핸들러 — dispatcher 가 사용자 OAuth access_token 을 주입.
# 이 set 에 포함되면 핸들러 시그니처에 `*, user_token: str | None` 이 있어야 함.
_GITHUB_USING_HANDLERS: set[str] = {
    "runLint",
    "analyzeLineage",
}

# LLM 을 호출하는 핸들러 — dispatcher 가 (1) 토큰 한도 가드 호출 + (2) user_email kwarg 주입.
# 핸들러 시그니처에 `*, user_email: str` 이 있어야 함.
# 누락 시: quota 우회 가능 (FE 의 plan.vue 등이 이 dispatcher 로 LLM 호출함).
_LLM_HANDLERS: set[str] = {
    "postMeeting",       # CPS + PRD
    "cps",               # CPS 단독 (검수 모드 — PRD 는 별도 트리거)
    "deleteMeeting",     # Master rebuild LLM 2회
    "recommendSkillsByAI",
    "improveSkill",
    "createDesign", "createSpack",  # alias
    "createMD",
    "runLint",
    "generateFixSpec",
}

# postMeeting / cps 모두 새 미팅 로그를 추가하는 흐름 — 토큰 + 글자수 + 미팅 카운트 가드.
# 다른 LLM 핸들러는 토큰 가드만 (재실행/생성 외 호출이라 미팅 등록 아님).
_MEETING_CREATING_HANDLERS: set[str] = {"postMeeting", "cps"}

# LLM 호출은 안 하지만 user_email 이 필요한 핸들러 (멀티테넌트 격리 / ownership 검증).
# 예: getJobStatus 는 task_id → project_name 회수 → assert_owns 흐름이 핸들러 내부에서 일어남.
# deleteProject 는 [Phase 2D] Project 노드 매칭에 owner_email 사용 — body.email 신뢰 X.
_USER_EMAIL_REQUIRED_HANDLERS: set[str] = {
    "getJobStatus",
    "cancelJob",
    "deleteProject",
}

# Request 객체를 핸들러에 직접 주입해야 하는 action 들.
# 사용 예: createDesign — pipeline 이 stage 사이마다 request.is_disconnected()
# 로 클라이언트 중지 감지 → 최종 commit 전에 graceful 종료. 기존 데이터 보존.
_REQUEST_REQUIRED_HANDLERS: set[str] = {
    "createDesign", "createSpack",
}


_OWNERSHIP_CREATE: set[str] = {
    # 처음 프로젝트를 만드는 mutation — owner 가 본인이 됨 (dispatcher 가 claim 호출:
    # OWNS 등록 + max_projects 쿼터 402 + 동명 타 유저 409).
    # createProject = 미팅 로그 없이 빈 프로젝트를 즉시 등록하는 명시적 진입점.
    "postMeeting", "cps", "addProjectRepo", "postSkill", "createProject",
}

_OWNERSHIP_ACCESS: set[str] = {
    # 기존 프로젝트에 대한 write / LLM mutation — owner 만 허용 (비소유 → 403).
    # [2026-06] read 는 _OWNERSHIP_READ 로 분리 (비소유 → 403 대신 200-empty).
    "deleteMeeting", "deleteProject", "deleteSkill", "deleteProjectRepo",
    "recommendSkillsByAI",
    "createDesign", "createSpack", "createMD", "createPRD",
    "runLint", "generateFixSpec", "analyzeLineage",
}

# [2026-06] ownership read — owner 만 실데이터, 비소유(아직 claim 안 된 본인 신규
# 프로젝트 포함)는 핸들러 미실행 + 정상 빈응답(_read_empty_response).
# 도메인 read 쿼리가 전역 project 이름으로 조회해 테넌트 격리가 이 게이트에 의존
# (security_tenant_isolation_gap) → 비소유자에게 핸들러를 절대 태우지 않아 동명
# 타 유저 데이터 노출(IDOR)을 차단한다. (pre-claim 403 콘솔 노이즈 제거 목적)
_OWNERSHIP_READ: set[str] = {
    "getCPS", "getPRD", "getDDD", "getSpack", "getArchitecture",
    "getMeetingLogs", "getMeetingVersions",
    "getSkill", "getAllSkill", "getAllSkillDetail", "getDuplicateSkill",
    "getProjectRepos", "getLastLintResult", "getLastLineage",
    "getProjectTimeline", "getProjectBusy",
}

# 분류 무결성 — 한 action 이 read/write 양쪽에 들어가면 게이트 의미가 모호.
assert not (_OWNERSHIP_READ & _OWNERSHIP_ACCESS), (
    "_OWNERSHIP_READ 와 _OWNERSHIP_ACCESS 는 disjoint 여야 합니다"
)

_OWNERSHIP_FREE: set[str] = {
    # project 와 무관한 system 라우트
    "setupUserConstraints",
    # getJobStatus / cancelJob 은 task_id 만 받음 → dispatcher 가 project_name 추출 못 함.
    # 핸들러 내부의 get_job_status_for_user 가 task_id → kwargs.project_name 회수 →
    # assert_owns 로 ownership 직접 검증한다.
    "getJobStatus",
    "cancelJob",
    # improveSkill — 편집 중인 규칙 1개를 다듬을 뿐 프로젝트 데이터 미접근 (인증 + 토큰 quota 만).
    "improveSkill",
}


def _extract_project(body: Dict[str, Any], query: Dict[str, str]) -> Optional[str]:
    """body / query 에서 projectName | project_name 추출."""
    for src in (body or {}, query or {}):
        for key in ("projectName", "project_name"):
            v = src.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _extract_team_id(body: Dict[str, Any], query: Dict[str, str]) -> Optional[str]:
    """body / query 에서 teamId | team_id 추출. 없으면 개인 프로젝트."""
    for src in (body or {}, query or {}):
        for key in ("teamId", "team_id"):
            v = src.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _read_empty_response(action: str, project: str) -> Dict[str, Any]:
    """비소유 read 의 '데이터 없음' 응답 — 각 _OWNERSHIP_READ 핸들러가 데이터 없는
    프로젝트에서 내는 shape 와 동일해야 한다. (핸들러를 태우지 않고 직접 반환하므로
    여기서 shape 를 맞춘다. tests/api/test_gateway_read_empty.py 가 각 핸들러의 실제
    빈 출력과 대조해 drift 를 잡는다.)

    대부분 `{"result": []}`. 핸들러별 특수 shape 만 분기.
    """
    if action == "getProjectBusy":
        return {"result": {"project_name": project, "busy": False}}
    if action == "getDuplicateSkill":
        return {"isDuplicate": False, "existingIds": []}
    if action == "getProjectRepos":
        return {"repos": [], "count": 0}
    if action in ("getLastLintResult", "getLastLineage"):
        return {"found": False, "result": None, "savedAt": None}
    if action == "getProjectTimeline":
        return {"project": project, "since": 0, "events": [], "counts": {}}
    if action == "getSkill":
        return {"result": None}
    # [2026-06] DDD/Spack/Architecture 그래프 read 는 데이터가 없어도 핸들러가 항상
    # (빈) 그래프 객체를 반환한다(get_*_graph: row={} → 모든 필드 빈 리스트) → _as_array 가
    # 단일 원소 리스트로 감싼다. 따라서 {"result": []} 가 아니라 {"result": [빈그래프]} 여야
    # FE 가 result[0] 으로 그래프를 읽는다. 빈 Pydantic 모델을 핸들러와 동일하게 _as_array
    # 로 통과시켜 shape 가 drift 하지 않게 한다.
    if action == "getDDD":
        return {"result": _as_array(query_repository.DddGraph())}
    if action == "getSpack":
        return {"result": _as_array(query_repository.SpackGraph())}
    if action == "getArchitecture":
        return {"result": _as_array(query_repository.ArchitectureGraph())}
    # getCPS/getPRD/getMeetingLogs/getMeetingVersions/getAllSkill/getAllSkillDetail →
    # get_master_*/get_meeting_log 가 빈 프로젝트에서 None/[] 반환 → {"result": []}
    return {"result": []}


# ─── Multipart upload routes (등록 순서 중요 — wildcard dispatcher 보다 먼저) ─

# 음성 전사용 허용 MIME prefix. wildcard 가 아니므로 명시적으로 화이트리스트.
# 일반적 회의 녹음 포맷: m4a (iOS/macOS 기본), mp3, wav, mp4 (Zoom recording 포함),
# webm (Chrome MediaRecorder), ogg / flac / aac.
_AUDIO_MIME_ALLOWED = {
    "audio/mpeg",       # mp3
    "audio/mp3",
    "audio/mp4",        # m4a
    "audio/m4a",
    "audio/x-m4a",
    "audio/aac",
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/webm",
    "audio/ogg",
    "audio/flac",
    "audio/x-flac",
    # 영상 컨테이너에 음성 트랙만 — Zoom mp4 녹화 등.
    "video/mp4",
    "video/webm",
}

# 30MB — 미들웨어 MAX_REQUEST_BODY_BYTES 와 같은 값 (양쪽 가드).
_TRANSCRIBE_MAX_BYTES = 30 * 1024 * 1024

# 일부 브라우저/OS 는 .m4a(iOS/macOS 기본 녹음) 등에 빈/generic MIME 을 실어 보낸다.
# content_type 을 못 믿을 때 확장자로 보정 — 정상 녹음이 415 로 잘못 거부되는 것 방지.
_EXT_TO_AUDIO_MIME = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}
_GENERIC_MIMES = {"", "application/octet-stream", "binary/octet-stream"}


@router.post(
    "/transcribeAudio",
    summary="음성 파일 → 한국어 raw 전사 (Gemini multimodal)",
)
async def transcribe_audio_route(
    request: Request,  # noqa: ARG001 — dispatcher consistency; future use 가능
    file: UploadFile = File(...),
    projectName: Optional[str] = Form(None),
    team_id: Optional[str] = Form(None),
    current_user: UserPublic = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    multipart/form-data 로 audio 파일 업로드 → Gemini Files API + generateContent 로
    한국어 전사 → text + 사용 토큰을 반환.

    응답 shape (FE 가 `response.data.result === "success"` 체크):
        {
          "result": "success",
          "text": "...전사된 내용...",
          "model": "gemini-2.5-flash",
          "tokens_used": 12345,
          "duration_sec": null,
          "truncated": false
        }

    Quota:
        Gemini 토큰을 usage_repository.add_tokens 로 누적 — 기존 LLM 호출과 동일 풀.

    Ownership:
        projectName 이 있으면 owner 검증 (assert_owns 가 직접 403 raise). 없으면 free 허용.
    """
    # ── 1) Ownership 검증 (projectName 있을 때만) ──
    pn = (projectName or "").strip() or None
    if pn:
        await ownership_repository.assert_access(current_user.email, pn, team_id)

    # ── 2) Quota 가드 — 토큰 한도 미리 체크 ──
    await quota.assert_tokens_within_limit(current_user.email)

    # ── 3) 파일 검증 — MIME / 크기 ──
    mime = (file.content_type or "").lower()
    # content_type 이 비었거나 generic 이면 확장자로 보정 (.m4a 등 정상 녹음 false 415 방지).
    if mime in _GENERIC_MIMES:
        ext = os.path.splitext(file.filename or "")[1].lower()
        mime = _EXT_TO_AUDIO_MIME.get(ext, mime)
    if mime not in _AUDIO_MIME_ALLOWED:
        raise HTTPException(
            status_code=415,
            detail=(
                f"지원하지 않는 파일 형식입니다 ({mime or '알 수 없음'}). "
                "mp3 / m4a / mp4 / wav / webm 등의 오디오 파일을 올려주세요."
            ),
        )

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="빈 파일입니다.")
    if len(audio_bytes) > _TRANSCRIBE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"파일이 너무 큽니다 ({len(audio_bytes) // 1024 // 1024}MB). "
                "최대 30MB 까지 가능합니다. 더 짧은 구간으로 잘라서 업로드해 주세요."
            ),
        )

    # ── 4) 전사 호출 ──
    logger.info(
        "[transcribeAudio] user=%s project=%s mime=%s size=%dB",
        current_user.email, pn, mime, len(audio_bytes),
    )
    result = await transcribe_audio(audio_bytes, mime_type=mime)

    # ── 5) 토큰 누적 (best-effort — 실패해도 응답은 정상 반환) ──
    if result.usage.total_tokens > 0:
        try:
            await usage_repository.add_tokens(
                current_user.email, result.usage.total_tokens,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "transcribeAudio: add_tokens 실패 (user=%s tokens=%d): %s",
                current_user.email, result.usage.total_tokens, e,
            )

    return {
        "result": "success",
        "text": result.text,
        "model": result.model,
        "tokens_used": result.usage.total_tokens,
        "duration_sec": result.duration_sec,
        # 전사가 출력 토큰 상한에 걸려 잘렸으면 FE 가 "더 짧게 나눠 재시도" 안내.
        "truncated": result.truncated,
    }


# ─── Dispatcher route ──────────────────────────────────────────


# [2026-05 Phase 5] per-user 버스트 가드 — 이 catch-all 은 v2 라우트(3/min)와 달리
# rate limit 이 없어, 무거운 action(postMeeting/createDesign 등)을 burst-enqueue 할 수
# 있는 표면이었다. 비용은 quota 가드가 차감 직전 차단하므로 여기선 DoS/버스트만 방어.
# 120/min(=2/s) 은 정상 폴링/연속 조작엔 충분히 여유, 폭주 스크립트만 차단.
# 키는 limiter.rate_limit_key = JWT email > IP (사용자 단위 정확 제한).
@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
@limiter.limit("120/minute")
async def gateway_compat_dispatch(
    path: str,
    request: Request,
    current_user: UserPublic = Depends(get_current_user),
) -> Any:
    """
    `/api/gateway/{action}` → dispatch table 기반 핸들러 호출.
    미지원 action 은 410 Gone.

    [Ownership]
    - create 류 (postMeeting / addProjectRepo / postSkill): 진입 시 ownership 멱등 등록.
    - access 류 (조회/수정/삭제): owner 가 아니면 403.
    - free 류 (setup): 검증 생략.
    """
    if request.method.upper() == "OPTIONS":
        # CORS preflight — 그냥 200
        return {"status": "ok"}

    handler = _DISPATCH.get(path)
    if handler is None:
        logger.warning("[gateway_compat] unsupported action: /api/gateway/%s", path)
        raise HTTPException(
            status_code=410,
            detail=(
                f"'/api/gateway/{path}' 는 더 이상 지원되지 않습니다. "
                f"/api/v2/* 또는 /gateway/* 도메인 라우트 사용."
            ),
        )

    # body + query 파싱
    try:
        body = (
            await request.json()
            if request.method.upper() not in ("GET", "HEAD")
            else {}
        )
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {"_raw": body}

    query = dict(request.query_params)

    # ── Ownership 검증/등록 — deny-by-default ──
    # [정책 — 2026-05]
    # 신규 action 을 _DISPATCH 에 추가하면서 _OWNERSHIP_{CREATE|ACCESS|FREE} 셋 중
    # 어디에도 분류 안 하면 명시적 500 — silently 가드 우회되는 회귀 방지.
    # 추가하는 사람이 의도적으로 정책 결정해서 분류하게 강제.
    project = _extract_project(body, query)
    team_id = _extract_team_id(body, query)
    if path in _OWNERSHIP_CREATE:
        if project:
            try:
                await ownership_repository.claim(current_user.email, project, team_id)
            except ownership_repository.ProjectOwnershipConflict as e:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"'{e.project}' 는 이미 다른 사용자가 사용 중인 프로젝트 "
                        f"이름입니다. 다른 이름을 사용하세요."
                    ),
                ) from e
    elif path in _OWNERSHIP_READ:
        if not project:
            raise HTTPException(status_code=422, detail="projectName 이 필요합니다.")
        # [2026-06] read — 비소유(아직 claim 안 된 본인 신규 프로젝트 포함)면 핸들러를
        # 실행하지 않고 정상 빈응답을 돌려준다. 도메인 read 쿼리가 전역 project 이름이라
        # 비소유자에게 핸들러를 태우면 동명 타 유저 데이터 노출(IDOR) — 절대 금지.
        # (팀 멤버이나 유료 플랜 미달이면 can_access 가 402 를 그대로 raise.)
        if not await ownership_repository.can_access(current_user.email, project, team_id):
            return _read_empty_response(path, project)
    elif path in _OWNERSHIP_ACCESS:
        if not project:
            raise HTTPException(status_code=422, detail="projectName 이 필요합니다.")
        await ownership_repository.assert_access(current_user.email, project, team_id)
    elif path in _OWNERSHIP_FREE:
        pass  # 의도적으로 project 와 무관한 시스템 라우트 (setup 등)
    else:
        # 분류 안 된 action — 개발자가 _DISPATCH 추가 시 셋 중 한 곳에 등록 누락.
        # silently 통과시키면 ownership 가드 우회 회귀 표면이 됨. 500 으로 명시 거부.
        logger.error(
            "[gateway_compat] action '%s' is in _DISPATCH but NOT in any of "
            "_OWNERSHIP_{CREATE|READ|ACCESS|FREE} — refusing for safety. "
            "Add it to the correct set in gateway_compat_routes.py.",
            path,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                f"'/api/gateway/{path}' 는 ownership 분류가 누락된 상태입니다. "
                "관리자에게 문의해주세요."
            ),
        )

    # ── Quota 가드 — LLM 호출 직전 차단 (LLM 비용 0) ──
    # LLM 핸들러는 dispatcher 가 가드를 일관 호출. 핸들러 본문은 tracked_pipeline_context
    # 로 토큰 자동 누적.
    if path in _LLM_HANDLERS:
        await quota.assert_tokens_within_limit(current_user.email)
        if path in _MEETING_CREATING_HANDLERS:
            meeting_content = (
                body.get("meeting_content") or body.get("meetingContent") or ""
            )
            # [2026-05-18] 의미적 최소치 검증 — quota 가드 *전에* 차단.
            # 너무 짧은 입력 → 미팅 카운트 차감 안 됨 (사용자 보호 + LLM 환각 차단).
            try:
                assert_meeting_content_substantial(meeting_content)
            except MeetingContentTooShort as e:
                raise HTTPException(
                    status_code=400,
                    detail=str(e),
                ) from e
            await quota.assert_summary_within_limit(current_user.email, meeting_content)
            await quota.acquire_meeting_quota(current_user.email)

    logger.info("[gateway_compat] %s /api/gateway/%s → dispatch", request.method, path)

    # GitHub API 를 호출하는 핸들러 (runLint / analyzeLineage) 는 사용자 OAuth
    # access_token 을 함께 주입한다. private repo / rate-limit 5000/hr 접근에 필요.
    # 사용자가 GitHub 미연결이면 None → handler 는 anonymous 호출로 진행 (public repo만).
    # LLM 핸들러 + user_email 만 필요한 핸들러 양쪽에 user_email 주입.
    handler_kwargs: Dict[str, Any] = {}
    if path in _GITHUB_USING_HANDLERS:
        handler_kwargs["user_token"] = await user_repository.get_github_access_token(
            current_user.email
        )
    if path in _LLM_HANDLERS or path in _USER_EMAIL_REQUIRED_HANDLERS:
        handler_kwargs["user_email"] = current_user.email
    if path in _REQUEST_REQUIRED_HANDLERS:
        handler_kwargs["request"] = request

    # ── 핸들러 예외 → HTTP 응답 매핑 (CORS 안전성 필수) ──
    # [2026-06 버그 수정] 파이프라인(delete/rebuild 등)이 던지는 ValueError/RuntimeError
    # 를 여기서 잡지 않으면 Starlette ServerErrorMiddleware(=CORSMiddleware 보다 바깥)
    # 가 500 을 내보내고, 그 500 엔 Access-Control-Allow-Origin 헤더가 안 붙는다.
    # 결과: 브라우저가 "No 'Access-Control-Allow-Origin' header ... blocked by CORS
    # policy" 로 오인 표시하며 실제 에러 메시지를 가린다(특히 /deleteMeeting — LLM
    # 빈 응답/데이터 손상 가드 RuntimeError). HTTPException 으로 변환하면 정상 응답
    # 경로(ExceptionMiddleware → CORSMiddleware)를 타 CORS 헤더 + 사유가 함께 전달된다.
    # 정상 v2 라우트(delete_routes.delete_meeting_route)와 동일한 매핑 정책.
    try:
        if handler_kwargs:
            return await handler(body, query, **handler_kwargs)
        return await handler(body, query)
    except (HTTPException, GeminiError):
        # HTTPException: 이미 정상 응답 경로. GeminiError: 전역 핸들러가 변환(CORS 안전).
        raise
    except (ValueError, RuntimeError) as e:
        # 파이프라인이 사용자向 한글 메시지를 담아 raise (재시도 안내 등).
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        # 예상 못 한 예외도 헤더 없는 raw 500 대신 CORS 통과하는 깔끔한 500 으로.
        logger.exception(
            "[gateway_compat] unhandled error: action=%s err=%s", path, type(e).__name__
        )
        raise HTTPException(
            status_code=500,
            detail="서버 내부 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
        ) from e
