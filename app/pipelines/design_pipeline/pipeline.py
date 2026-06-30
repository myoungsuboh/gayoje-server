from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.pipelines.base import PipelineContext
from app.pipelines.design_validator import (
    normalize_architecture,
    normalize_ddd,
    normalize_spack,
    summarize_reports,
)
from .types import DesignInput, DesignResult
from .prd import (
    fetch_master_prd,
    extract_prd_sections,
    _extract_prd_story_ids,
    detect_dirty_prd,
)
from .agents import call_spack_agent, call_ddd_agent, call_architecture_agent
from .cypher import (
    build_save_spack_query,
    build_save_ddd_query,
    build_save_architecture_query,
    build_reset_design_stale_query,
)
from .ddd_filter import filter_ddd_for_codegen
from .arch_context import (
    slim_spack_for_ddd,
    slim_spack_for_arch,
    slim_ddd_for_arch,
)

logger = logging.getLogger(__name__)


class DesignPipelineCancelled(Exception):
    """
    클라이언트가 createSpack 호출을 중간에 끊었을 때 raise.

    [중지 정책 — 2026-05-18]
    백엔드의 최종 Neo4j 트랜잭션은 모든 LLM stage 가 끝난 뒤에 한 번에 실행
    되므로(pipeline.py 끝부분), commit 전에 이 예외를 raise 하면 기존
    Spack/DDD/Architecture 데이터가 그대로 보존된다. 라우트 핸들러가
    이 예외를 잡아 "cancelled" 응답을 돌려준다.
    """

    pass


class DesignQuotaExceeded(Exception):
    """
    파이프라인 진행 중(stage 사이) 사용자가 토큰 한도를 초과했을 때 raise.

    [2026-05 비용 가드]
    Design 은 SPACK→DDD→Architecture 3개의 비싼 LLM 을 순차 실행한다(한 사이클
    ~105K tokens). 라우트의 사전 atomic quota 체크는 enqueue 시점 1회뿐이라,
    SPACK 단계에서 한도를 넘겨도 DDD·Architecture LLM 이 그대로 더 호출되어
    한도를 크게 초과할 수 있었다. stage 사이마다 한도를 재확인해, 초과 시 다음
    (더 비싼) LLM 호출 *전에* bail 한다. DesignPipelineCancelled 와 동일하게
    최종 트랜잭션 전이라 기존 Spack/DDD/Architecture 데이터는 보존된다.
    잡이 이 예외를 잡아 "quota_exceeded" 응답으로 변환한다.
    """

    pass


class DesignPrecheckFailed(Exception):
    """
    PRD 가 누더기(degenerate) + 과대해 설계를 진행하면 SPACK/DDD/Architecture LLM 이
    느려 timeout 날 게 확실할 때, 진입 전에 fail-fast 로 raise.

    [2026-05-28] 운영 사고: 22개 회의 누적 + spec 없는 placeholder 로 PRD 가 누더기
    (중복 Product Vision 6+, Section 2·3 대규모 불일치, ~35KB). design 이 Architecture
    단계에서 10분 매달리다 crash. cleanup(Stage 1.5) 시도 후에도 dirty+거대면 즉시
    명확한 안내로 중단 → 시간·토큰 낭비 차단. 최종 트랜잭션 전이라 기존 설계 보존.
    라우트/잡이 이 예외를 잡아 "precheck_failed" 응답으로 변환한다.
    """

    def __init__(self, message: str, *, diagnostic: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic or {}


async def _maybe_auto_cleanup_dirty_prd(
    ctx: PipelineContext,
    project_name: str,
    prd_content: str,
) -> Dict[str, Any]:
    """
    Master PRD 가 dirty 면 cleanup pipeline 호출, 깨끗하면 no-op.

    [정책 — 2026-05-26]
    - dirty 판정은 detect_dirty_prd 의 결정적 trigger 만 사용 (Product Vision 5+ 또는
      Section 2/3 mismatch 3+). 정상 PRD 에 cleanup 발동 차단.
    - cleanup 실패 시 (LLM 오류 / reconcile guard raise) 원본 PRD 로 fall-through —
      design 결과 받는 게 사용자에게 우선. warning 만 logging.
    - cleanup 성공 시 master PRD update (별 트랜잭션) + cleaned content in-memory 반환.
    - diagnostic 으로 FE 가 toast 표시 ("PRD 자동 정리 완료" 등).

    Returns:
        {
            "attempted": bool,           # dirty 감지되어 cleanup 시도 여부
            "applied": bool,             # 실 적용 여부 (성공)
            "cleaned_content": str,      # applied=True 일 때만 비어있지 않음
            "reduction_pct": int,        # cleanup 의 축소율 (applied 시)
            "dirty_diagnostic": dict,    # detect_dirty_prd 결과
            "failure_reason": str|None,  # cleanup 실패 시 이유
        }
    """
    dirty = detect_dirty_prd(prd_content)
    if not dirty["is_dirty"]:
        return {
            "attempted": False, "applied": False, "cleaned_content": "",
            "reduction_pct": 0, "dirty_diagnostic": dirty["diagnostic"],
            "failure_reason": None,
        }

    logger.info(
        "design auto-cleanup triggered: project=%s reasons=%s diag=%s",
        project_name, dirty["reasons"], dirty["diagnostic"],
    )

    # 순환 import 회피 — 함수 안에서 lazy import.
    from app.pipelines.cleanup_master_prd_pipeline import (
        CleanupMasterPrdInput,
        run_cleanup_master_prd_pipeline,
    )

    try:
        cleanup_result = await run_cleanup_master_prd_pipeline(
            ctx,
            CleanupMasterPrdInput(
                project_name=project_name,
                # user_email 은 design pipeline 의 ctx 에 직접 안 들어와 있음. quota
                # tracking 은 design pipeline 호출자가 이미 처리 — diagnostic 만 필요.
                user_email="<auto_cleanup>",
                dry_run=False,  # 자동화 — master PRD 직접 update.
                team_id=ctx.team_id,  # [멀티테넌시] 팀 master PRD 스코프 유지.
            ),
        )
    except (ValueError, RuntimeError) as e:
        # cleanup 실패 — design 은 원본으로 계속. logger 에 명시.
        logger.warning(
            "design auto-cleanup failed (fall-through to original PRD): "
            "project=%s error=%s",
            project_name, e,
        )
        return {
            "attempted": True, "applied": False, "cleaned_content": "",
            "reduction_pct": 0, "dirty_diagnostic": dirty["diagnostic"],
            "failure_reason": str(e),
        }

    # cleanup 적용 후 cleaned content 를 in-memory 로 받기 위해 1차례 더 fetch.
    # alternative: cleanup pipeline 이 cleaned content 도 반환하게 schema 변경.
    # → 후자가 깔끔하지만 PR 작아지려고 transient fetch.
    refreshed = await fetch_master_prd(ctx, project_name)
    cleaned_content = refreshed.get("prd_content") or ""
    logger.info(
        "design auto-cleanup applied: project=%s reduction=%d%% before=%d after=%d",
        project_name, cleanup_result.reduction_pct,
        cleanup_result.before_size, cleanup_result.after_size,
    )
    return {
        "attempted": True,
        "applied": True,
        "cleaned_content": cleaned_content,
        "reduction_pct": cleanup_result.reduction_pct,
        "dirty_diagnostic": dirty["diagnostic"],
        "failure_reason": None,
    }


# [2026-05-28 fail-fast] 정상 PRD 는 ~15-25KB. 그 이상 + 누더기면 설계 LLM 이 느려
# timeout. 이 임계는 post-meeting auto-cleanup 의 _CLEANUP_SIZE_THRESHOLD(30KB)와 정렬.
_DESIGN_PRECHECK_MAX_BYTES = 30_000


def _design_precheck(prd_content: str) -> Optional[Dict[str, Any]]:
    """설계 진입 전 PRD 건전성 체크 (cleanup 시도 이후 content 기준).

    **dirty(누더기) AND 과대(>30KB)** 일 때만 fail-fast 사유 반환 — 둘 다일 때가
    SPACK/DDD/Architecture LLM 이 느려 timeout 나는 운영 사고 케이스다. 작은 dirty
    PRD(설계 빨리 됨)나 거대 clean PRD(정상 큰 프로젝트)는 None → 정상 진행.

    Returns:
        None (정상 진행) 또는 {reason, size_bytes, dirty_diagnostic, reasons}.
    """
    size = len((prd_content or "").encode("utf-8"))
    if size <= _DESIGN_PRECHECK_MAX_BYTES:
        return None
    dirty = detect_dirty_prd(prd_content)
    if not dirty["is_dirty"]:
        return None
    return {
        "reason": (
            f"PRD 가 정리되지 않은 누더기 상태(크기 {size:,}바이트)라 설계를 생성하면 "
            "시간 초과됩니다. 중복된 Product Vision·NFR 통합과 정의되지 않은 화면/Story "
            "정리가 필요합니다. PRD 를 정리(또는 양질의 회의록으로 재생성)한 뒤 다시 "
            "시도해 주세요."
        ),
        "size_bytes": size,
        "dirty_diagnostic": dirty.get("diagnostic", {}),
        "reasons": dirty.get("reasons", []),
    }


def _compute_lineage_coverage(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    노드 list 의 lineage.confidence 분포 계산.

    [B — 2026-05] design 페이지 상단의 'lineage 채움률' 표시 데이터.

    Returns:
        {
            "total": 10,
            "direct": 7,
            "inferred": 2,
            "none": 1,
            "coverage_pct": 90,   # direct + inferred / total * 100, 반올림
        }
    빈 list 면 모두 0.
    """
    total = len(items)
    if total == 0:
        return {"total": 0, "direct": 0, "inferred": 0, "none": 0, "coverage_pct": 0}
    counts = {"direct": 0, "inferred": 0, "none": 0}
    for it in items:
        c = (it.get("lineage") or {}).get("confidence") or "none"
        if c not in counts:
            c = "none"
        counts[c] += 1
    covered = counts["direct"] + counts["inferred"]
    return {
        "total": total,
        **counts,
        "coverage_pct": round(covered / total * 100),
    }


async def run_design_pipeline(
    ctx: PipelineContext,
    payload: DesignInput,
    *,
    check_cancel: Optional[Callable[[], Awaitable[bool]]] = None,
    check_over_quota: Optional[Callable[[], Awaitable[bool]]] = None,
    on_spack_ready: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
) -> DesignResult:
    """
    Get PRD → Section Extract → Spack → DDD → Architecture.

    Strict sequential: DDD 가 Spack 결과를, Architecture 가 Spack+DDD 결과를
    프롬프트에 직접 받아야 하므로 병렬화 불가능.

    3개 LLM 호출이 모두 완료된 다음 run_in_transaction 으로
    Spack / DDD / Architecture Write 를 단일 트랜잭션에 묶어 원자적으로 실행.

    [중지 지원 — 2026-05-18]
    check_cancel 은 stage 사이마다 호출되는 awaitable predicate. True 면
    DesignPipelineCancelled 를 raise 해 chain 을 즉시 종료. 최종 트랜잭션
    이전에 빠져나가므로 기존 데이터는 보존된다.
    라우트 핸들러가 request.is_disconnected 를 전달한다.

    [on_spack_ready — 2026-06-10 병렬 autofill]
    SPACK 정규화가 끝나는 즉시 (정규화된 apis list 로) 동기 호출되는 훅.
    잡(design_pipeline_job)이 이걸로 API error_cases/auth autofill LLM 을
    DDD/Architecture 단계와 **병렬**로 시작한다 — autofill 은 각 API 의
    method/endpoint/description 만 입력으로 쓰므로 뒤 단계와 독립. 훅 실패는
    설계를 깨지 않는다 (swallow).
    """
    logger.info(
        "design pipeline start: project=%s key=%s",
        payload.project_name,
        ctx.idempotency_key,
    )
    # [2026-06-10 관측성] stage 별 소요 시간 — "최신 업데이트 왜 오래 걸리나"의
    # 실측 근거. 종료 시 한 줄 요약 로그로 남긴다.
    _timing: Dict[str, float] = {}
    _t_total = time.monotonic()

    async def _bail_if_cancelled(stage_label: str) -> None:
        if check_cancel is None:
            return
        if await check_cancel():
            logger.info(
                "design pipeline cancelled before %s: project=%s",
                stage_label,
                payload.project_name,
            )
            raise DesignPipelineCancelled(stage_label)

    async def _bail_if_over_quota(stage_label: str) -> None:
        # [2026-05 비용 가드] 다음(더 비싼) LLM 호출 전에 토큰 한도 재확인.
        # 최종 트랜잭션 전이라 기존 설계 데이터는 보존된다.
        if check_over_quota is None:
            return
        if await check_over_quota():
            logger.warning(
                "design pipeline bail (over token quota) before %s: project=%s",
                stage_label,
                payload.project_name,
            )
            raise DesignQuotaExceeded(stage_label)

    # Stage 1: Get PRD
    _t = time.monotonic()
    prd_row = await fetch_master_prd(ctx, payload.project_name)
    prd_content = prd_row.get("prd_content") or ""
    master_prd_id = prd_row.get("master_prd_id")

    # Stage 1.5: Auto-cleanup dirty PRD (2026-05-26)
    # design 단계 직전 dirty PRD 감지. dirty 면 cleanup pipeline 호출 → cleaned content
    # 사용 + master PRD update. 사용자는 별도 클릭 없이 자동 정리.
    # 실패 시 (LLM 오류 / reconcile guard raise) 원본 PRD 로 fall-through —
    # design 결과 받는 게 우선.
    auto_cleanup_info = await _maybe_auto_cleanup_dirty_prd(
        ctx, payload.project_name, prd_content,
    )
    if auto_cleanup_info["applied"]:
        # cleanup 성공 — cleaned content 사용. master_prd_id 는 update 가 같은 노드를
        # in-place 갱신하므로 그대로.
        prd_content = auto_cleanup_info["cleaned_content"]

    # Stage 1.6: fail-fast precheck (2026-05-28)
    # cleanup 시도 후에도 PRD 가 누더기 + 과대면, 느린 SPACK/DDD/Architecture LLM 으로
    # 진입해 10분 매달리다 crash 하는 대신 즉시 명확한 안내로 중단. 최종 트랜잭션 전이라
    # 기존 설계 데이터는 보존된다. (cleanup 이 살릴 수 있는 PRD 는 위에서 이미 정리됨.)
    precheck = _design_precheck(prd_content)
    if precheck is not None:
        logger.warning(
            "design precheck failed (degenerate+oversized PRD): project=%s size=%dB reasons=%s",
            payload.project_name, precheck["size_bytes"], precheck.get("reasons"),
        )
        raise DesignPrecheckFailed(precheck["reason"], diagnostic=precheck)

    _timing["prepare"] = time.monotonic() - _t  # PRD fetch + cleanup + precheck

    # Stage 2: PRD Section Extractor
    inputs, section_diag = extract_prd_sections(prd_content)

    # [A — 2026-05] PRD Story IDs set — normalize_* 에 전달해 lineage 의 fake
    # story_id 자동 drop. extract_prd_sections 후 PRD 원본에서 추출 (Epic Map
    # 안에 모든 Story 가 들어있음).
    valid_story_ids = _extract_prd_story_ids(prd_content)
    section_diag["valid_story_count"] = len(valid_story_ids)

    # Stage 3: Spack — LLM 출력 후 normalize (정렬 + ID 재부여 + intra-검증)
    await _bail_if_cancelled("spack_llm")
    # [progress] FE 진행바가 경과시간이 아닌 실제 단계 기반으로 차도록 stage 마커 emit.
    await ctx.emit_stage("design:spack")
    _t = time.monotonic()
    spack_raw = await call_spack_agent(ctx, inputs["spack_input"])
    spack, spack_report = normalize_spack(spack_raw, valid_story_ids=valid_story_ids)
    _timing["spack"] = time.monotonic() - _t
    # downstream LLM 에 보낼 때는 _id_remap 같은 내부 키 제거
    spack_for_llm = {k: v for k, v in spack.items() if not k.startswith("_")}

    # [2026-06-10 병렬 autofill] SPACK 결과가 확정되는 즉시 훅 호출 — 잡이
    # error_cases/auth autofill 생성을 DDD/Architecture LLM 과 병렬로 시작한다.
    # (저장은 잡이 최종 트랜잭션 이후에 수행 — wipe-and-redraw 순서 보장.)
    if on_spack_ready is not None:
        try:
            on_spack_ready(list(spack_for_llm.get("apis") or []))
        except Exception:  # noqa: BLE001 — 훅 실패가 설계를 깨지 않게
            logger.exception(
                "design on_spack_ready hook 실패 — autofill 병렬화만 생략: project=%s",
                payload.project_name,
            )
    # [멀티테넌시] 저장되는 SPACK/DDD/Architecture 노드의 project property 는 스코프
    # 키 — get_ddd/spack/arch_graph · lineage fetch 가 team_id 로 격리 조회. LLM 입력은
    # 깨끗한 이름 유지(여기 build_save_* 는 project property 만 설정).
    from app.core.project_scope import scoped_project
    db_project = scoped_project(payload.project_name, ctx.team_id)
    spack_query, spack_params = build_save_spack_query(db_project, spack_for_llm)

    # Stage 4: DDD — Spack 정규화 결과를 입력으로 + cross-검증
    # [2026-05-27 성능] DDD 에이전트는 SPACK Entity 명칭·설명·ID만 사용 →
    # APIs/policies/screens/entity attributes 제거한 slim Spack 전달.
    await _bail_if_cancelled("ddd_llm")
    await _bail_if_over_quota("ddd_llm")
    await ctx.emit_stage("design:ddd")
    spack_json_for_ddd = json.dumps(slim_spack_for_ddd(spack_for_llm), ensure_ascii=False)
    _t = time.monotonic()
    ddd_raw = await call_ddd_agent(ctx, inputs["ddd_input"], spack_json_for_ddd)
    ddd, ddd_report = normalize_ddd(ddd_raw, spack, valid_story_ids=valid_story_ids)
    _timing["ddd"] = time.monotonic() - _t
    ddd_for_llm = {k: v for k, v in ddd.items() if not k.startswith("_")}
    # [2026-05-27] "전시 vs 코드-입력" 분리 — Architecture 에이전트 입력에서만
    # confidence=none DDD(PRD 근거 없음)를 제외해 코드 오염 차단. 저장(ddd_query)·
    # 응답(ddd_for_llm)·화면은 원본 유지. inferred 는 남기고 프롬프트가 '추정' 처리.
    ddd_for_arch = filter_ddd_for_codegen(ddd_for_llm)
    ddd_query, ddd_params = build_save_ddd_query(db_project, ddd_for_llm)

    # Stage 5: Architecture — Spack + DDD 정규화 결과를 입력으로 + cross-검증
    await _bail_if_cancelled("architecture_llm")
    await _bail_if_over_quota("architecture_llm")
    await ctx.emit_stage("design:architecture")
    # [2026-05-27 성능] Architecture 에이전트에 slim 버전만 전달.
    # api_service_mapping + owned_aggregates 결정에 필요한 식별 정보만.
    # 저장·화면 데이터는 원본 그대로 — Architecture 입력만 slim 적용.
    spack_json_for_arch = json.dumps(slim_spack_for_arch(spack_for_llm), ensure_ascii=False)
    ddd_json_for_arch = json.dumps(slim_ddd_for_arch(ddd_for_arch), ensure_ascii=False)
    _t = time.monotonic()
    arch_raw = await call_architecture_agent(
        ctx,
        inputs["arch_input"],
        spack_json_for_arch,
        ddd_json_for_arch,
    )
    arch, arch_report = normalize_architecture(
        arch_raw, spack, ddd, valid_story_ids=valid_story_ids,
    )
    _timing["architecture"] = time.monotonic() - _t
    arch_for_save = {k: v for k, v in arch.items() if not k.startswith("_")}
    arch_query, arch_params = build_save_architecture_query(db_project, arch_for_save)

    # 최종 commit 직전 마지막 cancel 체크 — 여기서 통과하면 데이터가 바뀜.
    await _bail_if_cancelled("final_commit")
    await ctx.emit_stage("design:saving")

    # [2026-05-27 #3] 빈 생성 결과 감지 — LLM 이 노드를 못 뽑았는지 layer 별 판정.
    # dirty PRD underextract 등으로 SPACK/DDD 가 비면 build_save_* 가 wipe 를
    # 건너뛰어 기존 데이터를 보존 (cypher.py 가드). 여기서는 그 사실을 진단으로
    # 노출하고 stale reset 여부를 결정.
    spack_empty = not (
        spack_for_llm.get("apis")
        or spack_for_llm.get("entities")
        or spack_for_llm.get("policies")
    )
    ddd_empty = not (
        ddd_for_llm.get("contexts")
        or ddd_for_llm.get("aggregates")
        or ddd_for_llm.get("entities")
        or ddd_for_llm.get("events")
    )
    arch_empty = not (
        arch_for_save.get("services") or arch_for_save.get("databases")
    )
    empty_generation = {
        "spack": spack_empty,
        "ddd": ddd_empty,
        "architecture": arch_empty,
    }

    # 3개 Write를 단일 트랜잭션으로 묶어 원자성 보장 — 부분 기록 방지.
    # [Phase 3.6] 마지막에 Project.design_source_stale=false 도 같이 — design
    # 재생성 성공 = 옛 PRD 기준에서 벗어남. 트랜잭션 묶으면 design 저장 실패 시
    # stale 도 안 풀려 정합성 유지.
    # [2026-06-01] stale 디커플 — design_source_stale 는 "옛 PRD 기준" 신호일 뿐
    # 완성도(빈 layer)와 별개다. 재생성이 성공한 시점에서 설계는 (일부 layer 가
    # 비었더라도) 최신 PRD 기준으로 다시 만들어진 것이므로 stale 는 무조건 해제한다.
    # 불완전은 diagnostic.empty_generation 으로 별도 노출 → FE 가 emptyGenNotice
    # 로 "일부 생성 안 됨, PRD 보강 후 재생성" 안내. (이전엔 빈 생성 시 stale 를
    # 유지해서, 사용자가 최신 PRD 로 막 재생성했는데도 "옛 PRD 기준" 배너가
    # 안 사라지는 모순 — 완성도와 최신성을 혼동한 버그였음.)
    design_complete = not (spack_empty or ddd_empty or arch_empty)
    # [2026-06-05] name-only 스코핑 (scoped name 이 팀 격리 담당). 이전엔 owner_email
    # 조건 + email="" 전달로 reset 이 no-op 이라 stale 배너가 재생성 후에도 부활했음.
    reset_query, reset_params = build_reset_design_stale_query(db_project)
    operations = [
        (spack_query, spack_params),
        (ddd_query, ddd_params),
        (arch_query, arch_params),
        (reset_query, reset_params),
    ]
    if not design_complete:
        logger.warning(
            "design pipeline empty generation — stale 는 해제하되 불완전은 진단으로 노출: "
            "project=%s empty=%s",
            payload.project_name, empty_generation,
        )
    _t = time.monotonic()
    await ctx.neo4j.run_in_transaction(operations)
    _timing["save"] = time.monotonic() - _t

    # [2026-06-10 관측성] stage 별 소요 — 느린 단계 특정용 한 줄 요약.
    logger.info(
        "design stage timing: project=%s prepare=%.1fs spack=%.1fs ddd=%.1fs "
        "arch=%.1fs save=%.1fs total=%.1fs",
        payload.project_name,
        _timing.get("prepare", 0.0),
        _timing.get("spack", 0.0),
        _timing.get("ddd", 0.0),
        _timing.get("architecture", 0.0),
        _timing.get("save", 0.0),
        time.monotonic() - _t_total,
    )

    # ─ 정합성 종합 보고 ─
    design_health = summarize_reports(spack_report, ddd_report, arch_report)
    if design_health["total_errors"] > 0:
        logger.warning(
            "design pipeline finished WITH errors: project=%s errors=%d warnings=%d",
            payload.project_name,
            design_health["total_errors"],
            design_health["total_warnings"],
        )

    # [B — 2026-05] lineage 채움률 metric — 각 노드 종류별 confidence 분포.
    # FE 가 design 페이지 상단에 "Entity 8/10 direct, 1/10 inferred, 1/10 none"
    # 같은 형태로 표시. '핵심 기능' 가시성 ↑.
    # [C — 2026-05] DomainEntity / Database 도 lineage 적용 → coverage 포함
    lineage_coverage = {
        "entity": _compute_lineage_coverage(spack_for_llm.get("entities") or []),
        "aggregate": _compute_lineage_coverage(ddd_for_llm.get("aggregates") or []),
        "domain_entity": _compute_lineage_coverage(ddd_for_llm.get("entities") or []),
        "service": _compute_lineage_coverage(arch_for_save.get("services") or []),
        "database": _compute_lineage_coverage(arch_for_save.get("databases") or []),
    }

    # [2026-05] top-level health 필드 — FE 가 즉시 빨간 배지/경고 배지 표시.
    # diagnostic.design_health 는 디버그용 detailed 그대로 유지 (운영 분석용).
    health_summary = {
        "total_errors": design_health.get("total_errors", 0),
        "total_warnings": design_health.get("total_warnings", 0),
        "has_errors": design_health.get("total_errors", 0) > 0,
        "has_warnings": design_health.get("total_warnings", 0) > 0,
        # 카테고리별 violation code top — FE 가 "어느 영역 깨졌는지" 표시
        "top_violation_codes": design_health.get("top_violation_codes", []),
        # [B — 2026-05] PRD lineage 채움률
        "lineage_coverage": lineage_coverage,
    }

    return DesignResult(
        project_name=payload.project_name,
        master_prd_id=master_prd_id,
        spack=spack_for_llm,
        ddd=ddd_for_llm,
        architecture=arch_for_save,
        health=health_summary,
        diagnostic={
            "section_extractor": section_diag,
            "spack": {
                "api_count": len(spack_for_llm.get("apis") or []),
                "entity_count": len(spack_for_llm.get("entities") or []),
                "policy_count": len(spack_for_llm.get("policies") or []),
            },
            "ddd": {
                "context_count": len(ddd_for_llm.get("contexts") or []),
                "aggregate_count": len(ddd_for_llm.get("aggregates") or []),
                "entity_count": len(ddd_for_llm.get("entities") or []),
                "event_count": len(ddd_for_llm.get("events") or []),
                # [2026-05-27] 전시(원본) 대비 코드-입력에서 제외된 none-confidence
                # 노드 수 — "전시 vs 코드 입력" 차이를 운영에서 가시화.
                "codegen_filtered_out": (
                    len(ddd_for_llm.get("aggregates") or [])
                    + len(ddd_for_llm.get("entities") or [])
                    - len(ddd_for_arch.get("aggregates") or [])
                    - len(ddd_for_arch.get("entities") or [])
                ),
            },
            "architecture": {
                "service_count": len(arch_for_save.get("services") or []),
                "database_count": len(arch_for_save.get("databases") or []),
                "connection_count": len(arch_for_save.get("connections") or []),
            },
            "design_health": design_health,
            # [2026-05-26] FE toast/UX 트리거 — design 완료 후 PRD 가 자동
            # 정리되었으면 "PRD 자동 정리 (N% 축소) + 설계 생성 완료" 표시.
            "auto_cleanup": auto_cleanup_info,
            # [2026-05-27 #3] layer 별 빈 생성 여부 — FE 가 "SPACK/DDD 가 생성되지
            # 않았습니다" 명확한 안내 + 원인(PRD Epic/Story 부족, auto_cleanup 결과)
            # 표시. 빈 layer 는 기존 데이터 보존(또는 비어있음) + stale 유지.
            "empty_generation": empty_generation,
        },
    )
