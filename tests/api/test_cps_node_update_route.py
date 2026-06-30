"""
PATCH /api/v2/cps/nodes/{id} 회귀 가드 — Phase 3.1 노드 단위 inline edit.

[검증]
- 정상 → repository 호출 + UpdateCpsNodeResponse 반환
- ownership 가드 (타인 프로젝트 → 403)
- 노드 없음 → 404
- Pydantic 검증 (빈/2KB+ summary, project_name 누락)
- rate limit 데코레이터 적용
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import query_routes
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
        url=SimpleNamespace(path="/api/v2/cps/nodes/prb_01"),
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
def fake_update_node(monkeypatch):
    state = {"return": {"id": "prb_01", "label": "Problem", "summary": "updated"}}
    async def fake(project, node_id, summary, team_id=""):
        state["last_call"] = {"project": project, "node_id": node_id, "summary": summary}
        return state["return"]
    monkeypatch.setattr(
        "app.api.query_routes.q.update_cps_node", fake
    )
    return state


# ─── 정상 ───────────────────────────────────────────


async def test_update_node_returns_response(allow_ownership, fake_update_node):
    payload = query_routes.UpdateCpsNodeRequest(
        project_name="p",
        summary="edited",
    )
    out = await query_routes.update_cps_node_route.__wrapped__(
        request=_fake_request(),
        node_id="prb_01",
        payload=payload,
        current_user=_user(),
    )
    assert out.id == "prb_01"
    assert out.label == "Problem"
    assert out.summary == "updated"
    # repository 호출 인자 검증
    assert fake_update_node["last_call"] == {
        "project": "p", "node_id": "prb_01", "summary": "edited",
    }


# ─── IDOR ─────────────────────────────────────────────


async def test_update_node_denies_when_not_owner(deny_ownership):
    payload = query_routes.UpdateCpsNodeRequest(
        project_name="victim", summary="evil",
    )
    with pytest.raises(HTTPException) as exc:
        await query_routes.update_cps_node_route.__wrapped__(
            request=_fake_request(),
            node_id="prb_01",
            payload=payload,
            current_user=_user("attacker@evil.com"),
        )
    assert exc.value.status_code == 403


# ─── 404 ──────────────────────────────────────────────


async def test_update_node_404_when_missing(allow_ownership, fake_update_node):
    """노드 없으면 repository None → 라우트 404."""
    fake_update_node["return"] = None
    payload = query_routes.UpdateCpsNodeRequest(project_name="p", summary="x")
    with pytest.raises(HTTPException) as exc:
        await query_routes.update_cps_node_route.__wrapped__(
            request=_fake_request(),
            node_id="ghost",
            payload=payload,
            current_user=_user(),
        )
    assert exc.value.status_code == 404
    assert "Problem/Solution" in exc.value.detail


# ─── Pydantic 검증 ─────────────────────────────────────


def test_request_rejects_empty_summary():
    with pytest.raises(Exception):
        query_routes.UpdateCpsNodeRequest(project_name="p", summary="")


def test_request_rejects_oversized_summary():
    """2KB 초과 거부 — 단일 노드 요약치곤 비정상."""
    with pytest.raises(Exception):
        query_routes.UpdateCpsNodeRequest(
            project_name="p",
            summary="a" * 2001,
        )


def test_request_rejects_missing_project_name():
    with pytest.raises(Exception):
        query_routes.UpdateCpsNodeRequest(summary="x")


# ─── rate limit ───────────────────────────────────────


def test_update_node_route_has_rate_limit():
    assert hasattr(query_routes.update_cps_node_route, "__wrapped__"), (
        "update_cps_node_route 에 @limiter.limit 누락"
    )
