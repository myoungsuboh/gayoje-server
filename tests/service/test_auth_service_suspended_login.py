"""auth_service.login — 정지된 사용자는 403 + account_suspended code."""
from __future__ import annotations
from typing import Optional
import pytest
from fastapi import HTTPException

from app.service import auth_service
from app.service.auth_service import LoginRequest
from app.service.user_repository import UserInDB

pytestmark = pytest.mark.asyncio


def _user(*, is_suspended=False, reason: Optional[str] = None) -> UserInDB:
    return UserInDB(
        id="1", email="a@x.com", name="A",
        hashed_password="$2b$12$validbcrypthashplaceholder0000000000000000000000000000",
        subscription_type="free", is_admin=False,
        is_suspended=is_suspended, suspended_reason=reason,
    )


async def test_suspended_user_login_returns_403_with_code(monkeypatch):
    async def fake_get(email):
        return _user(is_suspended=True, reason="abuse")
    monkeypatch.setattr(
        "app.service.auth_service.users.get_user_by_email", fake_get
    )
    monkeypatch.setattr(
        "app.service.auth_service.verify_password",
        lambda plain, hashed: True,
    )

    with pytest.raises(HTTPException) as exc:
        await auth_service.login(LoginRequest(email="a@x.com", password="x"))
    assert exc.value.status_code == 403
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "account_suspended"
    assert "abuse" in exc.value.detail["message"]


async def test_suspended_user_no_reason_returns_generic_message(monkeypatch):
    async def fake_get(email):
        return _user(is_suspended=True, reason=None)
    monkeypatch.setattr(
        "app.service.auth_service.users.get_user_by_email", fake_get
    )
    monkeypatch.setattr(
        "app.service.auth_service.verify_password",
        lambda plain, hashed: True,
    )

    with pytest.raises(HTTPException) as exc:
        await auth_service.login(LoginRequest(email="a@x.com", password="x"))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "account_suspended"
    assert "고객센터" in exc.value.detail["message"]
    assert "abuse" not in exc.value.detail["message"]


async def test_wrong_password_still_401_no_enumeration(monkeypatch):
    """정지 상태여도 비번 틀리면 401 (계정 존재 노출 방지)."""
    async def fake_get(email):
        return _user(is_suspended=True, reason="abuse")
    monkeypatch.setattr(
        "app.service.auth_service.users.get_user_by_email", fake_get
    )
    monkeypatch.setattr(
        "app.service.auth_service.verify_password",
        lambda plain, hashed: False,
    )

    with pytest.raises(HTTPException) as exc:
        await auth_service.login(LoginRequest(email="a@x.com", password="bad"))
    assert exc.value.status_code == 401
    # 403/account_suspended 가 아님
    detail = exc.value.detail
    if isinstance(detail, dict):
        assert detail.get("code") != "account_suspended"
