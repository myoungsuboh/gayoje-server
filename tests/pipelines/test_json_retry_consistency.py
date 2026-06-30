"""
LLM JSON 파싱 strict retry 일관 적용 회귀 가드.

[배경]
2026-05 이전: cps/prd/design/lint 의 JSON LLM 호출이 generate_json_with_retry
없이 raw ctx.gemini.generate + extract_json_object 직접 호출. 첫 응답이 fence/
머릿말 섞이면 빈 dict → ValueError 또는 빈 결과. retry 정책 함수마다 들쭉날쭉.

[가드]
- 첫 응답이 unparseable, 두 번째는 valid JSON → 결과 정상 반환 (retry 작동)
- 두 시도 모두 unparseable → 빈 dict 또는 ValueError (호출자 분기 책임)

[검증 대상]
- cps_pipeline.call_cps_agent
- cps_pipeline.call_impact_analyzer
- prd_pipeline.call_prd_graph
- prd_pipeline.call_impact_analyzer
- design_pipeline.call_spack_agent
- design_pipeline.call_ddd_agent
- design_pipeline.call_architecture_agent
- lint_pipeline 의 residual LLM 호출 (직접 호출 어려움 — generate_json_with_retry
  import 만 검증)
"""
from __future__ import annotations

from typing import List

import pytest

from app.clients.gemini_client import GeminiResult, TokenUsage
from app.pipelines.base import PipelineContext
from app.pipelines.cps_pipeline import (
    CpsInput,
    call_cps_agent,
    call_impact_analyzer,
)


pytestmark = pytest.mark.asyncio


class _GeminiWithSequence:
    """반환값을 순차 큐로. 첫 시도 / 재시도 시뮬레이션."""

    def __init__(self, texts: List[str]) -> None:
        self._texts = list(texts)
        self.calls: List[str] = []

    async def generate(self, prompt: str, *, temperature: float = 0.2):
        self.calls.append(prompt)
        text = self._texts.pop(0) if self._texts else ""
        return GeminiResult(
            text=text, model="fake", finish_reason="stop",
            usage=TokenUsage(),
        )


class _FakeNeo:
    """run_cypher 응답 미사용 — 호출만 받음."""

    async def run_cypher(self, cypher, params=None):
        return []


def _ctx(gemini) -> PipelineContext:
    return PipelineContext(gemini=gemini, neo4j=_FakeNeo(), idempotency_key="t")


def _cps_input() -> CpsInput:
    return CpsInput(
        project_name="p", version="v1", date="2026-01-01",
        meeting_content="meeting",
    )


# ─── cps_agent: 첫 시도 fence 섞임 → 두 번째 정상 ─────────────


async def test_cps_agent_recovers_via_strict_retry():
    """[회귀 가드] 첫 응답이 JSON 파싱 실패 → 두 번째 strict prefix 응답이 정상."""
    gemini = _GeminiWithSequence([
        # 1차 — JSON 파싱 자체 실패 (객체 추출 안 됨)
        "여기 답입니다: 다음 줄에 JSON 을 드릴게요... (이상한 응답)",
        # 2차 — strict prefix 부착 후 정상 응답
        '{"_harness_metadata":{},"nodes":[{"id":"p1","label":"Problem","properties":{}}],"relationships":[]}',
    ])
    out = await call_cps_agent(_ctx(gemini), _cps_input())
    assert "nodes" in out and len(out["nodes"]) == 1
    # generate 두 번 호출됨 (retry 작동)
    assert len(gemini.calls) == 2
    # 두 번째 호출은 strict prefix 가 prepend 됨
    assert "SYSTEM" in gemini.calls[1] or "strict" in gemini.calls[1].lower() \
        or "반드시" in gemini.calls[1]


async def test_cps_agent_first_success_no_retry():
    """첫 시도 성공이면 retry 안 함 (불필요 토큰 호출 회피)."""
    gemini = _GeminiWithSequence([
        '{"_harness_metadata":{},"nodes":[{"id":"x","label":"Problem","properties":{}}],"relationships":[]}',
    ])
    await call_cps_agent(_ctx(gemini), _cps_input())
    assert len(gemini.calls) == 1


async def test_cps_agent_both_fail_raises_value_error():
    """두 번째도 실패면 ValueError — 호출자가 처리."""
    gemini = _GeminiWithSequence([
        "garbage 1",
        "garbage 2",
    ])
    with pytest.raises(ValueError, match="unparseable"):
        await call_cps_agent(_ctx(gemini), _cps_input())
    assert len(gemini.calls) == 2


# ─── impact_analyzer: 빈 dict 도 안전 (방어 기본값) ──────────


async def test_impact_analyzer_both_fail_safe_defaults():
    """둘 다 실패 → 빈 dict 라도 affected_sections/removed_* 가 [] 로 흡수."""
    gemini = _GeminiWithSequence(["bogus", "bogus2"])
    out = await call_impact_analyzer(_ctx(gemini), [], "content")
    # 모든 키가 기본값 — ValueError 안 던짐
    assert out["affected_sections"] == []
    assert out["removed_prb_ids"] == []
    assert out["removed_res_ids"] == []
    assert out["analysis"] == ""
    assert len(gemini.calls) == 2  # retry 했음


# ─── design / prd pipeline 의 import 검증 ────────────────────


def test_all_pipelines_import_generate_json_with_retry():
    """5개 파이프라인 모듈이 generate_json_with_retry 를 import 했는지 — 회귀 가드."""
    import app.pipelines.cps_pipeline as cps_mod
    import app.pipelines.prd_pipeline as prd_mod
    import app.pipelines.design_pipeline as design_mod
    import app.pipelines.lint_pipeline as lint_mod
    import app.pipelines.skill_recommend_pipeline as skill_mod
    for mod, name in [
        (cps_mod, "cps_pipeline"),
        (prd_mod, "prd_pipeline"),
        (design_mod, "design_pipeline"),
        (lint_mod, "lint_pipeline"),
        (skill_mod, "skill_recommend_pipeline"),
    ]:
        assert hasattr(mod, "generate_json_with_retry"), (
            f"{name} 가 generate_json_with_retry import 안 함 — retry 미적용 회귀 위험"
        )


async def test_skill_picker_uses_retry():
    """[회귀] call_skill_picker 가 첫 응답 unparseable 시 재시도."""
    from app.pipelines.skill_recommend_pipeline import call_skill_picker
    gemini = _GeminiWithSequence([
        "garbage",
        '{"recommended":[{"id":"SKL-01"}]}',
    ])
    out = await call_skill_picker(
        _ctx(gemini),
        {
            "project_name": "p",
            "cps_text": "x", "prd_text": "y",
            "catalog_json": "[]",
            "catalog_ids": set(),
        },
    )
    assert "recommended" in out
    assert len(gemini.calls) == 2
