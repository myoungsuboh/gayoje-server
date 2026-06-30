"""
[Phase E — 2026-05-25] eval-score 라우트.

프로젝트의 SPACK/DDD/Architecture 그래프를 4-tier 점수로 채점해 응답.
FE 가 design 패널에 충실도 카드 / 막대 차트로 표시하기 위한 endpoint.

설계:
- LLM 호출 없음 — Neo4j 에서 그래프 fetch + scorer 호출 만.
- 응답 시간 < 100ms (Neo4j 3회 fetch + Python 채점).
- 인증 + ownership 검사 (다른 v2 라우트와 동일 패턴).

응답:
{
  "project_name": "plant",
  "overall": 0.9825,
  "tier1": { "score": 1.0,    "weight": 0.10, "sub_metrics": {...} },
  "tier2": { "score": 1.0,    "weight": 0.40, "sub_metrics": {...}, "notes": [] },
  "tier3": { "score": 0.95,   "weight": 0.25, "sub_metrics": {...} },
  "tier4": { "score": 1.0,    "weight": 0.25, "sub_metrics": {...}, "notes": [...] },
  "summary": {
    "api_count": 5, "entity_count": 5, "policy_count": 2,
    "tier1": 1.0, "tier2": 1.0, "tier3": 0.95, "tier4": 1.0, "overall": 0.9825
  }
}
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api._quota_helpers import tracked_pipeline_context
from app.clients.gemini_client import GeminiError, gemini_error_to_http
from app.core import quota
from app.core.limiter import limiter
from app.core.security import get_current_user
from app.queue.client import enqueue_autofill_api_specs
from app.service import (
    ownership_repository,
    query_repository,
)
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["EvalScore"])


# ─── 응답 모델 ──────────────────────────────────────────────────────────


class TierScoreResponse(BaseModel):
    score: float = Field(..., ge=0.0, le=1.0)
    weight: float = Field(..., ge=0.0, le=1.0)
    sub_metrics: Dict[str, float] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)


class ViolationCodeItem(BaseModel):
    code: str
    count: int


class FixTargetItem(BaseModel):
    """[2026-05-28] 사용자가 콕 집어 고칠 구체적 보강 대상."""
    metric_key: str
    label: str
    tier: int
    # 빠진 개별 항목들 — {id, name}. 사용자에게 'API-01 작업 생성' 처럼 노출.
    missing: List[Dict[str, str]] = Field(default_factory=list)
    missing_total: int = 0
    total: int = 0
    fix: str = ""
    prd_section: str = ""
    # [2026-06-06] 이 target 을 다 채웠을 때 overall 이 약 몇 %p 오르는지(근사).
    delta_pct: int = 0


class EvalScoreResponse(BaseModel):
    project_name: str
    overall: float = Field(..., ge=0.0, le=1.0)
    tier1: TierScoreResponse
    tier2: TierScoreResponse
    tier3: TierScoreResponse
    tier4: TierScoreResponse
    summary: Dict[str, Any]
    # [k — 2026-05-25] Tier 4 위반 코드 상세 — 사용자가 \"무엇이 위반인지\" 확인.
    # summarize_reports 의 top_violation_codes (상위 5개) 그대로.
    top_violation_codes: List[ViolationCodeItem] = Field(default_factory=list)
    # [2026-05-28] 구체적 보강 대상 — 어느 API/Entity/화면이 무엇이 빠졌는지 이름까지.
    # 사용자 피드백 "정확히 어딜 고쳐야하는지 진짜 떠먹여줘야" 대응.
    fix_targets: List[FixTargetItem] = Field(default_factory=list)


# ─── handler ───────────────────────────────────────────────────────────


def _tier_to_response(tier: Any) -> TierScoreResponse:
    """ScoreReport 의 tier 객체 → TierScoreResponse (score/sub_metrics 소수 4자리 반올림)."""
    return TierScoreResponse(
        score=round(tier.score, 4),
        weight=tier.weight,
        sub_metrics={k: round(v, 4) for k, v in tier.sub_metrics.items()},
        notes=list(tier.notes),
    )


@router.get(
    "/projects/{project_name}/eval-score",
    response_model=EvalScoreResponse,
    summary="프로젝트 SPACK/DDD/Architecture 명세의 4-tier 충실도 점수",
)
async def get_eval_score(
    project_name: str,
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> EvalScoreResponse:
    """그래프 fetch + scorer 호출. 응답 시간 ~100ms 목표 (LLM 호출 X)."""
    if not project_name or not project_name.strip():
        raise HTTPException(status_code=400, detail="project_name 필수")
    await ownership_repository.assert_access(current_user.email, project_name, team_id)

    # 그래프 3종 병렬 fetch — Neo4j connection 1개 공유라 직렬도 OK
    spack = await query_repository.get_spack_graph(project_name, team_id=team_id or "")
    ddd = await query_repository.get_ddd_graph(project_name, team_id=team_id or "")
    arch = await query_repository.get_architecture_graph(project_name, team_id=team_id or "")

    # [2026-05-25 fix] Tier 4 (정합성) 가 항상 만점이던 버그.
    # 이전엔 validation_report=None → 만점 처리 → 명세 위반 다수여도 점수 미반영.
    # 이제 normalize_* 를 재호출해 실제 violation 수 받아 Tier 4 채점.
    # LLM 호출 없음 — 순수 검증 로직 (~10ms).
    from app.pipelines.design_validator import (
        normalize_architecture,
        normalize_ddd,
        normalize_spack,
        summarize_reports,
    )
    from evals.fix_targets import collect_fix_targets
    from evals.scorer import score_spack

    spack_dict = spack.model_dump()
    ddd_dict = ddd.model_dump()
    arch_dict = arch.model_dump()

    # [refactor 2026-06] get_*_graph dict ↔ normalize_* 입력 모양 불일치 보정(false-positive
    # 방지)을 design_validator.eval_backfill 로 추출. 행위 동일 (테스트: test_eval_backfill).
    from app.pipelines.design_validator.eval_backfill import backfill_graph_dicts

    backfill_graph_dicts(spack_dict, ddd_dict, arch_dict)

    # normalize_* 는 ValidationReport 도 반환 — 두 번째 인자는 normalized 결과.
    norm_spack, spack_report = normalize_spack(spack_dict)
    norm_ddd, ddd_report = normalize_ddd(ddd_dict, norm_spack)
    _, arch_report = normalize_architecture(arch_dict, norm_spack, norm_ddd)
    summary = summarize_reports(spack_report, ddd_report, arch_report)
    validation_report = {
        "total_errors": summary.get("total_errors", 0),
        "total_warnings": summary.get("total_warnings", 0),
        "total_infos": 0,  # summarize 가 INFO 미집계 — 점수에 영향 없음
    }

    report = score_spack(
        spack_dict,
        ddd=ddd_dict,
        arch=arch_dict,
        validation_report=validation_report,
    )

    # [k — 2026-05-25] top_violation_codes 추출 (summary 의 dict list → response model).
    top_codes_raw = summary.get("top_violation_codes") or []
    top_violations = [
        ViolationCodeItem(code=str(v.get("code", "")), count=int(v.get("count", 0)))
        for v in top_codes_raw
        if v.get("code")
    ]

    # [2026-05-28] 구체적 보강 대상 — 어느 항목이 무엇이 빠졌는지 이름까지.
    # normalize 결과(norm_spack/norm_ddd)를 써야 id 가 정규화된 상태(API-01 등)로 노출.
    fix_targets_raw = collect_fix_targets(norm_spack, ddd=norm_ddd, arch=arch_dict)

    # [2026-06-06] delta_pct — 이 target 을 다 채우면 overall 이 약 몇 %p 오르는지.
    # 스코어러의 실제 tier 객체(sub_metrics 개수 + weight + 현재 metric 값)로 산출.
    from evals.fix_targets import delta_pct_for

    _tier_objs = {1: report.tier1, 2: report.tier2, 3: report.tier3, 4: report.tier4}
    for ft in fix_targets_raw:
        tobj = _tier_objs.get(ft.get("tier"))
        if tobj is None:
            ft["delta_pct"] = 1
            continue
        now = float(tobj.sub_metrics.get(ft.get("metric_key"), 0.0))
        ft["delta_pct"] = delta_pct_for(now, len(tobj.sub_metrics), tobj.weight)

    fix_targets = [FixTargetItem(**ft) for ft in fix_targets_raw]

    return EvalScoreResponse(
        project_name=project_name,
        overall=round(report.overall, 4),
        tier1=_tier_to_response(report.tier1),
        tier2=_tier_to_response(report.tier2),
        tier3=_tier_to_response(report.tier3),
        tier4=_tier_to_response(report.tier4),
        summary=report.summary,
        top_violation_codes=top_violations,
        fix_targets=fix_targets,
    )


# ─── [AI 초안 보완 — 2026-05-29] API error_cases/auth 자동 보완 ──────────────


class AutofillApiSpecsRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None


class FilledApiSpecResponse(BaseModel):
    id: str
    error_cases: List[Dict[str, Any]] = Field(default_factory=list)
    auth: Dict[str, Any] = Field(default_factory=dict)
    generated: bool = False
    saved: bool = False


class AutofillApiSpecsResponse(BaseModel):
    status: str
    # [2026-05 비동기 전환] enqueue 즉시 반환 — FE 가 task_id 로 폴링.
    task_id: Optional[str] = None
    apis: List[FilledApiSpecResponse] = Field(default_factory=list)
    meta: Optional[Dict[str, Any]] = None


@router.post(
    "/pipelines/autofill_api_specs",
    response_model=AutofillApiSpecsResponse,
    summary=(
        "autofillApiSpecs — error_cases/auth 가 빈 API 에 AI 초안 자동 생성 (병렬). "
        "생성 항목은 [AI 초안]으로 마킹돼 검토 전엔 완성도 점수 절반만 인정."
    ),
)
@limiter.limit("3/minute")
async def autofill_api_specs_route(
    request: Request,
    payload: AutofillApiSpecsRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> AutofillApiSpecsResponse:
    # 본인 소유 프로젝트만 — 타인 API 로 LLM 호출 + 본인 quota 소진 abuse 방어.
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    task_id = str(uuid.uuid4())
    # 토큰 한도 가드 — LLM 호출 전 차단.
    await quota.assert_tokens_within_limit(current_user.email)

    # [2026-05 비동기 전환] 이전엔 N개 병렬 LLM 을 동기 HTTP 요청 안에서 돌려
    # axios 180s / 프록시 한계에 걸려 "결국 실패" 했다. 이제 enqueue 즉시 반환 →
    # worker 가 job_timeout 안에서 처리 + stage 마커 emit → FE 폴링/진행바 작업량 기반.
    try:
        await enqueue_autofill_api_specs(
            task_id=task_id,
            project_name=payload.project_name,
            user_email=current_user.email,  # quota 토큰 누적 + 등급별 큐 라우팅
            team_id=payload.team_id or "",  # 프로젝트 게이트 + SPACK 조회 스코프 일치
        )
    except HTTPException:
        raise  # [2026-06] 동시성 429 등 의도된 HTTP 에러는 503 으로 가리지 말고 그대로 전파
    except Exception as e:  # noqa: BLE001
        logger.exception("enqueue autofill_api_specs failed (task=%s)", task_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {e}",
        ) from e

    return AutofillApiSpecsResponse(status="accepted", task_id=task_id)


# ─── [완성도 어시스턴트 — 2026-06-06] AI 초안 검토 완료 → 만점 반영 ───────────


class MarkReviewedRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None


class MarkReviewedResponse(BaseModel):
    success: bool
    api_id: Optional[str] = None


class MarkAllReviewedResponse(BaseModel):
    success: bool
    updated: int = 0


@router.post(
    "/spack/{api_id}/mark-reviewed",
    response_model=MarkReviewedResponse,
    summary="단일 API 의 AI 초안 error_cases/auth 를 검토 완료 처리(→ 완성도 만점 반영)",
)
async def mark_api_reviewed_route(
    api_id: str,
    payload: MarkReviewedRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> MarkReviewedResponse:
    await ownership_repository.assert_access(
        current_user.email, payload.project_name, payload.team_id
    )
    ok = await query_repository.mark_api_reviewed(
        payload.project_name, api_id, team_id=payload.team_id or ""
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"API 를 찾을 수 없습니다: {api_id}")
    return MarkReviewedResponse(success=True, api_id=api_id)


@router.post(
    "/spack/mark-reviewed-all",
    response_model=MarkAllReviewedResponse,
    summary="프로젝트의 모든 AI 초안 API 명세를 일괄 검토 완료 처리(→ 완성도 만점 반영)",
)
async def mark_all_apis_reviewed_route(
    payload: MarkReviewedRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> MarkAllReviewedResponse:
    await ownership_repository.assert_access(
        current_user.email, payload.project_name, payload.team_id
    )
    updated = await query_repository.mark_all_apis_reviewed(
        payload.project_name, team_id=payload.team_id or ""
    )
    return MarkAllReviewedResponse(success=True, updated=updated)
