"""
GET /api/v2/cps/nodes & /api/v2/prd/nodes 회귀 — Phase 3.3 노드 listing.

FE 사이드바가 markdown 파싱이 아닌 그래프 ID 로 PATCH 호출하기 위한 endpoint.
IDOR / 빈 리스트 / cypher whitelist 회귀 모두 가드.
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


def _fake_request(path: str = "/api/v2/cps/nodes"):
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path=path),
        method="GET",
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


# ─── CPS list route ───────────────────────────────────


async def test_list_cps_nodes_returns_problem_and_solution(allow_ownership, monkeypatch):
    async def fake(project, team_id=""):
        return [
            {"id": "prb_01_1", "label": "Problem", "summary": "p1"},
            {"id": "res_01_1", "label": "Solution", "summary": "s1"},
        ]
    monkeypatch.setattr("app.api.query_routes.q.list_cps_nodes", fake)

    out = await query_routes.list_cps_nodes_route.__wrapped__(
        request=_fake_request(),
        project_name="p",
        current_user=_user(),
    )
    assert len(out.nodes) == 2
    assert out.nodes[0].id == "prb_01_1"
    assert out.nodes[0].label == "Problem"
    assert out.nodes[1].label == "Solution"


async def test_list_cps_nodes_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await query_routes.list_cps_nodes_route.__wrapped__(
            request=_fake_request(),
            project_name="victim",
            current_user=_user("attacker@evil.com"),
        )
    assert exc.value.status_code == 403


async def test_list_cps_nodes_empty_when_no_match(allow_ownership, monkeypatch):
    async def fake(project, team_id=""): return []
    monkeypatch.setattr("app.api.query_routes.q.list_cps_nodes", fake)
    out = await query_routes.list_cps_nodes_route.__wrapped__(
        request=_fake_request(),
        project_name="p",
        current_user=_user(),
    )
    assert out.nodes == []


def test_list_cps_nodes_route_has_rate_limit():
    assert hasattr(query_routes.list_cps_nodes_route, "__wrapped__")


# ─── PRD list route ───────────────────────────────────


async def test_list_prd_nodes_returns_epic_and_story(allow_ownership, monkeypatch):
    async def fake(project, team_id=""):
        return [
            {"id": "epic_01", "label": "Epic", "summary": "e1"},
            {"id": "story_01_1", "label": "Story", "summary": "s1"},
        ]
    monkeypatch.setattr("app.api.query_routes.q.list_prd_nodes", fake)

    out = await query_routes.list_prd_nodes_route.__wrapped__(
        request=_fake_request("/api/v2/prd/nodes"),
        project_name="p",
        current_user=_user(),
    )
    assert len(out.nodes) == 2
    assert out.nodes[0].label == "Epic"
    assert out.nodes[1].id == "story_01_1"


async def test_list_prd_nodes_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await query_routes.list_prd_nodes_route.__wrapped__(
            request=_fake_request("/api/v2/prd/nodes"),
            project_name="victim",
            current_user=_user("attacker@evil.com"),
        )
    assert exc.value.status_code == 403


def test_list_prd_nodes_route_has_rate_limit():
    assert hasattr(query_routes.list_prd_nodes_route, "__wrapped__")


# ─── service 단 ────────────────────────────────────────


class _FakeRun:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher, params=None, database=None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        return self._responses.pop(0) if self._responses else []


async def test_list_cps_nodes_service_filters_empty_ids(monkeypatch):
    fake = _FakeRun([[
        {"id": "prb_01_1", "label": "Problem", "summary": "x"},
        {"id": None, "label": "Problem", "summary": "y"},  # 잘못된 row — 제거되어야
    ]])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake
    )
    out = await q.list_cps_nodes("p")
    assert len(out) == 1
    assert out[0]["id"] == "prb_01_1"


def test_list_cps_cypher_label_whitelist():
    """[회귀] Problem/Solution 만 — 다른 라벨 보호."""
    cypher = q._LIST_CPS_NODES_CYPHER
    assert "n:Problem OR n:Solution" in cypher
    assert "project: $project" in cypher


def test_list_prd_cypher_label_whitelist():
    """[회귀] Epic/Story 만 — 다른 라벨 보호."""
    cypher = q._LIST_PRD_NODES_CYPHER
    assert "n:Epic OR n:Story" in cypher
    assert "project: $project" in cypher


async def test_list_prd_nodes_service_returns_sorted(monkeypatch):
    fake = _FakeRun([[
        {"id": "epic_01", "label": "Epic", "summary": "e1"},
        {"id": "story_01_1", "label": "Story", "summary": "s1"},
    ]])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake
    )
    out = await q.list_prd_nodes("p")
    assert [n["label"] for n in out] == ["Epic", "Story"]
