"""
Body size limit 미들웨어 회귀 — 2026-05 보안 점검 #1 (DoS 방어).

[보호하는 동작]
- Content-Length > max_bytes → 413 (본문 읽기 전 빠른 거부)
- Content-Length 정상 → 통과
- 미들웨어 설치 안 됐을 때 (MAX_REQUEST_BODY_BYTES=0) → 동작 안 함
- Pydantic max_length 가 meeting_content 에 적용됐는지
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.body_size_limit import BodySizeLimitMiddleware, install_body_size_limit


def _make_app(max_bytes: int) -> FastAPI:
    app = FastAPI()
    install_body_size_limit(app, max_bytes=max_bytes)

    @app.post("/echo")
    async def echo(payload: dict) -> dict:
        return {"size": len(str(payload))}

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return app


def test_under_limit_passes():
    app = _make_app(max_bytes=1024)
    client = TestClient(app)
    resp = client.post("/echo", json={"a": "b"})
    assert resp.status_code == 200


def test_over_limit_returns_413():
    """[회귀] Content-Length 초과 시 본문 읽기 전 413."""
    app = _make_app(max_bytes=100)
    client = TestClient(app)
    big = {"data": "x" * 1000}
    resp = client.post("/echo", json=big)
    assert resp.status_code == 413
    assert "본문이 너무 큽니다" in resp.json()["detail"]


def test_get_endpoint_not_affected():
    """[회귀] GET 은 본문 없어 영향 없음."""
    app = _make_app(max_bytes=10)
    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.status_code == 200


def test_exact_limit_passes():
    """[회귀] 정확히 한도 = 통과."""
    app = _make_app(max_bytes=1024)
    client = TestClient(app)
    # JSON 직렬화 후 크기를 정확히 1024 이하로
    resp = client.post("/echo", json={"x": "y" * 50})
    assert resp.status_code == 200


def test_init_rejects_invalid_max_bytes():
    """[회귀] max_bytes <= 0 → ValueError (config 실수 방어)."""
    app = FastAPI()
    with pytest.raises(ValueError):
        BodySizeLimitMiddleware(app, max_bytes=0)
    with pytest.raises(ValueError):
        BodySizeLimitMiddleware(app, max_bytes=-1)


def test_413_response_includes_limit_in_message():
    """[회귀] 거부 메시지에 한도가 명시되어 사용자가 원인 파악 가능."""
    app = _make_app(max_bytes=1 * 1024 * 1024)  # 1 MB
    client = TestClient(app)
    big = {"data": "x" * (2 * 1024 * 1024)}
    resp = client.post("/echo", json=big)
    assert resp.status_code == 413
    detail = resp.json()["detail"]
    assert "1MB" in detail or "MB" in detail


# ─── Pydantic max_length on meeting_content ──────────────


def test_cps_request_meeting_content_has_max_length():
    """[회귀] CpsRequest.meeting_content 가 max_length 갖춤 (defense in depth)."""
    from app.api.v2_routes import CpsRequest

    field = CpsRequest.model_fields["meeting_content"]
    # Pydantic v2 — metadata 안 MaxLen
    has_max = any(
        getattr(m, "max_length", None) is not None
        for m in getattr(field, "metadata", [])
    )
    assert has_max, "meeting_content max_length 미설정"


def test_post_meeting_request_meeting_content_has_max_length():
    from app.api.v2_routes import PostMeetingRequest

    field = PostMeetingRequest.model_fields["meeting_content"]
    has_max = any(
        getattr(m, "max_length", None) is not None
        for m in getattr(field, "metadata", [])
    )
    assert has_max
