"""
arq job functions.

규칙:
  - 모든 job 인자는 직렬화 가능 (pickle/json) — dataclass 도 OK.
  - 외부 리소스(Gemini/Neo4j)는 worker 의 `on_startup` 에서 한 번만 구성.
  - 실패해도 arq 가 자동 재시도하므로 멱등성 키(`_job_id`)를 활용.

PR3:
  - post_meeting_pipeline_job: CPS + PRD 체이닝 (postMeeting 엔드포인트).
  - prd_pipeline_job: PRD 단독 (cps_graph 직접 입력).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Dict, List, Optional

from app.clients import neo4j_client
from app.clients.gemini_client import GeminiClient, GeminiError, TokenAccumulator, TrackedGemini
from app.core import quota
from app.core.config import settings
# [2026-06] 멀티디바이스 이중작업 — 같은 프로젝트 master 에 대한 동시 merge 가
# lost update 를 일으키지 않도록 프로젝트(scoped key) 단위 쓰기 잠금.
from app.core.master_lock import MasterLockTimeout, master_write_lock
from app.core.project_scope import scoped_project
# subscription 상수는 settings 평가 안 거치는 lightweight module 에서 직접 import.
# user_repository 경로 (token_encryption → config) 회피.
from app.core.subscription import SUBSCRIPTION_FREE
from app.pipelines.base import PipelineContext
from app.service import usage_repository
from app.pipelines.cleanup_master_prd_pipeline import (
    CleanupMasterPrdInput,
    run_cleanup_master_prd_pipeline,
)
from app.pipelines.cps_pipeline import (
    CpsInput,
    run_cps_extract,
    run_cps_merge,
    run_cps_pipeline,
)
from app.pipelines.delete_pipeline import (
    DeleteMeetingInput,
    run_delete_meeting_pipeline,
)
from app.pipelines.create_md_pipeline import (
    CreateMdInput,
    run_create_md_pipeline,
)
from app.pipelines.design_pipeline import (
    DesignInput,
    DesignPipelineCancelled,
    DesignPrecheckFailed,
    DesignQuotaExceeded,
    run_design_pipeline,
)
from app.pipelines.fix_spec_pipeline import FixSpecInput, run_fix_spec_pipeline
from app.pipelines.github_onboard_pipeline import (
    GithubOnboardInput,
    run_github_onboard_pipeline,
)
from app.clients.github_client import GitHubClient
from app.pipelines.lineage_pipeline import LineageInput, run_lineage_pipeline
from app.pipelines.lint_pipeline import LintInput, run_lint_pipeline
from app.pipelines.prd_pipeline import (
    PrdInput,
    parse_cps_for_prd,
    run_prd_extract,
    run_prd_pipeline,
    _prd_merge_compute,
)
from app.queue.extract_cache import (
    extract_cache_key,
    get_cached_extract,
    set_cached_extract,
    try_acquire_extract_lock,
    wait_for_cached_extract,
)
from app.pipelines.skill_recommend_pipeline import (
    CatalogEntry,
    RecommendInput,
    run_skill_recommend_pipeline,
)

logger = logging.getLogger(__name__)


# [2026-05-28 FIX] 이전엔 여기 로컬 `_Neo4jProxy` 가 따로 정의돼 run_cypher 만
# 노출했다. base.Neo4jClientProxy 통합("9개 라우트 중복 흡수") 에서 워커만 빠져,
# design/delete 파이프라인의 ctx.neo4j.run_in_transaction(...) 호출이 운영에서
# `AttributeError: '_Neo4jProxy' object has no attribute 'run_in_transaction'`
# 로 폭발(예: '최신 업데이트' Design 트리거). 단일 진실원본으로 통일.
from app.pipelines.base import Neo4jClientProxy as _Neo4jProxy  # noqa: E402  (의도된 위치)


def _ctx(arq_ctx: Dict[str, Any]) -> PipelineContext:
    return PipelineContext(
        gemini=arq_ctx["gemini"],
        neo4j=arq_ctx["neo4j"],
        idempotency_key=arq_ctx.get("job_id", "unknown"),
    )


async def _tracked_ctx(
    arq_ctx: Dict[str, Any], user_email: Optional[str], team_id: str = ""
) -> tuple[PipelineContext, TokenAccumulator, "quota.QuotaDecision"]:
    """LLM 호출 누적용 PipelineContext + accumulator + 쿼터 결정 트리플 생성.

    [2026-06 메인 + Lite 오버플로우]
    user_email 로 QuotaDecision 조회 → 풀(gemini_free/pro/lite) + 토큰 버킷 결정.
    worker on_startup 이 세 인스턴스 (gemini_free / gemini_pro / gemini_lite) 생성.
    메인 소진 유료 등급은 mode=overflow → gemini_lite 풀 + lite 버킷으로 적재.
    반환된 decision.bucket 을 호출자가 _persist_token_usage 에 전달.

    [Backward compat]
    arq_ctx 에 풀 key 가 없으면 (legacy / 일부 tests) gemini_lite→pro/free→gemini
    순으로 graceful fallback.
    """
    # [2026-06 워커 신선도] admin 한도 변경이 워커 메모리에 반영되도록 결정 직전
    # DB 재로드 (15s TTL 캐시). 워커 부팅 후 admin 이 한도를 올렸는데도 옛 값으로
    # 결정을 내려 메인을 폭주시키던 사고를 차단.
    await quota.ensure_overrides_fresh()
    if user_email:
        decision = await quota.resolve_quota_decision(user_email)
    else:
        decision = quota.QuotaDecision(
            mode="main", subscription_type=SUBSCRIPTION_FREE, bucket="main"
        )
    subscription = decision.subscription_type

    # 결정 → 풀 키. overflow=gemini_lite, 유료=gemini_pro, free=gemini_free.
    pool = quota.pool_for_decision(decision)
    inner = arq_ctx.get(pool)
    # lite 풀 미구성(legacy worker) → pro/free 로 graceful fallback (비용만 ↑, 동작 유지).
    if inner is None and pool == quota.MODEL_POOL_LITE:
        inner = arq_ctx.get("gemini_pro") or arq_ctx.get("gemini_free")
    if inner is None:
        inner = arq_ctx.get("gemini")

    if inner is None:
        raise RuntimeError(
            "arq_ctx 에 GeminiClient 가 없음 — on_startup 에서 gemini_free/_pro/_lite "
            "또는 gemini 가 등록돼야 함."
        )

    accumulator = TokenAccumulator()
    # lite 모델명 — overflow 강제 / mid-job 강등 후 모델 강제에 사용.
    lite_model = quota.model_for_decision(
        quota.QuotaDecision(mode="overflow", subscription_type=subscription, bucket="lite")
    )
    # [2026-06 overflow 모델 강제] 이미 overflow 면 비싼 모델 명시 호출(인터뷰 등)을
    # 무시하고 lite 강제. main 모드면 None(파이프라인 모델 선택 존중).
    force_model = lite_model if decision.mode == "overflow" else None
    # [2026-06 mid-job 강등 안전망] main 모드 + 오버플로우 가능 등급이면, 잡 진행 중
    # (시작 누적 + 이번 잡 누적) 이 메인 한도를 넘는 순간 이후 호출을 lite 풀로 강등.
    # 결정이 잡 시작 1회뿐이라 동시/폭주 잡이 메인을 크게 초과하던 race 를 잡 안에서 차단.
    lite_inner_for_downgrade = None
    downgrade_force_model = None
    if decision.mode == "main" and decision.overflow_available:
        lite_inner_for_downgrade = arq_ctx.get(quota.MODEL_POOL_LITE)
        downgrade_force_model = lite_model
    tracked = TrackedGemini(
        inner,
        accumulator,
        downgrade_lite_inner=lite_inner_for_downgrade,
        base_usage=decision.main_current,
        main_limit=decision.main_limit,
        force_model=force_model,
        downgrade_force_model=downgrade_force_model,
    )

    # [2026-05-26 perf C] pipeline 이 sub-stage 마커를 Redis 에 기록하도록 wire.
    # 파이프라인 호출자 (FE 폴링) 가 cps_extract / cps_impact / cps_merge /
    # prd_extract / prd_graph / prd_merge 같은 세부 단계까지 표시 가능.
    async def _stage_cb(stage: str) -> None:
        await _set_job_stage(arq_ctx, stage)

    ctx = PipelineContext(
        gemini=tracked,
        neo4j=arq_ctx["neo4j"],
        idempotency_key=arq_ctx.get("job_id", "unknown"),
        stage_callback=_stage_cb,
        # [Phase 2D] 멀티테넌시 ID 격리 — _derive_ids / _DELETE_PROJECT_NODE_CYPHER
        # 등이 ctx.user_email 기반으로 master_id email prefix 와 Project 노드 매칭.
        # 비면 옛 형식으로 회귀 + cross-tenant 충돌 위험.
        user_email=user_email or "",
        team_id=team_id or "",
    )
    logger.info(
        "_tracked_ctx: user=%s subscription=%s mode=%s pool=%s job=%s",
        user_email, subscription, decision.mode, pool, arq_ctx.get("job_id", "unknown"),
    )
    return ctx, accumulator, decision


# ─── Stage 진행 마커 (C — 2026-05) ───────────────────────────
#
# 사용자가 폴링 중 "지금 어느 단계 진행 중인지" 알 수 있도록 worker 가 Redis
# 키 `harness:job:{task_id}:stage` 에 현재 stage 문자열 저장. get_job_status 가
# 응답에 포함시켜 FE 가 UI 표시.
#
# Stage 값 (단순화 — 2단계):
#   'cps_running'  : CPS 추출 + 병합 진행 중
#   'prd_running'  : PRD 추출 + 병합 진행 중
#   'done'         : 종료 (성공/실패 무관 — final status 는 별도)
#
# TTL: 1시간. job 완료 후 자연 소멸 (arq keep_result 와 같은 수명).
_STAGE_KEY_PREFIX = "harness:job:"
_STAGE_TTL_SEC = 3600


async def _set_job_stage(
    arq_ctx: Dict[str, Any], stage: str
) -> None:
    """현재 진행 stage 를 Redis 에 기록. arq ctx 의 redis 가 없으면 silently skip
    (단위 테스트 호환). 운영에선 항상 있음.
    """
    redis = arq_ctx.get("redis")
    job_id = arq_ctx.get("job_id")
    if not redis or not job_id:
        return
    try:
        await redis.set(f"{_STAGE_KEY_PREFIX}{job_id}:stage", stage, ex=_STAGE_TTL_SEC)
    except Exception as e:  # noqa: BLE001 — stage 마커 실패가 job 결과를 망치면 안 됨
        logger.warning("set_job_stage failed (job=%s stage=%s): %s", job_id, stage, e)


# ─── 자동 cleanup trigger (2026-05-26) ──────────────────────────
#
# 누적된 master PRD 가 "누더기" (같은 Epic 반복 / 비정상 크기) 가 됐을 때
# post_meeting_pipeline_job 끝에서 자동으로 cleanup_master_prd_job enqueue.
# 사용자에게는 "AI 정리 버튼" 같은 거 안 보임 — 다음 PRD 조회 때 깔끔한 결과만.
#
# Detection (둘 중 하나라도 trip):
#   1. master_full_markdown 크기 >= _CLEANUP_SIZE_THRESHOLD (bytes)
#   2. Epic 헤더 (`#### 📦 [Epic-XX]`) 중복 ID 발견 (같은 ID 가 2번 이상)
#
# best-effort: enqueue 실패해도 사용자 응답에 영향 X.
_CLEANUP_SIZE_THRESHOLD = 30_000  # 30KB. 4 sections + 5~10 Epic + 20~30 Story 정상 크기 ~15-25KB.


def _should_trigger_cleanup(master_markdown: str) -> tuple[bool, str]:
    """누더기 detection. (trigger, reason) 반환.

    [정책]
    - size 30KB+ : 일반 4섹션 + 합리적 Epic/Story 수면 보통 ≤25KB. 그 이상은 누적 의심.
    - Epic ID 중복: 같은 ID 가 2번 이상 등장 = 명백한 누더기 (merge_agent 실패 케이스).
    """
    if not master_markdown:
        return False, ""
    import re
    size = len(master_markdown.encode("utf-8"))
    if size >= _CLEANUP_SIZE_THRESHOLD:
        return True, f"master PRD size={size}b > threshold={_CLEANUP_SIZE_THRESHOLD}b"

    # Epic ID 중복 detection — `[Epic-01]` 형태로 같은 ID 가 여러 번
    epic_ids = re.findall(r"\[Epic-\d+\]", master_markdown)
    if len(epic_ids) != len(set(epic_ids)):
        duplicates = [eid for eid in set(epic_ids) if epic_ids.count(eid) > 1]
        return True, f"Epic ID duplicates: {duplicates[:5]}"

    # [2026-06-01] Section 1 Overview 중복 detection.
    # 정상 PRD 엔 Product Vision/통합 비전·Success Metrics 가 각 1회만 등장한다.
    # 미팅 로그 순차 누적 merge 로 같은 bold 라벨이 2회 이상 쌓이면 (스샷처럼 Vision
    # 4개) 누더기 — cleanup_master_prd 프롬프트 §1 이 정확히 dedupe 하는 케이스인데,
    # 기존엔 size/Epic-ID 만 봐서 trip 못 했다. bold 라벨(`**Product Vision**`) 2회+ 면 trigger.
    # prose 언급("product vision")엔 `**` 가 없어 오탐 위험 낮음. cleanup 출력은 라벨이
    # 각 1회라 재-trip 안 됨(수렴).
    for _label in ("Product Vision", "통합 비전", "Success Metrics"):
        cnt = len(re.findall(rf"\*\*\s*{re.escape(_label)}", master_markdown))
        if cnt >= 2:
            return True, f"duplicate Overview label '{_label}' x{cnt}"
    return False, ""


def _deterministic_cleanup_task_id(project_name: str, master_markdown: str) -> str:
    """master content 기반 deterministic task_id — arq dedup 활용.

    같은 master 상태에서 여러 번 lazy trigger 호출 → 모두 같은 task_id → arq 가
    중복 enqueue 무시 (첫 enqueue 만 실행). cleanup 완료 후 master 가 바뀌면 새
    task_id → 다음 trip 때 재 cleanup 가능.
    """
    import hashlib
    digest = hashlib.sha1(master_markdown.encode("utf-8")).hexdigest()[:12]
    # arq task_id 는 임의 문자열 OK — uuid 형식 아니어도 됨.
    return f"auto_cleanup_{project_name}_{digest}"


async def maybe_lazy_trigger_cleanup(
    project_name: str, master_markdown: str, user_email: Optional[str],
    team_id: str = "",
) -> Optional[str]:
    """PRD 조회 endpoint 에서 호출 — 누더기 상태 (기존 미팅에서 누적된) 감지 시
    백그라운드 cleanup 자동 enqueue. 반환: enqueue 한 task_id 또는 None.

    [차이점 — `_maybe_trigger_auto_cleanup` 대비]
    - 호출자가 이미 master 를 fetch 한 상태 → master_markdown 직접 받음 (재 fetch X)
    - 호출 빈도 ↑ (PRD 조회마다) → deterministic task_id 로 arq dedup 강제

    [best-effort] 어떤 예외도 swallow — PRD 조회 결과 영향 0.
    """
    try:
        if not master_markdown:
            return None
        trigger, reason = _should_trigger_cleanup(master_markdown)
        if not trigger:
            return None

        from app.queue.client import enqueue_cleanup_master_prd
        cleanup_task_id = _deterministic_cleanup_task_id(project_name, master_markdown)
        await enqueue_cleanup_master_prd(
            task_id=cleanup_task_id,
            project_name=project_name,
            dry_run=False,
            user_email=user_email,
            team_id=team_id or "",
        )
        logger.info(
            "lazy auto-cleanup enqueued: project=%s task=%s reason=%s",
            project_name, cleanup_task_id, reason,
        )
        return cleanup_task_id
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning(
            "lazy auto-cleanup enqueue failed (project=%s): %s",
            project_name, e,
        )
        return None


async def _maybe_trigger_auto_cleanup(
    project_name: str, user_email: Optional[str], parent_job_id: str,
    team_id: str = "",
) -> None:
    """post_meeting job 끝에서 호출 — 누더기 trip 시 cleanup_master_prd_job 자동 enqueue.

    [best-effort] 어떤 예외도 swallow — 사용자 미팅 결과 영향 0.
    """
    try:
        from app.service import query_repository
        master = await query_repository.get_master_prd(project_name, team_id=team_id or "")
        if not master:
            return
        master_md = master.prd_content or ""
        trigger, reason = _should_trigger_cleanup(master_md)
        if not trigger:
            logger.debug(
                "auto-cleanup skip: project=%s job=%s reason=clean",
                project_name, parent_job_id,
            )
            return

        # cleanup job enqueue — dry_run=False 로 즉시 적용.
        # 사용자가 다음 PRD 조회 때 깔끔한 결과 봄.
        # [2026-05-26] deterministic task_id — 같은 master 상태면 같은 id → arq dedup.
        from app.queue.client import enqueue_cleanup_master_prd
        cleanup_task_id = _deterministic_cleanup_task_id(project_name, master_md)
        await enqueue_cleanup_master_prd(
            task_id=cleanup_task_id,
            project_name=project_name,
            dry_run=False,
            user_email=user_email,
            team_id=team_id or "",
        )
        logger.info(
            "auto-cleanup enqueued: project=%s parent_job=%s cleanup_task=%s reason=%s",
            project_name, parent_job_id, cleanup_task_id, reason,
        )
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning(
            "auto-cleanup enqueue failed (project=%s, parent_job=%s): %s",
            project_name, parent_job_id, e,
        )


async def _release_concurrency_slot(
    arq_ctx: Dict[str, Any], user_email: Optional[str], job_id: str,
    *, project_key: str = "",
) -> None:
    """[2026-06] heavy job 종료 시 동시성 슬롯 해제 (best-effort).

    모든 job finally 에서 호출되지만, heavy job 이 아니면(슬롯 미보유) ZREM no-op —
    안전. Redis 오류는 swallow (stale 정리가 안전망).

    project_key 가 주어지면(master 쓰기 잡 3종) enqueue 시 잡은 프로젝트 inflight
    마커도 함께 해제 — 다음 작업(배치의 다음 항목 등)이 즉시 enqueue 가능.
    """
    if not user_email:
        return
    try:
        from app.core import concurrency
        await concurrency.release_slot(arq_ctx.get("redis"), user_email, job_id)
        if project_key:
            await concurrency.release_project(arq_ctx.get("redis"), project_key, job_id)
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning("concurrency 슬롯 해제 실패 (user=%s, job=%s): %s", user_email, job_id, e)


async def _persist_token_usage(
    user_email: Optional[str], accumulator: TokenAccumulator, *,
    job_id: str, bucket: str = "main",
) -> None:
    """job finally 절에서 호출 — 누적 토큰을 사용자 카운터에 영구 적재.

    bucket 은 _tracked_ctx 가 돌려준 decision.bucket ("main"|"lite"). overflow 로
    돌린 job 의 토큰은 lite 버킷(월간 + 일일)으로 적재돼 일일캡에 반영된다.

    user_email 이 없으면 (e.g. legacy enqueue) skip — quota 누락 로깅만.
    add_tokens 실패는 swallow (Neo4j 일시 장애가 job 결과를 망치면 안 됨 — usage 는 best-effort).
    """
    total = accumulator.total.total_tokens
    if total <= 0:
        return
    if not user_email:
        logger.warning(
            "_persist_token_usage: user_email 없음 — quota 누적 skip "
            "(job=%s, tokens=%d). enqueue 시 user_email 전달 필요.",
            job_id, total,
        )
        return

    # [2026-06 mid-job 강등] 잡 도중 메인 한도를 넘어 lite 로 강등됐으면, 강등
    # 시점까지(main_bucket_tokens)는 main, 나머지는 lite 버킷으로 분할 적재.
    split = accumulator.main_bucket_tokens
    if split is not None and 0 <= split < total:
        main_part = split
        lite_part = total - split
        try:
            if main_part > 0:
                await usage_repository.add_tokens(user_email, main_part, bucket="main")
            new_total = await usage_repository.add_tokens(user_email, lite_part, bucket="lite")
            logger.info(
                "quota: mid-job 강등 분할 적재 main+%d / lite+%d → %s (user=%s, job=%s)",
                main_part, lite_part, new_total, user_email, job_id,
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.warning(
                "quota: add_tokens(분할) 실패 (user=%s, job=%s, main=%d lite=%d): %s",
                user_email, job_id, main_part, lite_part, e,
            )
        return

    try:
        new_total = await usage_repository.add_tokens(user_email, total, bucket=bucket)
        # [2026-05-27] Gemini implicit caching 적중률 — 프롬프트 prefix 재구조
        # 효과 검증용. cached/prompt ratio 가 30%+ 면 캐시 잘 듣는 것.
        cached = accumulator.total.cached_tokens
        prompt = accumulator.total.prompt_tokens
        cache_ratio = (cached / prompt * 100) if prompt > 0 else 0
        logger.info(
            "quota: tokens +%d → %s (user=%s, job=%s, bucket=%s, cached=%d/%d=%.0f%%)",
            total, new_total, user_email, job_id, bucket,
            cached, prompt, cache_ratio,
        )
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning(
            "quota: add_tokens 실패 (user=%s, job=%s, delta=%d): %s",
            user_email, job_id, total, e,
        )


# ─── extract 단계 (batch 파이프라이닝) ──────────────────────────────
#
# extract(순수 LLM: cps_agent + prd_extract + prd_graph)는 그 회의록 원문에만
# 의존하고 누적 master 와 무관하다. 따라서 다음 버전 처리 전에 prefetch 로 미리
# 계산해 Redis 캐시에 넣어두면, 본 post_meeting job 이 재사용(LLM 3회 skip)해
# batch 벽시계를 단축한다. **모든 Neo4j 쓰기는 merge 단계에 남아 있어** 데이터
# 무결성은 그대로다 (extract 는 그래프를 안 건드림).


def _is_valid_extract(d: Any) -> bool:
    """캐시된 extract dict 가 merge 가 기대하는 형태인지 검증.

    cache HIT 경로가 extract['cps_graph'] 를 바로 쓰므로, 손상/구버전 엔트리를 hit 으로
    오인하면 KeyError 로 job 이 깨진다. 형태 불충족 시 miss 로 취급 → 재계산(self-heal).

    [2026-06-04] prd_markdown 이 **빈 문자열**이면 invalid 로 취급 → 재계산. 빈/환각
    extract 가 캐시에 남으면 batch 의 K+1 이 그걸 HIT 으로 재사용해 master PRD 가
    엉뚱하게 굳는 사고를 막는다 (form 만 보던 기존 검증의 구멍). 정상 추출은 항상
    템플릿 기반 본문이 채워지므로 회귀 없음.
    """
    return (
        isinstance(d, dict)
        and isinstance(d.get("cps_graph"), dict)
        and isinstance(d.get("prd_graph"), dict)
        and isinstance(d.get("prd_markdown"), str)
        and bool(d["prd_markdown"].strip())
    )


async def _compute_extract(
    pipeline_ctx: PipelineContext,
    *,
    project_name: str,
    version: str,
    meeting_content: str,
    previous_cps_id: Optional[str],
    previous_prd_id: Optional[str],
    team_id: str = "",
) -> Dict[str, Any]:
    """CPS extract → PRD extract (순수 LLM). 캐시 가능한 dict 반환.

    반환 {cps_graph, prd_markdown, prd_graph} — Neo4j 쓰기 0건
    (run_cps_extract/run_prd_extract 의 불변식). parsed 는 cps_graph 의 순수 함수라
    캐시 안 함 — 소비 시점에 parse_cps_for_prd 로 재구성.
    """
    cps_graph = await run_cps_extract(
        pipeline_ctx,
        CpsInput(
            project_name=project_name,
            version=version,
            date="",
            meeting_content=meeting_content,
            previous_cps_id=previous_cps_id,
            team_id=team_id,
        ),
    )
    prd_extract = await run_prd_extract(
        pipeline_ctx,
        PrdInput(
            project_name=project_name,
            version=version,
            cps_graph=cps_graph,
            previous_prd_id=previous_prd_id,
            team_id=team_id,
            # [2026-06-04] CPS delta 가 비어도 PRD 가 회의록으로 생성되도록 raw fallback 전달.
            meeting_content=meeting_content,
        ),
    )
    return {
        "cps_graph": cps_graph,
        "prd_markdown": prd_extract["prd_markdown"],
        "prd_graph": prd_extract["prd_graph"],
    }


def _prd_extract_from_cache(cached: Dict[str, Any]) -> Dict[str, Any]:
    """캐시된 extract dict → run_prd_merge 입력 형태로 복원 (parsed 재구성)."""
    cps_graph = cached.get("cps_graph") or {}
    return {
        "parsed": parse_cps_for_prd(cps_graph),
        "prd_markdown": cached.get("prd_markdown") or "",
        "prd_graph": cached.get("prd_graph") or {},
    }


async def _is_over_token_quota(user_email: Optional[str]) -> bool:
    """best-effort 차단 여부 — prefetch 가 토큰을 낭비하지 않도록 사전 체크.

    [2026-06] 메인 소진이어도 Lite 오버플로우 가능(Pro/Pro+/Max)하면 skip 안 함
    — prefetch 도 Lite 로 이어 처리. 진짜 차단(blocked: Free 소진 / 일일캡 소진)일
    때만 True. 조회 실패/사용자 없음 → False (진행 허용, 실제 게이트는 라우트 가드).
    """
    if not user_email:
        return False
    try:
        decision = await quota.resolve_quota_decision(user_email)
        return decision.mode == "blocked"
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning("_is_over_token_quota check failed (user=%s): %s", user_email, e)
        return False


async def prefetch_extract_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    version: str,
    meeting_content: str,
    previous_cps_id: str | None = None,
    previous_prd_id: str | None = None,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """arq job: 다음 버전 extract 를 미리 계산해 캐시 (batch 파이프라이닝).

    best-effort — 실패/skip 해도 본 post_meeting job 이 캐시 미스로 직접 추출(기존
    동작)하므로 안전. **Neo4j 그래프엔 쓰지 않음** (extract 순수 LLM) → 데이터 안전.
    토큰은 사용자 quota 에 누적 (본 job 이 캐시 hit 시 추출 LLM 을 skip 하므로 총
    토큰은 보존 — 이중과금은 single-flight 락으로 방지).
    """
    job_id = ctx.get("job_id", "unknown")
    redis = ctx.get("redis")
    key = extract_cache_key(project_name, version, meeting_content)

    # 이미 (정상) 캐시됨 → 재계산 불필요. 손상 엔트리면 아래로 진행해 재계산(self-heal).
    if _is_valid_extract(await get_cached_extract(redis, key)):
        return {"status": "already_cached"}

    # 과금 cap 초과 사용자면 skip (본 job 은 어차피 라우트 게이트에서 차단됨).
    if await _is_over_token_quota(user_email):
        logger.info(
            "prefetch skip (over token quota): user=%s project=%s version=%s",
            user_email, project_name, version,
        )
        return {"status": "skipped_quota"}

    # single-flight — 본 job 과 동시 추출(토큰 이중과금) 방지.
    if not await try_acquire_extract_lock(redis, key):
        return {"status": "locked"}

    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email, team_id)
    try:
        extract = await _compute_extract(
            pipeline_ctx,
            project_name=project_name,
            version=version,
            meeting_content=meeting_content,
            previous_cps_id=previous_cps_id,
            previous_prd_id=previous_prd_id,
            team_id=team_id,
        )
        await set_cached_extract(redis, key, extract)
        logger.info(
            "prefetch cached: project=%s version=%s job=%s", project_name, version, job_id
        )
        return {"status": "cached"}
    except Exception as e:  # noqa: BLE001 — best-effort, 본 job 이 직접 추출하면 됨
        logger.warning(
            "prefetch_extract_job failed (project=%s version=%s): %s",
            project_name, version, e,
        )
        return {"status": "error", "error": str(e)}
    finally:
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(ctx, user_email, job_id)


async def _get_or_compute_extract(
    arq_ctx: Dict[str, Any],
    pipeline_ctx: PipelineContext,
    *,
    project_name: str,
    version: str,
    meeting_content: str,
    previous_cps_id: Optional[str],
    previous_prd_id: Optional[str],
    team_id: str = "",
) -> Dict[str, Any]:
    """extract 를 캐시에서 가져오거나(hit → LLM skip) 직접 계산(miss).

    single-flight: 캐시 미스 + 락을 prefetch 가 쥐고 있으면 짧게 대기 후 결과 사용,
    그래도 없으면 직접 계산. 어떤 경로든 동일한 extract dict 반환 → 결과 품질 불변.
    """
    redis = arq_ctx.get("redis")
    key = extract_cache_key(project_name, version, meeting_content)

    cached = await get_cached_extract(redis, key)
    if _is_valid_extract(cached):
        logger.info(
            "post_meeting extract cache HIT: project=%s version=%s", project_name, version
        )
        return cached

    # miss(또는 손상 엔트리) — single-flight. 락 못 잡으면 prefetch 가 계산 중 → 결과 대기.
    if not await try_acquire_extract_lock(redis, key):
        waited = await wait_for_cached_extract(redis, key)
        if _is_valid_extract(waited):
            logger.info(
                "post_meeting extract cache HIT (after wait): project=%s version=%s",
                project_name, version,
            )
            return waited
        logger.info(
            "post_meeting extract: 락 점유됐으나 결과 없음 — 직접 계산 "
            "(project=%s version=%s)", project_name, version,
        )

    extract = await _compute_extract(
        pipeline_ctx,
        project_name=project_name,
        version=version,
        meeting_content=meeting_content,
        previous_cps_id=previous_cps_id,
        previous_prd_id=previous_prd_id,
        team_id=team_id,
    )
    await set_cached_extract(redis, key, extract)
    return extract


async def _enqueue_next_prefetch(
    arq_ctx: Dict[str, Any],
    *,
    project_name: str,
    next_meeting: Dict[str, Any],
    user_email: Optional[str],
    team_id: str = "",
) -> None:
    """다음 회의(batch 의 K+1) extract 선반입 enqueue. best-effort — 실패해도 본
    흐름 영향 0 (K+1 job 이 캐시 미스로 직접 추출하면 됨)."""
    try:
        content = next_meeting.get("content") or next_meeting.get("meeting_content")
        nxt_version = next_meeting.get("version")
        if not content or not nxt_version:
            return
        previous_cps_id = next_meeting.get("previous_cps_id") or next_meeting.get("previousCpsId")
        previous_prd_id = next_meeting.get("previous_prd_id") or next_meeting.get("previousPrdId")
        # 결정적 task_id — 같은 prefetch 중복 enqueue 를 arq 가 dedup.
        import hashlib
        digest = hashlib.sha1(
            f"{project_name}:{nxt_version}:{content}".encode("utf-8")
        ).hexdigest()[:12]
        task_id = f"prefetch_{project_name}_{nxt_version}_{digest}"
        # 지연 import — client → (잠재) 순환 회피 (기존 _maybe_trigger_auto_cleanup 패턴).
        from app.queue.client import enqueue_prefetch_extract
        await enqueue_prefetch_extract(
            task_id=task_id,
            project_name=project_name,
            version=nxt_version,
            meeting_content=content,
            previous_cps_id=previous_cps_id,
            previous_prd_id=previous_prd_id,
            user_email=user_email,
            team_id=team_id,
        )
        logger.info(
            "next-meeting prefetch enqueued: project=%s next_version=%s task=%s",
            project_name, nxt_version, task_id,
        )
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning(
            "next-meeting prefetch enqueue failed (project=%s): %s", project_name, e
        )


async def cps_pipeline_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    version: str,
    date: str,
    meeting_content: str,
    previous_cps_id: str | None = None,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """arq job: postMeeting → CPS 파이프라인 실행 (CPS 단독).

    user_email: 호출자(인증된 사용자) — quota 토큰 누적용. 빈 값이면 누적 skip.
    """
    job_id = ctx.get("job_id", "unknown")
    logger.info("cps_pipeline_job start: job=%s project=%s", job_id, project_name)

    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email, team_id)
    try:
        # [2026-06 멀티디바이스 이중작업] 단독 잡은 extract/merge 분리가 없어 전체를
        # 프로젝트 단위 직렬화 — 같은 master 동시 merge 의 lost update 차단.
        async with master_write_lock(
            ctx.get("redis"), scoped_project(project_name, team_id or None), job_id,
        ):
            result = await run_cps_pipeline(
                pipeline_ctx,
                CpsInput(
                    project_name=project_name,
                    version=version,
                    date=date,
                    meeting_content=meeting_content,
                    previous_cps_id=previous_cps_id,
                    team_id=team_id,
                ),
            )
        return {
            "meeting_log_id": result.meeting_log_id,
            "delta_cps_id": result.delta_cps_id,
            "master_cps_id": result.master_cps_id,
            "mode": result.mode,
            "diagnostic": result.diagnostic,
        }
    finally:
        # 성공/실패와 무관하게 실제 호출된 토큰만큼 누적. LLM 비용은 응답에 관계 없이 발생.
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(
            ctx, user_email, job_id,
            project_key=scoped_project(project_name, team_id or None),
        )


async def post_meeting_pipeline_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    version: str,
    date: str,
    meeting_content: str,
    previous_cps_id: str | None = None,
    previous_prd_id: str | None = None,
    user_email: Optional[str] = None,
    next_meeting: Optional[Dict[str, Any]] = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """
    arq job: postMeeting 엔드포인트 — CPS 실행 후 PRD 체이닝.

    실패 시 부분 상태:
      - CPS 도중 실패 → arq retry (멱등성 키 동일하면 같은 입력으로 재실행).
      - CPS 성공 + PRD 실패 → arq retry. PRD 만 재시도되지 않고 전체가 재실행되지만
        Save CPS / Save Meeting Log 가 MERGE 기반이라 idempotent.

    user_email: quota 토큰 누적 — CPS + PRD 두 단계의 LLM 사용량 합산.

    [batch 파이프라이닝]
    extract(순수 LLM)는 캐시에서 재사용(prefetch hit 시 LLM 3회 skip), merge(DB 쓰기)
    는 그대로 순차 실행 → 누적 master 무결성 보존. next_meeting 이 주어지면(batch 의
    다음 항목) merge 시작 시 그 extract 를 선반입 enqueue → K+1 처리 시 캐시 hit.
    next_meeting={"content","version","previous_cps_id"?}.
    """
    job_id = ctx.get("job_id", "unknown")
    logger.info("post_meeting_pipeline_job start: job=%s project=%s", job_id, project_name)

    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email, team_id)
    try:
        # ── EXTRACT 단계 (순수 LLM, 캐시 가능 — DB 쓰기 0) ──
        extract = await _get_or_compute_extract(
            ctx, pipeline_ctx,
            project_name=project_name,
            version=version,
            meeting_content=meeting_content,
            previous_cps_id=previous_cps_id,
            previous_prd_id=previous_prd_id,
            team_id=team_id,
        )

        # ── 다음 회의 extract 선반입 (이 job 의 merge 가 도는 동안 미리 계산) ──
        if next_meeting:
            await _enqueue_next_prefetch(
                ctx, project_name=project_name,
                next_meeting=next_meeting, user_email=user_email,
                team_id=team_id,
            )

        # ── MERGE 단계 (모든 DB 쓰기 — 누적 master 무결성 보존) ──
        # [2026-06-04 perf] CPS 병합과 PRD 병합의 **읽기+LLM(compute)** 을 동시 실행 →
        # 두 flash LLM(cps_merge·prd_merge agent) 오버랩으로 항목당 ~7s 단축. PRD **쓰기
        # (commit)** 는 CPS 병합 완료 후 수행 → master CPS 가 먼저 존재해 BASED_ON 무결성
        # 보장 + 동시 DB 쓰기 0건(데이터 안전). 쓰기 순서·내용·LLM 입출력은 순차와 동일.
        # (cps_result.cps_graph 는 입력 graph 그대로라 extract["cps_graph"] 와 동일 →
        #  PRD compute 는 cps 완료를 기다리지 않고 즉시 시작.)
        await _set_job_stage(ctx, "cps_running")
        # [2026-06 멀티디바이스 이중작업] MERGE 구간(읽기→LLM→쓰기 전체)을 프로젝트
        # 단위로 직렬화 — 웹 배치 + 모바일 단건이 같은 master 를 동시에 만져 마지막
        # 쓰기가 앞 잡의 delta 를 덮는 lost update 차단. EXTRACT/prefetch 는 락 밖
        # (DB 쓰기 0, 병렬 유지). 키는 master_id 와 같은 scoped key — 팀 멤버 간
        # 동시 작업도 막힘. 대기 초과(MasterLockTimeout)는 전파 → arq 재시도로 회복.
        async with master_write_lock(
            ctx.get("redis"), scoped_project(project_name, team_id or None), job_id,
        ):
            cps_task = asyncio.create_task(
                run_cps_merge(
                    pipeline_ctx,
                    CpsInput(
                        project_name=project_name,
                        version=version,
                        date=date,
                        meeting_content=meeting_content,
                        previous_cps_id=previous_cps_id,
                        team_id=team_id,
                    ),
                    extract["cps_graph"],
                )
            )
            # [2026-06 R1 비대칭 차단] PRD 의 **결정적** 실패(빈-merge ValueError / orphan
            # RuntimeError 등)는 arq 재시도해도 같은 결과 → 'CPS 가득 / PRD 빈 + 무한 재시도'의
            # 구조적 원천(CPS 는 807 에서 이미 커밋됨). 결정적 실패는 raise 대신 prd error 로
            # 강등해 job 을 성공시키고(FE 는 prd.mode='error' 로 재생성 안내), **비결정적**(transient:
            # LLM 5xx/타임아웃/네트워크)만 전파해 arq 재시도(CPS 멱등 재실행)로 회복 기회를 보존한다.
            prd_commit = None
            prd_result = None
            prd_error: Optional[BaseException] = None
            try:
                prd_commit = await _prd_merge_compute(
                    pipeline_ctx,
                    PrdInput(
                        project_name=project_name,
                        version=version,
                        cps_graph=extract["cps_graph"],
                        previous_prd_id=previous_prd_id,
                        team_id=team_id,
                        meeting_content=meeting_content,
                    ),
                    _prd_extract_from_cache(extract),
                )
            except (ValueError, RuntimeError) as e:
                # 결정적 PRD compute 실패 → CPS 는 끝까지 완결(아래 await cps_task) + PRD error 강등.
                prd_error = e
                logger.exception(
                    "post_meeting: PRD compute 결정적 실패 → CPS 보존 + PRD error 강등 "
                    "(job=%s project=%s version=%s): %s", job_id, project_name, version, e,
                )
            except BaseException:
                # 비결정적(transient) — 기존 동작: CPS 완결 후 전체 재시도(arq)로 일시 오류 회복.
                with contextlib.suppress(BaseException):
                    await cps_task
                raise
            # CPS master 쓰기 완료 보장 → 이후 PRD commit 의 BASED_ON 이 master CPS 를 찾음.
            cps_result = await cps_task
            await _set_job_stage(ctx, "prd_running")
            if prd_commit is not None:
                try:
                    prd_result = await prd_commit()
                except (ValueError, RuntimeError) as e:
                    # 결정적 PRD commit 실패(orphan RuntimeError 등)도 동일하게 error 강등.
                    prd_error = e
                    logger.exception(
                        "post_meeting: PRD commit 결정적 실패 → CPS 보존 + PRD error 강등 "
                        "(job=%s project=%s version=%s): %s", job_id, project_name, version, e,
                    )
        await _set_job_stage(ctx, "done")
        # [2026-05-26] 누더기 detection — trip 되면 cleanup_master_prd_job 자동 enqueue.
        # best-effort: 사용자 응답 전 빠르게 (자체 LLM 호출 X, Neo4j get_master_prd 만).
        await _maybe_trigger_auto_cleanup(
            project_name=project_name,
            user_email=user_email,
            parent_job_id=job_id,
            team_id=team_id or "",
        )
        if prd_error is not None or prd_result is None:
            prd_payload = {
                "delta_prd_id": "",
                "master_prd_id": "",
                "mode": "error",
                "diagnostic": {
                    "error": str(prd_error) if prd_error is not None else "PRD 결과 없음",
                    "error_type": type(prd_error).__name__ if prd_error is not None else "None",
                },
            }
        else:
            prd_payload = {
                "delta_prd_id": prd_result.delta_prd_id,
                "master_prd_id": prd_result.master_prd_id,
                "mode": prd_result.mode,
                "diagnostic": prd_result.diagnostic,
            }
        return {
            "cps": {
                "meeting_log_id": cps_result.meeting_log_id,
                "delta_cps_id": cps_result.delta_cps_id,
                "master_cps_id": cps_result.master_cps_id,
                "mode": cps_result.mode,
                "diagnostic": cps_result.diagnostic,
                # [2026-05-25] FE 표시용 추출 모드 + 안내.
                "extraction_mode": cps_result.extraction_mode,
                "extraction_warning": cps_result.extraction_warning,
            },
            "prd": prd_payload,
        }
    finally:
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(
            ctx, user_email, job_id,
            project_key=scoped_project(project_name, team_id or None),
        )


async def prd_pipeline_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    version: str,
    cps_graph: Dict[str, Any],
    previous_prd_id: str | None = None,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """arq job: PRD 단독 (수동 재실행, cps_graph 직접 입력)."""
    job_id = ctx.get("job_id", "unknown")
    logger.info("prd_pipeline_job start: job=%s project=%s", job_id, project_name)

    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email, team_id)
    try:
        # [2026-06 멀티디바이스 이중작업] post_meeting / cps 단독과 같은 프로젝트
        # 잠금 — master PRD 동시 merge 의 lost update 차단.
        async with master_write_lock(
            ctx.get("redis"), scoped_project(project_name, team_id or None), job_id,
        ):
            result = await run_prd_pipeline(
                pipeline_ctx,
                PrdInput(
                    project_name=project_name,
                    version=version,
                    cps_graph=cps_graph,
                    previous_prd_id=previous_prd_id,
                    team_id=team_id,
                ),
            )
        return {
            "delta_prd_id": result.delta_prd_id,
            "master_prd_id": result.master_prd_id,
            "mode": result.mode,
            "diagnostic": result.diagnostic,
        }
    finally:
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(
            ctx, user_email, job_id,
            project_key=scoped_project(project_name, team_id or None),
        )


def _make_autofill_hook(
    pipeline_ctx: PipelineContext,
    *,
    user_email: Optional[str],
) -> tuple:
    """design 병렬 autofill — run_design_pipeline 의 on_spack_ready 훅 + 상태 생성.

    [2026-06-10 병렬화 — 이전: design 완료 후 직렬 후처리]
    autofill 은 각 API 의 method/endpoint/description 만 입력으로 쓰므로 DDD/
    Architecture 와 독립이다. 이전엔 design 저장 후에야 시작해 잡 끝에 50~90s 가
    통째로 더해졌는데, 이제 SPACK 확정 즉시 생성 LLM 을 백그라운드 task 로 띄워
    DDD/Architecture LLM 과 겹친다. **저장은 여전히 design 트랜잭션 이후**
    (_finish_parallel_autofill) — 먼저 쓰면 design 의 wipe-and-redraw 가 지워버림.

    Returns: (hook, state) — hook 은 동기 callable(파이프라인이 호출),
        state = {"task": asyncio.Task|None, "started": float|None, "inputs": [...]}.
        잡은 성공 시 _finish_parallel_autofill(state, ...) 로 회수하고,
        실패/취소 경로에선 state["task"].cancel() 로 토큰 낭비를 멈춘다.
    """
    state: Dict[str, Any] = {"task": None, "started": None, "inputs": []}

    def _hook(apis: List[Dict[str, Any]]) -> None:
        if state["task"] is not None:  # 중복 시작 방지 (이론상 1회 호출)
            return
        if not settings.DESIGN_AUTOFILL_API_SPECS:
            return
        from app.pipelines.api_spec_autofill_pipeline import ApiSpecInput

        inputs = [
            ApiSpecInput(
                id=str(a.get("id") or ""),
                name=str(a.get("name") or ""),
                method=str(a.get("method") or ""),
                endpoint=str(a.get("endpoint") or ""),
                description=str(a.get("description") or ""),
                error_cases=a.get("error_cases") or [],
                auth=a.get("auth") or {},
            )
            for a in apis
            if a.get("id")
        ]
        if not inputs:
            return
        state["inputs"] = inputs

        async def _generate() -> Optional[Dict[str, Any]]:
            # [비용 가드] 한도 넘긴 사용자는 autofill 생략 (기존 직렬 경로와 동일).
            if await _is_over_token_quota(user_email):
                return None
            # 지연 import — 테스트가 모듈 attr 로 monkeypatch 가능 + 부팅 의존 최소화.
            from app.pipelines import api_spec_autofill_pipeline as autofill_mod

            # emit_progress=False: design:ddd/architecture 마커와 섞이면 FE 진행바가
            # 미지의 stage(=SPACK)로 후퇴해 보이므로 병렬 모드에선 마커 생략.
            return await autofill_mod.generate_api_spec_fills(
                pipeline_ctx, inputs,
                fallback_model=autofill_mod.resolve_fallback_model(),
                emit_progress=False,
            )

        state["started"] = time.monotonic()
        state["task"] = asyncio.create_task(_generate())

    return _hook, state


async def _finish_parallel_autofill(
    state: Dict[str, Any],
    project_name: str,
    team_id: str = "",
) -> Optional[Dict[str, Any]]:
    """병렬 autofill 회수 — design 저장 완료 후 호출. 실패는 모두 None (design 보존).

    [시간 예산] DESIGN_AUTOFILL_BUDGET_SEC 은 **잔여(tail) 대기** 상한 — 생성 대부분이
    DDD/Architecture 와 겹쳐 끝나 있으므로 보통 tail≈0. 최악(생성이 design 보다 오래)
    에도 잡 전체가 기존 직렬 모드의 상한(design + budget)을 넘지 않는다.
    """
    task: Optional[asyncio.Task] = state.get("task")
    if task is None:
        return None
    started = state.get("started") or time.monotonic()
    overlapped = time.monotonic() - started  # design 단계와 겹쳐 숨겨진 시간
    _t_tail = time.monotonic()
    try:
        generated_map = await asyncio.wait_for(
            task, timeout=settings.DESIGN_AUTOFILL_BUDGET_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "design parallel autofill 잔여 예산(%ss) 초과 — design 결과는 보존: project=%s",
            settings.DESIGN_AUTOFILL_BUDGET_SEC, project_name,
        )
        return None
    except Exception:  # noqa: BLE001 — autofill 실패가 design 을 깨지 않게
        logger.exception(
            "design parallel autofill 생성 실패 (design 결과는 보존): project=%s",
            project_name,
        )
        return None
    if generated_map is None:  # over-quota skip
        return None
    try:
        from app.pipelines import api_spec_autofill_pipeline as autofill_mod

        result = await autofill_mod.merge_and_save_fills(
            project_name, state.get("inputs") or [], generated_map, team_id=team_id,
        )
        logger.info(
            "design parallel autofill done: project=%s meta=%s overlapped=%.1fs tail=%.1fs",
            project_name, result.meta, overlapped, time.monotonic() - _t_tail,
        )
        return result.meta
    except Exception:  # noqa: BLE001
        logger.exception(
            "design parallel autofill 저장 실패 (design 결과는 보존): project=%s",
            project_name,
        )
        return None


async def design_pipeline_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """arq job: createDesign — PRD 마스터 → Spack/DDD/Architecture."""
    job_id = ctx.get("job_id", "unknown")
    logger.info("design_pipeline_job start: job=%s project=%s", job_id, project_name)

    # [2026-05 비용 가드] 사전 차단 — 이미 토큰 한도를 넘긴 사용자는 비싼 3단계
    # design 사이클(~105K tokens)을 시작조차 하지 않는다. 라우트의 atomic 체크는
    # enqueue 시점뿐이라, 큐 대기 중 다른 작업으로 한도를 넘긴 경우를 여기서 잡는다.
    if await _is_over_token_quota(user_email):
        logger.warning(
            "design_pipeline_job skip (over token quota): job=%s user=%s project=%s",
            job_id, user_email, project_name,
        )
        return {"result": "quota_exceeded", "project_name": project_name}

    # [2026-05-27] 비동기 중지 — FE 가 cancel API 로 Redis 에 job_cancel:{job_id} flag 를
    # set 하면 worker 가 stage 사이마다 이를 감지해 graceful 종료. 비동기 큐 전환 후
    # design_pipeline_job 이 check_cancel 을 안 넘겨 중지가 worker 로 전달 안 되던 버그
    # 수정. run_design_pipeline 은 최종 Neo4j 트랜잭션 전에 bail 하므로 기존 SPACK/DDD/
    # Architecture 데이터는 보존된다.
    redis = ctx.get("redis")
    cancel_key = f"job_cancel:{job_id}"

    async def _check_cancel() -> bool:
        if redis is None:
            return False
        try:
            return bool(await redis.exists(cancel_key))
        except Exception:  # noqa: BLE001 — 취소 확인 실패는 진행을 막지 않음
            return False

    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email, team_id)
    # [2026-06-10 병렬 autofill] SPACK 확정 즉시 빈 API 의 error_cases/auth 생성 LLM 을
    # DDD/Architecture 와 병렬로 시작 — 이전 직렬 후처리 대비 잡 시간 50~90s 단축.
    # 실패/취소/한도 경로에선 아래 finally 가 task.cancel() 로 토큰 낭비를 멈춘다.
    autofill_hook, autofill_state = _make_autofill_hook(
        pipeline_ctx, user_email=user_email,
    )
    try:
        # [2026-06 멀티디바이스 이중작업] 설계 그래프는 Wipe-and-Redraw(DETACH
        # DELETE→MERGE, SPACK/DDD/ARCH 3 stage) — 동시 실행되면 stage 별로 다른
        # 잡의 결과가 섞여 정합성이 깨진다. merge 와 같은 프로젝트 락으로 직렬화
        # (design 은 master PRD 를 읽으므로 merge 와의 상호 배제도 정합성에 유리).
        # 병렬 autofill 의 결과 회수(노드 저장)도 같은 그래프를 패치하므로 락 안에서.
        async with master_write_lock(
            ctx.get("redis"), scoped_project(project_name, team_id or None), job_id,
        ):
            result = await run_design_pipeline(
                pipeline_ctx,
                DesignInput(project_name=project_name),
                check_cancel=_check_cancel,
                # [2026-05 비용 가드] stage 사이마다 토큰 한도 재확인 → 초과 시 다음
                # (더 비싼) LLM 호출 전에 bail. 기존 설계 데이터는 보존된다.
                check_over_quota=lambda: _is_over_token_quota(user_email),
                on_spack_ready=autofill_hook,
            )
            # design 저장 완료 — 병렬 autofill 결과 회수 + 노드 저장 (wipe 이후라 안전).
            # 어떤 실패도 design 결과를 깨지 않음 (내부에서 모두 None 처리).
            autofill_meta: Optional[Dict[str, Any]] = await _finish_parallel_autofill(
                autofill_state, project_name, team_id,
            )
        return {
            "project_name": result.project_name,
            "master_prd_id": result.master_prd_id,
            "spack": result.spack,
            "ddd": result.ddd,
            "architecture": result.architecture,
            # [2026-05-26] health top-level — FE 가 cross-stage 정합성 배지 표시.
            # 이전엔 job result 에서 빠져 있어 비동기 path 사용 시 FE 가 health 못 받음.
            "health": result.health,
            "diagnostic": result.diagnostic,
            # [2026-06] design 안에서 API 스펙을 자동으로 채웠을 때의 요약(없으면 null).
            "autofill": autofill_meta,
        }
    except DesignPipelineCancelled as e:
        # 사용자 중지 — FE 는 result=='cancelled' 분기로 "중지했습니다" 안내.
        logger.info("design_pipeline_job cancelled: job=%s stage=%s", job_id, e)
        return {"result": "cancelled", "project_name": project_name}
    except DesignQuotaExceeded as e:
        # [2026-05 비용 가드] stage 중 한도 초과 — FE 는 result=='quota_exceeded'
        # 분기로 UpgradePromptDialog 안내. 기존 설계 데이터는 보존됨.
        logger.warning("design_pipeline_job quota_exceeded: job=%s stage=%s", job_id, e)
        return {"result": "quota_exceeded", "project_name": project_name}
    except DesignPrecheckFailed as e:
        # [2026-05-28] 누더기+거대 PRD fail-fast — FE 는 result=='precheck_failed' 분기로
        # "PRD 정리 필요" 안내. 결정적 실패라 raise 하지 않고 결과로 반환(arq 재시도 방지).
        logger.warning("design_pipeline_job precheck_failed: job=%s reason=%s", job_id, e)
        return {
            "result": "precheck_failed",
            "project_name": project_name,
            "message": str(e),
            "diagnostic": getattr(e, "diagnostic", {}),
        }
    except MasterLockTimeout:
        # 락 대기 초과 — 다른 잡이 길게 보유 중인 '일시' 상태라, 아래 generic
        # except 의 "결정적 오류 → error 결과" 변환 대상이 아니다. 전파해 arq
        # 재시도(최대 3회)로 회복 (merge 잡들과 동일 정책).
        raise
    except GeminiError as e:
        # [2026-06-10 실사고] 모델 공급자(Gemini) 쪽 quota/선불 크레딧 소진 —
        # 429 RESOURCE_EXHAUSTED "prepayment credits are depleted". 이전엔 아래
        # generic except 가 "설계 생성 중 오류 — 잠시 후 다시 시도" 를 띄워 사용자가
        # 재시도해도 똑같이 실패(크레딧 문제는 재시도로 안 풀림)하는 오안내였다.
        # quota/auth 만 구분해 정직한 안내 + arq 재시도 차단(결과 반환).
        kind = getattr(e, "kind", "")
        if kind in ("quota", "auth"):
            logger.warning(
                "design_pipeline_job gemini %s (재시도 무의미 — 공급자측 한도/인증): "
                "job=%s project=%s err=%s",
                kind, job_id, project_name, e,
            )
            return {
                "result": "error",
                "error": (
                    "AI 서비스 사용량이 한도에 도달해 설계를 생성하지 못했습니다. "
                    "잠시 후 다시 시도하고, 계속되면 운영자에게 문의해 주세요."
                    if kind == "quota"
                    else "AI 서비스 인증에 문제가 있습니다. 운영자에게 문의해 주세요."
                ),
                "project_name": project_name,
            }
        # 그 외 GeminiError(transient 소진/invalid_response 등) — generic 안내.
        logger.exception(
            "design_pipeline_job gemini error — arq 재시도 차단 위해 error 결과 반환: "
            "job=%s project=%s",
            job_id, project_name,
        )
        return {
            "result": "error",
            "error": "설계 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            "project_name": project_name,
        }
    except Exception:  # noqa: BLE001
        # [2026-06-05 버그픽스 — 진행바 SPACK 후퇴 현상]
        # 여기까지 전파된 예외를 그대로 raise 하면 arq(max_tries=3)가 **3-LLM
        # 파이프라인 전체를 처음(SPACK)부터 재실행**한다. 사용자에겐 진행바가
        # Architecture → SPACK 으로 후퇴하는 것으로 보여(혼란) + 토큰(~105K)도
        # 재낭비된다.
        #
        # 핵심: 일시(transient) 오류는 이미 하위 레이어에서 처리된다 —
        #   • Neo4j 최종 commit: session.execute_write 가 transient 자동 재시도
        #   • Gemini 호출: 멀티키 로테이션 + 429 재시도 + 모델 폴백(max_retries=3)
        # 따라서 이 지점까지 올라온 예외는 재실행해도 같은 곳에서 깨지는 **결정적
        # 오류**다. precheck 패턴과 동일하게 raise 대신 error 결과로 반환해 arq
        # 재시도를 차단하고, FE 가 정직한 실패 안내(토스트)를 띄우게 한다.
        # (full traceback 은 logger.exception 으로 보존 — 관측성 유지.)
        logger.exception(
            "design_pipeline_job failed — arq 재시도 차단 위해 error 결과 반환: "
            "job=%s project=%s",
            job_id, project_name,
        )
        return {
            "result": "error",
            "error": "설계 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            "project_name": project_name,
        }
    finally:
        # [2026-06-10 병렬 autofill] 취소/한도/오류 경로에서 아직 도는 autofill 생성
        # task 중단 — 설계가 실패했는데 autofill LLM 만 계속 돌며 토큰을 태우지 않게.
        # (성공 경로는 _finish_parallel_autofill 이 이미 await 완료 → done → no-op.)
        _af_task = autofill_state.get("task")
        if _af_task is not None and not _af_task.done():
            _af_task.cancel()
            with contextlib.suppress(BaseException):
                await _af_task
        # cancel flag 정리 (재사용 task_id 의 오판 방지) + 토큰 누적.
        if redis is not None:
            try:
                await redis.delete(cancel_key)
            except Exception:  # noqa: BLE001
                pass
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(
            ctx, user_email, job_id,
            project_key=scoped_project(project_name, team_id or None),
        )


async def recommend_skills_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    skill_catalog: list[dict],
    allowed_categories: list[str],
    user_email: Optional[str] = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """arq job: recommendSkillsByAI."""
    job_id = ctx.get("job_id", "unknown")
    logger.info(
        "recommend_skills_job start: job=%s project=%s catalog_size=%d",
        job_id,
        project_name,
        len(skill_catalog),
    )

    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email, team_id)
    try:
        result = await run_skill_recommend_pipeline(
            pipeline_ctx,
            RecommendInput(
                project_name=project_name,
                skill_catalog=[
                    CatalogEntry(
                        id=c.get("id", ""),
                        name=c.get("name", ""),
                        description=c.get("description", ""),
                        category=c.get("category", ""),
                    )
                    for c in skill_catalog
                ],
                allowed_categories=allowed_categories,
            ),
        )
        return {
            "recommended": [
                {"id": r.id, "reason": r.reason, "confidence": r.confidence}
                for r in result.recommended
            ],
            "meta": result.meta,
        }
    finally:
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(ctx, user_email, job_id)


async def run_lint_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    github_url: str,
    user_token: str | None = None,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """arq job: runLint (Spack/DDD/Arch/Skill + GitHub tree → LLM → save).

    user_token: 호출자(인증된 사용자)의 GitHub OAuth access_token.
                private repo 접근 + rate limit 확장(5000/hr) 용.
    user_email: quota 토큰 누적용.
    """
    job_id = ctx.get("job_id", "unknown")
    logger.info(
        "run_lint_job start: job=%s project=%s url=%s authed=%s",
        job_id, project_name, github_url, bool(user_token),
    )
    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email, team_id)
    try:
        result = await run_lint_pipeline(
            pipeline_ctx,
            LintInput(project_name=project_name, github_url=github_url, team_id=team_id),
            user_token=user_token,
        )
        return result.model_dump()
    finally:
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(ctx, user_email, job_id)


async def generate_fix_spec_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    github_url: str,
    lint_result: Dict[str, Any],
    user_email: Optional[str] = None,
) -> Dict[str, Any]:
    """arq job: generateFixSpec."""
    job_id = ctx.get("job_id", "unknown")
    logger.info("generate_fix_spec_job start: job=%s project=%s", job_id, project_name)
    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email)
    try:
        result = await run_fix_spec_pipeline(
            pipeline_ctx,
            FixSpecInput(
                project_name=project_name,
                github_url=github_url,
                lint_result=lint_result,
            ),
        )
        return {
            "success": result.success,
            "markdown": result.markdown,
            "filename": result.filename,
            "message": result.message,
            "metadata": result.metadata,
        }
    finally:
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(ctx, user_email, job_id)


async def analyze_lineage_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    user_token: str | None = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """arq job: analyzeLineage (deterministic matching, LLM 미사용)."""
    job_id = ctx.get("job_id", "unknown")
    logger.info(
        "analyze_lineage_job start: job=%s project=%s authed=%s",
        job_id, project_name, bool(user_token),
    )

    # lineage 는 Gemini 안 씀 → ctx['gemini'] 가 없어도 동작하도록 직접 PipelineContext 구성.
    # [progress] FE 진행바가 실제 단계 기반으로 차도록 stage_callback 배선 (_tracked_ctx
    # 를 안 거치므로 여기서 직접 연결).
    async def _stage_cb(stage: str) -> None:
        await _set_job_stage(ctx, stage)

    pipeline_ctx = PipelineContext(
        gemini=ctx.get("gemini") or _NullGemini(),
        neo4j=ctx["neo4j"],
        idempotency_key=job_id,
        stage_callback=_stage_cb,
    )
    result = await run_lineage_pipeline(
        pipeline_ctx,
        LineageInput(project_name=project_name, team_id=team_id),
        user_token=user_token,
    )
    return result.model_dump()


class _NullGemini:
    """lineage 가 Gemini 미사용임을 보장 (호출되면 즉시 실패)."""

    async def generate(self, *args, **kwargs):  # pragma: no cover
        raise RuntimeError("analyze_lineage_job 은 Gemini 를 호출하지 않습니다.")


async def autofill_api_specs_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """arq job: autofillApiSpecs — error_cases/auth 빈 API 에 AI 초안 병렬 생성.

    [2026-05 비동기 전환] 이전엔 동기 POST 로 N개 병렬 LLM 을 한 HTTP 요청 안에서
    돌려 axios 180s / 프록시 한계에 걸려 "결국 실패" 했다. 큐 전환으로 enqueue 즉시
    반환 + worker job_timeout 안에서 처리 + FE 폴링. stage 마커로 진행바도 작업량 기반.

    team_id: SPACK 조회 + 프로젝트 락의 스코프 — 이전엔 안 받아 팀 프로젝트에서
    빈(개인 스코프) 그래프를 읽었다.
    """
    job_id = ctx.get("job_id", "unknown")
    logger.info(
        "autofill_api_specs_job start: job=%s project=%s", job_id, project_name
    )

    # 지연 import — 모듈 로드 시점 의존성 최소화.
    from app.pipelines.api_spec_autofill_pipeline import (
        ApiSpecInput,
        AutofillInput,
        run_api_spec_autofill_pipeline,
    )
    from app.service import query_repository

    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email, team_id)
    try:
        # [2026-06 멀티디바이스 이중작업] design 의 Wipe-and-Redraw 와 겹치면
        # 막 지워진/다시 그려지는 그래프에 패치해 유실 — 같은 프로젝트 락으로 직렬화.
        # 읽기(spack 조회)도 락 안에서 — wipe 직후의 빈 그래프를 읽는 것 방지.
        async with master_write_lock(
            ctx.get("redis"), scoped_project(project_name, team_id or None), job_id,
        ):
            # 서버가 SPACK 그래프에서 API 목록 조회 → 파이프라인이 빈 것만 골라 LLM 호출.
            spack = await query_repository.get_spack_graph(project_name, team_id)
            apis = [
                ApiSpecInput(
                    id=str(a.get("id") or ""),
                    name=str(a.get("name") or ""),
                    method=str(a.get("method") or ""),
                    endpoint=str(a.get("endpoint") or ""),
                    description=str(a.get("description") or ""),
                    error_cases=a.get("error_cases") or [],
                    auth=a.get("auth") or {},
                )
                for a in (spack.apis or [])
                if a.get("id")
            ]
            _t0 = time.monotonic()
            result = await run_api_spec_autofill_pipeline(
                pipeline_ctx,
                AutofillInput(project_name=project_name, apis=apis, team_id=team_id),
            )

            # [2026-06-12 연결 채우기] error/auth 채움 후 같은 락 안에서 PRD 연결
            # (API/Entity/Policy ↔ Story) 미연결 노드를 AI 매칭으로 보완 — 완성도
            # 모달의 "지금 이것부터" + "SPACK PRD 연결 상세" 가 함께 채워진다.
            # 실패는 내부에서 강등(예외 미전파) — 이미 저장된 error/auth 결과 보호.
            from app.pipelines.api_spec_autofill_pipeline import resolve_fallback_model
            from app.pipelines.story_link_autofill import run_story_link_autofill

            await pipeline_ctx.emit_stage("autofill:linking")
            link_meta = await run_story_link_autofill(
                pipeline_ctx, project_name, spack,
                team_id=team_id, fallback_model=resolve_fallback_model(),
            )
            result.meta.update(link_meta)

            # [2026-06-10 관측성] 동시성/모델 노브 튜닝의 실측 근거 (락 대기 제외).
            logger.info(
                "autofill_api_specs_job done: job=%s meta=%s elapsed=%.1fs",
                job_id, result.meta, time.monotonic() - _t0,
            )
        await _set_job_stage(ctx, "done")
        return {
            "status": "success",
            "apis": [
                {
                    "id": f.id,
                    "error_cases": f.error_cases,
                    "auth": f.auth,
                    "generated": f.generated,
                    "saved": f.saved,
                }
                for f in result.apis
            ],
            "meta": result.meta,
        }
    finally:
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(
            ctx, user_email, job_id,
            project_key=scoped_project(project_name, team_id or None),
        )


async def delete_meeting_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    version: str,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """arq job: deleteMeeting + Master CPS/PRD rebuild.

    delete_pipeline 은 Master CPS/PRD rebuild 시 LLM 호출 (2회) — 토큰 적재 필요.
    """
    job_id = ctx.get("job_id", "unknown")
    logger.info(
        "delete_meeting_job start: job=%s project=%s version=%s",
        job_id, project_name, version,
    )
    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email, team_id)
    try:
        # [2026-06 감사 G2] delete 도 master CPS/PRD rebuild(쓰기) — 같은 프로젝트
        # merge 와 동시 실행되면 lost update. 동일 락으로 직렬화.
        async with master_write_lock(
            ctx.get("redis"), scoped_project(project_name, team_id or None), job_id,
        ):
            result = await run_delete_meeting_pipeline(
                pipeline_ctx,
                DeleteMeetingInput(project_name=project_name, version=version, team_id=team_id),
            )
        return {
            "status": result.status,
            "message": result.message,
            "project_name": result.project_name,
            "deleted_version": result.deleted_version,
            "remaining_cps_count": result.remaining_cps_count,
            "remaining_prd_count": result.remaining_prd_count,
            "cps_master_rebuilt": result.cps_master_rebuilt,
            "prd_master_rebuilt": result.prd_master_rebuilt,
        }
    finally:
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(
            ctx, user_email, job_id,
            project_key=scoped_project(project_name, team_id or None),
        )


async def github_onboard_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    github_url: str,
    user_token: str | None = None,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """arq job: GitHub URL → V1 + CPS 자동 생성 (Vibe Coding entry — 2026-05-26).

    user_token: 호출자의 GitHub OAuth access_token. private repo 접근 + rate
                limit 확장(5000/hr) 용. 미연결 사용자는 None — public repo 만 가능.
    user_email: quota 토큰 누적 + 등급별 Gemini 모델 분기.

    job 결과 (FE 폴링이 조회):
      {
        "project_name", "github_url", "repo_full_name",
        "v1_markdown_size", "sampled_file_count", "sampled_file_paths",
        "cps_master_id", "cps_delta_id", "cps_mode",
        "diagnostic",
      }
    """
    job_id = ctx.get("job_id", "unknown")
    logger.info(
        "github_onboard_job start: job=%s project=%s url=%s authed=%s",
        job_id, project_name, github_url, bool(user_token),
    )
    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email, team_id)
    # user_token 이 있으면 그것으로 GitHub client 생성, 없으면 anonymous (env GITHUB_TOKEN
    # fallback). lint job 과 동일 패턴.
    github = GitHubClient(user_token=user_token) if user_token else GitHubClient()
    try:
        result = await run_github_onboard_pipeline(
            pipeline_ctx,
            GithubOnboardInput(
                project_name=project_name,
                github_url=github_url,
                user_email=user_email or "",
                team_id=team_id,
            ),
            github_client=github,
        )
        # CPS + PRD 결과를 직렬화 가능 형태로 평탄화.
        cps = result.cps_result
        prd = result.prd_result
        return {
            "project_name": result.project_name,
            "github_url": result.github_url,
            "repo_full_name": result.repo_full_name,
            "v1_markdown_size": result.v1_markdown_size,
            "sampled_file_count": result.sampled_file_count,
            "sampled_file_paths": result.sampled_file_paths,
            "cps_master_id": cps.master_cps_id if cps else None,
            "cps_delta_id": cps.delta_cps_id if cps else None,
            "cps_mode": cps.mode if cps else None,
            "prd_master_id": prd.master_prd_id if prd else None,
            "prd_delta_id": prd.delta_prd_id if prd else None,
            "prd_mode": prd.mode if prd else None,
            "diagnostic": result.diagnostic,
        }
    finally:
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(ctx, user_email, job_id)


async def cleanup_master_prd_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    dry_run: bool = True,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> Dict[str, Any]:
    """arq job: cleanup_master_prd — V1~Vn 누적 master PRD dedupe.

    [2026-05-26] post_meeting_pipeline_job 끝에서 누더기 detection 시 자동 호출.
    수동 호출 라우트는 admin/debug 용 — 일반 사용자에게 노출 X.

    dry_run=True (기본): cleaned markdown 결과만 반환 (FE diff 모달용 — 현재 미사용).
    dry_run=False: 즉시 master apply — 자동 cleanup path 에서 사용.

    user_email: quota 토큰 누적용.
    """
    job_id = ctx.get("job_id", "unknown")
    logger.info(
        "cleanup_master_prd_job start: job=%s project=%s dry_run=%s",
        job_id, project_name, dry_run,
    )
    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email)
    try:
        # [2026-06 감사 G3] cleanup 은 master PRD 를 재작성 — merge/delete 와 동시
        # 실행되면 lost update. 동일 락으로 직렬화 (post_meeting auto-trigger 가
        # enqueue 한 cleanup 이 다음 배치 항목의 merge 와 겹치는 케이스 포함).
        async with master_write_lock(
            ctx.get("redis"), scoped_project(project_name, team_id or None), job_id,
        ):
            result = await run_cleanup_master_prd_pipeline(
                pipeline_ctx,
                CleanupMasterPrdInput(
                    project_name=project_name,
                    user_email=user_email or "",
                    dry_run=dry_run,
                    team_id=team_id,
                ),
            )
        return {
            "status": "success",
            "project_name": result.project_name,
            "before_size": result.before_size,
            "after_size": result.after_size,
            "reduction_pct": result.reduction_pct,
            "master_prd_id": result.master_prd_id,
            "cleaned_markdown": result.cleaned_markdown,
            "original_markdown": result.original_markdown,
            "dry_run": result.dry_run,
        }
    finally:
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(ctx, user_email, job_id)


async def create_md_job(
    ctx: Dict[str, Any],
    *,
    project_name: str,
    user_email: Optional[str] = None,
) -> Dict[str, Any]:
    """arq job: createMD — Spack/DDD/Architecture → MD 3종 (LLM × 3 병렬)."""
    job_id = ctx.get("job_id", "unknown")
    logger.info("create_md_job start: job=%s project=%s", job_id, project_name)
    pipeline_ctx, accumulator, decision = await _tracked_ctx(ctx, user_email)
    try:
        result = await run_create_md_pipeline(
            pipeline_ctx, CreateMdInput(project_name=project_name)
        )
        return {
            "project_name": result.project_name,
            "spack_md": result.spack_md,
            "ddd_md": result.ddd_md,
            "arch_md": result.arch_md,
            "orchestrator_md": result.orchestrator_md,
            "checklist_md": result.checklist_md,
            "diagnostic": result.diagnostic,
        }
    finally:
        await _persist_token_usage(user_email, accumulator, job_id=job_id, bucket=decision.bucket)
        await _release_concurrency_slot(ctx, user_email, job_id)


# ─── Worker lifecycle ────────────────────────────────────────


async def on_startup(ctx: Dict[str, Any]) -> None:
    """Worker 부팅 시 1회: 등급별 Gemini + Neo4j 클라이언트 구성 후 ctx 에 보관.

    [등급별 모델 분기]
    Free / Pro 가 서로 다른 모델을 쓰는 정책 — worker 가 두 GeminiClient 를 미리
    lifeline-shared 로 만들어둠. job 안의 _tracked_ctx 가 user_email → subscription
    조회 후 적절한 인스턴스 선택.

    설정값:
      - settings.gemini_model_for_free (.env: GEMINI_MODEL_FREE, 미설정 시 GEMINI_MODEL)
      - settings.gemini_model_for_pro (.env: GEMINI_MODEL_PRO, 미설정 시 GEMINI_MODEL)
    두 env 모두 미설정 시 두 인스턴스가 같은 모델 — 단일 모델 운영 호환.
    """
    # [2026-05] 관측성 — worker 도 backend 와 동일하게 구조화 로깅 + Sentry 활성.
    # arq 가 자체 stdout 핸들러를 붙이므로 setup_logging 으로 포맷/레벨 통일.
    from app.core.observability import init_sentry, setup_logging
    setup_logging()
    init_sentry(component="worker")

    # 워커 job 메트릭(worker_jobs_total / _duration)을 Prometheus 가 스크랩하도록
    # 독립 노출 서버 기동 — 워커엔 ASGI /metrics 가 없어 필요. METRICS_ENABLED=false
    # 또는 prometheus 미설치 시 no-op (graceful).
    if settings.METRICS_ENABLED:
        from app.core.metrics import start_worker_metrics_server
        if start_worker_metrics_server(settings.WORKER_METRICS_PORT):
            logger.info("워커 메트릭 노출 :%s/metrics", settings.WORKER_METRICS_PORT)

    ctx["gemini_free"] = GeminiClient(model=settings.gemini_model_for_free)
    ctx["gemini_pro"] = GeminiClient(model=settings.gemini_model_for_pro)
    # [2026-06] Lite 오버플로우 풀 — 메인 소진 유료 등급이 강등돼 쓰는 저비용 모델.
    ctx["gemini_lite"] = GeminiClient(model=settings.gemini_model_lite)
    ctx["neo4j"] = _Neo4jProxy()
    logger.info(
        "arq worker startup complete (gemini_free=%s, gemini_pro=%s, gemini_lite=%s)",
        settings.gemini_model_for_free,
        settings.gemini_model_for_pro,
        settings.gemini_model_lite,
    )


async def on_shutdown(ctx: Dict[str, Any]) -> None:
    """Worker 종료 시: 두 GeminiClient HTTP pool 정리 + Neo4j driver close."""
    for key in ("gemini_free", "gemini_pro", "gemini_lite", "gemini"):
        g = ctx.get(key)
        if g is not None and hasattr(g, "aclose"):
            try:
                await g.aclose()
            except Exception as e:  # noqa: BLE001
                logger.warning("gemini client (%s) aclose failed: %s", key, e)
    await neo4j_client.close_driver()
    logger.info("arq worker shutdown complete")
