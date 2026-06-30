"""
조회 라우트 — getCPS / getPRD / getDDD / getSpack / getArchitecture /
getMeetingLogs / getMeetingVersions 의 Pydantic 타입화 엔드포인트.

[Path 컨벤션]
- GET /api/v2/cps?project_name=X
- GET /api/v2/prd?project_name=X
- GET /api/v2/ddd?project_name=X
- GET /api/v2/spack?project_name=X
- GET /api/v2/architecture?project_name=X
- GET /api/v2/meetings/logs?project_name=X&version=Y
- GET /api/v2/meetings/versions?project_name=X
"""
from __future__ import annotations

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api._quota_helpers import tracked_pipeline_context
from app.clients.gemini_client import GeminiError, gemini_error_to_http
from app.pipelines.prd_autofix_pipeline import run_prd_autofix
from app.pipelines.prd_fidelity_verify import verify_fidelity_llm
from app.core import quota
from app.core.limiter import limiter
from app.core.security import get_current_user
from app.service import ownership_repository, query_repository as q
from app.service.query_repository import (
    ArchitectureGraph,
    CpsMaster,
    DddGraph,
    MeetingLog,
    MeetingVersion,
    PrdMaster,
    ProjectGraph,
    ProjectTimeline,
    SpackGraph,
)
from app.service.user_repository import UserPublic


# [2026-05] 검수 게이트 — 사용자가 LLM 결과를 markdown 수정 후 저장.
# is_latest=true Master 1개만 영향. Problem/Solution / Epic/Story 그래프 노드는
# 그대로 유지 (markdown 은 display only). CPS/PRD 공유 schema.
class UpdateMarkdownRequest(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=200)
    team_id: Optional[str] = None
    content: str = Field(
        ...,
        min_length=1,
        max_length=500_000,
        description="새 full_markdown. 500KB 캡 — 비정상 입력 차단",
    )
    # [2026-05-18 Phase 2 Optimistic Locking]
    # FE 가 GET 응답의 last_updated 를 보내면 BE 가 DB updated_at 과 비교 →
    # 다른 디바이스가 먼저 편집한 경우 409 응답. 없으면 (legacy / 미갱신 FE) 조건 skip.
    client_updated_at: int | None = Field(
        None, description="GET 응답의 last_updated. 동시 편집 충돌 시 409.",
    )


class UpdateMarkdownResponse(BaseModel):
    master_id: str
    last_updated: int | None = None


# [호환] Phase 2.1 에서 노출된 별칭 — 외부 import 시 깨지지 않게 유지.
UpdateCpsRequest = UpdateMarkdownRequest
UpdateCpsResponse = UpdateMarkdownResponse


# [2026-05 검수 게이트 Phase 3.1+3.2] CPS / PRD 그래프 노드 단일 update.
# CPS: Problem | Solution, PRD: Epic | Story 모두 동일 (project_name, summary) 시그니처.
class UpdateNodeRequest(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=200)
    team_id: Optional[str] = None
    summary: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="새 summary 텍스트. 2KB 캡 — 단일 노드 요약 분량",
    )


class UpdateNodeResponse(BaseModel):
    id: str
    label: str  # CPS: 'Problem' | 'Solution', PRD: 'Epic' | 'Story'
    summary: str


# [호환] Phase 3.1 에서 노출된 별칭.
UpdateCpsNodeRequest = UpdateNodeRequest
UpdateCpsNodeResponse = UpdateNodeResponse


# [2026-05 검수 게이트 Phase 3.3] CPS / PRD 노드 listing — FE 사이드바가 그래프 ID 알기 위함.
class NodeListItem(BaseModel):
    id: str
    label: str
    summary: str


class NodeListResponse(BaseModel):
    nodes: List[NodeListItem]


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["Queries (Read-only)"])


@router.get(
    "/cps",
    response_model=CpsMaster,
    summary="getCPS — 프로젝트의 마스터 CPS 문서 + 흡수된 delta IDs",
)
@limiter.limit("60/minute")
async def get_cps_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> CpsMaster:
    # IDOR 방어 — 본인 소유 프로젝트만 조회 (FE 정상 경로 gateway_compat 와 동일 가드)
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    out = await q.get_master_cps(project_name, team_id=team_id or "")
    if out is None:
        raise HTTPException(
            status_code=404,
            detail="마스터 CPS 를 찾을 수 없습니다. postMeeting 먼저 실행 필요.",
        )
    return out


@router.patch(
    "/cps",
    response_model=UpdateCpsResponse,
    summary="updateCPS — 사용자가 직접 수정한 markdown 으로 Master CPS 덮어쓰기 (검수 게이트 모드)",
)
@limiter.limit("10/minute")
async def update_cps_route(
    request: Request,
    payload: UpdateCpsRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> UpdateCpsResponse:
    """
    [2026-05 검수 게이트 Phase 2]
    auto_progress=false 모드 사용자가 LLM 결과를 검토 후 markdown 직접 수정.

    - is_latest=true Master CPS 의 full_markdown 만 덮어쓰기
    - Problem/Solution 그래프 노드는 그대로 (markdown 은 display only)
    - 없는 project → 404
    - rate limit 10/min — mutation 이라 read 보다 빡빡, LLM 호출보단 여유
    """
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    try:
        out = await q.update_master_cps_markdown(
            payload.project_name,
            payload.content,
            client_updated_at=payload.client_updated_at,
            team_id=payload.team_id or "",
        )
    except q.OptimisticLockConflict as e:
        # [Phase 2 동시접속] 다른 디바이스가 먼저 편집 → 409 + 안내
        raise HTTPException(status_code=409, detail=str(e)) from e
    if out is None:
        raise HTTPException(
            status_code=404,
            detail="수정할 마스터 CPS 가 없습니다. postMeeting 먼저 실행 필요.",
        )
    return UpdateCpsResponse(**out)


@router.get(
    "/prd",
    response_model=PrdMaster,
    summary="getPRD — 프로젝트의 마스터 PRD + CPS 연결 + 흡수된 delta IDs",
)
@limiter.limit("60/minute")
async def get_prd_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> PrdMaster:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    out = await q.get_master_prd(project_name, team_id=team_id or "")
    if out is None:
        raise HTTPException(
            status_code=404,
            detail="마스터 PRD 를 찾을 수 없습니다. postMeeting 먼저 실행 필요.",
        )
    # [2026-05-26 lazy trigger] 기존 누더기 PRD 자동 해소 — PRD 조회 시점에 detection.
    # post_meeting 자동 cleanup (PR #52) 이전에 누적된 프로젝트들을 사용자 노출 없이
    # 점진적으로 정리. deterministic task_id 로 arq dedup → 반복 호출 안전.
    try:
        from app.queue.jobs import maybe_lazy_trigger_cleanup
        await maybe_lazy_trigger_cleanup(
            project_name=project_name,
            master_markdown=out.prd_content or "",
            user_email=current_user.email,
            team_id=team_id or "",
        )
    except Exception:  # noqa: BLE001 — best-effort, PRD 조회 영향 0
        pass
    return out


@router.patch(
    "/prd",
    response_model=UpdateMarkdownResponse,
    summary="updatePRD — 사용자가 직접 수정한 markdown 으로 Master PRD 덮어쓰기 (검수 게이트 모드)",
)
@limiter.limit("10/minute")
async def update_prd_route(
    request: Request,
    payload: UpdateMarkdownRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> UpdateMarkdownResponse:
    """
    [2026-05 검수 게이트 Phase 2.2]
    PRD markdown 직접 편집 — CPS PATCH 와 동일 정책.

    - is_latest=true Master PRD 의 full_markdown 만 덮어쓰기
    - Epic/Story 그래프 노드는 그대로 (markdown 은 display only)
    - master 없으면 404 + "createPRD 또는 postMeeting 먼저" 안내
    """
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    try:
        out = await q.update_master_prd_markdown(
            payload.project_name,
            payload.content,
            client_updated_at=payload.client_updated_at,
            team_id=payload.team_id or "",
        )
    except q.OptimisticLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if out is None:
        raise HTTPException(
            status_code=404,
            detail="수정할 마스터 PRD 가 없습니다. createPRD 또는 postMeeting 먼저 실행 필요.",
        )
    return UpdateMarkdownResponse(**out)


# [Phase 3.5a] markdown_stale flag dismiss — 사용자가 graph 수정 후 markdown 과
# 의도적 desync 를 OK 한 경우. markdown 내용은 변경 안 됨, banner 만 사라짐.
class DismissStaleRequest(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=200)
    team_id: Optional[str] = None


class DismissStaleResponse(BaseModel):
    project_name: str
    dismissed: bool = True


@router.post(
    "/cps/markdown-stale/dismiss",
    response_model=DismissStaleResponse,
    summary="dismissCpsMarkdownStale — graph↔markdown desync banner 무시 (검수 게이트 Phase 3.5a)",
)
@limiter.limit("10/minute")
async def dismiss_cps_stale_route(
    request: Request,
    payload: DismissStaleRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> DismissStaleResponse:
    """
    [Phase 3.5a]
    노드 수정 후 markdown_stale=true 가 set 되어 FE 가 banner 띄움.
    사용자가 manual 검토 후 OK 라고 판단하면 이 endpoint 로 dismiss.
    """
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    ok = await q.dismiss_cps_markdown_stale(payload.project_name, team_id=payload.team_id or "")
    if not ok:
        raise HTTPException(
            status_code=404, detail="마스터 CPS 가 없습니다."
        )
    return DismissStaleResponse(project_name=payload.project_name)


@router.post(
    "/prd/markdown-stale/dismiss",
    response_model=DismissStaleResponse,
    summary="dismissPrdMarkdownStale — PRD desync banner 무시 (검수 게이트 Phase 3.5a)",
)
@limiter.limit("10/minute")
async def dismiss_prd_stale_route(
    request: Request,
    payload: DismissStaleRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> DismissStaleResponse:
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    ok = await q.dismiss_prd_markdown_stale(payload.project_name, team_id=payload.team_id or "")
    if not ok:
        raise HTTPException(
            status_code=404, detail="마스터 PRD 가 없습니다."
        )
    return DismissStaleResponse(project_name=payload.project_name)


# ─── [Phase 3.6] Design source-stale ───────────────────────────────
#
# PRD 가 갱신되면 Design (SPACK/DDD/Arch) 은 옛 PRD 기준이라 stale.
# FE 가 design 페이지 진입 시 GET 으로 banner 표시 여부 결정.
# 사용자가 banner 무시(dismiss) 하거나, createSpack 으로 design 재생성하면 자동 해제.
class DesignStaleStatus(BaseModel):
    project_name: str
    design_source_stale: bool
    design_source_stale_at: Optional[int] = None


@router.get(
    "/design/source-stale",
    response_model=DesignStaleStatus,
    summary="getDesignSourceStale — Design 이 옛 PRD 기준인지 확인 (Phase 3.6)",
)
@limiter.limit("60/minute")
async def get_design_stale_route(
    request: Request,
    project_name: str = Query(..., min_length=1, max_length=200),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> DesignStaleStatus:
    """
    Project 노드의 design_source_stale 플래그 + 마킹 시각 반환.
    Project 가 아직 없으면 stale=false / at=None (= 첫 사용).
    """
    # [2026-06] 비소유(아직 claim 안 된 본인 신규 프로젝트 포함) → 핸들러 미실행, 빈(=not
    # stale) 상태. read 쿼리가 전역 name 이라 비소유자에게 핸들러를 태우면 동명 타 유저
    # Project 플래그 노출(IDOR) — 절대 금지. (팀 플랜 미달은 can_access 가 402 raise.)
    if not await ownership_repository.can_access(current_user.email, project_name, team_id):
        return DesignStaleStatus(
            project_name=project_name, design_source_stale=False, design_source_stale_at=None,
        )
    status = await q.get_design_stale_status(project_name, team_id=team_id or "")
    return DesignStaleStatus(
        project_name=project_name,
        design_source_stale=status["design_source_stale"],
        design_source_stale_at=status["design_source_stale_at"],
    )


@router.post(
    "/design/source-stale/dismiss",
    response_model=DismissStaleResponse,
    summary="dismissDesignSourceStale — Design stale banner 무시 (Phase 3.6)",
)
@limiter.limit("10/minute")
async def dismiss_design_stale_route(
    request: Request,
    payload: DismissStaleRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> DismissStaleResponse:
    """
    [Phase 3.6]
    사용자가 banner 무시 — flag 만 false 로. design 자체는 재생성 안 함.
    실제 재생성은 design 페이지의 "최신 업데이트" → createSpack 경로.
    """
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    ok = await q.dismiss_design_source_stale(payload.project_name, team_id=payload.team_id or "")
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="Project 노드가 없습니다. 먼저 미팅 로그 업로드 또는 PRD 생성 필요.",
        )
    return DismissStaleResponse(project_name=payload.project_name)


# [B — 2026-06] 그래프 임팩트 분석 ─────────────────────────────────────────
#
# design 재생성 이후 수정된 Epic/Story 와, 그 노드에 DERIVED_FROM 엣지로 연결된
# Design 노드(API/Screen/Entity 등)를 반환. FE 는 이 데이터로 "어떤 Story 가 바뀌어서
# 어떤 API 를 재생성해야 하는지" 를 flat boolean 배너보다 정밀하게 표시한다.
class DesignImpactDesignDetail(BaseModel):
    id: str
    label: str
    tier: str  # "confirmed" | "review"
    quote: str = ""
    error_cases: str = ""  # API 노드의 에러 케이스 (비어있으면 해당 없음)


class DesignImpactCascadeNode(BaseModel):
    id: str
    label: str
    tier: str  # "estimated"


class DesignImpactScreenNode(BaseModel):
    id: str
    name: str
    tier: str = "direct"  # RENDERS / CALLS_API — 직접 연결


class DesignImpactApiChainNode(BaseModel):
    id: str
    endpoint: str
    method: str
    tier: str = "estimated"  # 연결 서비스 경유 peer API


class DesignImpactLayers(BaseModel):
    design: List[DesignImpactDesignDetail] = []
    ddd: List[DesignImpactCascadeNode] = []
    arch: List[DesignImpactCascadeNode] = []
    events: List[DesignImpactCascadeNode] = []
    screens: List[DesignImpactScreenNode] = []
    api_chain: List[DesignImpactApiChainNode] = []


class DesignImpactChangedNode(BaseModel):
    node_id: str
    node_label: str
    summary: str
    changed_at: int
    impact_layers: DesignImpactLayers = DesignImpactLayers()


class DesignImpactResponse(BaseModel):
    project_name: str
    design_last_generated_at: Optional[int] = None
    changed_nodes: List[DesignImpactChangedNode]
    total_affected_design_count: int


@router.get(
    "/design/impact",
    response_model=DesignImpactResponse,
    summary="getDesignImpact — PRD 변경이 Design 에 미치는 영향 노드 분석 (B)",
)
@limiter.limit("30/minute")
async def get_design_impact_route(
    request: Request,
    project_name: str = Query(..., min_length=1, max_length=200),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> DesignImpactResponse:
    """
    design 재생성 이후 수정된 Epic/Story 를 찾고, 각 노드에서 DERIVED_FROM 엣지로
    연결된 Design 노드(API/Screen/Entity 등)를 반환한다.

    - design 재생성 전이면 changed_nodes = [] (아직 수정 없음).
    - DERIVED_FROM 엣지가 없는 노드는 impact_layers.design = [] 로 포함 — 영향 추정 불가.
    - design 이 한 번도 생성된 적 없으면 design_last_generated_at = null.
    """
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    result = await q.get_design_impact(project_name, team_id=team_id or "")
    changed = [
        DesignImpactChangedNode(
            node_id=n["node_id"],
            node_label=n["node_label"],
            summary=n["summary"],
            changed_at=n["changed_at"],
            impact_layers=DesignImpactLayers(
                design=[
                    DesignImpactDesignDetail(
                        id=d["id"],
                        label=d["label"],
                        tier=d["tier"],
                        quote=d.get("quote") or "",
                        error_cases=d.get("error_cases") or "",
                    )
                    for d in (n.get("impact_layers", {}).get("design") or [])
                ],
                ddd=[
                    DesignImpactCascadeNode(id=x["id"], label=x["label"], tier=x["tier"])
                    for x in (n.get("impact_layers", {}).get("ddd") or [])
                ],
                arch=[
                    DesignImpactCascadeNode(id=x["id"], label=x["label"], tier=x["tier"])
                    for x in (n.get("impact_layers", {}).get("arch") or [])
                ],
                events=[
                    DesignImpactCascadeNode(id=x["id"], label=x["label"], tier=x["tier"])
                    for x in (n.get("impact_layers", {}).get("events") or [])
                ],
                screens=[
                    DesignImpactScreenNode(id=s["id"], name=s["name"], tier=s["tier"])
                    for s in (n.get("impact_layers", {}).get("screens") or [])
                ],
                api_chain=[
                    DesignImpactApiChainNode(
                        id=p["id"],
                        endpoint=p["endpoint"],
                        method=p["method"],
                        tier=p["tier"],
                    )
                    for p in (n.get("impact_layers", {}).get("api_chain") or [])
                ],
            ),
        )
        for n in result.get("changed_nodes") or []
    ]
    return DesignImpactResponse(
        project_name=project_name,
        design_last_generated_at=result.get("design_last_generated_at"),
        changed_nodes=changed,
        total_affected_design_count=result.get("total_affected_design_count", 0),
    )


# [2026-06] Design 품질 체크 — MAPPED_TO.role 제약 위반 감지 ──────────────
class DesignQualityViolation(BaseModel):
    aggregate_id: str
    aggregate_name: str
    root_count: int
    root_entity_ids: List[str] = []
    violation_type: str  # "missing_aggregate_root" | "multiple_aggregate_roots"


class DesignQualityReport(BaseModel):
    project_name: str
    violation_count: int
    violations: List[DesignQualityViolation]


@router.get(
    "/design/quality",
    response_model=DesignQualityReport,
    summary="getDesignQuality — aggregate_root 제약 위반 탐지 (MAPPED_TO.role)",
)
@limiter.limit("30/minute")
async def get_design_quality_route(
    request: Request,
    project_name: str = Query(..., min_length=1, max_length=200),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> DesignQualityReport:
    """
    각 Aggregate 의 aggregate_root 엔티티 수가 정확히 1개인지 검증한다.

    - 0개: missing_aggregate_root — 집합 루트가 없는 불완전한 Aggregate 모델.
    - 2개 이상: multiple_aggregate_roots — LLM 이 중복 지정한 것으로 DDD 위반.
    - 위반 0건: violations=[] (정상).
    - design 이 한 번도 생성된 적 없으면 violations=[] 로 응답.
    """
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    result = await q.get_design_quality(project_name, team_id=team_id or "")
    return DesignQualityReport(
        project_name=project_name,
        violation_count=result["violation_count"],
        violations=[
            DesignQualityViolation(
                aggregate_id=v["aggregate_id"],
                aggregate_name=v["aggregate_name"],
                root_count=v["root_count"],
                root_entity_ids=v["root_entity_ids"],
                violation_type=v["violation_type"],
            )
            for v in result["violations"]
        ],
    )


# [Phase 3.5b/c] LLM 기반 graph → markdown 재합성 — preview 반환 ──
# 사용자가 그래프 노드를 편집한 뒤 markdown 도 그에 맞게 동기화하고 싶을 때.
# - 비용: LLM 1회 호출 / 요청. tracked_pipeline_context 가 토큰 quota 자동 적재.
# - rate limit 5/min — LLM 호출 비용 제어.
# - sync 호출 (대기). 한 요청 평균 ~5~15초. 큰 markdown 은 더 걸릴 수 있음.
# - LLM 출력 형식 검증 실패 시 404 반환 + markdown 변경 안 함 (stale flag 유지).
# - [Phase 3.5c] preview 만 반환 — markdown 저장 안 함. caller(FE) 가 diff
#   확인 후 PATCH /api/v2/{cps,prd} 로 명시적 저장. LLM 출력 신뢰 전 사용자 검토.
class ResynthesizeRequest(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=200)
    team_id: Optional[str] = None


class ResynthesizeResponse(BaseModel):
    project_name: str
    markdown: str
    master_id: str | None = None
    current_markdown: str | None = None  # [Phase 3.5c] FE diff 표시용
    # [2026-06-10 lost-update 가드] 재합성이 기준으로 읽은 master 의 updated_at.
    # FE 가 적용 PATCH 의 client_updated_at 으로 사용 — 클라 화면이 낡았어도
    # (서버본 기준 diff 라) 거짓 409 없이 정확한 버전으로 충돌 검사.
    last_updated: int | None = None


# ── [2026-05] PRD autofix — lint finding 을 AI 가 기존 맥락으로 자동 보완 (하이브리드) ──
# 본문은 preview 만 반환(저장 X). needs_input 가 있으면 FE 가 그 항목만 AI 인터뷰로
# 수집 — [2026-06] needs_input 은 master 노드에 영속화해 새로고침/다른 기기에서 복원.
class PrdAutofixRequest(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=200)
    team_id: Optional[str] = None
    # FE 가 화면에 띄운 현재 PRD. 없으면 BE 가 master PRD 를 조회.
    text: str | None = Field(default=None, description="현재 PRD Markdown (선택)")


class PrdAutofixNeedsInput(BaseModel):
    topic: str
    question: str


class PrdAutofixResponse(BaseModel):
    project_name: str
    markdown: str                       # 보완된 PRD (preview)
    current_markdown: str | None = None  # diff 비교용 원본
    before_score: float
    after_score: float
    changed: bool                        # 실제 변경이 있었는지
    before_issue_count: int
    after_issue_count: int
    remaining_issues: List[dict] = Field(default_factory=list)
    needs_input: List[PrdAutofixNeedsInput] = Field(default_factory=list)



@router.post(
    "/cps/resynthesize",
    response_model=ResynthesizeResponse,
    summary="resynthesizeCps — graph 노드 변경을 markdown 에 LLM 으로 반영 — preview (Phase 3.5b/c)",
)
@limiter.limit("5/minute")
async def resynthesize_cps_route(
    request: Request,
    payload: ResynthesizeRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> ResynthesizeResponse:
    """
    [Phase 3.5b/c] CPS markdown 재합성 — preview 만 반환 (save 안 함).

    - 성공: 200 + 새 markdown (preview) + 현재 markdown (diff 비교용).
      FE 가 diff 보여주고 사용자 승인 시 PATCH /api/v2/cps 로 저장.
    - master 또는 그래프 노드 없음 / LLM 출력 형식 실패: 404
    - LLM 호출 실패: GeminiError → 적절한 HTTP 코드
    """
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    # createMD 와 동일 정책 — LLM 비용 큰 작업이라 pre-check.
    await quota.assert_tokens_within_limit(current_user.email)

    task_id = f"resync-cps-{uuid.uuid4().hex[:12]}"
    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key=task_id,
    ) as ctx:
        try:
            new_md = await q.resync_cps_markdown_from_graph(ctx, payload.project_name, team_id=payload.team_id or "")
        except GeminiError as e:
            logger.exception("resync_cps gemini error (task=%s)", task_id)
            raise gemini_error_to_http(e) from e

    if new_md is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "재합성 대상이 없습니다. master CPS / Problem · Solution 그래프 "
                "노드가 존재하는지 확인하세요. LLM 출력이 형식을 못 지킨 경우도 "
                "이 코드로 반환 — 다시 시도하거나 markdown 직접 편집을 사용하세요."
            ),
        )
    master = await q.get_master_cps(payload.project_name, team_id=payload.team_id or "")
    return ResynthesizeResponse(
        project_name=payload.project_name,
        markdown=new_md,
        master_id=master.master_id if master else None,
        current_markdown=master.content if master else None,
        # [2026-06-11 lost-update 가드] PRD resynth(2026-06-10)와 동일 — 재합성이
        # 기준으로 읽은 master 버전. FE 적용 PATCH 의 client_updated_at 으로 사용.
        last_updated=master.last_updated if master else None,
    )


@router.post(
    "/prd/resynthesize",
    response_model=ResynthesizeResponse,
    summary="resynthesizePrd — graph 노드 변경을 PRD markdown 에 LLM 으로 반영 — preview (Phase 3.5b/c)",
)
@limiter.limit("5/minute")
async def resynthesize_prd_route(
    request: Request,
    payload: ResynthesizeRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> ResynthesizeResponse:
    """[Phase 3.5b/c] PRD markdown 재합성 preview — CPS 와 동일 패턴. save 안 함."""
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    await quota.assert_tokens_within_limit(current_user.email)

    task_id = f"resync-prd-{uuid.uuid4().hex[:12]}"
    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key=task_id,
    ) as ctx:
        try:
            new_md = await q.resync_prd_markdown_from_graph(ctx, payload.project_name, team_id=payload.team_id or "")
        except GeminiError as e:
            logger.exception("resync_prd gemini error (task=%s)", task_id)
            raise gemini_error_to_http(e) from e

    if new_md is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "재합성 대상이 없습니다. master PRD / Epic · Story 그래프 노드가 "
                "존재하는지 확인하세요. LLM 출력 형식 실패 시도 이 코드 — 재시도 "
                "또는 markdown 직접 편집을 사용하세요."
            ),
        )
    master = await q.get_master_prd(payload.project_name, team_id=payload.team_id or "")
    return ResynthesizeResponse(
        project_name=payload.project_name,
        markdown=new_md,
        master_id=master.master_prd_id if master else None,
        current_markdown=master.prd_content if master else None,
        last_updated=master.last_updated if master else None,
    )


@router.post(
    "/prd/autofix",
    response_model=PrdAutofixResponse,
    summary="autofixPrd — PRD lint finding 을 AI 가 기존 맥락으로 자동 보완 (preview, 하이브리드)",
)
@limiter.limit("5/minute")
async def autofix_prd_route(
    request: Request,
    payload: PrdAutofixRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> PrdAutofixResponse:
    """
    [2026-05] PRD lint 경고를 "직접 고치세요" 대신 AI 가 자동 보완.

    - PRD 본문 + CPS + Screens 참조 등 이미 있는 맥락으로 최대한 자동 보완.
    - 근거 없는 항목(인증 방식/NFR 수치 등)은 지어내지 않고 needs_input 으로 반환
      → FE 가 그 항목만 AI 인터뷰로 수집.
    - preview 만 반환(저장 X). 사용자 승인 시 PATCH /api/v2/prd 로 저장.
    - 대상 PRD 없음 / LLM 출력 형식 실패: 404 / GeminiError → 적절한 HTTP.
    """
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    await quota.assert_tokens_within_limit(current_user.email)

    task_id = f"autofix-prd-{uuid.uuid4().hex[:12]}"
    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key=task_id,
    ) as ctx:
        try:
            result = await run_prd_autofix(
                ctx, payload.project_name, current_markdown=payload.text,
            )
        except GeminiError as e:
            logger.exception("autofix_prd gemini error (task=%s)", task_id)
            raise gemini_error_to_http(e) from e

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="보완할 PRD 가 없습니다. 먼저 회의록으로 PRD 를 생성하세요.",
        )

    # [2026-06] needs_input 영속화 — 새로고침/다른 기기에서도 '인터뷰로 채우기'
    # 상태를 복원해, 같은 진단을 얻으려고 LLM 을 재호출하는 토큰 낭비 방지.
    # 빈 리스트면 해제(이전 진단 잔존 방지). best-effort — 저장 실패가 보완
    # 응답 자체를 막으면 안 됨.
    try:
        await q.set_prd_autofix_needs_input(
            payload.project_name, result.needs_input, team_id=payload.team_id or "",
        )
    except Exception:  # noqa: BLE001
        logger.warning("autofix needs_input 저장 실패 (task=%s) — 응답은 정상 진행", task_id)

    return PrdAutofixResponse(
        project_name=result.project_name,
        markdown=result.improved_markdown,
        current_markdown=result.current_markdown,
        before_score=result.before_score,
        after_score=result.after_score,
        changed=result.changed,
        before_issue_count=len(result.before_issues),
        after_issue_count=len(result.after_issues),
        remaining_issues=result.after_issues,
        needs_input=[
            PrdAutofixNeedsInput(topic=n["topic"], question=n["question"])
            for n in result.needs_input
        ],
    )


# ── [2026-06] autofix needs_input dismiss — 사용자가 X 로 안내 닫기 ──
class AutofixNeedsDismissRequest(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=200)
    team_id: Optional[str] = None


class AutofixNeedsDismissResponse(BaseModel):
    project_name: str
    dismissed: bool = True


@router.post(
    "/prd/autofix/needs-input/dismiss",
    response_model=AutofixNeedsDismissResponse,
    summary="dismissAutofixNeedsInput — 영속화된 '인터뷰로 채우기' 안내 닫기",
)
@limiter.limit("10/minute")
async def dismiss_autofix_needs_route(
    request: Request,
    payload: AutofixNeedsDismissRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> AutofixNeedsDismissResponse:
    """
    [2026-06] needs_input 이 master 노드에 영속화되며, FE 의 X(닫기)도 BE 를
    함께 지워야 새로고침/다른 기기에서 다시 안 뜬다. master 없음도 dismissed
    로 응답 — 멱등 (지울 게 없으면 이미 목적 달성).
    """
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    await q.clear_prd_autofix_needs_input(payload.project_name, team_id=payload.team_id or "")
    return AutofixNeedsDismissResponse(project_name=payload.project_name)


# ── [2026-06] PRD 정확성 검증 — 원본 회의록 ↔ PRD 신호 양방향 대조 (1단계: 토큰 0) ──
class PrdFidelityRequest(BaseModel):
    project_name: str
    team_id: str = ""


class PrdFidelityMissing(BaseModel):
    point: str                # 빠진 핵심 내용 (한 줄)
    evidence: str = ""        # 회의록 근거
    section: str = ""         # overview | epic | screen | nfr
    severity: str = "medium"  # high | medium | low


class PrdFidelityHall(BaseModel):
    point: str                # 회의록 근거 없는 PRD 주장
    severity: str = "medium"


class PrdFidelityResponse(BaseModel):
    available: bool = True    # 회의록 + PRD 둘 다 있어 대조 가능했는지
    coverage_pct: int = 100   # 회의록 핵심 중 PRD 에 반영된 비율
    summary: str = ""
    missing: List[PrdFidelityMissing] = Field(default_factory=list)     # 핵심 누락
    hallucination: List[PrdFidelityHall] = Field(default_factory=list)  # 환각 후보


@router.post(
    "/prd/fidelity",
    response_model=PrdFidelityResponse,
    summary="prdFidelity — 원본 회의록 ↔ 생성 PRD 정밀 대조 (LLM 2단계: 핵심 누락·환각)",
)
@limiter.limit("5/minute")
async def prd_fidelity_route(
    request: Request,
    payload: PrdFidelityRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> PrdFidelityResponse:
    """원본 회의록(전체)과 master PRD 를 LLM 으로 정밀 대조해, 제품적으로 중요한 누락/환각만
    반환. 잡담·진행 메타·중복은 무시(1단계 토큰 비교의 노이즈 제거). 회의록·PRD 가 없으면
    available=False. 온디맨드 — LLM 1회 호출.
    """
    await ownership_repository.assert_access(
        current_user.email, payload.project_name, payload.team_id
    )
    meeting = await q.get_all_meeting_content(payload.project_name, payload.team_id)
    prd = await q.get_master_prd(payload.project_name, payload.team_id)
    if not meeting or not prd or not prd.prd_content:
        return PrdFidelityResponse(available=False)

    await quota.assert_tokens_within_limit(current_user.email)
    task_id = f"fidelity-{uuid.uuid4().hex[:12]}"
    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key=task_id,
    ) as ctx:
        try:
            result = await verify_fidelity_llm(ctx, meeting, prd.prd_content)
        except GeminiError as e:
            logger.exception("prd_fidelity gemini error (task=%s)", task_id)
            raise gemini_error_to_http(e) from e
    return PrdFidelityResponse(available=True, **result)


@router.patch(
    "/cps/nodes/{node_id}",
    response_model=UpdateCpsNodeResponse,
    summary="updateCpsNode — Problem 또는 Solution 노드의 summary 단일 수정 (검수 게이트 Phase 3)",
)
@limiter.limit("20/minute")
async def update_cps_node_route(
    request: Request,
    node_id: str,
    payload: UpdateCpsNodeRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> UpdateCpsNodeResponse:
    """
    [2026-05 검수 게이트 Phase 3.1]
    CPS 그래프의 Problem 또는 Solution 노드 1개의 summary 수정.

    - cypher label whitelist: Problem | Solution 만 매칭 (다른 노드 보호)
    - ownership: 라우트 진입 시 assert_owns + cypher 자체에 project 필터 이중망
    - markdown 재합성은 별도 (Phase 3.5+). 이번엔 그래프 노드만 수정.
    - rate limit 20/min — 사용자가 여러 노드 빠르게 수정할 수 있어 mutation 보다 여유

    [응답]
    { id, label: 'Problem'|'Solution', summary }
    노드 없음 / 라벨 불일치 / project 불일치 모두 404.
    """
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    out = await q.update_cps_node(payload.project_name, node_id, payload.summary, team_id=payload.team_id or "")
    if out is None:
        raise HTTPException(
            status_code=404,
            detail="수정할 Problem/Solution 노드가 없습니다. 노드 ID 또는 프로젝트를 확인하세요.",
        )
    return UpdateCpsNodeResponse(**out)


@router.patch(
    "/prd/nodes/{node_id}",
    response_model=UpdateNodeResponse,
    summary="updatePrdNode — Epic 또는 Story 노드의 summary 단일 수정 (검수 게이트 Phase 3.2)",
)
@limiter.limit("20/minute")
async def update_prd_node_route(
    request: Request,
    node_id: str,
    payload: UpdateNodeRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> UpdateNodeResponse:
    """
    [2026-05 검수 게이트 Phase 3.2]
    PRD 그래프의 Epic 또는 Story 노드 1개의 summary 수정.

    Phase 3.1 (CPS node) 와 동일 정책:
    - cypher label whitelist: Epic | Story
    - project 필터 + 라우트 단 ownership 검증 (이중 IDOR 방어)
    - markdown 재합성 별도 (Phase 3.5+)
    - Screen 노드는 schema 가 다름 (name 필드) — 별도 PR

    [응답]
    { id, label: 'Epic'|'Story', summary }
    """
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    out = await q.update_prd_node(payload.project_name, node_id, payload.summary, team_id=payload.team_id or "")
    if out is None:
        raise HTTPException(
            status_code=404,
            detail="수정할 Epic/Story 노드가 없습니다. 노드 ID 또는 프로젝트를 확인하세요.",
        )
    return UpdateNodeResponse(**out)


@router.get(
    "/cps/nodes",
    response_model=NodeListResponse,
    summary="listCpsNodes — Problem/Solution 그래프 노드 ID 리스트 (검수 게이트 Phase 3.3)",
)
@limiter.limit("60/minute")
async def list_cps_nodes_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> NodeListResponse:
    """
    [2026-05 검수 게이트 Phase 3.3]
    FE 사이드바가 markdown 파싱 (display ID 'PRB-01') 대신 실제 그래프 ID
    ('prb_01_1') 로 PATCH /api/v2/cps/nodes/{id} 호출하기 위한 listing.
    """
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    nodes = await q.list_cps_nodes(project_name, team_id=team_id or "")
    return NodeListResponse(nodes=[NodeListItem(**n) for n in nodes])


@router.get(
    "/prd/nodes",
    response_model=NodeListResponse,
    summary="listPrdNodes — Epic/Story 그래프 노드 ID 리스트 (검수 게이트 Phase 3.3)",
)
@limiter.limit("60/minute")
async def list_prd_nodes_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> NodeListResponse:
    """
    [2026-05 검수 게이트 Phase 3.3]
    PRD 사이드바용 — CPS list 와 동일 패턴.
    """
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    nodes = await q.list_prd_nodes(project_name, team_id=team_id or "")
    return NodeListResponse(nodes=[NodeListItem(**n) for n in nodes])


@router.get(
    "/ddd",
    response_model=DddGraph,
    summary="getDDD — Bounded Context / Aggregate / Entity / Event + 관계",
)
@limiter.limit("60/minute")
async def get_ddd_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> DddGraph:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    # createDesign 실행 안 했으면 모든 컬렉션 빈 그래프 반환 (404 X — 정상 응답)
    return await q.get_ddd_graph(project_name, team_id=team_id or "")


@router.get(
    "/spack",
    response_model=SpackGraph,
    summary="getSpack — API / Entity / Policy + 관계",
)
@limiter.limit("60/minute")
async def get_spack_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> SpackGraph:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    return await q.get_spack_graph(project_name, team_id=team_id or "")


@router.get(
    "/architecture",
    response_model=ArchitectureGraph,
    summary="getArchitecture — Service / Database + CONNECTS_TO",
)
@limiter.limit("60/minute")
async def get_architecture_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> ArchitectureGraph:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    return await q.get_architecture_graph(project_name, team_id=team_id or "")


@router.get(
    "/meetings/logs",
    response_model=MeetingLog,
    summary="getMeetingLogs — 특정 (project, version) 의 미팅 로그",
)
@limiter.limit("60/minute")
async def get_meeting_log_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    version: str = Query(..., min_length=1),
    current_user: UserPublic = Depends(get_current_user),
) -> MeetingLog:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    out = await q.get_meeting_log(project_name, version, team_id=team_id or "")
    if out is None:
        raise HTTPException(
            status_code=404,
            detail=f"미팅 로그를 찾을 수 없습니다: project={project_name}, version={version}",
        )
    return out


@router.get(
    "/meetings/versions",
    response_model=List[MeetingVersion],
    summary="getMeetingVersions — 프로젝트의 모든 미팅 버전 목록 (version ASC 정렬)",
)
@limiter.limit("60/minute")
async def get_meeting_versions_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> List[MeetingVersion]:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    return await q.get_meeting_versions(project_name, team_id=team_id or "")


@router.get(
    "/projects/timeline",
    response_model=ProjectTimeline,
    summary="프로젝트 최근 활동 — 미팅/CPS/PRD/Lint/Lineage/Repo 이벤트 통합",
)
@limiter.limit("60/minute")
async def get_project_timeline_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    days: int = Query(7, ge=1, le=90, description="조회 윈도우 (1~90일)"),
    limit: int = Query(30, ge=1, le=100),
    current_user: UserPublic = Depends(get_current_user),
) -> ProjectTimeline:
    # 본인 소유 프로젝트만 조회 가능
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    import time
    since_ms = int(time.time() * 1000) - days * 24 * 60 * 60 * 1000
    return await q.get_project_timeline(project_name, since_ms=since_ms, limit=limit, team_id=team_id or "")


@router.get(
    "/graph",
    response_model=ProjectGraph,
    summary="프로젝트 그래프 스냅샷 — Neo4j 직결 우회용 read-only proxy (project 격리)",
)
@limiter.limit("60/minute")
async def get_project_graph_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> ProjectGraph:
    """
    프론트의 그래프 시각화용. Neo4j 자격증명을 클라이언트에 노출하지 않도록
    BE 가 project 단위 격리된 노드/엣지를 반환한다.

    - 인증: JWT 필수.
    - 소유권: 본인 소유 프로젝트만 (assert_owns → 403).
    - 캡: 노드 500 / 엣지 2000 — 그 이상은 잘림.
    - 헤비 속성(embedding/full_markdown/raw_content) 은 응답에서 제거.
    """
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    return await q.get_project_graph(project_name, team_id=team_id or "")


@router.get(
    "/graph/screen",
    response_model=ProjectGraph,
    summary="화면 단위 PRD 서브그래프 — Screen + 연결된 Story + 포함 Epic 만",
)
@limiter.limit("60/minute")
async def get_screen_subgraph_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    screen_name: str = Query(..., min_length=1),
    current_user: UserPublic = Depends(get_current_user),
) -> ProjectGraph:
    """
    PRD 의 Screens 탭에서 화면별 그래프 시각화용. 프로젝트 전체 노드 대신
    선택한 화면(:Screen {name=screen_name}) 에 연결된 Story 와 그 상위 Epic 만
    반환 → 모달의 시각적 노이즈 제거.

    - 인증: JWT 필수.
    - 소유권: 본인 소유 프로젝트만 (assert_owns → 403).
    - Screen 미존재 또는 미연결 시 nodes=[] 로 응답 — FE 가 빈 상태 안내.
    - Screen 의 id 는 별도로 없어 응답에선 'screen:<name>' 으로 합성.
    """
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    return await q.get_screen_subgraph(project_name, screen_name, team_id=team_id or "")


@router.get(
    "/graph/lineage",
    response_model=ProjectGraph,
    summary="Design ↔ PRD Story lineage 서브그래프 — DERIVED_FROM 그래프",
)
@limiter.limit("60/minute")
async def get_design_lineage_graph_route(
    request: Request,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    focus_story_id: Optional[str] = Query(None, description="특정 Story 만 — 예: story_01_1"),
    current_user: UserPublic = Depends(get_current_user),
) -> ProjectGraph:
    """
    [D — 2026-05] Design 페이지의 Lineage Graph 탭용.

    Design 노드 (Entity/Aggregate/ArchService) 와 PRD Story 사이의
    DERIVED_FROM 관계를 그래프로 반환. focus_story_id 지정 시 해당 Story
    중심의 작은 서브그래프만.

    Edges:
      - DERIVED_FROM (Design → Story): properties.confidence + quote 보유
      - CONTAINS (Epic → Story): 컨텍스트 표시용

    - 인증: JWT 필수
    - 소유권: 본인 소유 프로젝트만 (assert_owns → 403)
    - 데이터 없으면 nodes=[] (FE 빈 상태 안내)
    """
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    return await q.get_design_lineage_graph(project_name, focus_story_id, team_id=team_id or "")
