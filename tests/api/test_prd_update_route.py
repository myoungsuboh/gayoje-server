"""
PATCH /api/v2/prd 회귀 가드 — 검수 게이트 PRD markdown 직접 편집 (Phase 2.2).

[검증]
- ownership 가드 (타인 프로젝트 → 403)
- master 없으면 404 + 안내
- 정상 → repository 호출 + UpdateMarkdownResponse
- rate limit 적용 (__wrapped__)
- service update_master_prd_markdown 도 함께 검증 (간단)
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.api import query_routes
from app.service import query_repository as q
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


def _user(email: str = "owner@x.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="t",
        subscription_type="free", is_admin=False,
    )


def _fake_request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/v2/prd"),
        method="PATCH",
    )


@pytest.fixture
def allow_ownership(monkeypatch):
    async def fake(email, project): return None
    monkeypatch.setattr(
        "app.api.query_routes.ownership_repository.assert_owns", fake
    )


@pytest.fixture
def deny_ownership(monkeypatch):
    async def fake(email, project):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")
    monkeypatch.setattr(
        "app.api.query_routes.ownership_repository.assert_owns", fake
    )


@pytest.fixture
def fake_update(monkeypatch):
    state = {"return": {"master_id": "prd-m1", "last_updated": 1700000000000}}
    # [2026-05-18 Phase 2] client_updated_at kwarg (optimistic locking)
    async def fake(project, content, *, client_updated_at=None, team_id=""):
        state["last_call"] = {
            "project": project,
            "content": content,
            "client_updated_at": client_updated_at,
        }
        return state["return"]
    monkeypatch.setattr(
        "app.api.query_routes.q.update_master_prd_markdown", fake
    )
    return state


# ─── 라우트 ────────────────────────────────────────────


async def test_update_prd_returns_master_id(allow_ownership, fake_update):
    payload = query_routes.UpdateMarkdownRequest(
        project_name="p",
        content="# new prd",
    )
    out = await query_routes.update_prd_route.__wrapped__(
        request=_fake_request(),
        payload=payload,
        current_user=_user(),
    )
    assert out.master_id == "prd-m1"
    assert fake_update["last_call"]["project"] == "p"
    assert fake_update["last_call"]["content"] == "# new prd"


async def test_update_prd_denies_when_not_owner(deny_ownership):
    payload = query_routes.UpdateMarkdownRequest(
        project_name="victim", content="evil",
    )
    with pytest.raises(HTTPException) as exc:
        await query_routes.update_prd_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user("attacker@evil.com"),
        )
    assert exc.value.status_code == 403


async def test_update_prd_404_when_master_missing(allow_ownership, fake_update):
    fake_update["return"] = None
    payload = query_routes.UpdateMarkdownRequest(project_name="p", content="x")
    with pytest.raises(HTTPException) as exc:
        await query_routes.update_prd_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user(),
        )
    assert exc.value.status_code == 404
    assert "createPRD" in exc.value.detail or "postMeeting" in exc.value.detail


def test_update_prd_route_has_rate_limit():
    assert hasattr(query_routes.update_prd_route, "__wrapped__"), (
        "update_prd_route 에 @limiter.limit 누락"
    )


# ─── service 단 ─────────────────────────────────────────


class _FakeRun:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher: str, params: Optional[Dict[str, Any]] = None,
                       database: Optional[str] = None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        return self._responses.pop(0) if self._responses else []


async def test_update_prd_service_returns_master_id(monkeypatch):
    fake = _FakeRun([[{"master_id": "prd-m1", "last_updated": 1700000000000}]])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake
    )
    out = await q.update_master_prd_markdown("p", "# x")
    assert out == {"master_id": "prd-m1", "last_updated": 1700000000000}
    # cypher 가 PRD_Document + Master + is_latest 필터
    cypher = fake.calls[0]["cypher"]
    assert "PRD_Document" in cypher
    assert "type: 'Master'" in cypher
    assert "is_latest: true" in cypher


async def test_update_prd_service_returns_none_when_missing(monkeypatch):
    fake = _FakeRun([[]])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake
    )
    out = await q.update_master_prd_markdown("ghost", "x")
    assert out is None


# ─── [2026-05-18 Phase 2] Optimistic Locking ────────────


async def test_update_prd_409_on_conflict(allow_ownership, monkeypatch):
    """다른 디바이스가 먼저 편집 → 409."""
    from app.service.query_repository import OptimisticLockConflict

    async def fake_conflict(project, content, *, client_updated_at=None, team_id=""):
        raise OptimisticLockConflict("다른 디바이스에서 먼저 편집됐습니다.")
    monkeypatch.setattr(
        "app.api.query_routes.q.update_master_prd_markdown", fake_conflict
    )

    payload = query_routes.UpdateMarkdownRequest(
        project_name="p",
        content="new",
        client_updated_at=1700000000000,
    )
    with pytest.raises(HTTPException) as exc:
        await query_routes.update_prd_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user(),
        )
    assert exc.value.status_code == 409


async def test_update_prd_passes_client_updated_at_to_repo(allow_ownership, fake_update):
    """client_updated_at 전달 확인."""
    payload = query_routes.UpdateMarkdownRequest(
        project_name="p",
        content="x",
        client_updated_at=1700000000000,
    )
    await query_routes.update_prd_route.__wrapped__(
        request=_fake_request(),
        payload=payload,
        current_user=_user(),
    )
    assert fake_update["last_call"]["client_updated_at"] == 1700000000000
