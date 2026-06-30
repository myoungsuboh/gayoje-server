"""AdminUserRow 가 suspend 필드 + has_password 까지 노출하는지 검증."""
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


async def test_list_users_returns_suspend_and_has_password(monkeypatch):
    fake = _FakeRunCypher([
        [{"user": {
            "id": "1", "email": "a@x.com", "name": "A",
            "github_username": "", "subscription_type": "free",
            "subscription_updated_at": None,
            "is_admin": False,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "is_suspended": True,
            "suspended_at": "2026-05-18T10:00:00Z",
            "suspended_reason": "abuse",
            "suspended_by_email": "admin@x.com",
            "unsuspended_at": None,
            "has_password": True,
        }}],
        [{"total": 1}],
    ])
    monkeypatch.setattr(
        "app.service.admin_repository.neo4j_client.run_cypher", fake
    )
    out = await admin_repository.list_users()
    u = out["users"][0]
    assert u.is_suspended is True
    assert u.suspended_reason == "abuse"
    assert u.suspended_by_email == "admin@x.com"
    assert u.has_password is True


async def test_list_users_defaults_when_missing(monkeypatch):
    fake = _FakeRunCypher([
        [{"user": {
            "id": "1", "email": "a@x.com", "name": "A",
            "github_username": "", "subscription_type": "free",
            "is_admin": False, "has_password": False,
        }}],
        [{"total": 1}],
    ])
    monkeypatch.setattr(
        "app.service.admin_repository.neo4j_client.run_cypher", fake
    )
    out = await admin_repository.list_users()
    u = out["users"][0]
    assert u.is_suspended is False
    assert u.suspended_at is None
    assert u.has_password is False
