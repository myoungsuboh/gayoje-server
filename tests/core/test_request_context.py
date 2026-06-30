"""
요청 컨텍스트 (request_id + user_email) 회귀 — 2026-05 보안 점검 #2.

[보호하는 동작]
- 들어오는 X-Request-ID 헤더가 응답에 echo 됨
- 헤더 없으면 새 UUID 생성 + 응답에 노출
- 너무 긴 외부 입력은 64자로 truncate (로그 폭주 방지)
- 인증 JWT 가 있으면 contextvar 에 user_email 채워짐
- 잘못된/만료 JWT 는 user_email='-' (best-effort)
- ContextFilter 가 logger record 에 자동 첨부
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from io import StringIO

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.request_context import (
    RequestIdMiddleware,
    _ContextFilter,
    current_request_id,
    current_user_email,
    install_request_context_logging,
)


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo")
    async def echo(request: Request) -> dict:
        return {
            "rid": current_request_id(),
            "email": current_user_email(),
            "state_rid": request.state.request_id,
        }
    return app


def _jwt(email: str, exp_minutes: int = 60) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": email, "iat": now, "exp": now + timedelta(minutes=exp_minutes)},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


# ─── X-Request-ID 동작 ──────────────────────────────────


def test_incoming_request_id_is_echoed():
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/echo", headers={"X-Request-ID": "abc-123"})
    assert resp.status_code == 200
    assert resp.json()["rid"] == "abc-123"
    assert resp.headers["X-Request-ID"] == "abc-123"


def test_missing_request_id_generates_one():
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/echo")
    rid = resp.json()["rid"]
    assert rid != "-"
    assert len(rid) >= 16
    assert resp.headers["X-Request-ID"] == rid


def test_overly_long_incoming_id_is_truncated():
    """[회귀] 외부 입력 길이 제한 — 로그 line 폭주 방지."""
    app = _make_app()
    client = TestClient(app)
    long_id = "x" * 500
    resp = client.get("/echo", headers={"X-Request-ID": long_id})
    assert resp.status_code == 200
    rid = resp.json()["rid"]
    assert len(rid) <= 64


def test_request_state_is_set():
    """[회귀] request.state.request_id 도 채워짐 — route 내부에서 직접 접근."""
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/echo", headers={"X-Request-ID": "abc"})
    assert resp.json()["state_rid"] == "abc"


# ─── JWT → user_email ─────────────────────────────────


def test_jwt_extracts_email_into_context():
    app = _make_app()
    client = TestClient(app)
    token = _jwt("alice@x.com")
    resp = client.get("/echo", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["email"] == "alice@x.com"


def test_expired_jwt_falls_back_to_dash():
    app = _make_app()
    client = TestClient(app)
    expired = _jwt("late@x.com", exp_minutes=-1)
    resp = client.get("/echo", headers={"Authorization": f"Bearer {expired}"})
    assert resp.json()["email"] == "-"


def test_no_auth_header_means_dash_email():
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/echo")
    assert resp.json()["email"] == "-"


def test_invalid_signature_jwt_falls_back():
    app = _make_app()
    client = TestClient(app)
    bad = jwt.encode(
        {"sub": "evil@x.com", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        "wrong-secret",
        algorithm=settings.JWT_ALGORITHM,
    )
    resp = client.get("/echo", headers={"Authorization": f"Bearer {bad}"})
    assert resp.json()["email"] == "-"


# ─── Context propagation 후 context 정리 ───────────────


def test_context_resets_after_request():
    """[회귀] 요청 종료 후 contextvar 가 default 로 복귀 — 누수 방지."""
    app = _make_app()
    client = TestClient(app)
    client.get("/echo", headers={"X-Request-ID": "rrr"})
    # TestClient 외부 (메인 thread) 에서는 default.
    assert current_request_id() == "-"
    assert current_user_email() == "-"


# ─── 로깅 필터 ────────────────────────────────────────


def test_context_filter_adds_request_id_and_user_email_to_record():
    """[회귀] _ContextFilter 가 record 에 두 필드 첨부."""
    flt = _ContextFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="hi", args=(), exc_info=None,
    )
    flt.filter(record)
    assert hasattr(record, "request_id")
    assert hasattr(record, "user_email")
    assert record.request_id == "-"
    assert record.user_email == "-"


def test_install_request_context_logging_attaches_filter():
    """[회귀] install 가 root logger handlers 에 _ContextFilter 부착."""
    root = logging.getLogger()
    stream = StringIO()
    h = logging.StreamHandler(stream)
    h.setLevel(logging.DEBUG)
    h.setFormatter(logging.Formatter("%(message)s req=%(request_id)s"))
    root.addHandler(h)
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        install_request_context_logging(root)
        assert any(isinstance(f, _ContextFilter) for f in h.filters)
        # 실제 로그 record 발행 — format string 의 %(request_id)s 가 채워지는지.
        logging.getLogger("ctx.test").info("hello")
        out = stream.getvalue()
        assert "hello" in out
        assert "req=-" in out  # 컨텍스트 밖이라 default "-"
    finally:
        root.removeHandler(h)
        root.setLevel(prev_level)


def test_install_is_idempotent():
    """[회귀] 두 번 호출해도 필터 중복 부착 안 함."""
    root = logging.getLogger()
    h = logging.StreamHandler(StringIO())
    root.addHandler(h)
    try:
        install_request_context_logging(root)
        install_request_context_logging(root)
        filters = [f for f in h.filters if isinstance(f, _ContextFilter)]
        assert len(filters) == 1
    finally:
        root.removeHandler(h)
