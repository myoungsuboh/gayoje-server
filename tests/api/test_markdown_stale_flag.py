"""
Phase 3.5a 회귀 — markdown_stale flag.

[보호하는 동작]
1. update_cps_node / update_prd_node cypher 가 master 의 markdown_stale=true 마킹.
2. update_master_cps_markdown / update_master_prd_markdown 이 markdown_stale=false.
3. GET 응답에 markdown_stale 노출.
4. POST /api/v2/{cps,prd}/markdown-stale/dismiss → 명시적 false 처리 + IDOR 가드.
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


def _fake_request(path: str = "/api/v2/cps/markdown-stale/dismiss"):
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path=path),
        method="POST",
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


# ─── cypher contract ───────────────────────────────────


def test_update_cps_node_cypher_marks_master_stale():
    """[회귀] update_cps_node 가 master CPS 에 markdown_stale=true 설정."""
    c = q._UPDATE_CPS_NODE_CYPHER
    assert "CPS_Document" in c
    assert "markdown_stale = true" in c
    assert "is_latest: true" in c


def test_update_prd_node_cypher_marks_master_stale():
    """[회귀] update_prd_node 가 master PRD 에 markdown_stale=true 설정."""
    c = q._UPDATE_PRD_NODE_CYPHER
    assert "PRD_Document" in c
    assert "markdown_stale = true" in c
    assert "is_latest: true" in c


def test_update_cps_markdown_cypher_clears_stale():
    """[회귀] update_master_cps_markdown 이 markdown_stale=false."""
    c = q._UPDATE_CPS_MARKDOWN_CYPHER
    assert "markdown_stale = false" in c


def test_update_prd_markdown_cypher_clears_stale():
    c = q._UPDATE_PRD_MARKDOWN_CYPHER
    assert "markdown_stale = false" in c


def test_get_cps_cypher_returns_markdown_stale():
    """[회귀] GET CPS cypher 가 markdown_stale 필드 반환 (FE banner 용)."""
    c = q._GET_CPS_CYPHER
    assert "markdown_stale" in c


def test_get_prd_cypher_returns_markdown_stale():
    c = q._GET_PRD_CYPHER
    assert "markdown_stale" in c


# ─── service unit ──────────────────────────────────────


class _FakeRun:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls = []
        self._responses = list(responses or [])

    async def __call__(self, cypher, params=None, database=None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        return self._responses.pop(0) if self._responses else []


async def test_dismiss_cps_stale_returns_true_when_master_exists(monkeypatch):
    fake = _FakeRun([[{"master_id": "m-1"}]])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake
    )
    assert await q.dismiss_cps_markdown_stale("p") is True


async def test_dismiss_cps_stale_returns_false_when_missing(monkeypatch):
    fake = _FakeRun([[]])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake
    )
    assert await q.dismiss_cps_markdown_stale("ghost") is False


async def test_dismiss_prd_stale_returns_true_when_master_exists(monkeypatch):
    fake = _FakeRun([[{"master_id": "p-1"}]])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake
    )
    assert await q.dismiss_prd_markdown_stale("p") is True


# ─── route — IDOR + 404 ───────────────────────────────


async def test_dismiss_cps_stale_route_denies_idor(deny_ownership):
    payload = query_routes.DismissStaleRequest(project_name="victim")
    with pytest.raises(HTTPException) as exc:
        await query_routes.dismiss_cps_stale_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user("attacker@evil.com"),
        )
    assert exc.value.status_code == 403


async def test_dismiss_cps_stale_route_404_when_no_master(allow_ownership, monkeypatch):
    async def fake(project, team_id=""): return False
    monkeypatch.setattr(
        "app.api.query_routes.q.dismiss_cps_markdown_stale", fake
    )
    payload = query_routes.DismissStaleRequest(project_name="p")
    with pytest.raises(HTTPException) as exc:
        await query_routes.dismiss_cps_stale_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user(),
        )
    assert exc.value.status_code == 404


async def test_dismiss_cps_stale_route_ok(allow_ownership, monkeypatch):
    async def fake(project, team_id=""): return True
    monkeypatch.setattr(
        "app.api.query_routes.q.dismiss_cps_markdown_stale", fake
    )
    payload = query_routes.DismissStaleRequest(project_name="p")
    out = await query_routes.dismiss_cps_stale_route.__wrapped__(
        request=_fake_request(),
        payload=payload,
        current_user=_user(),
    )
    assert out.project_name == "p"
    assert out.dismissed is True


async def test_dismiss_prd_stale_route_denies_idor(deny_ownership):
    payload = query_routes.DismissStaleRequest(project_name="victim")
    with pytest.raises(HTTPException) as exc:
        await query_routes.dismiss_prd_stale_route.__wrapped__(
            request=_fake_request("/api/v2/prd/markdown-stale/dismiss"),
            payload=payload,
            current_user=_user("attacker@evil.com"),
        )
    assert exc.value.status_code == 403


def test_dismiss_routes_have_rate_limit():
    assert hasattr(query_routes.dismiss_cps_stale_route, "__wrapped__")
    assert hasattr(query_routes.dismiss_prd_stale_route, "__wrapped__")
