"""
skill_improve_pipeline 단위 테스트.

improveSkill 동작 검증:
- LLM 개선 결과가 매핑되는지 (이름/지시사항/조건/범위/설명)
- 지시사항이 비면 fallback(원본 유지, improved=False) — AI 가 망치지 않게
- unparseable 이면 fallback
- 개선 결과의 빈 필드는 원본으로 채움 (의도 보존)
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.skill_improve_pipeline import (
    SkillImproveInput,
    run_skill_improve_pipeline,
)
from tests.conftest import FakeNeo4j, _FakeResult


class _Gemini:
    """고정 payload 를 반환하는 fake. dict 면 JSON 직렬화, str 이면 그대로(unparseable 테스트용)."""

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    async def generate(self, prompt, *, temperature=0.3, response_schema=None):
        self.calls.append(prompt)
        text = json.dumps(self._payload) if isinstance(self._payload, dict) else self._payload
        return _FakeResult(text=text)


def _ctx(gemini) -> PipelineContext:
    return PipelineContext(gemini=gemini, neo4j=FakeNeo4j(), idempotency_key="t")


@pytest.mark.asyncio
async def test_improves_vague_draft():
    """모호한 초안 → 구체적 규칙으로 개선 + 필드 매핑."""
    gemini = _Gemini({
        "improved_name": "React 컴포넌트 상태 관리 규칙",
        "improved_scope": "Frontend",
        "improved_trigger_condition": "React 컴포넌트를 작성할 때",
        "improved_instructions": [
            "state 를 직접 변경하지 말고 setState 를 사용한다",
            "useEffect 의존성 배열을 빠짐없이 명시한다",
        ],
        "explanation": "모호한 이름과 지시를 측정 가능한 기준으로 구체화했습니다.",
    })
    result = await run_skill_improve_pipeline(
        _ctx(gemini),
        SkillImproveInput(name="Rule 1", instructions=["state 잘 관리"], tags=["react"]),
    )
    assert result.improved is True
    assert result.name == "React 컴포넌트 상태 관리 규칙"
    assert result.scope == "Frontend"
    assert result.trigger_condition == "React 컴포넌트를 작성할 때"
    assert len(result.instructions) == 2
    assert result.explanation
    assert len(gemini.calls) == 1


@pytest.mark.asyncio
async def test_empty_instructions_falls_back_to_original():
    """LLM 이 지시사항을 못 주면 원본 유지 (AI 가 사용자 입력을 망치지 않게)."""
    gemini = _Gemini({
        "improved_name": "뭔가",
        "improved_instructions": [],
        "explanation": "x",
    })
    original = SkillImproveInput(name="원본 이름", instructions=["원본 지시"], scope="Backend")
    result = await run_skill_improve_pipeline(_ctx(gemini), original)
    assert result.improved is False
    assert result.name == "원본 이름"
    assert result.instructions == ["원본 지시"]
    assert result.scope == "Backend"


@pytest.mark.asyncio
async def test_unparseable_falls_back():
    """LLM 응답이 JSON 이 아니면 fallback(원본 유지)."""
    gemini = _Gemini("이건 JSON 이 아님")
    original = SkillImproveInput(name="원본", instructions=["지시"])
    result = await run_skill_improve_pipeline(_ctx(gemini), original)
    assert result.improved is False
    assert result.name == "원본"
    assert result.instructions == ["지시"]


@pytest.mark.asyncio
async def test_partial_fields_keep_original():
    """개선 결과에서 빈 필드(scope/trigger)는 원본으로 채운다 (의도 보존)."""
    gemini = _Gemini({
        "improved_name": "개선된 이름",
        "improved_instructions": ["구체적 지시 1"],
        # scope, trigger_condition, explanation 누락
    })
    original = SkillImproveInput(
        name="x", instructions=["y"], scope="원본범위", trigger_condition="원본조건",
    )
    result = await run_skill_improve_pipeline(_ctx(gemini), original)
    assert result.improved is True
    assert result.name == "개선된 이름"
    assert result.scope == "원본범위"          # 누락 → 원본 유지
    assert result.trigger_condition == "원본조건"  # 누락 → 원본 유지
