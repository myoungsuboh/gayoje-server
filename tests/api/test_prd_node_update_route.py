"""
PATCH /api/v2/prd/nodes/{id} 회귀 가드 — Phase 3.2 Epic/Story 단일 수정.

CPS 3.1 와 대칭 패턴 — 같은 IDOR / 404 / Pydantic 검증.
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
        url=SimpleNamespace(path="/api/v2/prd/nodes/epic_01"),
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
def fake_update_prd_node(monkeypatch):
    state = {"return": {"id": "epic_01", "label": "Epic", "summary": "edited"}}
    async def fake(project, node_id, summary, team_id=""):
        state["last_call"] = {"project": project, "node_id": node_id, "summary": summary}
        return state["return"]
    monkeypatch.setattr(
        "app.api.query_routes.q.update_prd_node", fake
    )
    return state


# ─── 라우트 ───────────────────────────────────────────


async def test_update_epic_returns_response(allow_ownership, fake_update_prd_node):
    payload = query_routes.UpdateNodeRequest(project_name="p", summary="edited")
    out = await query_routes.update_prd_node_route.__wrapped__(
        request=_fake_request(),
        node_id="epic_01",
        payload=payload,
        current_user=_user(),
    )
    assert out.id == "epic_01"
    assert out.label == "Epic"
    assert out.summary == "edited"


async def test_update_story_returns_story_label(allow_ownership, fake_update_prd_node):
    fake_update_prd_node["return"] = {
        "id": "story_01_1", "label": "Story", "summary": "story updated",
    }
    payload = query_routes.UpdateNodeRequest(project_name="p", summary="story updated")
    out = await query_routes.update_prd_node_route.__wrapped__(
        request=_fake_request(),
        node_id="story_01_1",
        payload=payload,
        current_user=_user(),
    )
    assert out.label == "Story"


async def test_update_prd_node_denies_when_not_owner(deny_ownership):
    payload = query_routes.UpdateNodeRequest(project_name="victim", summary="evil")
    with pytest.raises(HTTPException) as exc:
        await query_routes.update_prd_node_route.__wrapped__(
            request=_fake_request(),
            node_id="epic_01",
            payload=payload,
            current_user=_user("attacker@evil.com"),
        )
    assert exc.value.status_code == 403


async def test_update_prd_node_404_when_missing(allow_ownership, fake_update_prd_node):
    fake_update_prd_node["return"] = None
    payload = query_routes.UpdateNodeRequest(project_name="p", summary="x")
    with pytest.raises(HTTPException) as exc:
        await query_routes.update_prd_node_route.__wrapped__(
            request=_fake_request(),
            node_id="ghost",
            payload=payload,
            current_user=_user(),
        )
    assert exc.value.status_code == 404
    assert "Epic/Story" in exc.value.detail


def test_update_prd_node_route_has_rate_limit():
    assert hasattr(query_routes.update_prd_node_route, "__wrapped__")


# ─── service 단 ────────────────────────────────────────


class _FakeRun:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher, params=None, database=None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        return self._responses.pop(0) if self._responses else []


async def test_update_prd_node_service_returns_label(monkeypatch):
    fake = _FakeRun([[{"id": "epic_01", "label": "Epic", "summary": "x"}]])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake
    )
    out = await q.update_prd_node("p", "epic_01", "x")
    assert out == {"id": "epic_01", "label": "Epic", "summary": "x"}


def test_prd_node_cypher_label_whitelist():
    """[회귀] cypher Epic|Story 만 매칭 — 다른 라벨 보호."""
    cypher = q._UPDATE_PRD_NODE_CYPHER
    assert "n:Epic OR n:Story" in cypher
    assert "project: $project" in cypher
    assert "user_edited_at" in cypher


async def test_update_prd_node_service_returns_none_when_missing(monkeypatch):
    fake = _FakeRun([[]])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake
    )
    out = await q.update_prd_node("p", "ghost", "x")
    assert out is None
