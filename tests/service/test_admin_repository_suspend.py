"""admin_repository.suspend_user / unsuspend_user — atomic last-admin 보호 검증."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import pytest

from app.service import admin_repository

pytestmark = pytest.mark.asyncio


class _FakeRunCypher:
    def __init__(self, responses): self._r = list(responses); self.calls = []
    async def __call__(self, cypher, params=None, database=None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        return self._r.pop(0) if self._r else []


def _ok_user_row(email: str, **kw) -> Dict[str, Any]:
    base = {
        "id": "1", "email": email, "name": "X",
        "github_username": "", "subscription_type": "free",
        "is_admin": False, "has_password": True,
        "is_suspended": True,
        "suspended_at": "2026-05-18T10:00:00Z",
        "suspended_reason": "abuse",
        "suspended_by_email": "admin@x.com",
    }
    base.update(kw)
    return base


async def test_suspend_user_ok(monkeypatch):
    fake = _FakeRunCypher([
        [{"result": {"status": "ok", "user": _ok_user_row("u@x.com")}}],
    ])
    monkeypatch.setattr(
        "app.service.admin_repository.neo4j_client.run_cypher", fake
    )
    out = await admin_repository.suspend_user(
        target_email="u@x.com", reason="abuse", by_admin_email="admin@x.com"
    )
    assert out["status"] == "ok"
    assert out["user"].is_suspended is True
    assert out["user"].suspended_reason == "abuse"
    p = fake.calls[0]["params"]
    assert p["email"] == "u@x.com"
    assert p["reason"] == "abuse"
    assert p["by"] == "admin@x.com"


async def test_suspend_user_last_admin_blocked(monkeypatch):
    fake = _FakeRunCypher([
        [{"result": {"status": "last_admin", "message": "마지막 관리자입니다."}}],
    ])
    monkeypatch.setattr(
        "app.service.admin_repository.neo4j_client.run_cypher", fake
    )
    out = await admin_repository.suspend_user(
        target_email="onlyadmin@x.com", reason="", by_admin_email="other@x.com"
    )
    assert out["status"] == "last_admin"
    assert "마지막 관리자" in out["message"]


async def test_suspend_user_not_found(monkeypatch):
    fake = _FakeRunCypher([[]])
    monkeypatch.setattr(
        "app.service.admin_repository.neo4j_client.run_cypher", fake
    )
    out = await admin_repository.suspend_user(
        target_email="ghost@x.com", reason="", by_admin_email="admin@x.com"
    )
    assert out["status"] == "not_found"


async def test_unsuspend_user_ok(monkeypatch):
    fake = _FakeRunCypher([
        [{"result": {"status": "ok", "user": _ok_user_row(
            "u@x.com", is_suspended=False,
            unsuspended_at="2026-05-18T11:00:00Z",
        )}}],
    ])
    monkeypatch.setattr(
        "app.service.admin_repository.neo4j_client.run_cypher", fake
    )
    out = await admin_repository.unsuspend_user(target_email="u@x.com")
    assert out["status"] == "ok"
    assert out["user"].is_suspended is False


async def test_unsuspend_user_not_found(monkeypatch):
    fake = _FakeRunCypher([[]])
    monkeypatch.setattr(
        "app.service.admin_repository.neo4j_client.run_cypher", fake
    )
    out = await admin_repository.unsuspend_user(target_email="ghost@x.com")
    assert out["status"] == "not_found"
