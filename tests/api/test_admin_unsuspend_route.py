"""PATCH /api/admin/users/{email}/unsuspend — 정상/없음."""
from __future__ import annotations
from types import SimpleNamespace
from typing import Any, Dict, List
import pytest
from fastapi import HTTPException

from app.api import admin_routes
from app.service.admin_repository import AdminUserRow
from app.service.user_repository import UserPublic

pytestmark = pytest.mark.asyncio


def _admin() -> UserPublic:
    return UserPublic(id="a-1", email="admin@x.com", name="A",
                      subscription_type="free", is_admin=True)


def _req():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"), scope={"type": "http"},
        headers={}, state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/admin/users/x/unsuspend"),
        method="PATCH",
    )


@pytest.fixture
def audit_recorder(monkeypatch):
    calls: List[Dict[str, Any]] = []
    async def fake_write(**kw): calls.append(kw); return "id"
    monkeypatch.setattr(
        "app.api.admin_routes.audit_repository.write", fake_write
    )
    return calls


@pytest.fixture
def unsuspend_recorder(monkeypatch):
    state = {"return": {"status": "ok"}}

    def _user(email):
        return AdminUserRow(
            id="1", email=email, name="U", subscription_type="free",
            is_admin=False, is_suspended=False,
            suspended_at="2026-05-18T10:00:00Z",
            suspended_reason="abuse",
            suspended_by_email="admin@x.com",
            unsuspended_at="2026-05-18T11:00:00Z",
            has_password=True,
        )

    async def fake_unsuspend(**kw):
        ret = state["return"]
        if ret.get("status") == "ok" and "user" not in ret:
            ret = {**ret, "user": _user(kw["target_email"])}
        return ret
    monkeypatch.setattr(
        "app.api.admin_routes.admin_repository.unsuspend_user", fake_unsuspend
    )
    return state


async def test_unsuspend_success(unsuspend_recorder, audit_recorder):
    out = await admin_routes.unsuspend_user_route.__wrapped__(
        request=_req(), email="u@x.com", admin=_admin(),
    )
    assert out["user"]["email"] == "u@x.com"
    assert out["user"]["is_suspended"] is False
    assert out["user"]["unsuspended_at"] == "2026-05-18T11:00:00Z"
    assert audit_recorder[0]["action"] == "user_unsuspend"
    assert audit_recorder[0]["target_email"] == "u@x.com"
    assert audit_recorder[0]["payload"] == {}


async def test_unsuspend_not_found(unsuspend_recorder, audit_recorder):
    unsuspend_recorder["return"] = {"status": "not_found"}
    with pytest.raises(HTTPException) as exc:
        await admin_routes.unsuspend_user_route.__wrapped__(
            request=_req(), email="ghost@x.com", admin=_admin(),
        )
    assert exc.value.status_code == 404
    assert audit_recorder == []
