"""
TokenAccumulator + TrackedGemini 단위 테스트.

[검증 범위]
- TokenAccumulator: add() 가 TokenUsage 를 누적 (operator + 호출)
- TrackedGemini: inner.generate() 호출 → usage 추출 후 accumulator 에 add → result 반환
- TrackedGemini: inner 가 usage 미보유 응답 반환해도 안전 (defensive getattr)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from app.clients.gemini_client import (
    GeminiResult,
    TokenAccumulator,
    TokenUsage,
    TrackedGemini,
)


# ─── TokenAccumulator ────────────────────────────────────────


def test_accumulator_starts_at_zero():
    acc = TokenAccumulator()
    assert acc.total.prompt_tokens == 0
    assert acc.total.completion_tokens == 0
    assert acc.total.total_tokens == 0


def test_accumulator_adds_single_usage():
    acc = TokenAccumulator()
    acc.add(TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30))
    assert acc.total.prompt_tokens == 10
    assert acc.total.completion_tokens == 20
    assert acc.total.total_tokens == 30


def test_accumulator_sums_multiple_calls():
    acc = TokenAccumulator()
    acc.add(TokenUsage(prompt_tokens=5, completion_tokens=10, total_tokens=15))
    acc.add(TokenUsage(prompt_tokens=20, completion_tokens=30, total_tokens=50))
    assert acc.total.prompt_tokens == 25
    assert acc.total.completion_tokens == 40
    assert acc.total.total_tokens == 65


# ─── TrackedGemini (async) ───────────────────────────────────


pytestmark = pytest.mark.asyncio


class _FakeInner:
    """GeminiClient stub — generate() 가 미리 정한 GeminiResult 반환."""

    def __init__(self, result: GeminiResult):
        self._result = result
        self.calls: list[str] = []

    async def generate(self, prompt: str, *, temperature: float = 0.2) -> GeminiResult:
        self.calls.append(prompt)
        return self._result


async def test_tracked_gemini_accumulates_usage_per_call():
    inner = _FakeInner(
        GeminiResult(
            text="hello",
            model="fake",
            finish_reason="STOP",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )
    )
    acc = TokenAccumulator()
    tracked = TrackedGemini(inner, acc)

    # 1회 호출
    r1 = await tracked.generate("prompt 1")
    assert r1.text == "hello"
    assert acc.total.total_tokens == 30

    # 2회 호출 — 같은 inner 인스턴스라 같은 usage 가 또 누적
    r2 = await tracked.generate("prompt 2")
    assert r2.text == "hello"
    assert acc.total.total_tokens == 60

    # inner.generate 가 두 번 호출됐는지
    assert len(inner.calls) == 2


@dataclass
class _ResultWithoutUsage:
    """GeminiResult 의 usage 없는 변형 — conftest.py 의 FakeGemini._FakeResult 시뮬레이션."""

    text: str
    model: str = "fake"
    finish_reason: Optional[str] = "STOP"


class _FakeInnerNoUsage:
    async def generate(self, prompt: str, *, temperature: float = 0.2):
        return _ResultWithoutUsage(text="no usage")


async def test_tracked_gemini_handles_result_without_usage():
    """fake 응답이 usage 필드 미보유여도 예외 없이 통과 + accumulator 변경 없음."""
    inner = _FakeInnerNoUsage()
    acc = TokenAccumulator()
    tracked = TrackedGemini(inner, acc)

    result = await tracked.generate("anything")
    assert result.text == "no usage"
    # usage 가 없으니 누적 안 됨
    assert acc.total.total_tokens == 0


async def test_tracked_gemini_passes_through_temperature():
    """temperature 인자가 inner 에 그대로 전달되는지."""
    received: dict = {}

    class _CapturingInner:
        async def generate(self, prompt: str, *, temperature: float = 0.2):
            received["temperature"] = temperature
            return GeminiResult(
                text="x", model="fake", finish_reason="STOP",
                usage=TokenUsage(total_tokens=1),
            )

    tracked = TrackedGemini(_CapturingInner(), TokenAccumulator())
    await tracked.generate("p", temperature=0.7)
    assert received["temperature"] == 0.7


# ─── mid-job 강등 (2026-06 race 안전망) ───────────────────────


class _CountingInner:
    """generate() 마다 고정 usage 반환 + 어떤 inner 가 호출됐는지 라벨로 추적."""

    def __init__(self, label: str, per_call_tokens: int):
        self.label = label
        self._t = per_call_tokens
        self.calls = 0

    async def generate(self, prompt: str, *, temperature: float = 0.2, **kw) -> GeminiResult:
        self.calls += 1
        return GeminiResult(
            text=f"{self.label}-{self.calls}", model=self.label, finish_reason="STOP",
            usage=TokenUsage(prompt_tokens=self._t, completion_tokens=0, total_tokens=self._t),
        )


async def test_downgrade_switches_to_lite_when_main_limit_crossed():
    """base + 누적이 main_limit 을 넘는 순간 이후 호출이 lite inner 로 전환."""
    main = _CountingInner("main", per_call_tokens=100_000)
    lite = _CountingInner("lite", per_call_tokens=100_000)
    acc = TokenAccumulator()
    # base 400K, 한도 500K → 누적 100K 한 번 더 하면 500K 도달 → 다음부터 lite.
    tracked = TrackedGemini(
        main, acc, downgrade_lite_inner=lite, base_usage=400_000, main_limit=500_000,
    )

    r1 = await tracked.generate("call 1")   # 400K+100K=500K → 강등 트리거
    assert r1.model == "main"               # 이 호출은 아직 main 풀
    assert acc.main_bucket_tokens == 100_000  # 강등 시점 누적 기록

    r2 = await tracked.generate("call 2")   # 이미 강등 → lite 풀
    r3 = await tracked.generate("call 3")
    assert r2.model == "lite"
    assert r3.model == "lite"
    assert main.calls == 1 and lite.calls == 2
    assert acc.total.total_tokens == 300_000


async def test_no_downgrade_when_under_limit():
    """한도 미만이면 강등 없음 — 전량 main, main_bucket_tokens 는 None 유지."""
    main = _CountingInner("main", per_call_tokens=50_000)
    lite = _CountingInner("lite", per_call_tokens=50_000)
    acc = TokenAccumulator()
    tracked = TrackedGemini(
        main, acc, downgrade_lite_inner=lite, base_usage=0, main_limit=500_000,
    )
    for _ in range(3):
        await tracked.generate("p")
    assert main.calls == 3 and lite.calls == 0
    assert acc.main_bucket_tokens is None


async def test_no_downgrade_when_not_armed():
    """lite inner 미제공(armed 아님)이면 한도를 넘어도 강등 안 함 (graceful)."""
    main = _CountingInner("main", per_call_tokens=600_000)
    acc = TokenAccumulator()
    tracked = TrackedGemini(main, acc, base_usage=0, main_limit=500_000)  # lite 없음
    await tracked.generate("p")  # 600K > 500K 지만 강등 불가
    assert acc.main_bucket_tokens is None
    await tracked.generate("p")
    assert main.calls == 2


async def test_downgrade_triggers_only_once():
    """한 번 강등되면 이후 재트리거/덮어쓰기 없음 (main_bucket_tokens 고정)."""
    main = _CountingInner("main", per_call_tokens=300_000)
    lite = _CountingInner("lite", per_call_tokens=300_000)
    acc = TokenAccumulator()
    tracked = TrackedGemini(
        main, acc, downgrade_lite_inner=lite, base_usage=0, main_limit=500_000,
    )
    await tracked.generate("p")  # 300K < 500K, no downgrade
    await tracked.generate("p")  # 600K >= 500K → 강등, split=600K
    first_split = acc.main_bucket_tokens
    await tracked.generate("p")  # lite, split 불변
    assert first_split == 600_000
    assert acc.main_bucket_tokens == 600_000
    assert lite.calls == 1 and main.calls == 2


# ─── force_model: overflow 모델 강제 (2026-06) ────────────────


class _ModelCapturingInner:
    """generate() 에 전달된 model 인자를 기록."""

    def __init__(self):
        self.models: list = []

    async def generate(self, prompt: str, *, temperature: float = 0.2,
                       response_schema=None, model=None, **kw) -> GeminiResult:
        self.models.append(model)
        return GeminiResult(
            text="x", model=model or "default", finish_reason="STOP",
            usage=TokenUsage(total_tokens=1),
        )


async def test_force_model_overrides_caller_model():
    """force_model 설정 시 호출자의 명시 model 을 무시하고 강제 모델 사용."""
    inner = _ModelCapturingInner()
    tracked = TrackedGemini(inner, TokenAccumulator(), force_model="gemini-2.5-flash-lite")
    # 인터뷰처럼 비싼 flash 명시 — 무시돼야 함.
    await tracked.generate("p", model="gemini-2.5-flash")
    await tracked.generate("p", model="gemini-2.5-pro")
    assert inner.models == ["gemini-2.5-flash-lite", "gemini-2.5-flash-lite"]


async def test_no_force_model_passes_caller_model_through():
    """force_model 미설정이면 호출자 model 그대로 전달 (기존 동작)."""
    inner = _ModelCapturingInner()
    tracked = TrackedGemini(inner, TokenAccumulator())
    await tracked.generate("p", model="gemini-2.5-flash")
    await tracked.generate("p")  # model 미지정
    assert inner.models == ["gemini-2.5-flash", None]


async def test_downgrade_activates_force_model():
    """mid-job 강등이 트리거되면 이후 호출에 downgrade_force_model 강제."""
    main = _CountingInner("main", per_call_tokens=600_000)
    lite = _ModelCapturingInner()
    acc = TokenAccumulator()
    tracked = TrackedGemini(
        main, acc, downgrade_lite_inner=lite, base_usage=0, main_limit=500_000,
        downgrade_force_model="gemini-2.5-flash-lite",
    )
    await tracked.generate("p", model="gemini-2.5-flash")  # 600K>=500K → 강등
    # 강등 후 호출 — lite inner + 모델 강제.
    await tracked.generate("p", model="gemini-2.5-flash")
    assert lite.models == ["gemini-2.5-flash-lite"]
