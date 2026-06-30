"""
slowapi rate limiter key 회귀 — 2026-05 보안 점검 #1.

[보호하는 동작]
- 인증된 요청 → "user:<email>" 키 (proxy IP spoof 무관)
- 익명 요청 → "ip:<x-forwarded-for-first>" 또는 fallback
- 잘못된/만료 토큰 → IP fallback
- X-Forwarded-For 첫 IP 우선 (proxy 뒤에서도 사용자 IP 식별)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

import jwt
import pytest

from app.core.config import settings
from app.core.limiter import (
    _extract_client_ip,
    _extract_email_from_jwt,
    rate_limit_key,
)


def _request(headers: Optional[dict] = None, client_host: str = "10.0.0.1"):
    """slowapi 가 호출하는 starlette.Request 의 최소 stub."""
    return SimpleNamespace(
        headers=headers or {},
        client=SimpleNamespace(host=client_host),
        scope={"type": "http", "client": (client_host, 0), "headers": []},
    )


def _make_jwt(email: str, exp_minutes: int = 60) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": email, "iat": now, "exp": now + timedelta(minutes=exp_minutes)},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


# ─── email 추출 ──────────────────────────────────────


def test_extract_email_returns_sub_from_valid_token():
    token = _make_jwt("alice@x.com")
    req = _request({"authorization": f"Bearer {token}"})
    assert _extract_email_from_jwt(req) == "alice@x.com"


def test_extract_email_case_insensitive_header():
    token = _make_jwt("bob@x.com")
    req = _request({"Authorization": f"Bearer {token}"})
    assert _extract_email_from_jwt(req) == "bob@x.com"


def test_extract_email_returns_none_for_missing_header():
    assert _extract_email_from_jwt(_request({})) is None


def test_extract_email_returns_none_for_non_bearer():
    req = _request({"authorization": "Basic dXNlcjpwYXNz"})
    assert _extract_email_from_jwt(req) is None


def test_extract_email_returns_none_for_invalid_signature():
    bad = jwt.encode(
        {"sub": "evil@x.com", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        "wrong-secret",
        algorithm=settings.JWT_ALGORITHM,
    )
    req = _request({"authorization": f"Bearer {bad}"})
    assert _extract_email_from_jwt(req) is None


def test_extract_email_returns_none_for_expired_token():
    expired = _make_jwt("late@x.com", exp_minutes=-1)
    req = _request({"authorization": f"Bearer {expired}"})
    assert _extract_email_from_jwt(req) is None


def test_extract_email_returns_none_for_garbage_token():
    req = _request({"authorization": "Bearer not-a-jwt"})
    assert _extract_email_from_jwt(req) is None


# ─── IP 추출 ────────────────────────────────────────


def test_client_ip_prefers_x_forwarded_for_first():
    req = _request(
        {"x-forwarded-for": "203.0.113.5, 10.0.0.1"},
        client_host="10.0.0.1",
    )
    assert _extract_client_ip(req) == "203.0.113.5"


def test_client_ip_falls_back_to_x_real_ip():
    req = _request({"x-real-ip": "203.0.113.9"}, client_host="10.0.0.1")
    assert _extract_client_ip(req) == "203.0.113.9"


def test_client_ip_falls_back_to_remote_address():
    req = _request({}, client_host="198.51.100.7")
    assert _extract_client_ip(req) == "198.51.100.7"


def test_client_ip_x_forwarded_for_trims_whitespace():
    req = _request({"x-forwarded-for": "  203.0.113.5  ,10.0.0.1"})
    assert _extract_client_ip(req) == "203.0.113.5"


# ─── 통합 key 함수 ────────────────────────────────────


def test_rate_limit_key_authenticated_uses_email():
    token = _make_jwt("alice@x.com")
    req = _request({"authorization": f"Bearer {token}", "x-forwarded-for": "1.2.3.4"})
    assert rate_limit_key(req) == "user:alice@x.com"


def test_rate_limit_key_anonymous_uses_ip():
    req = _request({"x-forwarded-for": "203.0.113.5"})
    assert rate_limit_key(req) == "ip:203.0.113.5"


def test_rate_limit_key_invalid_token_falls_back_to_ip():
    """[회귀] 토큰 위조 시 IP 키로 fallback — 인증 우회 불가."""
    bad = jwt.encode(
        {"sub": "victim@x.com", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        "wrong-secret",
        algorithm=settings.JWT_ALGORITHM,
    )
    req = _request(
        {"authorization": f"Bearer {bad}", "x-forwarded-for": "203.0.113.5"}
    )
    assert rate_limit_key(req) == "ip:203.0.113.5"


def test_rate_limit_key_two_users_same_ip_get_different_buckets():
    """[회귀] proxy 뒤 다른 사용자 같은 IP 라도 다른 버킷."""
    t1 = _make_jwt("a@x.com")
    t2 = _make_jwt("b@x.com")
    req1 = _request({"authorization": f"Bearer {t1}"}, client_host="10.0.0.1")
    req2 = _request({"authorization": f"Bearer {t2}"}, client_host="10.0.0.1")
    assert rate_limit_key(req1) != rate_limit_key(req2)
