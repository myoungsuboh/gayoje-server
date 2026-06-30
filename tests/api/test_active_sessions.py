"""
[Phase 3 — 2026-05-18] 활성 세션 가시화 + 강제 로그아웃 라우트 테스트.

검증:
- GET /me/sessions — session_registry.list_sessions 위임 + current_jti 식별
- DELETE /me/sessions/{jti}:
    - 본인 세션 → revoke + unregister + 200
    - 타인 세션 → 404 (정보 누설 방지)
    - 이미 만료된 jti → 200 (멱등)
"""
from __future__ import annotations

import time
from typing import List

import pytest

from app.api import auth_routes
from app.core import security
from app.core.session_registry import SessionInfo
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


def _user(email: str = "alice@example.com") -> UserPublic:
    return UserPublic(
        id="u-1",
        email=email,
        name="Alice",
        github_username=None,
        is_admin=False,
        subscription_type="free",
        suspended=False,
        suspended_at=None,
        suspended_reason=None,
        suspended_by=None,
    )


def _token_for(email: str = "alice@example.com", jti: str = "jti-current") -> str:
    """jti 포함된 access token 발급 — decode_token_lenient 가 회수 가능."""
    import jwt
    from datetime import datetime, timedelta, timezone
    from app.core.config import settings
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": email,
            "jti": jti,
            "type": "access",
            "exp": int((now + timedelta(minutes=15)).timestamp()),
            "iat": int(now.timestamp()),
        },
        settings.JWT_SECRET_KEY,
        algorithm="HS256",
    )


# ─── GET /me/sessions ─────────────────────────────────────────


async def test_list_sessions_returns_with_current_flag(monkeypatch):
    """list_sessions → ActiveSessionsResponse + current_jti 매핑."""
    fake_sessions: List[SessionInfo] = [
        SessionInfo(
            jti="jti-current",
            email="alice@example.com",
            user_agent="Mozilla/5.0",
            ip="1.2.3.4",
            created_at=1700000000000,
            device_label="Chrome on macOS",
        ),
        SessionInfo(
            jti="jti-other",
            email="alice@example.com",
            user_agent="Mobile Safari",
            ip="5.6.7.8",
            created_at=1700000001000,
            device_label="Safari on iPhone",
        ),
    ]

    async def fake_list(email):
        return fake_sessions
    monkeypatch.setattr(
        "app.core.session_registry.list_sessions", fake_list
    )

    token = _token_for(jti="jti-current")
    resp = await auth_routes.list_sessions_route(
        token=token,
        current_user=_user(),
    )
    assert resp.current_jti == "jti-current"
    assert len(resp.sessions) == 2
    cur = next(s for s in resp.sessions if s.jti == "jti-current")
    other = next(s for s in resp.sessions if s.jti == "jti-other")
    assert cur.is_current is True
    assert other.is_current is False
    assert cur.device_label == "Chrome on macOS"


# ─── DELETE /me/sessions/{jti} — 강제 로그아웃 ────────────────


async def test_revoke_session_self_succeeds(monkeypatch):
    """본인 jti 강제 로그아웃 → token_blacklist.revoke + unregister + 200."""
    revoke_calls: List[tuple] = []
    unregister_calls: List[str] = []

    async def fake_get_email(jti):
        return "alice@example.com"
    async def fake_revoke(jti, exp):
        revoke_calls.append((jti, exp))
    async def fake_unregister(jti):
        unregister_calls.append(jti)

    monkeypatch.setattr(
        "app.core.session_registry.get_session_email", fake_get_email
    )
    monkeypatch.setattr("app.core.token_blacklist.revoke", fake_revoke)
    monkeypatch.setattr(
        "app.core.session_registry.unregister_session", fake_unregister
    )

    resp = await auth_routes.revoke_session_route(
        jti="jti-target", current_user=_user(),
    )
    assert "세션이 종료" in resp.message
    assert len(revoke_calls) == 1
    assert revoke_calls[0][0] == "jti-target"
    assert revoke_calls[0][1] > int(time.time())  # 미래 exp
    assert unregister_calls == ["jti-target"]


async def test_revoke_session_other_user_returns_404(monkeypatch):
    """다른 사용자의 jti → 404 (정보 누설 방지). revoke 호출 0."""
    revoke_calls = []
    async def fake_get_email(jti):
        return "bob@example.com"  # 다른 사용자
    async def fake_revoke(jti, exp):
        revoke_calls.append(jti)
    monkeypatch.setattr(
        "app.core.session_registry.get_session_email", fake_get_email
    )
    monkeypatch.setattr("app.core.token_blacklist.revoke", fake_revoke)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await auth_routes.revoke_session_route(
            jti="bob-jti", current_user=_user(),
        )
    assert exc.value.status_code == 404
    assert revoke_calls == []   # 다른 사용자 토큰 무효화 시도 안 함


async def test_revoke_session_missing_returns_idempotent_success(monkeypatch):
    """이미 만료된 jti (메타 없음) → 200 멱등 응답."""
    async def fake_get_email(jti):
        return None  # 세션 없음
    monkeypatch.setattr(
        "app.core.session_registry.get_session_email", fake_get_email
    )

    resp = await auth_routes.revoke_session_route(
        jti="expired-jti", current_user=_user(),
    )
    assert "이미 종료" in resp.message or "종료" in resp.message
