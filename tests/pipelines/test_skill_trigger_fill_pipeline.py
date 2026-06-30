"""
skill_trigger_fill_pipeline 단위 + e2e fakes.

fillSkillTriggers 동작 검증:
- trigger 가 이미 채워진 skill 은 LLM 호출 없이 건너뜀 (기존 값 보존)
- trigger 빈 skill 만 LLM 호출 — N개 병렬
- LLM mock 결과가 id 기준으로 올바르게 매핑되는지
- 원본 순서 유지
- 일부 LLM 이 빈 결과를 줘도 전체가 깨지지 않음 (generated=False)
"""
from __future__ import annotations

import asyncio
import json
from typing import Dict, List

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.skill_trigger_fill_pipeline import (
    SkillTriggerInput,
    TriggerFillInput,
    _split_targets,
    run_trigger_fill_pipeline,
)
from tests.conftest import FakeNeo4j


# ─── _split_targets (순수 동기 함수) ──────────────────────────


def test_split_targets_separates_empty_and_filled():
    skills = [
        SkillTriggerInput(id="A", name="a", trigger_condition=""),
        SkillTriggerInput(id="B", name="b", trigger_condition="이미 있음"),
        SkillTriggerInput(id="C", name="c", trigger_condition="   "),  # 공백만 → 빈 것
    ]
    targets, skipped = _split_targets(skills)
    assert [s.id for s in targets] == ["A", "C"]
    assert [s.id for s in skipped] == ["B"]


# ─── Fake Gemini: skill 이름에 따라 trigger 응답 매핑 ──────────


class _PerSkillGemini:
    """프롬프트 안의 skill 이름으로 분기해 trigger JSON 반환. 호출 이력 기록."""

    def __init__(self, name_to_trigger: Dict[str, str]) -> None:
        self._map = name_to_trigger
        self.calls: List[str] = []

    async def generate(
        self, prompt: str, *, temperature: float = 0.2, response_schema=None
    ):
        self.calls.append(prompt)
        # 가장 먼저 매칭되는 skill 이름의 trigger 반환.
        for name, trigger in self._map.items():
            if name in prompt:
                from tests.conftest import _FakeResult
                return _FakeResult(text=json.dumps({"trigger_condition": trigger}))
        from tests.conftest import _FakeResult
        return _FakeResult(text=json.dumps({"trigger_condition": ""}))


def _ctx(gemini) -> PipelineContext:
    return PipelineContext(gemini=gemini, neo4j=FakeNeo4j(), idempotency_key="t")


# ─── e2e ──────────────────────────────────────────────────────


pytestmark_async = pytest.mark.asyncio


@pytest.mark.asyncio
async def test_skips_filled_only_calls_empty():
    """trigger 채워진 skill 은 LLM 미호출, 빈 것만 호출 + 매핑."""
    skills = [
        SkillTriggerInput(
            id="SKL-01", name="React Hook 규칙", tags=["react"],
            instructions=["useEffect 의존성 배열을 채워라"],
            trigger_condition="",
        ),
        SkillTriggerInput(
            id="SKL-02", name="DB 인덱스 규칙", trigger_condition="이미 손으로 적음",
        ),
        SkillTriggerInput(
            id="SKL-03", name="API 에러 규칙", tags=["fastapi"],
            instructions=["4xx 를 일관되게 반환"],
            trigger_condition="",
        ),
    ]
    gemini = _PerSkillGemini({
        "React Hook 규칙": "React 컴포넌트를 작성할 때",
        "API 에러 규칙": "API 엔드포인트를 추가하거나 수정할 때",
        "DB 인덱스 규칙": "절대-호출되면-안됨",  # 이게 결과에 나오면 건너뜀 실패
    })
    ctx = _ctx(gemini)

    result = await run_trigger_fill_pipeline(ctx, TriggerFillInput(skills=skills))

    by_id = {f.id: f for f in result.skills}
    # 원본 순서 유지
    assert [f.id for f in result.skills] == ["SKL-01", "SKL-02", "SKL-03"]
    # 빈 것만 생성됨
    assert by_id["SKL-01"].generated is True
    assert by_id["SKL-01"].trigger_condition == "React 컴포넌트를 작성할 때"
    assert by_id["SKL-03"].generated is True
    assert by_id["SKL-03"].trigger_condition == "API 엔드포인트를 추가하거나 수정할 때"
    # 채워진 것은 기존 값 보존 + generated False
    assert by_id["SKL-02"].generated is False
    assert by_id["SKL-02"].trigger_condition == "이미 손으로 적음"
    # LLM 은 빈 trigger 2개에 대해서만 호출됨 (건너뛴 SKL-02 미호출)
    assert len(gemini.calls) == 2
    # 메타 카운트
    assert result.meta["total"] == 3
    assert result.meta["targetCount"] == 2
    assert result.meta["skippedCount"] == 1
    assert result.meta["generatedCount"] == 2


@pytest.mark.asyncio
async def test_parallel_gather_structure(monkeypatch):
    """대상 N개가 asyncio.gather 로 병렬 호출되는지 — 동시성 검증.

    각 LLM 호출이 진입하면 카운트를 올리고 배리어가 풀릴 때까지 대기.
    직렬이면 두 번째 호출이 시작되지 못해 max_concurrent==1 로 멈춘다.
    병렬(gather)이면 둘 다 진입 후 풀려 max_concurrent==2.
    """
    in_flight = 0
    max_concurrent = 0
    gate = asyncio.Event()

    class _ConcurrentGemini:
        def __init__(self) -> None:
            self.calls: List[str] = []

        async def generate(self, prompt, *, temperature=0.2, response_schema=None):
            nonlocal in_flight, max_concurrent
            self.calls.append(prompt)
            in_flight += 1
            max_concurrent = max(max_concurrent, in_flight)
            # 두 호출이 모두 진입할 때까지 대기 (gather 병렬이어야 풀림).
            if in_flight >= 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), timeout=2.0)
            in_flight -= 1
            from tests.conftest import _FakeResult
            return _FakeResult(text=json.dumps({"trigger_condition": "x 할 때"}))

    skills = [
        SkillTriggerInput(id="A", name="a", trigger_condition=""),
        SkillTriggerInput(id="B", name="b", trigger_condition=""),
    ]
    gemini = _ConcurrentGemini()
    result = await run_trigger_fill_pipeline(_ctx(gemini), TriggerFillInput(skills=skills))

    assert max_concurrent == 2, "두 LLM 호출이 동시에 in-flight 여야 함 (gather 병렬)"
    assert all(f.generated for f in result.skills)


@pytest.mark.asyncio
async def test_llm_empty_result_marks_not_generated():
    """LLM 이 빈 trigger 를 주면 generated=False, 나머지는 정상 — 부분 실패 격리."""
    skills = [
        SkillTriggerInput(id="A", name="good", trigger_condition=""),
        SkillTriggerInput(id="B", name="bad", trigger_condition=""),
    ]
    gemini = _PerSkillGemini({
        "good": "정상 조건일 때",
        "bad": "",  # 빈 응답 → 생성 실패로 간주
    })
    result = await run_trigger_fill_pipeline(_ctx(gemini), TriggerFillInput(skills=skills))
    by_id = {f.id: f for f in result.skills}
    assert by_id["A"].generated is True
    assert by_id["A"].trigger_condition == "정상 조건일 때"
    assert by_id["B"].generated is False
    assert by_id["B"].trigger_condition == ""
    assert result.meta["generatedCount"] == 1


@pytest.mark.asyncio
async def test_no_targets_no_llm_calls():
    """모든 skill 의 trigger 가 채워져 있으면 LLM 호출 0."""
    skills = [
        SkillTriggerInput(id="A", name="a", trigger_condition="t1"),
        SkillTriggerInput(id="B", name="b", trigger_condition="t2"),
    ]
    gemini = _PerSkillGemini({"a": "should-not", "b": "should-not"})
    result = await run_trigger_fill_pipeline(_ctx(gemini), TriggerFillInput(skills=skills))
    assert gemini.calls == []
    assert all(not f.generated for f in result.skills)
    assert [f.trigger_condition for f in result.skills] == ["t1", "t2"]
    assert result.meta["targetCount"] == 0


@pytest.mark.asyncio
async def test_empty_skills_returns_empty():
    gemini = _PerSkillGemini({})
    result = await run_trigger_fill_pipeline(_ctx(gemini), TriggerFillInput(skills=[]))
    assert result.skills == []
    assert result.meta["total"] == 0
    assert gemini.calls == []
