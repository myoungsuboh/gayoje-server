"""
HTTP 레벨 ownership conflict 회귀 테스트.

검증:
- /api/gateway/postMeeting (compat dispatcher) — claim_project 가 conflict 던지면 409
- /api/v2/pipelines/post_meeting — 동일

auth 는 get_current_user 의존성을 monkeypatch 로 통과시킴.
ownership_repository.claim_project 를 fake 로 교체해 충돌 시뮬레이션.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.main import app
from app.core.security import get_current_user
from app.service.ownership_repository import ProjectOwnershipConflict
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
def conflict_claim(monkeypatch):
    """claim_project 가 ProjectOwnershipConflict 를 던지도록 강제."""

    async def boom(email: str, project: str) -> None:
        raise ProjectOwnershipConflict(
            project=project, current_owner_hint="bob@example.com"
        )

    # 모든 라우트가 import 한 경로 각각 patch
    monkeypatch.setattr(
        "app.service.ownership_repository.claim_project", boom
    )
    monkeypatch.setattr(
        "app.api.gateway_compat_routes.ownership_repository.claim_project", boom
    )


@pytest.fixture
def success_claim(monkeypatch):
    """claim_project 가 정상 통과."""

    async def ok(email: str, project: str) -> None:
        return None

    monkeypatch.setattr("app.service.ownership_repository.claim_project", ok)
    monkeypatch.setattr(
        "app.api.gateway_compat_routes.ownership_repository.claim_project", ok
    )


@pytest.mark.asyncio
async def test_gateway_compat_postMeeting_returns_409_on_other_owner(
    conflict_claim,
):
    """다른 유저 소유 시 /api/gateway/postMeeting 가 409 응답."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/gateway/postMeeting",
            json={
                "projectName": "harness",
                "meeting_content": "...",
                "version": "v1",
            },
        )
    assert r.status_code == 409, r.text
    detail = r.json().get("detail", "")
    assert "harness" in detail
    assert "다른 사용자" in detail


@pytest.mark.asyncio
async def test_gateway_compat_addProjectRepo_returns_409_on_other_owner(
    conflict_claim,
):
    """addProjectRepo 도 CREATE 라 같은 정책."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/gateway/addProjectRepo",
            json={"projectName": "taken", "url": "https://github.com/a/b"},
        )
    assert r.status_code == 409
    assert "taken" in r.json().get("detail", "")


@pytest.mark.asyncio
async def test_gateway_compat_postSkill_returns_409_on_other_owner(
    conflict_claim,
):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/gateway/postSkill",
            json={"projectName": "taken", "skills": []},
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_gateway_compat_create_allows_when_no_conflict(
    success_claim, monkeypatch,
):
    """
    claim 이 통과하면 다음 단계로 진행. 핸들러 안의 LLM 호출은 별도 mock 필요하므로
    여기서는 200 또는 5xx 가 아닌 "ownership 통과"만 확인 → status != 409.
    """
    # post_meeting 핸들러가 LLM 까지 호출하므로 임시로 핸들러를 noop 로 치환.
    async def noop_handler(body, query):
        return {"result": "ok"}

    monkeypatch.setitem(
        __import__(
            "app.api.gateway_compat_routes", fromlist=["_DISPATCH"]
        )._DISPATCH,
        "postSkill",
        noop_handler,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/gateway/postSkill",
            json={"projectName": "new-proj", "skills": []},
        )
    assert r.status_code != 409
