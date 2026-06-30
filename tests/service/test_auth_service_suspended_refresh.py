"""refresh_access_token — 정지 + iat<suspended_at 거부."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException

import jwt
from app.core.config import settings
from app.service import auth_service
from app.service.user_repository import UserInDB

pytestmark = pytest.mark.asyncio


def _make_refresh(*, iat_offset_sec: int = -60) -> str:
    now = datetime.now(timezone.utc) + timedelta(seconds=iat_offset_sec)
    payload = {
        "sub": "a@x.com", "type": "refresh", "jti": "j1",
        "iat": now, "exp": now + timedelta(days=7),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY,
                      algorithm=settings.JWT_ALGORITHM)


def _user(*, is_suspended=False, suspended_at=None) -> UserInDB:
    return UserInDB(
        id="1", email="a@x.com", name="A", hashed_password="h",
        subscription_type="free", is_admin=False,
        is_suspended=is_suspended, suspended_at=suspended_at,
    )


async def test_suspended_user_refresh_rejected(monkeypatch):
    async def fake_get(email):
        return _user(is_suspended=True,
                     suspended_at="2099-01-01T00:00:00Z")
    monkeypatch.setattr(
        "app.service.auth_service.users.get_user_by_email", fake_get
    )
    async def fake_revoked(jti): return False
    monkeypatch.setattr(
        "app.service.auth_service.token_blacklist.is_revoked", fake_revoked
    )
    async def fake_revoke(jti, exp): return None
    monkeypatch.setattr(
        "app.service.auth_service.token_blacklist.revoke_if_new", fake_revoke
    )

    token = _make_refresh()
    with pytest.raises(HTTPException) as exc:
        await auth_service.refresh_access_token(token)
    assert exc.value.status_code == 401


async def test_iat_before_suspended_at_rejected(monkeypatch):
    """해제→재정지 시나리오: 이전 정지 이전 발급된 refresh 도 거부."""
    suspended_at_iso = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()

    async def fake_get(email):
        return _user(is_suspended=False, suspended_at=suspended_at_iso)
    monkeypatch.setattr(
        "app.service.auth_service.users.get_user_by_email", fake_get
    )
    async def fake_revoked(jti): return False
    monkeypatch.setattr(
        "app.service.auth_service.token_blacklist.is_revoked", fake_revoked
    )
    async def fake_revoke(jti, exp): return None
    monkeypatch.setattr(
        "app.service.auth_service.token_blacklist.revoke_if_new", fake_revoke
    )

    token = _make_refresh(iat_offset_sec=-60)
    with pytest.raises(HTTPException) as exc:
        await auth_service.refresh_access_token(token)
    assert exc.value.status_code == 401


async def test_iat_after_suspended_at_allowed(monkeypatch):
    """정지 해제 후 새로 발급된 토큰은 정상."""
    suspended_at_iso = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()

    async def fake_get(email):
        return _user(is_suspended=False, suspended_at=suspended_at_iso)
    monkeypatch.setattr(
        "app.service.auth_service.users.get_user_by_email", fake_get
    )
    async def fake_revoked(jti): return False
    monkeypatch.setattr(
        "app.service.auth_service.token_blacklist.is_revoked", fake_revoked
    )
    async def fake_revoke(jti, exp): return None
    monkeypatch.setattr(
        "app.service.auth_service.token_blacklist.revoke_if_new", fake_revoke
    )

    token = _make_refresh(iat_offset_sec=-60)
    new_access, new_refresh = await auth_service.refresh_access_token(token)
    assert isinstance(new_access, str) and isinstance(new_refresh, str)
