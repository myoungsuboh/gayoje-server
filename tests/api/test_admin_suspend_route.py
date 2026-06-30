"""PATCH /api/admin/users/{email}/suspend — 정상/본인/last-admin/없음 케이스."""
from __future__ import annotations
from types import SimpleNamespace
from typing import Any, Dict, List
import pytest
from fastapi import HTTPException

from app.api import admin_routes
from app.service.admin_repository import AdminUserRow
from app.service.user_repository import UserPublic

pytestmark = pytest.mark.asyncio


def _admin(email: str = "admin@x.com") -> UserPublic:
    return UserPublic(id="a-1", email=email, name="Admin",
                      subscription_type="free", is_admin=True)


def _fake_request() -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"}, headers={}, state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/admin/users/x/suspend"),
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
def suspend_recorder(monkeypatch):
    calls: List[Dict[str, Any]] = []
    state = {"return": {"status": "ok"}}

    def _user(email="u@x.com"):
        return AdminUserRow(
            id="1", email=email, name="U", subscription_type="free",
            is_admin=False, is_suspended=True,
            suspended_at="2026-05-18T10:00:00Z",
            suspended_reason="abuse", suspended_by_email="admin@x.com",
            has_password=True,
        )

    async def fake_suspend(**kw):
        calls.append(kw)
        ret = state["return"]
        if ret.get("status") == "ok" and "user" not in ret:
            ret = {**ret, "user": _user(kw["target_email"])}
        return ret
    monkeypatch.setattr(
        "app.api.admin_routes.admin_repository.suspend_user", fake_suspend
    )
    return calls, state


async def test_suspend_success_calls_repo_and_audit(suspend_recorder, audit_recorder):
    calls, _ = suspend_recorder
    out = await admin_routes.suspend_user_route.__wrapped__(
        request=_fake_request(), email="u@x.com",
        payload=admin_routes.SuspendUserRequest(reason="abuse"),
        admin=_admin(),
    )
    assert out["user"]["email"] == "u@x.com"
    assert out["user"]["is_suspended"] is True
    assert calls[0] == {"target_email": "u@x.com", "reason": "abuse",
                        "by_admin_email": "admin@x.com"}
    assert audit_recorder[0]["action"] == "user_suspend"
    assert audit_recorder[0]["target_email"] == "u@x.com"
    assert audit_recorder[0]["payload"] == {"reason": "abuse"}


async def test_suspend_self_blocked(suspend_recorder, audit_recorder):
    with pytest.raises(HTTPException) as exc:
        await admin_routes.suspend_user_route.__wrapped__(
            request=_fake_request(), email="admin@x.com",
            payload=admin_routes.SuspendUserRequest(reason="oops"),
            admin=_admin("admin@x.com"),
        )
    assert exc.value.status_code == 400
    assert "자기 자신" in exc.value.detail
    assert audit_recorder == []


async def test_suspend_last_admin_blocked(suspend_recorder, audit_recorder):
    _, state = suspend_recorder
    state["return"] = {"status": "last_admin", "message": "마지막 관리자입니다."}
    with pytest.raises(HTTPException) as exc:
        await admin_routes.suspend_user_route.__wrapped__(
            request=_fake_request(), email="last@x.com",
            payload=admin_routes.SuspendUserRequest(reason=""),
            admin=_admin(),
        )
    assert exc.value.status_code == 400
    assert "마지막 관리자" in exc.value.detail
    assert audit_recorder == []


async def test_suspend_not_found(suspend_recorder, audit_recorder):
    _, state = suspend_recorder
    state["return"] = {"status": "not_found"}
    with pytest.raises(HTTPException) as exc:
        await admin_routes.suspend_user_route.__wrapped__(
            request=_fake_request(), email="ghost@x.com",
            payload=admin_routes.SuspendUserRequest(reason=""),
            admin=_admin(),
        )
    assert exc.value.status_code == 404
    assert audit_recorder == []
