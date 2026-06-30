"""
모든 status 라우트의 ownership 검증 통합 테스트 (Sprint 8 P0).

[검증]
7개 status 라우트 — 다른 사용자의 task_id 조회 시 403, 본인 task_id 는 200.

라우트 목록:
  GET /api/v2/pipelines/lineage/status/{task_id}
  GET /api/v2/pipelines/lint/status/{task_id}
  GET /api/v2/pipelines/create_md/status/{task_id}
  GET /api/v2/pipelines/skill/status/{task_id}    (legacy — `/status/{task_id}`)
  GET /api/v2/delete/status/{task_id}
  GET /api/v2/cps/status/{task_id}                (legacy)
  GET /api/v2/pipelines/status/{task_id}          (통합)

각 라우트를 직접 호출하지 않고 status_guard 가드만 dependency-inject —
가드의 분기가 라우트 핸들러 안에서 호출되는지 확인.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import HTTPException

from app.api import (
    create_md_routes,
    delete_routes,
    lineage_routes,
    lint_routes,
    skill_routes,
    v2_routes,
)
from app.queue import status_guard
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio

def _fake_request():
    """slowapi limiter request 객체 우회."""
    from types import SimpleNamespace
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/v2/test"),
        method="POST",
    )



def _make_user(email: str) -> UserPublic:
    return UserPublic(id=email, email=email, name="test", created_at=None)


@pytest.fixture
def mock_guard(monkeypatch):
    """get_job_status_for_user 의 동작 주입."""
    state = {"info": None, "raises_status": None}

    async def fake(task_id, user_email):
        if state["raises_status"]:
            raise HTTPException(
                status_code=state["raises_status"],
                detail="testfake",
            )
        return state["info"]

    # 모든 라우트 모듈에 같은 가드 함수 reference 가 import 되어 있으므로,
    # 각 모듈 namespace 에서 patch.
    for mod in (
        create_md_routes,
        delete_routes,
        lineage_routes,
        lint_routes,
        skill_routes,
        v2_routes,
    ):
        monkeypatch.setattr(mod, "get_job_status_for_user", fake)
    return state


# ─── 7개 라우트의 핸들러 직접 호출 시뮬레이션 ──


@pytest.mark.parametrize("handler_provider", [
    lambda: lineage_routes.lineage_status_route,
    lambda: lint_routes.lint_status_route,
    lambda: create_md_routes.create_md_status_route,
    lambda: skill_routes.recommend_skills_status,
    lambda: delete_routes.delete_meeting_status_route,
    lambda: v2_routes.cps_status,
    lambda: v2_routes.pipeline_status,
])
async def test_owner_can_see_status(mock_guard, handler_provider):
    """본인 task — 가드 통과 → 응답 반환."""
    mock_guard["info"] = {
        "task_id": "t1",
        "project_name": "alice_proj",
        "status": "complete",
        "result": {"x": 1},
    }
    handler = handler_provider()
    # 모든 status 라우트가 @limiter.limit 데코레이터 적용 — __wrapped__ 로 호출.
    fn = getattr(handler, "__wrapped__", handler)
    out = await fn(_fake_request(), "t1", _make_user("alice@x"))
    assert out.task_id == "t1"
    assert out.project_name == "alice_proj"
    assert out.status == "complete"


@pytest.mark.parametrize("handler_provider", [
    lambda: lineage_routes.lineage_status_route,
    lambda: lint_routes.lint_status_route,
    lambda: create_md_routes.create_md_status_route,
    lambda: skill_routes.recommend_skills_status,
    lambda: delete_routes.delete_meeting_status_route,
    lambda: v2_routes.cps_status,
    lambda: v2_routes.pipeline_status,
])
async def test_other_user_gets_403(mock_guard, handler_provider):
    """다른 사용자 task — 가드가 403 raise → 라우트도 그대로 전파."""
    mock_guard["raises_status"] = 403
    handler = handler_provider()
    fn = getattr(handler, "__wrapped__", handler)
    with pytest.raises(HTTPException) as exc_info:
        await fn(_fake_request(), "t1", _make_user("bob@x"))
    assert exc_info.value.status_code == 403


@pytest.mark.parametrize("handler_provider", [
    lambda: lineage_routes.lineage_status_route,
    lambda: lint_routes.lint_status_route,
    lambda: create_md_routes.create_md_status_route,
    lambda: skill_routes.recommend_skills_status,
    lambda: delete_routes.delete_meeting_status_route,
    lambda: v2_routes.cps_status,
    lambda: v2_routes.pipeline_status,
])
async def test_not_found_gets_404(mock_guard, handler_provider):
    """존재하지 않는 task_id — 404 (정보 누설 방지)."""
    mock_guard["raises_status"] = 404
    handler = handler_provider()
    fn = getattr(handler, "__wrapped__", handler)
    with pytest.raises(HTTPException) as exc_info:
        await fn(_fake_request(), "nonexistent", _make_user("alice@x"))
    assert exc_info.value.status_code == 404
