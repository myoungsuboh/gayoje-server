"""
v2_routes.guard_wait_mode — `?wait=true` 운영 admin 가드 단위 테스트.

[정책]
- wait=False     : 항상 통과 (가드 무시)
- dev + 일반     : 통과 (디버깅 편의)
- dev + admin    : 통과
- prod + admin   : 통과
- prod + 일반    : 403
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.wait_guard import guard_wait_mode
from app.core.config import settings


@pytest.fixture
def make_user():
    def _make(*, is_admin: bool):
        return SimpleNamespace(
            email="x@example.com", name="X", id="1", is_admin=is_admin
        )
    return _make


def test_wait_false_always_passes(make_user, monkeypatch):
    """wait=False 면 어떤 환경에서도 가드 통과."""
    monkeypatch.setattr(settings, "ENV", "production")
    guard_wait_mode(False, make_user(is_admin=False))   # 예외 없음
    guard_wait_mode(False, make_user(is_admin=True))


def test_dev_non_admin_with_wait_passes(make_user, monkeypatch):
    """dev 환경에서는 일반 사용자도 wait=true 허용."""
    monkeypatch.setattr(settings, "ENV", "development")
    guard_wait_mode(True, make_user(is_admin=False))


def test_dev_admin_with_wait_passes(make_user, monkeypatch):
    monkeypatch.setattr(settings, "ENV", "development")
    guard_wait_mode(True, make_user(is_admin=True))


def test_production_admin_with_wait_passes(make_user, monkeypatch):
    """production + admin → 통과 (운영자 디버깅용)."""
    monkeypatch.setattr(settings, "ENV", "production")
    guard_wait_mode(True, make_user(is_admin=True))


def test_production_non_admin_with_wait_blocked(make_user, monkeypatch):
    """production + 일반 사용자 + wait=true → 403."""
    monkeypatch.setattr(settings, "ENV", "production")
    with pytest.raises(HTTPException) as exc:
        guard_wait_mode(True, make_user(is_admin=False))
    assert exc.value.status_code == 403
    assert "관리자" in exc.value.detail


def test_production_user_without_is_admin_attr_blocked(monkeypatch):
    """getattr fallback — is_admin 속성 자체가 없는 객체도 일반 사용자 취급."""
    monkeypatch.setattr(settings, "ENV", "production")
    minimal_user = SimpleNamespace(email="x@x", name="X", id="1")
    with pytest.raises(HTTPException) as exc:
        guard_wait_mode(True, minimal_user)
    assert exc.value.status_code == 403
