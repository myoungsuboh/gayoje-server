"""
OAuth callback redirect — 토큰을 fragment 로 옮긴 회귀 가드.

[배경]
2026-05 보안 감사 H3 — `_redirect_to_frontend` 가 access_token/refresh_token 을
URL query 로 전달 → Referer / access log / browser history 누설 위험.

[픽스]
토큰만 fragment (#) 로, 메타 (mode/error/new) 는 query 유지.
FE 가 location.hash 파싱 후 history.replaceState 로 즉시 정리.
"""
from __future__ import annotations

from urllib.parse import urlparse, parse_qs

import pytest

from app.api.auth_routes import _redirect_to_frontend


def _parse(resp_url: str):
    parsed = urlparse(resp_url)
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment) if parsed.fragment else {}
    return parsed, query, fragment


def test_tokens_are_in_fragment_not_query(monkeypatch):
    """access_token / refresh_token 은 query 에 없고 fragment 에만 존재."""
    monkeypatch.setattr(
        "app.api.auth_routes.settings.FRONTEND_OAUTH_CALLBACK_URL",
        "https://app.example.com/auth/callback",
    )
    resp = _redirect_to_frontend(
        mode="login",
        access_token="ACCESS_TOKEN_VALUE",
        refresh_token="REFRESH_TOKEN_VALUE",
    )
    _, query, fragment = _parse(resp.headers["location"])
    # 토큰은 fragment 에만
    assert "access_token" not in query, "access_token leak in query (Referer 노출 위험)"
    assert "refresh_token" not in query, "refresh_token leak in query"
    assert fragment.get("access_token") == ["ACCESS_TOKEN_VALUE"]
    assert fragment.get("refresh_token") == ["REFRESH_TOKEN_VALUE"]
    # 메타데이터는 query
    assert query.get("mode") == ["login"]


def test_error_path_no_fragment(monkeypatch):
    """error 모드는 토큰 없음 → fragment 도 비어있음."""
    monkeypatch.setattr(
        "app.api.auth_routes.settings.FRONTEND_OAUTH_CALLBACK_URL",
        "https://app.example.com/auth/callback",
    )
    resp = _redirect_to_frontend(mode="error", error="oauth_disabled")
    parsed, query, fragment = _parse(resp.headers["location"])
    assert query.get("mode") == ["error"]
    assert query.get("error") == ["oauth_disabled"]
    assert not parsed.fragment, "에러 응답에 불필요한 fragment 가 있음"


def test_new_user_flag_in_query(monkeypatch):
    """new=1 는 query (FE 가 회원가입 환영 UI 분기에 사용 — 민감 X)."""
    monkeypatch.setattr(
        "app.api.auth_routes.settings.FRONTEND_OAUTH_CALLBACK_URL",
        "https://app.example.com/auth/callback",
    )
    resp = _redirect_to_frontend(
        mode="login",
        access_token="A", refresh_token="R",
        new_user=True,
    )
    _, query, fragment = _parse(resp.headers["location"])
    assert query.get("new") == ["1"]
    # 토큰은 여전히 fragment
    assert "access_token" in fragment
