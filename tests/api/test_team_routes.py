"""
팀 라우트 HTTP 레벨 회귀 테스트 (Phase E).

검증:
- 인증 통과 후 라우트가 team_repository 보안 로직을 실제로 거치는지
- free 유저 팀 생성 → 402
- free 유저 초대 수락 → 402
- 권한 부족 → 403
- 정상 흐름 → 2xx

auth 는 get_current_user dependency override 로 통과.
team_repository 함수를 fake 로 교체해 분기만 검증.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import HTTPException

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


# ─── 팀 생성 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_team_free_user_returns_402(monkeypatch):
    async def boom(owner_email, name):
        raise HTTPException(status_code=402, detail="팀 기능은 유료 플랜 (Pro 이상) 이 필요합니다.")

    monkeypatch.setattr("app.api.team_routes.team_repository.create_team", boom)

    async with await _client() as client:
        r = await client.post("/api/teams", json={"name": "내 팀"})
    assert r.status_code == 402, r.text


@pytest.mark.asyncio
async def test_create_team_paid_user_returns_201(monkeypatch):
    async def ok(owner_email, name):
        return {"id": "t-1", "name": name, "created_at": "2026-01-01T00:00:00Z", "role": "owner"}

    monkeypatch.setattr("app.api.team_routes.team_repository.create_team", ok)

    async with await _client() as client:
        r = await client.post("/api/teams", json={"name": "내 팀"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "t-1"
    assert body["role"] == "owner"


@pytest.mark.asyncio
async def test_create_team_empty_name_422():
    """빈 이름 → Pydantic validation 422."""
    async with await _client() as client:
        r = await client.post("/api/teams", json={"name": ""})
    assert r.status_code == 422


# ─── 초대 수락 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_invite_free_user_returns_402(monkeypatch):
    async def boom(token, user_email):
        raise HTTPException(status_code=402, detail="팀 기능은 유료 플랜 (Pro 이상) 이 필요합니다.")

    monkeypatch.setattr("app.api.team_routes.team_repository.accept_invite", boom)

    async with await _client() as client:
        r = await client.post("/api/invites/tok-1/accept")
    assert r.status_code == 402, r.text


@pytest.mark.asyncio
async def test_accept_invite_success_returns_200(monkeypatch):
    async def ok(token, user_email):
        return {"team_id": "t-1", "team_name": "팀A", "role": "member"}

    monkeypatch.setattr("app.api.team_routes.team_repository.accept_invite", ok)

    async with await _client() as client:
        r = await client.post("/api/invites/tok-1/accept")
    assert r.status_code == 200, r.text
    assert r.json()["team_id"] == "t-1"


@pytest.mark.asyncio
async def test_get_invite_info_public(monkeypatch):
    """초대 정보 조회 — 로그인 없이도 동작 (토큰만)."""
    async def fake_get(token):
        return {
            "token": token, "team_id": "t-1", "team_name": "팀A",
            "inviter_email": "owner@example.com", "role": "member",
            "status": "pending", "expires_at": "2026-12-31T00:00:00Z",
        }

    monkeypatch.setattr("app.api.team_routes.team_repository.get_invite_by_token", fake_get)

    async with await _client() as client:
        r = await client.get("/api/invites/tok-1")
    assert r.status_code == 200, r.text
    assert r.json()["team_name"] == "팀A"


@pytest.mark.asyncio
async def test_get_invite_info_not_found_404(monkeypatch):
    async def fake_get(token):
        return None

    monkeypatch.setattr("app.api.team_routes.team_repository.get_invite_by_token", fake_get)

    async with await _client() as client:
        r = await client.get("/api/invites/nope")
    assert r.status_code == 404


# ─── 멤버 권한 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_insufficient_role_403(monkeypatch):
    """member 가 초대 시도 → 403."""
    async def boom(actor_email, team_id, invitee_email, role="member"):
        raise HTTPException(status_code=403, detail="권한이 부족합니다.")

    monkeypatch.setattr("app.api.team_routes.team_repository.create_invite", boom)

    async with await _client() as client:
        r = await client.post(
            "/api/teams/t-1/invites",
            json={"email": "new@example.com", "role": "member"},
        )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_invite_invalid_role_422():
    """owner 로 초대 시도 → Pydantic 패턴 위반 422."""
    async with await _client() as client:
        r = await client.post(
            "/api/teams/t-1/invites",
            json={"email": "new@example.com", "role": "owner"},
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_delete_team_non_owner_403(monkeypatch):
    async def boom(actor_email, team_id):
        raise HTTPException(status_code=403, detail="권한이 없거나 팀을 찾을 수 없습니다.")

    monkeypatch.setattr("app.api.team_routes.team_repository.delete_team", boom)

    async with await _client() as client:
        r = await client.delete("/api/teams/t-1")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_list_my_teams_returns_array(monkeypatch):
    async def fake_list(email):
        return [
            {"id": "t-1", "name": "팀A", "created_at": "2026-01-01T00:00:00Z",
             "role": "owner", "joined_at": "2026-01-01T00:00:00Z"},
        ]

    monkeypatch.setattr("app.api.team_routes.team_repository.get_teams_for_user", fake_list)

    async with await _client() as client:
        r = await client.get("/api/teams")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["name"] == "팀A"
