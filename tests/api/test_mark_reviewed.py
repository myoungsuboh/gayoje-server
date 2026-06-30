"""
mark-reviewed — AI 초안 API 명세(error_cases/auth) 검토 완료 → 만점 반영.

[검증]
- service: mark_api_reviewed 가 GET→reviewed 부착→update 호출, 없는 id 면 False
- service: mark_all_apis_reviewed 는 'ai_draft & 미검토' 항목 가진 API 만 카운트
- route: assert_access 가드(타인 403), 성공/404
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.api import eval_score_routes as routes
from app.service import query_repository as q
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


# ─── service 단 ────────────────────────────────────────


class _FakeRun:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher: str, params: Optional[Dict[str, Any]] = None,
                       database: Optional[str] = None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        return self._responses.pop(0) if self._responses else []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses=None):
        fake = _FakeRun(responses)
        monkeypatch.setattr("app.service.query_repository.neo4j_client.run_cypher", fake)
        return fake
    return _setup


async def test_mark_api_reviewed_sets_reviewed_true(fake_run):
    ec_json = json.dumps(
        [{"status": 404, "code": "NOT_FOUND", "source": "ai_draft", "reviewed": False}],
        ensure_ascii=False,
    )
    auth_json = json.dumps(
        {"required": True, "source": "ai_draft", "reviewed": False}, ensure_ascii=False
    )
    # 1) GET 현재 명세  2) UPDATE 결과(노드 존재)
    fake = fake_run([[{"error_cases": ec_json, "auth": auth_json}], [{"id": "API-01"}]])

    ok = await q.mark_api_reviewed("proj", "API-01")
    assert ok is True

    # 2번째 호출 = update_api_error_and_auth → reviewed=True 직렬화 확인
    upd = fake.calls[1]["params"]
    assert json.loads(upd["error_cases"])[0]["reviewed"] is True
    assert json.loads(upd["auth"])["reviewed"] is True


async def test_mark_api_reviewed_false_when_missing(fake_run):
    fake = fake_run([[]])  # GET 빈 결과 = 노드 없음
    ok = await q.mark_api_reviewed("proj", "GHOST")
    assert ok is False
    assert len(fake.calls) == 1  # update 미호출


async def test_mark_all_flips_only_unreviewed_ai_draft(fake_run):
    a1 = json.dumps([{"code": "X", "source": "ai_draft", "reviewed": False}], ensure_ascii=False)
    a2 = json.dumps([{"code": "Y", "source": "ai_draft", "reviewed": True}], ensure_ascii=False)
    fake = fake_run([
        [  # GET-all
            {"id": "API-01", "error_cases": a1, "auth": "{}"},
            {"id": "API-02", "error_cases": a2, "auth": "{}"},
        ],
        [{"id": "API-01"}],  # API-01 update 결과
    ])
    n = await q.mark_all_apis_reviewed("proj")
    assert n == 1  # API-01 만 변경
    # update 는 정확히 1번 (GET-all + update 1 = 총 2 호출)
    assert len(fake.calls) == 2
    assert fake.calls[1]["params"]["id"] == "API-01"


async def test_mark_all_zero_when_nothing_to_review(fake_run):
    human = json.dumps([{"code": "Z"}], ensure_ascii=False)  # source 없음 = 사람 작성
    fake = fake_run([[{"id": "API-01", "error_cases": human, "auth": "{}"}]])
    n = await q.mark_all_apis_reviewed("proj")
    assert n == 0
    assert len(fake.calls) == 1  # update 미호출


# ─── route 단 ──────────────────────────────────────────


def _user(email: str = "owner@x.com") -> UserPublic:
    return UserPublic(id="u-1", email=email, name="t", subscription_type="free", is_admin=False)


@pytest.fixture
def allow_access(monkeypatch):
    async def fake(email, project, team_id=None): return None
    monkeypatch.setattr("app.api.eval_score_routes.ownership_repository.assert_access", fake)


@pytest.fixture
def deny_access(monkeypatch):
    async def fake(email, project, team_id=None):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")
    monkeypatch.setattr("app.api.eval_score_routes.ownership_repository.assert_access", fake)


async def test_route_denies_when_not_owner(deny_access):
    payload = routes.MarkReviewedRequest(project_name="victim")
    with pytest.raises(HTTPException) as exc:
        await routes.mark_api_reviewed_route(
            api_id="API-01", payload=payload, current_user=_user("attacker@evil.com")
        )
    assert exc.value.status_code == 403


async def test_route_success(allow_access, monkeypatch):
    async def fake(project, api_id, team_id=""): return True
    monkeypatch.setattr("app.api.eval_score_routes.query_repository.mark_api_reviewed", fake)
    payload = routes.MarkReviewedRequest(project_name="p")
    out = await routes.mark_api_reviewed_route(
        api_id="API-01", payload=payload, current_user=_user()
    )
    assert out.success is True
    assert out.api_id == "API-01"


async def test_route_404_when_missing(allow_access, monkeypatch):
    async def fake(project, api_id, team_id=""): return False
    monkeypatch.setattr("app.api.eval_score_routes.query_repository.mark_api_reviewed", fake)
    payload = routes.MarkReviewedRequest(project_name="p")
    with pytest.raises(HTTPException) as exc:
        await routes.mark_api_reviewed_route(
            api_id="GHOST", payload=payload, current_user=_user()
        )
    assert exc.value.status_code == 404


async def test_route_all_returns_count(allow_access, monkeypatch):
    async def fake(project, team_id=""): return 3
    monkeypatch.setattr("app.api.eval_score_routes.query_repository.mark_all_apis_reviewed", fake)
    payload = routes.MarkReviewedRequest(project_name="p")
    out = await routes.mark_all_apis_reviewed_route(payload=payload, current_user=_user())
    assert out.success is True
    assert out.updated == 3
