"""
MCP 인증 미들웨어 + tool 가드 단위 테스트.

[검증 시나리오]
- Authorization 헤더 없음 → 401
- Bearer 가 아닌 토큰 → 401
- 만료된 토큰 → 401
- 잘못 서명된 토큰 → 401
- 블랙리스트 (jti) 등록된 토큰 → 401
- 탈퇴한 사용자의 토큰 → 401
- 정상 토큰 → ContextVar 에 user 가 들어가서 다음 layer 가 받음
- tool 가드 (`require_mcp_user_and_assert_owns`) — 본인 소유 OK / 타인 소유 PermissionError
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock

import jwt
import pytest

from app.core.config import settings
from app.core.security import create_access_token


# ─── Helpers ──────────────────────────────────────────────────


def _fake_user(email: str = "alice@example.com", is_admin: bool = False):
    """UserPublic-shape duck typing object — 미들웨어가 user_db 를 UserPublic.from_db 로 변환."""
    class _U:
        def __init__(self):
            self.id = "u1"
            self.email = email
            self.name = "Alice"
            self.is_admin = is_admin
    return _U()


@pytest.fixture
def patch_user_lookup(monkeypatch):
    """user_repository.get_user_by_email / UserPublic.from_db 를 fake 로 대체."""
    def _setup(user=None):
        async def _get(email):
            return user
        from app.service import user_repository as users
        monkeypatch.setattr(users, "get_user_by_email", _get)
        # UserPublic.from_db 는 그대로 사용 — 단 from_db 가 받는 객체가 model_dump
        # 가능해야 하므로, fake user 를 그대로 통과시키는 wrapper.
        monkeypatch.setattr(
            users.UserPublic, "from_db", staticmethod(lambda db: db)
        )
    return _setup


@pytest.fixture
def patch_blacklist(monkeypatch):
    """token_blacklist.is_revoked 의 응답을 제어."""
    def _setup(revoked: bool = False):
        async def _is_revoked(jti):
            return revoked
        from app.core import token_blacklist
        monkeypatch.setattr(token_blacklist, "is_revoked", _is_revoked)
    return _setup


# ─── 미들웨어 직접 호출 테스트 ────────────────────────────────


@pytest.mark.asyncio
async def test_middleware_rejects_missing_authorization(patch_user_lookup, patch_blacklist):
    """Authorization 헤더 없으면 401."""
    patch_user_lookup(_fake_user())
    patch_blacklist(False)

    from app.mcp.auth import MCPAuthMiddleware

    received_status = {}

    async def app(scope, receive, send):
        # 인증 통과했다면 여기 진입 — 통과 안 됐으면 호출 안 됨
        received_status["called"] = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent_messages = []
    async def send(msg):
        sent_messages.append(msg)

    mw = MCPAuthMiddleware(app)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/sse",
        "headers": [],
    }
    await mw(scope, receive, send)
    # app 은 절대 호출되지 않아야 함
    assert "called" not in received_status
    # 401 응답
    start = next(m for m in sent_messages if m["type"] == "http.response.start")
    assert start["status"] == 401


@pytest.mark.asyncio
async def test_middleware_rejects_non_bearer(patch_user_lookup, patch_blacklist):
    patch_user_lookup(_fake_user())
    patch_blacklist(False)

    from app.mcp.auth import MCPAuthMiddleware

    async def app(scope, receive, send):
        raise AssertionError("should not be called")

    sent = []
    async def send(m):
        sent.append(m)

    mw = MCPAuthMiddleware(app)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp/sse",
        "headers": [(b"authorization", b"Basic abc=")],
    }
    await mw(scope, lambda: None, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401


@pytest.mark.asyncio
async def test_middleware_rejects_expired_token(patch_user_lookup, patch_blacklist):
    """만료된 access 토큰 → 401."""
    patch_user_lookup(_fake_user())
    patch_blacklist(False)

    # 어제 만료된 토큰
    now = datetime.now(timezone.utc)
    expired = jwt.encode(
        {
            "sub": "alice@example.com",
            "type": "mcp",
            "jti": "x",
            "iat": now - timedelta(days=2),
            "exp": now - timedelta(days=1),
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )

    from app.mcp.auth import MCPAuthMiddleware

    async def app(scope, receive, send):
        raise AssertionError("should not be called")

    sent = []
    async def send(m):
        sent.append(m)

    mw = MCPAuthMiddleware(app)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp/sse",
        "headers": [(b"authorization", f"Bearer {expired}".encode())],
    }
    await mw(scope, lambda: None, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401


@pytest.mark.asyncio
async def test_middleware_rejects_wrong_token_type(patch_user_lookup, patch_blacklist):
    """refresh 토큰으로 MCP 접근 시 401."""
    patch_user_lookup(_fake_user())
    patch_blacklist(False)

    from app.core.security import create_refresh_token
    refresh = create_refresh_token("alice@example.com")

    from app.mcp.auth import MCPAuthMiddleware

    async def app(scope, receive, send):
        raise AssertionError("should not be called")

    sent = []
    async def send(m):
        sent.append(m)

    mw = MCPAuthMiddleware(app)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp/sse",
        "headers": [(b"authorization", f"Bearer {refresh}".encode())],
    }
    await mw(scope, lambda: None, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401


@pytest.mark.asyncio
async def test_middleware_rejects_revoked_token(patch_user_lookup, patch_blacklist):
    """jti 블랙리스트에 등록된 토큰 → 401."""
    patch_user_lookup(_fake_user())
    patch_blacklist(revoked=True)

    from app.core.security import create_mcp_token
    token, _ = create_mcp_token("alice@example.com", exp_days=90)

    from app.mcp.auth import MCPAuthMiddleware

    async def app(scope, receive, send):
        raise AssertionError("should not be called")

    sent = []
    async def send(m):
        sent.append(m)

    mw = MCPAuthMiddleware(app)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp/sse",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    await mw(scope, lambda: None, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401


@pytest.mark.asyncio
async def test_middleware_rejects_durably_revoked_token(
    patch_user_lookup, patch_blacklist, monkeypatch
):
    """Redis 블랙리스트는 통과(fail-open)해도, Neo4j McpToken.revoked=true 면 401.

    [의도] Redis 미가용/evict 시에도 명시적으로 회수된 90일 MCP 토큰을 차단하는
    durable backstop 검증. blacklist 는 False(회수 안 됨)로 두고, durable 검사만 True.
    """
    patch_user_lookup(_fake_user())
    patch_blacklist(False)  # Redis 블랙리스트는 '회수 안 됨' (혹은 미가용 fail-open)

    async def _durably_revoked(jti):
        return True
    from app.service import mcp_token_repository
    monkeypatch.setattr(
        mcp_token_repository, "is_durably_revoked", _durably_revoked
    )

    from app.core.security import create_mcp_token
    token, _ = create_mcp_token("alice@example.com", exp_days=90)

    from app.mcp.auth import MCPAuthMiddleware

    async def app(scope, receive, send):
        raise AssertionError("should not be called")

    sent = []
    async def send(m):
        sent.append(m)

    mw = MCPAuthMiddleware(app)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp/sse",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    await mw(scope, lambda: None, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401


@pytest.mark.asyncio
async def test_middleware_rejects_unknown_user(patch_user_lookup, patch_blacklist):
    """Neo4j 에서 사용자 못 찾으면 (탈퇴) 401."""
    patch_user_lookup(None)   # get_user_by_email → None
    patch_blacklist(False)

    from app.core.security import create_mcp_token
    token, _ = create_mcp_token("ghost@example.com", exp_days=90)

    from app.mcp.auth import MCPAuthMiddleware

    async def app(scope, receive, send):
        raise AssertionError("should not be called")

    sent = []
    async def send(m):
        sent.append(m)

    mw = MCPAuthMiddleware(app)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp/sse",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    await mw(scope, lambda: None, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401


@pytest.mark.asyncio
async def test_middleware_sets_contextvar_for_valid_token(
    patch_user_lookup, patch_blacklist
):
    """정상 토큰 → app() 진입 + ContextVar 에 user 설정됨."""
    user = _fake_user("alice@example.com")
    patch_user_lookup(user)
    patch_blacklist(False)

    from app.core.security import create_mcp_token
    token, _ = create_mcp_token("alice@example.com", exp_days=90)

    from app.mcp.auth import MCPAuthMiddleware, current_mcp_user

    captured_user = {}

    async def app(scope, receive, send):
        captured_user["u"] = current_mcp_user()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []
    async def send(m):
        sent.append(m)

    mw = MCPAuthMiddleware(app)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp/sse",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    await mw(scope, lambda: None, send)
    assert captured_user["u"] is user
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200


@pytest.mark.asyncio
async def test_middleware_resets_contextvar_after_request(
    patch_user_lookup, patch_blacklist
):
    """요청 처리 후 ContextVar 가 None 으로 복원되어야 함 (다른 요청 누수 방지)."""
    user = _fake_user()
    patch_user_lookup(user)
    patch_blacklist(False)

    from app.core.security import create_mcp_token
    token, _ = create_mcp_token("alice@example.com", exp_days=90)

    from app.mcp.auth import MCPAuthMiddleware, current_mcp_user

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []
    async def send(m):
        sent.append(m)

    mw = MCPAuthMiddleware(app)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp/sse",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    await mw(scope, lambda: None, send)
    # 요청 종료 후 ContextVar reset
    assert current_mcp_user() is None


@pytest.mark.asyncio
async def test_middleware_passes_through_non_http_scope():
    """websocket / lifespan scope 는 인증 무시하고 통과."""
    from app.mcp.auth import MCPAuthMiddleware

    called = {}
    async def app(scope, receive, send):
        called["scope"] = scope["type"]

    mw = MCPAuthMiddleware(app)
    await mw({"type": "lifespan"}, lambda: None, lambda m: None)
    assert called["scope"] == "lifespan"


# ─── tool 가드 (require_mcp_user_and_assert_owns) ─────────────


@pytest.mark.asyncio
async def test_require_mcp_user_raises_when_no_user(monkeypatch):
    """ContextVar 에 user 가 없으면 (미들웨어 통과 안 함) PermissionError."""
    from app.mcp.auth import require_mcp_user_and_assert_owns, _current_mcp_user

    # 기본 None 이지만 명시적 보장
    token_ctx = _current_mcp_user.set(None)
    try:
        with pytest.raises(PermissionError):
            await require_mcp_user_and_assert_owns("any-project")
    finally:
        _current_mcp_user.reset(token_ctx)


@pytest.mark.asyncio
async def test_require_mcp_user_calls_assert_owns(monkeypatch):
    """user 있고 assert_owns 통과 → 정상 리턴."""
    from app.mcp import auth as mcp_auth
    from app.service import ownership_repository

    user = _fake_user("alice@example.com")
    called = {}

    async def fake_assert_owns(email, project):
        called["args"] = (email, project)

    monkeypatch.setattr(ownership_repository, "assert_owns", fake_assert_owns)
    token_ctx = mcp_auth._current_mcp_user.set(user)
    try:
        await mcp_auth.require_mcp_user_and_assert_owns("proj-x")
    finally:
        mcp_auth._current_mcp_user.reset(token_ctx)

    assert called["args"] == ("alice@example.com", "proj-x")


@pytest.mark.asyncio
async def test_require_mcp_user_converts_403_to_permission_error(monkeypatch):
    """assert_owns 가 403 던지면 PermissionError 로 변환 (MCP 직렬화 호환)."""
    from fastapi import HTTPException

    from app.mcp import auth as mcp_auth
    from app.service import ownership_repository

    user = _fake_user("alice@example.com")

    async def fake_assert_owns(email, project):
        raise HTTPException(status_code=403, detail="not your project")

    monkeypatch.setattr(ownership_repository, "assert_owns", fake_assert_owns)
    token_ctx = mcp_auth._current_mcp_user.set(user)
    try:
        with pytest.raises(PermissionError, match="not your project"):
            await mcp_auth.require_mcp_user_and_assert_owns("foreign-project")
    finally:
        mcp_auth._current_mcp_user.reset(token_ctx)


# ─── 신규: mcp 타입 토큰 정책 검증 ────────────────────────────


@pytest.mark.asyncio
async def test_middleware_accepts_mcp_type_token(patch_user_lookup, patch_blacklist):
    """type=mcp 토큰은 통과해야 한다."""
    user = _fake_user("alice@example.com")
    patch_user_lookup(user)
    patch_blacklist(False)

    from app.core.security import create_mcp_token
    token, _jti = create_mcp_token("alice@example.com", exp_days=90)

    from app.mcp.auth import MCPAuthMiddleware

    called = {}
    async def app(scope, receive, send):
        called["ok"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []
    async def send(m):
        sent.append(m)

    mw = MCPAuthMiddleware(app)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp/sse",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    await mw(scope, lambda: None, send)
    assert called.get("ok") is True
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200


@pytest.mark.asyncio
async def test_middleware_rejects_access_type_token(patch_user_lookup, patch_blacklist):
    """기존 access 토큰은 MCP 에서 더 이상 통하지 않아야 한다 (보안 강화)."""
    patch_user_lookup(_fake_user())
    patch_blacklist(False)

    token = create_access_token("alice@example.com")

    from app.mcp.auth import MCPAuthMiddleware

    async def app(scope, receive, send):
        raise AssertionError("should not be called")

    sent = []
    async def send(m):
        sent.append(m)

    mw = MCPAuthMiddleware(app)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp/sse",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    await mw(scope, lambda: None, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401
