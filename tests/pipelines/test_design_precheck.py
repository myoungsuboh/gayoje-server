"""
Design fail-fast — 누더기(degenerate) + 거대 PRD 면 느린 SPACK/DDD/Architecture LLM
으로 진입하기 전에 즉시 중단(DesignPrecheckFailed). 10분 hang + 토큰 낭비 차단.

핵심 정밀도:
  - dirty AND 과대(>30KB) 일 때만 fail-fast. (운영 사고 케이스)
  - 작은 dirty PRD → 통과(설계 빨리 됨). 거대 clean PRD → 통과(정상 큰 프로젝트).
  - cleanup(Stage 1.5)을 먼저 시도하고, 그래도 여전히 누더기+거대면 중단.
"""
from __future__ import annotations

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.design_pipeline import DesignInput
from app.pipelines.design_pipeline import pipeline as design_pipeline
from app.pipelines.design_pipeline.pipeline import (
    DesignPrecheckFailed,
    _design_precheck,
    run_design_pipeline,
)
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio


# 작은 dirty PRD (Product Vision 6개) — 설계는 빨리 되므로 통과시켜야 함.
_SMALL_DIRTY = (
    "## Master PRD\n### 1. Product Overview\n"
    + "".join(f"- **Product Vision**: V{i} 자동화\n" for i in range(1, 7))
    + "### 2. Epic & User Story Map\n#### 📦 [Epic-01] 관리\n- `[Story-01.1]` 발행\n"
    + "### 3. Screen Architecture\n#### 🖥️ [Screen: Home]\n- `[Story-01.1]` 발행\n"
    + "### 4. Global Non-Functional Requirements\n- 응답 1초\n"
)
# 거대(>30KB) padding — Product Vision/Story 참조 없는 중립 텍스트.
_BIG_PAD = "\n- 응답 시간 1초 이내 / OAuth 2.0 / RBAC / HTTPS 암호화 공통 제약 반복" * 1500
_LARGE_DIRTY = _SMALL_DIRTY + _BIG_PAD          # dirty + 거대 → fail-fast 대상
_LARGE_CLEAN = (                                # 거대하지만 clean(비전 1개, 정합) → 통과
    "## Master PRD\n### 1. Product Overview\n- **통합 비전**: 펀딩 플랫폼\n"
    "### 2. Epic & User Story Map\n#### 📦 [Epic-01] 관리\n- `[Story-01.1]` 발행\n"
    "### 3. Screen Architecture\n#### 🖥️ [Screen: Home]\n- `[Story-01.1]` 발행\n"
    "### 4. Global Non-Functional Requirements\n- 응답 1초\n"
) + _BIG_PAD


def test_precheck_flags_large_dirty_prd():
    out = _design_precheck(_LARGE_DIRTY)
    assert out is not None
    assert "reason" in out and out["reason"]
    assert out["size_bytes"] > 30_000


def test_precheck_passes_large_but_clean_prd():
    # 거대해도 누더기가 아니면(정상 큰 프로젝트) 막지 않음.
    assert _design_precheck(_LARGE_CLEAN) is None


def test_precheck_passes_small_dirty_prd():
    # dirty 해도 작으면 설계가 빨리 끝나므로 통과(기존 동작 보존).
    assert _design_precheck(_SMALL_DIRTY) is None


async def test_run_design_pipeline_fails_fast_before_agents(monkeypatch):
    """거대+누더기 PRD: cleanup 시도 후에도 그대로면 SPACK 호출 전에 즉시 중단."""
    # cleanup 이 고치지 못한 상황 모사 — 원본(거대 dirty) 유지.
    async def _no_cleanup(ctx, project_name, prd_content):
        return {
            "attempted": True, "applied": False, "cleaned_content": "",
            "reduction_pct": 0, "dirty_diagnostic": {}, "failure_reason": "LLM fail",
        }
    monkeypatch.setattr(design_pipeline, "_maybe_auto_cleanup_dirty_prd", _no_cleanup)

    gemini = FakeGemini(lambda p: "should not be called")
    neo = FakeNeo4j(responses=[[{
        "master_prd_id": "doc_prd_master_x", "prd_content": _LARGE_DIRTY,
        "last_updated": 0, "related_master_cps_id": None, "absorbed_prd_ids": [],
    }]])
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="precheck")

    with pytest.raises(DesignPrecheckFailed):
        await run_design_pipeline(ctx, DesignInput(project_name="x"))

    # 어떤 LLM agent 도 호출되지 않음 — fail-fast (10분 hang 차단).
    assert len(gemini.calls) == 0
    # 최종 저장 트랜잭션도 없음 — 기존 설계 데이터 보존.
    save_calls = [e for e in neo.executed if "SET" in (e.get("cypher") or "").upper()]
    assert save_calls == []
