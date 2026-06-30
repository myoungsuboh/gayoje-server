"""
라우트의 sync/wait LLM 호출용 quota 토큰 추적 컨텍스트.

[배경]
arq async 큐 job 은 `app/queue/jobs.py` 의 `_tracked_ctx` + `_persist_token_usage`
로 토큰 누적. sync(wait=true 또는 큐 미사용) 라우트는 라우트 함수 안에서 직접 LLM
호출 → 동일한 누적/적재 로직이 필요. 이 모듈이 그 헬퍼.

[Usage]
    async with tracked_pipeline_context(
        user_email=current_user.email,
        idempotency_key=task_id,
    ) as ctx:
        result = await run_xxx_pipeline(ctx, ...)

yield 받은 ctx.gemini 는 TrackedGemini wrap → 모든 generate() 호출이 accumulator
에 자동 누적. context 종료 시 user_email + delta>0 이면 `add_tokens`.

[기존 _build_context / _ctx 와의 관계]
v2_routes.py / gateway_routes.py 등에 정의된 _build_context()/_ctx() 는 wait/sync
LLM 호출 외에도 사용됨 (lineage 등 LLM 미사용 경로 포함). LLM 호출 라우트만 골라
이 헬퍼로 대체. 점진 마이그레이션 안전.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from app.clients import neo4j_client
from app.clients.gemini_client import GeminiClient, TokenAccumulator, TrackedGemini
from app.core import quota
from app.pipelines.base import PipelineContext
from app.service import usage_repository
from app.service.user_repository import SUBSCRIPTION_FREE

logger = logging.getLogger(__name__)


# [2026-05] 공통 어댑터 — pipelines.base.Neo4jClientProxy 로 통합
# (run_cypher + run_in_transaction 모두 노출). 이전엔 9개 라우트 파일에 같은
# 클래스가 중복 정의됐던 패턴 제거.
from app.pipelines.base import Neo4jClientProxy as _Neo4jProxy


@asynccontextmanager
async def tracked_pipeline_context(
    *,
    user_email: Optional[str],
    idempotency_key: str,
    team_id: str = "",
):
    """라우트 sync LLM 호출 토큰 추적 + 종료 시 add_tokens 적재.

    [동작]
    - user_email 로 subscription 조회 → 등급별 Gemini 모델 결정
    - 새 GeminiClient(model=...) + TokenAccumulator + TrackedGemini wrap 으로 ctx 구성.
    - yield 받은 ctx 의 모든 ctx.gemini.generate() 호출이 자동 누적.
    - context 종료 시 (성공/실패 양쪽) user_email + total_tokens>0 이면 add_tokens.
    - Neo4j 일시 장애로 add_tokens 가 실패해도 swallow — 라우트 응답을 망치지 않음.
    - inner GeminiClient.aclose() 로 HTTP pool 정리.

    [수명]
    한 번의 라우트 호출 = 한 인스턴스. 라우트 끝나면 모두 소멸. worker 의 lifeline-shared
    gemini 와 분리 — 매 요청마다 새 GeminiClient 가 만들어지지만 sync 라우트는
    빈도가 낮아 부담 작음 (운영은 async 큐 우선).

    Args:
        user_email: 토큰 누적 대상 + 등급별 모델 선택. None 이면 누적 skip + free 모델.
        idempotency_key: PipelineContext.idempotency_key 로 그대로 전달. task_id 사용.
    """
    # [2026-06] 결정 기반 모델/버킷 — 메인 소진 유료 등급은 Lite 로 강등(overflow).
    # user_email 없으면 free 모델 (토큰 누적도 skip).
    # [2026-06 신선도] admin 한도 변경이 즉시 반영되도록 결정 직전 DB 재로드 (15s TTL).
    await quota.ensure_overrides_fresh()
    if user_email:
        decision = await quota.resolve_quota_decision(user_email)
    else:
        decision = quota.QuotaDecision(mode="main", subscription_type=SUBSCRIPTION_FREE, bucket="main")
    subscription = decision.subscription_type
    model = quota.model_for_decision(decision)
    accumulator = TokenAccumulator()
    inner = GeminiClient(model=model)
    # [2026-06 overflow 모델 강제] overflow(메인 소진 유료)면 lite 모델을 강제.
    # 인터뷰 등 일부 파이프라인이 generate(model="gemini-2.5-flash") 로 비싼 모델을
    # 명시 강제해 overflow→lite 강등을 우회하던 버그 차단. main/free 는 강제 안 함.
    force_model = model if decision.mode == "overflow" else None
    tracked = TrackedGemini(inner, accumulator, force_model=force_model)
    ctx = PipelineContext(
        gemini=tracked,
        neo4j=_Neo4jProxy(),
        idempotency_key=idempotency_key,
        # [Phase 2D] 멀티테넌시 ID 격리 — pipeline 안의 _derive_ids / Project 노드
        # MATCH 가 ctx.user_email 사용. 비면 옛 형식 회귀 + cross-tenant 충돌 위험.
        user_email=user_email or "",
        team_id=team_id or "",
    )
    logger.info(
        "tracked_pipeline_context: user=%s subscription=%s model=%s mode=%s key=%s",
        user_email, subscription, model, decision.mode, idempotency_key,
    )
    try:
        yield ctx
    finally:
        total = accumulator.total.total_tokens
        if total > 0 and user_email:
            try:
                new_total = await usage_repository.add_tokens(
                    user_email, total, bucket=decision.bucket
                )
                logger.info(
                    "quota[sync]: tokens +%d → %s (user=%s, bucket=%s, key=%s)",
                    total, new_total, user_email, decision.bucket, idempotency_key,
                )
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning(
                    "quota[sync]: add_tokens failed (user=%s, key=%s, delta=%d): %s",
                    user_email, idempotency_key, total, e,
                )
        elif total > 0:
            logger.warning(
                "quota[sync]: user_email 없음 — token 누적 skip (key=%s, tokens=%d)",
                idempotency_key, total,
            )
        # inner GeminiClient pool 정리 — 라우트 단위 인스턴스라 명시 close.
        try:
            await inner.aclose()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
