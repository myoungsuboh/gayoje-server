"""
autofix needs_input 영속화 — 라우트 레벨 (2026-06).

1) /prd/autofix 가 결과의 needs_input 을 master 노드에 저장 (빈 결과는 해제).
   저장 실패는 best-effort — 보완 응답 자체는 정상 반환.
2) /prd/autofix/needs-input/dismiss — FE 의 X(닫기)가 BE 도 함께 지움.
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


def _fake_request(path: str = "/api/v2/prd/autofix"):
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
    async def fake(email, project, team_id=None):
        return None
    monkeypatch.setattr(
        "app.api.query_routes.ownership_repository.assert_access", fake
    )


@pytest.fixture
def no_quota_guard(monkeypatch):
    async def fake(email):
        return None
    monkeypatch.setattr("app.api.query_routes.quota.assert_tokens_within_limit", fake)


@pytest.fixture
def fake_tracked(monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake(**kw):
        yield SimpleNamespace()
    monkeypatch.setattr("app.api.query_routes.tracked_pipeline_context", fake)


def _autofix_result(needs):
    return SimpleNamespace(
        project_name="projX", current_markdown="# old", improved_markdown="# new",
        before_score=0.5, after_score=0.9, before_issues=[], after_issues=[],
        needs_input=needs, changed=True,
    )


async def test_autofix_route_persists_needs_input(
    monkeypatch, allow_ownership, no_quota_guard, fake_tracked
):
    needs = [{"topic": "모델 제휴", "question": "파라미터?"}]

    async def fake_run(ctx, project, current_markdown=None):
        return _autofix_result(needs)
    monkeypatch.setattr("app.api.query_routes.run_prd_autofix", fake_run)

    saved = {}

    async def spy_set(project, items, team_id=""):
        saved["project"] = project
        saved["items"] = items
        saved["team_id"] = team_id
        return True
    monkeypatch.setattr("app.api.query_routes.q.set_prd_autofix_needs_input", spy_set)

    out = await query_routes.autofix_prd_route.__wrapped__(
        _fake_request(),
        query_routes.PrdAutofixRequest(project_name="projX", team_id="team-3", text="# old"),
        current_user=_user(),
    )
    assert saved == {"project": "projX", "items": needs, "team_id": "team-3"}
    assert [n.topic for n in out.needs_input] == ["모델 제휴"]


async def test_autofix_route_persist_failure_does_not_break_response(
    monkeypatch, allow_ownership, no_quota_guard, fake_tracked
):
    """저장 실패(Neo4j 일시 장애)가 보완 결과 응답을 막으면 안 됨 — best-effort."""
    async def fake_run(ctx, project, current_markdown=None):
        return _autofix_result([{"topic": "t", "question": "q"}])
    monkeypatch.setattr("app.api.query_routes.run_prd_autofix", fake_run)

    async def boom(project, items, team_id=""):
        raise RuntimeError("neo4j down")
    monkeypatch.setattr("app.api.query_routes.q.set_prd_autofix_needs_input", boom)

    out = await query_routes.autofix_prd_route.__wrapped__(
        _fake_request(),
        query_routes.PrdAutofixRequest(project_name="projX", text="# old"),
        current_user=_user(),
    )
    assert out.changed is True  # 응답 정상


async def test_dismiss_route_clears_and_is_idempotent(monkeypatch, allow_ownership):
    cleared = []

    async def spy_clear(project, team_id=""):
        cleared.append((project, team_id))
        return False  # master 없음 — 그래도 dismissed (멱등)
    monkeypatch.setattr(
        "app.api.query_routes.q.clear_prd_autofix_needs_input", spy_clear
    )

    out = await query_routes.dismiss_autofix_needs_route.__wrapped__(
        _fake_request("/api/v2/prd/autofix/needs-input/dismiss"),
        query_routes.AutofixNeedsDismissRequest(project_name="projX", team_id="team-3"),
        current_user=_user(),
    )
    assert cleared == [("projX", "team-3")]
    assert out.dismissed is True


async def test_dismiss_route_denies_non_owner(monkeypatch):
    async def deny(email, project, team_id=None):
        raise HTTPException(status_code=403, detail="forbidden")
    monkeypatch.setattr(
        "app.api.query_routes.ownership_repository.assert_access", deny
    )

    with pytest.raises(HTTPException) as ei:
        await query_routes.dismiss_autofix_needs_route.__wrapped__(
            _fake_request("/api/v2/prd/autofix/needs-input/dismiss"),
            query_routes.AutofixNeedsDismissRequest(project_name="projX"),
            current_user=_user("intruder@x.com"),
        )
    assert ei.value.status_code == 403
