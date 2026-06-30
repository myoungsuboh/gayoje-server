"""
GET /auth/me/usage 응답 회귀 가드.

[배경]
2026-05 lifetime → 월간 reset 전환 시 `MyUsageResponse.reset_at` 의 실제 전파가
누락된 회귀 발생 (cypher / dataclass / quota guard 까지 패치됐는데 라우트의
반환 statement 가 `reset_at=None` 으로 하드코딩 잔존). 이 테스트가 그 회귀를
잡는다.
"""
from __future__ import annotations

from typing import Optional

import pytest

from app.api.auth_routes import my_usage_route
from app.core import quota
from app.service.usage_repository import Usage
from app.service.user_repository import (
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    UserPublic,
)

pytestmark = pytest.mark.asyncio


# ─── Fake usage_repository.get_usage ────────────────────────────


def _patch_get_usage(monkeypatch, usage: Optional[Usage]) -> None:
    async def fake_get_usage(email: str):
        assert email == "u@example.com"
        return usage
    # auth_routes 모듈이 import 한 심볼을 패치 — 같은 모듈 내 호출이 가짜를 본다.
    monkeypatch.setattr(
        "app.api.auth_routes.usage_repository.get_usage", fake_get_usage
    )


def _user() -> UserPublic:
    return UserPublic(
        id="u-1",
        email="u@example.com",
        name="Tester",
        subscription_type=SUBSCRIPTION_FREE,
        is_admin=False,
    )


# ─── 회귀 가드 — reset_at 전파 ───────────────────────────────────


async def test_my_usage_returns_reset_at_from_repository(monkeypatch):
    """[BUG 1 회귀] 응답.reset_at == repository.usage.reset_at (하드 None 금지)."""
    reset_at = "2026-06-17T00:00:00.000000000Z"
    _patch_get_usage(
        monkeypatch,
        Usage(
            email="u@example.com",
            subscription_type=SUBSCRIPTION_PRO,
            meeting_count=42,
            total_tokens=12_345,
            total_chars=6_789,
            reset_at=reset_at,
        ),
    )
    resp = await my_usage_route(current_user=_user())
    assert resp.reset_at == reset_at, (
        "reset_at 이 None 으로 하드코딩됐다 — FE 대시보드가 'N일 후 reset' "
        "표시 불가. auth_routes.my_usage_route 의 return 문 확인."
    )
    # 사용량/등급도 정상 매핑되는지 곁다리로 가드
    assert resp.subscription_type == SUBSCRIPTION_PRO
    assert resp.usage.meeting_logs == 42
    assert resp.usage.total_tokens == 12_345


async def test_my_usage_reset_at_can_be_none_for_first_call(monkeypatch):
    """첫 호출 직전이라 repository 가 reset_at=None 으로 줄 때 그대로 전파."""
    _patch_get_usage(
        monkeypatch,
        Usage(
            email="u@example.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
            reset_at=None,
        ),
    )
    resp = await my_usage_route(current_user=_user())
    assert resp.reset_at is None


async def test_my_usage_missing_user_node_returns_none(monkeypatch):
    """User 노드가 없는 비정상 케이스 — free + 0 + reset_at=None 안전 응답."""
    _patch_get_usage(monkeypatch, None)
    resp = await my_usage_route(current_user=_user())
    assert resp.reset_at is None
    assert resp.subscription_type == SUBSCRIPTION_FREE
    assert resp.usage.meeting_logs == 0
    assert resp.usage.total_tokens == 0
    # [2026-06] lite 섹션도 안전 기본값
    assert resp.lite.daily_used == 0
    assert resp.lite.daily_cap == 0  # free → 오버플로우 없음
    assert resp.lite.overflow_active is False


async def test_my_usage_exposes_lite_overflow(monkeypatch):
    """[2026-06] 메인 소진 Pro 사용자 → lite 섹션이 일일캡/사용량 + overflow_active 노출."""
    _patch_get_usage(
        monkeypatch,
        Usage(
            email="u@example.com",
            subscription_type=SUBSCRIPTION_PRO,
            meeting_count=0,
            total_tokens=2_000_000,   # Pro 메인 한도(2M) 소진
            total_chars=0,
            lite_tokens=120_000,
            lite_daily_tokens=80_000,
            lite_daily_reset_at="2026-06-09T00:00:00.000000000Z",
        ),
    )
    resp = await my_usage_route(current_user=_user())
    assert resp.lite.daily_cap == 1_500_000        # Pro 주간캡 (2026-06-11 일→주 전환)
    assert resp.lite.daily_used == 80_000
    assert resp.lite.monthly_used == 120_000
    assert resp.lite.overflow_active is True        # 메인 소진 + cap>0
    assert resp.lite.daily_reset_at == "2026-06-09T00:00:00.000000000Z"


async def test_my_usage_refreshes_overrides(monkeypatch):
    """[2026-06-11] 표시 라우트도 admin 한도 변경을 재로드 — '일괄 반영 안 됨' 표시 갭 회귀 가드."""
    calls = []

    async def _spy(force=False):
        calls.append(1)

    monkeypatch.setattr(quota, "ensure_overrides_fresh", _spy)
    _patch_get_usage(monkeypatch, None)
    await my_usage_route(current_user=_user())
    assert calls, "my_usage_route 가 ensure_overrides_fresh 를 호출해야"
