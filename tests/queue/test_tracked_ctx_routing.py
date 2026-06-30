"""
_tracked_ctx 등급별 Gemini 라우팅 테스트.

[2026-05 fix 회귀 가드]
이전: `subscription == SUBSCRIPTION_PRO` 단일 비교 → pro_plus / pro_max
사용자가 free 분기로 떨어져 gemini_free 호출 → LiteLLM 의 flash-lite
미등록 매핑으로 400 에러.
이후: PAID_SUBSCRIPTIONS 멤버십 체크 → 모든 paid 등급 이 gemini_pro 로 정상 라우팅.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.core.subscription import (
    PAID_SUBSCRIPTIONS,
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
)
from app.queue import jobs as jobs_module
from app.queue.jobs import _tracked_ctx
from tests.conftest import FakeGemini, FakeNeo4j, make_arq_ctx

pytestmark = pytest.mark.asyncio


@dataclass
class _StubUsage:
    subscription_type: str
    # [2026-06] resolve_quota_decision 가 읽는 필드 — main 모드(메인 잔여)로 두려고
    # total_tokens=0. 라우팅(메인) 검증이 목적이라 오버플로우는 별도 테스트에서 커버.
    total_tokens: int = 0
    lite_tokens: int = 0
    lite_daily_tokens: int = 0
    reset_at: str | None = None
    lite_daily_reset_at: str | None = None


def _stub_usage(monkeypatch, subscription_type):
    """usage_repository.get_usage 명시적 monkeypatch.
    subscription_type=None 이면 usage row 자체가 없는 상황 (신규 사용자).
    """
    async def _get(_email):
        if subscription_type is None:
            return None
        return _StubUsage(subscription_type=subscription_type)
    monkeypatch.setattr(jobs_module.usage_repository, "get_usage", _get)


async def test_free_user_routes_to_gemini_free(monkeypatch):
    _stub_usage(monkeypatch, SUBSCRIPTION_FREE)
    free = FakeGemini(responses=[])
    pro = FakeGemini(responses=[])
    arq_ctx = make_arq_ctx(job_id="j", gemini_free=free, gemini_pro=pro, neo4j=FakeNeo4j())

    ctx, _, _ = await _tracked_ctx(arq_ctx, "user@free.com")
    # TrackedGemini._inner 에 래핑된 원본
    assert ctx.gemini._inner is free


async def test_pro_user_routes_to_gemini_pro(monkeypatch):
    _stub_usage(monkeypatch, SUBSCRIPTION_PRO)
    free = FakeGemini(responses=[])
    pro = FakeGemini(responses=[])
    arq_ctx = make_arq_ctx(job_id="j", gemini_free=free, gemini_pro=pro, neo4j=FakeNeo4j())

    ctx, _, _ = await _tracked_ctx(arq_ctx, "user@pro.com")
    assert ctx.gemini._inner is pro


@pytest.mark.parametrize("paid", sorted(PAID_SUBSCRIPTIONS))
async def test_all_paid_subscriptions_route_to_pro(monkeypatch, paid):
    """pro / pro_plus / pro_max 모두 gemini_pro 로 정상 매핑.
    PAID_SUBSCRIPTIONS 에 새 등급 추가 시 자동으로 커버리지에 포함."""
    _stub_usage(monkeypatch, paid)
    free = FakeGemini(responses=[])
    pro = FakeGemini(responses=[])
    arq_ctx = make_arq_ctx(job_id="j", gemini_free=free, gemini_pro=pro, neo4j=FakeNeo4j())

    ctx, _, _ = await _tracked_ctx(arq_ctx, "user@paid.com")
    assert ctx.gemini._inner is pro


async def test_none_email_defaults_to_free(monkeypatch):
    """user_email=None → _resolve 가 즉시 free 반환 → free 인스턴스 선택."""
    _stub_usage(monkeypatch, None)  # usage row 없음
    free = FakeGemini(responses=[])
    pro = FakeGemini(responses=[])
    arq_ctx = make_arq_ctx(job_id="j", gemini_free=free, gemini_pro=pro, neo4j=FakeNeo4j())

    ctx, _, _ = await _tracked_ctx(arq_ctx, None)
    assert ctx.gemini._inner is free


async def test_usage_repo_error_falls_back_to_free(monkeypatch):
    """Neo4j 일시 장애 등으로 usage 조회 실패 → free fallback (회귀 방지)."""
    async def _explode(_email):
        raise RuntimeError("neo4j 끊김")
    monkeypatch.setattr(jobs_module.usage_repository, "get_usage", _explode)

    free = FakeGemini(responses=[])
    pro = FakeGemini(responses=[])
    arq_ctx = make_arq_ctx(job_id="j", gemini_free=free, gemini_pro=pro, neo4j=FakeNeo4j())

    ctx, _, _ = await _tracked_ctx(arq_ctx, "user@x.com")
    assert ctx.gemini._inner is free


async def test_legacy_ctx_with_only_gemini_key_fallbacks(monkeypatch):
    """gemini_free/_pro 없고 'gemini' 만 있는 ctx 도 동작 (single-model 운영 호환)."""
    _stub_usage(monkeypatch, SUBSCRIPTION_FREE)
    legacy = FakeGemini(responses=[])
    # 수동 구성 — make_arq_ctx 는 free/pro 자동 채움이라 테스트 의도와 다름
    arq_ctx = {"job_id": "j", "gemini": legacy, "neo4j": FakeNeo4j()}

    ctx, _, _ = await _tracked_ctx(arq_ctx, "user@x.com")
    assert ctx.gemini._inner is legacy


async def test_no_gemini_in_ctx_raises(monkeypatch):
    """아무 인스턴스도 없으면 즉시 실패 — on_startup 누락 조기 감지."""
    _stub_usage(monkeypatch, SUBSCRIPTION_FREE)
    arq_ctx = {"job_id": "j", "neo4j": FakeNeo4j()}
    with pytest.raises(RuntimeError, match="GeminiClient 가 없음"):
        await _tracked_ctx(arq_ctx, "user@x.com")


async def test_returned_token_accumulator_starts_at_zero(monkeypatch):
    _stub_usage(monkeypatch, SUBSCRIPTION_FREE)
    arq_ctx = make_arq_ctx(
        job_id="j",
        gemini_free=FakeGemini(responses=[]),
        gemini_pro=FakeGemini(responses=[]),
        neo4j=FakeNeo4j(),
    )
    _, acc, _ = await _tracked_ctx(arq_ctx, None)
    assert acc.total.total_tokens == 0
