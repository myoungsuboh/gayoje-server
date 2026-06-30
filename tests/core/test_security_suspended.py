"""get_current_user — 정지된 사용자는 401."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
import jwt

from app.core import security
from app.core.config import settings
from app.service.user_repository import UserInDB

pytestmark = pytest.mark.asyncio


def _access(iat_offset_sec: int = -60) -> str:
    now = datetime.now(timezone.utc) + timedelta(seconds=iat_offset_sec)
    return jwt.encode(
        {"sub": "a@x.com", "type": "access", "jti": "j1",
         "iat": now, "exp": now + timedelta(minutes=15)},
        settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM,
    )


def _user(*, is_suspended=False, suspended_at=None) -> UserInDB:
    return UserInDB(
        id="1", email="a@x.com", name="A", hashed_password="h",
        subscription_type="free", is_admin=False,
        is_suspended=is_suspended, suspended_at=suspended_at,
    )


async def test_suspended_user_get_current_user_401(monkeypatch):
    async def fake_get(email):
        return _user(is_suspended=True)
    monkeypatch.setattr(
        "app.service.user_repository.get_user_by_email", fake_get
    )
    async def fake_blacklist(jti): return False
    monkeypatch.setattr(
        "app.core.security.token_blacklist.is_revoked", fake_blacklist
    )

    with pytest.raises(HTTPException) as exc:
        await security.get_current_user(token=_access())
    assert exc.value.status_code == 401
    assert "정지" in exc.value.detail


async def test_iat_before_suspended_at_rejected(monkeypatch):
    suspended_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    async def fake_get(email):
        return _user(is_suspended=False, suspended_at=suspended_at)
    monkeypatch.setattr(
        "app.service.user_repository.get_user_by_email", fake_get
    )
    async def fake_blacklist(jti): return False
    monkeypatch.setattr(
        "app.core.security.token_blacklist.is_revoked", fake_blacklist
    )
    with pytest.raises(HTTPException) as exc:
        await security.get_current_user(token=_access(iat_offset_sec=-60))
    assert exc.value.status_code == 401


async def test_normal_user_passes(monkeypatch):
    async def fake_get(email):
        return _user(is_suspended=False, suspended_at=None)
    monkeypatch.setattr(
        "app.service.user_repository.get_user_by_email", fake_get
    )
    async def fake_blacklist(jti): return False
    monkeypatch.setattr(
        "app.core.security.token_blacklist.is_revoked", fake_blacklist
    )
    user = await security.get_current_user(token=_access())
    assert user.email == "a@x.com"
