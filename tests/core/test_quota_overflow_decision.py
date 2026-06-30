"""
quota.resolve_quota_decision — 메인/오버플로우/차단 결정 로직 (2026-06).

[정책]
- main     : 메인 쿼터 잔여 → Flash/free 모델, main 버킷
- overflow : 메인 소진 + Lite 일일캡 잔여 → flash-lite, lite 버킷
- blocked  : Free 메인 소진(오버플로우 불가) / Lite 일일캡 소진 → 402
- Pro+/Max 는 일일캡이 커서 "무제한" 체감, Pro 는 소프트랜딩, Free 는 하드월.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from app.core import quota
from app.core.quota import (
    get_lite_daily_cap,
    model_for_decision,
    pool_for_decision,
    resolve_quota_decision,
)
from app.core.subscription import (
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    SUBSCRIPTION_PRO_MAX,
    SUBSCRIPTION_PRO_PLUS,
)

# asyncio_mode=auto (pytest.ini) — async 테스트 자동 감지. 동기 테스트엔 마크 불필요.


@dataclass
class _Usage:
    subscription_type: str
    total_tokens: int = 0
    lite_tokens: int = 0
    lite_daily_tokens: int = 0
    reset_at: Optional[str] = None
    lite_daily_reset_at: Optional[str] = None


def _patch_usage(monkeypatch, usage):
    async def _get(_email):
        return usage
    monkeypatch.setattr(quota_usage_repo(), "get_usage", _get)


def quota_usage_repo():
    # resolve_quota_decision 가 함수 안에서 import 하는 모듈 객체와 동일 참조.
    from app.service import usage_repository
    return usage_repository


# ─── 일일캡 상수 ────────────────────────────────────────────


def test_lite_daily_cap_values():
    assert get_lite_daily_cap(SUBSCRIPTION_FREE) == 0           # 하드월
    assert get_lite_daily_cap(SUBSCRIPTION_PRO) == 1_500_000    # 주간(롤링 7일) 소프트랜딩
    assert get_lite_daily_cap(SUBSCRIPTION_PRO_PLUS) == 3_000_000
    assert get_lite_daily_cap(SUBSCRIPTION_PRO_MAX) == 5_000_000
    assert get_lite_daily_cap("unknown") == 0


def test_lite_daily_cap_override(monkeypatch):
    """admin override(lite_daily_cap)가 상수보다 우선."""
    quota.apply_limits_override(SUBSCRIPTION_PRO, {"lite_daily_cap": 999})
    try:
        assert get_lite_daily_cap(SUBSCRIPTION_PRO) == 999
    finally:
        quota.clear_limits_override()


# ─── 결정 분기 ──────────────────────────────────────────────


async def test_main_when_quota_remaining(monkeypatch):
    _patch_usage(monkeypatch, _Usage(SUBSCRIPTION_PRO, total_tokens=1_000_000))
    d = await resolve_quota_decision("u")
    assert d.mode == "main" and d.bucket == "main" and d.allowed


async def test_free_main_exhausted_blocks(monkeypatch):
    _patch_usage(monkeypatch, _Usage(SUBSCRIPTION_FREE, total_tokens=1_000_000))
    d = await resolve_quota_decision("u")
    assert d.mode == "blocked" and d.blocked_reason == "free_main"
    assert d.blocked_limit == 1_000_000 and not d.allowed


async def test_pro_overflow_when_main_exhausted(monkeypatch):
    _patch_usage(monkeypatch, _Usage(SUBSCRIPTION_PRO, total_tokens=2_000_000, lite_daily_tokens=0))
    d = await resolve_quota_decision("u")
    assert d.mode == "overflow" and d.bucket == "lite" and d.allowed
    assert d.warning is None  # 0% — 넛지 없음


async def test_pro_daily_cap_exhausted_blocks_with_nudge(monkeypatch):
    _patch_usage(monkeypatch, _Usage(SUBSCRIPTION_PRO, total_tokens=2_500_000, lite_daily_tokens=1_500_000))
    d = await resolve_quota_decision("u")
    assert d.mode == "blocked" and d.blocked_reason == "lite_daily_cap"
    assert d.blocked_limit == 1_500_000
    assert d.warning and "엔터프라이즈" in d.warning


async def test_overflow_nudge_at_70_percent(monkeypatch):
    # Pro+ 주간캡 3M 의 70% = 2.1M. 그 이상(아래 2.5M)이면 넛지.
    _patch_usage(monkeypatch, _Usage(SUBSCRIPTION_PRO_PLUS, total_tokens=4_000_000, lite_daily_tokens=2_500_000))
    d = await resolve_quota_decision("u")
    assert d.mode == "overflow"
    assert d.warning and "엔터프라이즈" in d.warning


async def test_pro_max_overflow(monkeypatch):
    _patch_usage(monkeypatch, _Usage(SUBSCRIPTION_PRO_MAX, total_tokens=8_000_000, lite_daily_tokens=0))
    d = await resolve_quota_decision("u")
    assert d.mode == "overflow" and d.subscription_type == SUBSCRIPTION_PRO_MAX


async def test_no_user_defaults_main_free(monkeypatch):
    _patch_usage(monkeypatch, None)
    d = await resolve_quota_decision("ghost")
    assert d.mode == "main" and d.subscription_type == SUBSCRIPTION_FREE


async def test_get_usage_error_falls_back_main_free(monkeypatch):
    async def _explode(_email):
        raise RuntimeError("neo4j down")
    monkeypatch.setattr(quota_usage_repo(), "get_usage", _explode)
    d = await resolve_quota_decision("u")
    assert d.mode == "main" and d.subscription_type == SUBSCRIPTION_FREE


# ─── 모델/풀 매핑 ───────────────────────────────────────────


def test_model_for_decision_overflow_uses_lite(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "GEMINI_MODEL_LITE", "gemini-2.5-flash-lite")
    d = quota.QuotaDecision(mode="overflow", subscription_type=SUBSCRIPTION_PRO, bucket="lite")
    assert model_for_decision(d) == "gemini-2.5-flash-lite"


def test_pool_for_decision_mapping():
    overflow = quota.QuotaDecision(mode="overflow", subscription_type=SUBSCRIPTION_PRO, bucket="lite")
    paid = quota.QuotaDecision(mode="main", subscription_type=SUBSCRIPTION_PRO_PLUS, bucket="main")
    free = quota.QuotaDecision(mode="main", subscription_type=SUBSCRIPTION_FREE, bucket="main")
    assert pool_for_decision(overflow) == quota.MODEL_POOL_LITE
    assert pool_for_decision(paid) == quota.MODEL_POOL_PRO
    assert pool_for_decision(free) == quota.MODEL_POOL_FREE
