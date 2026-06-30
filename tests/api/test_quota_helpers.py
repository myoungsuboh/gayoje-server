"""
tracked_pipeline_context 헬퍼 단위 테스트 — sync/wait 라우트의 토큰 적재.

[검증 범위]
- yield 받은 ctx.gemini 가 TrackedGemini wrap 인스턴스
- LLM 호출 후 accumulator 에 자동 누적
- context 종료 시 user_email + total>0 이면 add_tokens 호출
- user_email=None 시 누적 skip (warning log)
- delta=0 시 add_tokens 호출 안 함 (불필요 라운드트립 회피)
- 예외 발생해도 finally 절 동작
- Neo4j add_tokens 실패해도 swallow (라우트 응답 망치지 않음)
- inner GeminiClient.aclose() 호출 (HTTP pool 정리)
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import pytest

from app.api._quota_helpers import tracked_pipeline_context
from app.clients.gemini_client import (
    GeminiResult,
    TokenUsage,
    TrackedGemini,
)

pytestmark = pytest.mark.asyncio


# ─── Fake helpers ───────────────────────────────────────────


class _FakeInnerGemini:
    """GeminiClient 인스턴스 stub — generate() 호출 시 미리 정한 GeminiResult 반환.

    aclose() 호출 추적 → 헬퍼가 pool 정리하는지 회귀 가드.
    """

    def __init__(self, usage_per_call: int = 100):
        self._usage = usage_per_call
        self.calls: List[str] = []
        self.closed = False

    async def generate(self, prompt: str, *, temperature: float = 0.2) -> GeminiResult:
        self.calls.append(prompt)
        return GeminiResult(
            text="ok",
            model="fake",
            finish_reason="STOP",
            usage=TokenUsage(total_tokens=self._usage),
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def patch_gemini_and_repo(monkeypatch):
    """GeminiClient(model=...) / usage_repository.{get_usage,add_tokens} fake 교체.

    [등급별 모델 분기 변경 (2026-05)]
    tracked_pipeline_context 가 GeminiClient(model=...) kwarg 호출 + get_usage 로
    등급 조회. fake 가 둘 다 지원해야 함.

    반환:
        (inner_holder, add_calls, raise_on_add):
        - inner_holder[i] = (fake inner, model 이름)
        - add_calls = [(email, delta)]
        - raise_on_add 에 예외 push 하면 다음 add_tokens 호출 시 raise
    """
    inner_holder: List = []  # [(inner, model)]
    add_calls: List[Tuple[str, int]] = []
    raise_on_add: List[Exception] = []

    def fake_gemini_client(model: str = "fake-default", **kwargs):
        inner = _FakeInnerGemini()
        inner_holder.append((inner, model))
        return inner

    async def fake_get_usage(email: str):
        # 등급 조회 — 항상 free 로 응답 (테스트는 등급별 모델 분기 자체보단 wrap 동작 검증).
        from app.service.usage_repository import Usage
        return Usage(
            email=email, subscription_type="free",
            meeting_count=0, total_tokens=0, total_chars=0,
        )

    async def fake_add(email: str, delta: int, *, bucket: str = "main") -> Optional[int]:
        add_calls.append((email, delta))
        if raise_on_add:
            raise raise_on_add.pop(0)
        return 1000 + delta

    monkeypatch.setattr("app.api._quota_helpers.GeminiClient", fake_gemini_client)
    monkeypatch.setattr(
        "app.api._quota_helpers.usage_repository.add_tokens", fake_add
    )
    monkeypatch.setattr(
        "app.api._quota_helpers.usage_repository.get_usage", fake_get_usage
    )

    return inner_holder, add_calls, raise_on_add


# ─── 정상 케이스 ──────────────────────────────────────────


async def test_context_yields_tracked_gemini(patch_gemini_and_repo):
    """yield 받은 ctx.gemini 가 TrackedGemini 인스턴스."""
    async with tracked_pipeline_context(
        user_email="a@b.com", idempotency_key="t-1",
    ) as ctx:
        assert isinstance(ctx.gemini, TrackedGemini)
        assert ctx.idempotency_key == "t-1"


async def test_context_accumulates_tokens_and_persists(patch_gemini_and_repo):
    """LLM 호출 후 누적 → context 종료 시 add_tokens 호출."""
    inner_holder, add_calls, _ = patch_gemini_and_repo
    async with tracked_pipeline_context(
        user_email="a@b.com", idempotency_key="t-1",
    ) as ctx:
        await ctx.gemini.generate("prompt 1")
        await ctx.gemini.generate("prompt 2")
        # context 안에서는 아직 add_tokens 호출 전
        assert add_calls == []
    # context 종료 후 add_tokens 호출 — 100*2 = 200 토큰
    assert add_calls == [("a@b.com", 200)]
    # inner GeminiClient.aclose() 도 호출됨 (pool 정리)
    assert inner_holder[0][0].closed is True  # (inner, model) 튜플의 inner


async def test_context_skips_persist_when_no_email(patch_gemini_and_repo, caplog):
    """user_email=None 시 누적 자체는 동작 + add_tokens 호출 skip."""
    _, add_calls, _ = patch_gemini_and_repo
    async with tracked_pipeline_context(
        user_email=None, idempotency_key="t-1",
    ) as ctx:
        await ctx.gemini.generate("prompt")
    assert add_calls == []


async def test_context_skips_persist_when_no_llm_calls(patch_gemini_and_repo):
    """LLM 호출 0건 → 누적 0 → add_tokens 호출 안 함 (cypher 라운드트립 회피)."""
    _, add_calls, _ = patch_gemini_and_repo
    async with tracked_pipeline_context(
        user_email="a@b.com", idempotency_key="t-1",
    ) as _ctx:
        pass  # LLM 호출 안 함
    assert add_calls == []


# ─── 예외 처리 ─────────────────────────────────────────────


async def test_context_persists_even_on_exception(patch_gemini_and_repo):
    """context 안에서 예외 발생해도 finally 가 add_tokens 호출.

    이미 사용된 LLM 토큰은 비용 발생 → 적재 필수.
    """
    _, add_calls, _ = patch_gemini_and_repo
    with pytest.raises(ValueError, match="boom"):
        async with tracked_pipeline_context(
            user_email="a@b.com", idempotency_key="t-1",
        ) as ctx:
            await ctx.gemini.generate("prompt")
            raise ValueError("boom")
    # 100 토큰 사용됨 — 예외 raise 와 무관하게 적재
    assert add_calls == [("a@b.com", 100)]


async def test_context_swallows_add_tokens_exception(patch_gemini_and_repo, caplog):
    """add_tokens 실패해도 raise 안 함 — 라우트 응답 망치지 않음."""
    _, add_calls, raise_on_add = patch_gemini_and_repo
    raise_on_add.append(RuntimeError("Neo4j down"))
    # context 자체는 정상 종료
    async with tracked_pipeline_context(
        user_email="a@b.com", idempotency_key="t-1",
    ) as ctx:
        await ctx.gemini.generate("prompt")
    # add_tokens 는 한 번 호출됐고 (예외 발생)
    assert add_calls == [("a@b.com", 100)]


async def test_context_closes_inner_even_on_exception(patch_gemini_and_repo):
    """예외 발생해도 inner.aclose() 호출 — HTTP pool 누수 방지."""
    inner_holder, _, _ = patch_gemini_and_repo
    with pytest.raises(ValueError):
        async with tracked_pipeline_context(
            user_email="a@b.com", idempotency_key="t-1",
        ) as ctx:
            await ctx.gemini.generate("prompt")
            raise ValueError("boom")
    assert inner_holder[0][0].closed is True  # (inner, model) 튜플의 inner


# ─── overflow 모델 강제 (2026-06 인터뷰 우회 버그) ────────────


async def test_overflow_forces_lite_model_over_caller_model(monkeypatch):
    """overflow 결정이면 호출자가 비싼 모델을 명시해도 lite 로 강제 (인터뷰 우회 차단)."""
    from app.core import quota
    from app.service.usage_repository import Usage

    # Pro+ 메인 소진 + 일일캡 잔여 → overflow 결정.
    main_limit = quota.get_limit("pro_plus", "total_tokens")

    async def fake_get_usage(email: str):
        return Usage(
            email=email, subscription_type="pro_plus",
            meeting_count=0, total_tokens=main_limit + 1, total_chars=0,
            lite_daily_tokens=0,
        )
    monkeypatch.setattr("app.api._quota_helpers.usage_repository.get_usage", fake_get_usage)

    captured_models: list = []

    class _Inner:
        async def generate(self, prompt, *, temperature=0.2, response_schema=None,
                           model=None, **kw):
            captured_models.append(model)
            return GeminiResult(text="x", model=model or "d", finish_reason="STOP",
                                usage=TokenUsage(total_tokens=1))

        async def aclose(self):
            pass

    created_models: list = []

    def fake_client(model="d", **kw):
        created_models.append(model)
        return _Inner()
    monkeypatch.setattr("app.api._quota_helpers.GeminiClient", fake_client)

    async def fake_add(email, delta, *, bucket="main"):
        return 1
    monkeypatch.setattr("app.api._quota_helpers.usage_repository.add_tokens", fake_add)

    lite_model = quota.model_for_decision(
        quota.QuotaDecision(mode="overflow", subscription_type="pro_plus", bucket="lite")
    )
    async with tracked_pipeline_context(user_email="a@b.com", idempotency_key="t") as ctx:
        # 인터뷰처럼 비싼 flash 를 명시 강제 — overflow 라 무시되고 lite 로 가야 함.
        await ctx.gemini.generate("p", model="gemini-2.5-flash")
    # inner client 자체도 lite 모델로 생성됐고, generate 도 lite 강제.
    assert created_models == [lite_model]
    assert captured_models == [lite_model]


async def test_main_mode_does_not_force_model(patch_gemini_and_repo):
    """main(또는 free) 모드면 force_model 없음 — 파이프라인 모델 선택 존중."""
    # fixture 의 get_usage 는 free + total 0 → main 모드.
    async with tracked_pipeline_context(user_email="a@b.com", idempotency_key="t") as ctx:
        assert ctx.gemini._force_model is None
