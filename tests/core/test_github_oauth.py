"""
github_oauth helper 단위 테스트.

- state 토큰 round-trip + 위변조 거부
- build_authorize_url 의 scope 포함, redirect_uri 일치
- fetch_github_user 의 email fallback (/user 에 없을 때 /user/emails 조회)
- assert_oauth_configured: 미설정 시 503

httpx.AsyncClient 는 monkeypatch 로 fake 응답 주입.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import pytest

from app.core import github_oauth
from app.core.config import settings

# 일부 테스트만 async — 모듈 단위 mark 대신 함수별 @pytest.mark.asyncio 사용.


# ─── State 토큰 ────────────────────────────────────────────────


def test_create_and_verify_state_token_roundtrip(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "test-secret")
    token = github_oauth.create_state_token(mode="login")
    payload = github_oauth.verify_state_token(token)
    assert payload["mode"] == "login"
    assert payload["type"] == github_oauth.STATE_TYPE


def test_state_token_link_mode_includes_email(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "test-secret")
    token = github_oauth.create_state_token(mode="link", email="x@y.com")
    payload = github_oauth.verify_state_token(token)
    assert payload["mode"] == "link"
    assert payload["email"] == "x@y.com"


def test_state_token_invalid_mode_rejected():
    with pytest.raises(ValueError):
        github_oauth.create_state_token(mode="bogus")


def test_state_token_tampered_rejected(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "test-secret")
    token = github_oauth.create_state_token(mode="login")
    tampered = token[:-2] + ("X" if token[-1] != "X" else "Y")
    with pytest.raises(github_oauth.GitHubOAuthError):
        github_oauth.verify_state_token(tampered)


# ─── [2026-05 H2] OAuth state 키 분리 ─────────────────────────


def test_state_uses_dedicated_key_when_set(monkeypatch):
    """OAUTH_STATE_SECRET_KEY 가 설정되면 그 키로 서명, JWT_SECRET 으로 verify 불가."""
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "jwt-secret")
    monkeypatch.setattr(settings, "OAUTH_STATE_SECRET_KEY", "state-secret-separate")

    token = github_oauth.create_state_token(mode="login")

    # 정상 검증 (같은 키)
    payload = github_oauth.verify_state_token(token)
    assert payload["mode"] == "login"

    # JWT_SECRET 으로 직접 decode 시도하면 실패 — 키 분리 확인
    import jwt as _jwt
    with pytest.raises(_jwt.InvalidTokenError):
        _jwt.decode(token, "jwt-secret", algorithms=[settings.JWT_ALGORITHM])


def test_state_falls_back_to_jwt_secret_when_dedicated_key_absent(monkeypatch):
    """OAUTH_STATE_SECRET_KEY 미설정 시 JWT_SECRET 로 fallback — 옛 환경 호환."""
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "jwt-only-secret")
    monkeypatch.setattr(settings, "OAUTH_STATE_SECRET_KEY", None)

    token = github_oauth.create_state_token(mode="login")
    # JWT_SECRET 으로 직접 decode 가능해야 함 (fallback 검증)
    import jwt as _jwt
    payload = _jwt.decode(
        token, "jwt-only-secret", algorithms=[settings.JWT_ALGORITHM]
    )
    assert payload["mode"] == "login"


def test_state_token_signed_with_jwt_secret_rejected_when_dedicated_key_set(monkeypatch):
    """
    별도 OAUTH_STATE_SECRET_KEY 가 설정된 상태에서 JWT_SECRET 으로 만든 토큰은 거부.
    공격 시나리오: JWT_SECRET 유출됐는데 OAuth state 까지 위조하려는 시도 차단.
    """
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "leaked-jwt-secret")
    monkeypatch.setattr(settings, "OAUTH_STATE_SECRET_KEY", "state-secret")

    # 공격자가 JWT_SECRET 으로 직접 state JWT 만듦
    import jwt as _jwt
    from datetime import datetime, timedelta, timezone
    forged = _jwt.encode(
        {
            "type": github_oauth.STATE_TYPE,
            "mode": "login",
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        "leaked-jwt-secret",
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(github_oauth.GitHubOAuthError):
        github_oauth.verify_state_token(forged)


def test_state_token_wrong_type_rejected(monkeypatch):
    """JWT 자체는 유효하지만 type 이 다르면 거부 — 다른 곳의 access token 재활용 방어."""
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "test-secret")
    import jwt as _jwt
    from datetime import datetime, timedelta, timezone

    bogus = _jwt.encode(
        {
            "type": "access",
            "mode": "login",
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(github_oauth.GitHubOAuthError):
        github_oauth.verify_state_token(bogus)


# ─── build_authorize_url ──────────────────────────────────────────


def test_build_authorize_url_includes_scope_and_state(monkeypatch):
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(
        settings, "GITHUB_OAUTH_REDIRECT_URI", "https://api.x/auth/github/callback"
    )
    monkeypatch.setattr(
        settings, "FRONTEND_OAUTH_CALLBACK_URL", "https://fe.x/auth/callback"
    )
    monkeypatch.setattr(settings, "GITHUB_OAUTH_SCOPES", "read:user user:email repo")

    url = github_oauth.build_authorize_url("STATE-XYZ")
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert "github.com" in parsed.netloc
    assert qs["client_id"] == ["cid"]
    assert qs["state"] == ["STATE-XYZ"]
    assert qs["redirect_uri"] == ["https://api.x/auth/github/callback"]
    # scope 는 공백 구분 — urlencode 가 "+" 로 인코드함
    assert "read:user" in qs["scope"][0]
    assert "user:email" in qs["scope"][0]
    assert "repo" in qs["scope"][0]


def test_build_authorize_url_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_ID", None)
    with pytest.raises(github_oauth.GitHubOAuthDisabled):
        github_oauth.build_authorize_url("S")


# ─── exchange_code_for_token ──────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, body: Dict[str, Any]):
        self.status_code = status_code
        self._body = body
        self.content = json.dumps(body).encode()

    def json(self) -> Dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                "boom",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )


class _FakeAsyncClient:
    """httpx.AsyncClient context manager 와 호환되는 fake."""

    def __init__(self, response_map: Dict[str, _FakeResponse]):
        self._map = response_map
        self.calls: List[Dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url: str, data=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "data": data, "headers": headers})
        return self._map.get(url) or _FakeResponse(404, {"error": "no-mock"})

    async def get(self, url: str, headers=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return self._map.get(url) or _FakeResponse(404, {"error": "no-mock"})


def _config_enabled(monkeypatch):
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(
        settings, "GITHUB_OAUTH_REDIRECT_URI", "https://api.x/auth/github/callback"
    )
    monkeypatch.setattr(
        settings, "FRONTEND_OAUTH_CALLBACK_URL", "https://fe.x/auth/callback"
    )


@pytest.mark.asyncio
async def test_exchange_code_returns_access_token(monkeypatch):
    _config_enabled(monkeypatch)
    fake = _FakeAsyncClient(
        {
            github_oauth.GITHUB_TOKEN_URL: _FakeResponse(
                200, {"access_token": "gho_abc", "scope": "repo", "token_type": "bearer"}
            )
        }
    )
    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", lambda **kw: fake)

    token = await github_oauth.exchange_code_for_token("the-code")
    assert token == "gho_abc"
    assert fake.calls[0]["data"]["code"] == "the-code"
    assert fake.calls[0]["data"]["client_secret"] == "csecret"


@pytest.mark.asyncio
async def test_exchange_code_raises_when_no_token(monkeypatch):
    _config_enabled(monkeypatch)
    fake = _FakeAsyncClient(
        {
            github_oauth.GITHUB_TOKEN_URL: _FakeResponse(
                200, {"error": "bad_verification_code", "error_description": "expired"}
            )
        }
    )
    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", lambda **kw: fake)

    with pytest.raises(github_oauth.GitHubOAuthError, match="expired"):
        await github_oauth.exchange_code_for_token("x")


# ─── fetch_github_user ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_github_user_uses_email_from_user_endpoint(monkeypatch):
    fake = _FakeAsyncClient(
        {
            github_oauth.GITHUB_USER_URL: _FakeResponse(
                200,
                {
                    "id": 12345,
                    "login": "octocat",
                    "name": "The Octocat",
                    "email": "octo@github.example",
                    "avatar_url": "https://avatars.example/octocat",
                },
            )
        }
    )
    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", lambda **kw: fake)

    info = await github_oauth.fetch_github_user("token-x")
    assert info["github_id"] == 12345
    assert info["login"] == "octocat"
    assert info["email"] == "octo@github.example"
    # /user/emails 는 호출 안 됨 (이미 email 받음)
    assert all(c["url"] != github_oauth.GITHUB_USER_EMAILS_URL for c in fake.calls)


@pytest.mark.asyncio
async def test_fetch_github_user_falls_back_to_emails_endpoint(monkeypatch):
    fake = _FakeAsyncClient(
        {
            github_oauth.GITHUB_USER_URL: _FakeResponse(
                200,
                {"id": 99, "login": "u", "name": None, "email": None, "avatar_url": ""},
            ),
            github_oauth.GITHUB_USER_EMAILS_URL: _FakeResponse(
                200,
                [
                    {"email": "alt@x.com", "primary": False, "verified": True},
                    {"email": "primary@x.com", "primary": True, "verified": True},
                ],
            ),
        }
    )
    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", lambda **kw: fake)

    info = await github_oauth.fetch_github_user("token-x")
    assert info["email"] == "primary@x.com"
    assert info["name"] == "u"  # name None → login 으로 fallback


@pytest.mark.asyncio
async def test_fetch_github_user_raises_when_no_verified_email(monkeypatch):
    fake = _FakeAsyncClient(
        {
            github_oauth.GITHUB_USER_URL: _FakeResponse(
                200, {"id": 1, "login": "u", "email": None}
            ),
            github_oauth.GITHUB_USER_EMAILS_URL: _FakeResponse(
                200,
                [
                    {"email": "x@y.com", "primary": True, "verified": False},  # unverified
                ],
            ),
        }
    )
    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", lambda **kw: fake)

    with pytest.raises(github_oauth.GitHubOAuthError, match="verified"):
        await github_oauth.fetch_github_user("token-x")


# ─── assert_oauth_configured ──────────────────────────────────────


def test_assert_oauth_configured_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "GITHUB_OAUTH_CLIENT_ID", None)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        github_oauth.assert_oauth_configured()
    assert exc_info.value.status_code == 503


def test_assert_oauth_configured_passes_when_enabled(monkeypatch):
    _config_enabled(monkeypatch)
    # 예외 안 던져야 함
    github_oauth.assert_oauth_configured()
