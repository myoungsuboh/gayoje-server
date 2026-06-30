"""
Phase F — 라우트가 team_id 를 ownership 레이어로 정확히 전달하는지 회귀 테스트.

검증:
- v2 CPS POST: payload.team_id → claim(team_id) 로 전달 (개인/팀 분기)
- query GET (?team_id=): assert_access(team_id) 로 전달
- delete_project DELETE (?team_id=): assert_access(team_id) 로 전달

[전략]
get_current_user 를 override 로 통과. ownership_repository.claim / assert_access
를 spy 로 교체해 전달된 team_id 인자만 검증 (다운스트림 파이프라인은 무시 —
claim/assert 단계에서 의도적으로 멈춤).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.main import app
from app.core.security import get_current_user
from app.service.user_repository import UserPublic


_FAKE_USER = UserPublic(
    id="u-1",
    email="alice@example.com",
    name="Alice",
    created_at="2025-01-01T00:00:00Z",
)


@pytest.fixture(autouse=True)
def _bypass_auth():
    app.dependency_overrides[get_current_user] = lambda: _FAKE_USER
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _Stop(HTTPException):
    """spy 가 의도적으로 흐름을 멈추기 위한 마커 (418)."""

    def __init__(self):
        super().__init__(status_code=418, detail="stop-after-ownership")


# ─── v2 CPS: payload.team_id → claim ──────────────────────────


@pytest.mark.asyncio
async def test_v2_cps_forwards_team_id_to_claim(monkeypatch):
    captured = {}

    async def spy_claim(email, project, team_id=None):
        captured["email"] = email
        captured["project"] = project
        captured["team_id"] = team_id
        raise _Stop()

    monkeypatch.setattr("app.service.ownership_repository.claim", spy_claim)

    async with await _client() as client:
        r = await client.post(
            "/api/v2/pipelines/cps",
            json={
                "project_name": "team-proj",
                "version": "v1",
                "meeting_content": "x" * 500,
                "team_id": "team-42",
            },
        )
    assert r.status_code == 418, r.text
    assert captured["team_id"] == "team-42"
    assert captured["project"] == "team-proj"


@pytest.mark.asyncio
async def test_v2_cps_without_team_id_passes_none(monkeypatch):
    captured = {}

    async def spy_claim(email, project, team_id=None):
        captured["team_id"] = team_id
        raise _Stop()

    monkeypatch.setattr("app.service.ownership_repository.claim", spy_claim)

    async with await _client() as client:
        r = await client.post(
            "/api/v2/pipelines/cps",
            json={
                "project_name": "personal-proj",
                "version": "v1",
                "meeting_content": "x" * 500,
            },
        )
    assert r.status_code == 418, r.text
    assert captured["team_id"] is None


# ─── query GET: ?team_id= → assert_access ─────────────────────


@pytest.mark.asyncio
async def test_query_get_cps_forwards_team_id_to_assert_access(monkeypatch):
    captured = {}

    async def spy_assert(email, project, team_id=None):
        captured["team_id"] = team_id
        captured["project"] = project
        raise _Stop()

    monkeypatch.setattr("app.service.ownership_repository.assert_access", spy_assert)

    async with await _client() as client:
        r = await client.get(
            "/api/v2/cps", params={"project_name": "team-proj", "team_id": "team-99"}
        )
    assert r.status_code == 418, r.text
    assert captured["team_id"] == "team-99"
    assert captured["project"] == "team-proj"


@pytest.mark.asyncio
async def test_query_get_cps_without_team_id_passes_none(monkeypatch):
    captured = {}

    async def spy_assert(email, project, team_id=None):
        captured["team_id"] = team_id
        raise _Stop()

    monkeypatch.setattr("app.service.ownership_repository.assert_access", spy_assert)

    async with await _client() as client:
        r = await client.get("/api/v2/cps", params={"project_name": "personal-proj"})
    assert r.status_code == 418, r.text
    assert captured["team_id"] is None


# ─── delete_project DELETE: ?team_id= → assert_access ─────────


@pytest.mark.asyncio
async def test_delete_project_forwards_team_id_to_assert_access(monkeypatch):
    captured = {}

    async def spy_assert(email, project, team_id=None):
        captured["team_id"] = team_id
        raise _Stop()

    monkeypatch.setattr("app.service.ownership_repository.assert_access", spy_assert)

    async with await _client() as client:
        r = await client.delete(
            "/api/v2/projects/team-proj", params={"team_id": "team-7"}
        )
    assert r.status_code == 418, r.text
    assert captured["team_id"] == "team-7"
