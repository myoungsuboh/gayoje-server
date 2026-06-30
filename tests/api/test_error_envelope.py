"""BE-E01-T05 회귀 — 표준 에러 envelope + 예외계층 매핑 + traceId 전파.

NOTE: 이 파일은 `from __future__ import annotations` 를 쓰지 않는다 — 함수-로컬
Body 모델을 라우트가 body 파라미터로 인식하려면 타입 힌트가 문자열이 아니라 실제
클래스여야 하기 때문(FastAPI 게이트).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.common.errors import ConflictError, NotFoundError
from app.common.exception_handlers import install_exception_handlers
from app.core.request_context import RequestIdMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    class Body(BaseModel):
        name: str

    @app.get("/boom")
    async def boom():
        raise NotFoundError("가요제를 찾을 수 없습니다.", detail="id=42")

    @app.get("/conflict")
    async def conflict():
        raise ConflictError()

    @app.post("/echo")
    async def echo(b: Body):
        return {"ok": b.name}

    @app.get("/crash")
    async def crash():
        raise RuntimeError("boom internal secret")

    return app


client = TestClient(_make_app(), raise_server_exceptions=False)


def test_app_error_envelope_with_traceid():
    r = client.get("/boom", headers={"X-Request-ID": "trace-1"})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["message"] == "가요제를 찾을 수 없습니다."
    assert err["detail"] == "id=42"
    assert err["traceId"] == "trace-1"  # camelCase alias + traceId 전파


def test_default_message_from_code():
    r = client.get("/conflict")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_validation_error_422_field_level():
    r = client.post("/echo", json={})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert any("name" in f["field"] for f in err["fields"])


def test_unknown_route_404_envelope():
    r = client.get("/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_unhandled_500_envelope_no_internal_leak():
    r = client.get("/crash")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret" not in str(body)  # 내부 메시지 비노출
