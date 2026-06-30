"""
/api/v2/trace 라우트 HTTP 레벨 테스트.

검증:
- happy path: 200 + UpstreamTrace shape
- invalid kind: 422
- empty id: 422 (FastAPI Query min_length=1)
- 시작 노드 없음: 404
- ownership 실패: 403 (assert_owns 가 raise)
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException, status
from httpx import ASGITransport, AsyncClient

from app.api.main import app
from app.core.security import get_current_user
from app.service.graph_repository import ArtifactRef, UpstreamTrace
from app.service.user_repository import UserPublic


_FAKE_USER = UserPublic(
    id="u-1",
    email="alice@example.com",
    name="Alice",
    created_at="2025-01-01T00:00:00Z",
)


@pytest.fixture(autouse=True)
def _bypass_auth():
    """모든 보호 라우트의 인증을 통과시킴."""
    app.dependency_overrides[get_current_user] = lambda: _FAKE_USER
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def _ownership_ok(monkeypatch):
    """assert_owns 통과 (true owner)."""

    async def _ok(email: str, project: str) -> None:
        return None

    monkeypatch.setattr(
        "app.api.trace_routes.ownership_repository.assert_owns", _ok
    )


@pytest.fixture
def _ownership_forbidden(monkeypatch):
    """assert_owns 가 403 raise — 다른 사용자 프로젝트 접근."""

    async def _boom(email: str, project: str) -> None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="not owner"
        )

    monkeypatch.setattr(
        "app.api.trace_routes.ownership_repository.assert_owns", _boom
    )


@pytest.mark.asyncio
async def test_trace_happy_path(monkeypatch, _ownership_ok):
    """정상: 200 + UpstreamTrace shape."""

    async def _fake_trace(*, kind, start_id, project, team_id=""):
        return UpstreamTrace(
            target=ArtifactRef(
                kind="api", id="API-03", label="POST /tickets/{id}/refund",
                project="p1",
            ),
            stories=[
                ArtifactRef(kind="story", id="story_03_2", label="환불 신청", project="p1")
            ],
            meetings=[
                ArtifactRef(kind="meeting", id="log_p1_v1_3", label="v1.3 (2026-04-15)", project="p1")
            ],
        )

    monkeypatch.setattr(
        "app.api.trace_routes.graph_repository.trace_upstream", _fake_trace
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            "/api/v2/trace",
            params={"project_name": "p1", "kind": "api", "id": "API-03"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"]["id"] == "API-03"
    assert body["target"]["label"] == "POST /tickets/{id}/refund"
    assert body["not_found"] is False
    assert len(body["stories"]) == 1
    assert body["stories"][0]["label"] == "환불 신청"
    assert len(body["meetings"]) == 1


@pytest.mark.asyncio
async def test_trace_invalid_kind_returns_422(_ownership_ok):
    """미지원 kind 면 422 — assert_owns 호출 전에 검증."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            "/api/v2/trace",
            params={"project_name": "p1", "kind": "aggregate", "id": "AGG-01"},
        )
    assert r.status_code == 422
    detail = r.json().get("detail", "")
    assert "aggregate" in detail or "지원하지 않는" in detail


@pytest.mark.asyncio
async def test_trace_empty_id_returns_422():
    """id 빈 문자열 — FastAPI Query min_length=1 이 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            "/api/v2/trace",
            params={"project_name": "p1", "kind": "api", "id": ""},
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_trace_missing_params_returns_422():
    """project_name 누락 — FastAPI 가 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/v2/trace", params={"kind": "api", "id": "API-01"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_trace_not_found_returns_404(monkeypatch, _ownership_ok):
    """그래프에 시작 노드 없으면 404."""

    async def _fake_not_found(*, kind, start_id, project, team_id=""):
        return UpstreamTrace(target=None, not_found=True)

    monkeypatch.setattr(
        "app.api.trace_routes.graph_repository.trace_upstream", _fake_not_found
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            "/api/v2/trace",
            params={"project_name": "p1", "kind": "api", "id": "API-99"},
        )
    assert r.status_code == 404
    assert "API-99" in r.json().get("detail", "")


@pytest.mark.asyncio
async def test_trace_other_owner_returns_403(_ownership_forbidden):
    """다른 사용자 프로젝트 접근 — assert_owns 가 403."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            "/api/v2/trace",
            params={"project_name": "someone_else", "kind": "api", "id": "API-01"},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_trace_case_insensitive_kind(monkeypatch, _ownership_ok):
    """kind=API (대문자) 도 정상 동작 — 내부에서 lowercase 처리."""

    async def _fake(*, kind, start_id, project, team_id=""):
        # 라우트가 lowercase 로 변환한 뒤 호출
        assert kind == "api"
        return UpstreamTrace(
            target=ArtifactRef(kind="api", id=start_id, label="X", project=project),
        )

    monkeypatch.setattr(
        "app.api.trace_routes.graph_repository.trace_upstream", _fake
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get(
            "/api/v2/trace",
            params={"project_name": "p1", "kind": "API", "id": "API-01"},
        )
    assert r.status_code == 200
