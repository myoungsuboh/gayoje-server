"""
query_routes.py 의 IDOR 회귀 가드 — 모든 조회 라우트에 assert_owns 호출.

[배경]
2026-05 보안 감사에서 발견된 IDOR: /api/v2/{cps,prd,ddd,spack,architecture,
meetings/logs,meetings/versions} 가 인증만 있고 ownership 검증 없음 → 본인
JWT 보유 사용자가 project_name 만 알면 타인 데이터 조회 가능. gateway_compat
경로는 정상 차단하나 v2 native 라우트가 누락됐던 상태.

[회귀 가드]
각 라우트가 다른 사용자 프로젝트 호출 시 403 raise 하는지 검증.
ownership_repository.assert_owns 가 호출 → 403 → 응답 본문이 leak 되지 않는지
확인.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException, status as http_status

from app.api import query_routes
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


def _user(email: str = "attacker@evil.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="t",
        subscription_type="free", is_admin=False,
    )


def _fake_request() -> SimpleNamespace:
    """slowapi limiter 가 request 객체 요구 — SimpleNamespace 우회."""
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/v2/test"),
        method="GET",
    )


@pytest.fixture
def deny_ownership(monkeypatch):
    """assert_owns 를 호출 시 403 raise 하도록 stub — 타인 프로젝트 조회 시나리오."""
    async def fake_assert(email: str, project: str):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="해당 프로젝트에 대한 권한이 없습니다.",
        )
    monkeypatch.setattr(
        "app.api.query_routes.ownership_repository.assert_owns", fake_assert
    )


@pytest.fixture
def allow_ownership(monkeypatch):
    """assert_owns 통과 + repository 빈 응답 → 라우트가 정상 동작 검증."""
    calls: List[Dict[str, str]] = []

    async def fake_assert(email: str, project: str):
        calls.append({"email": email, "project": project})

    monkeypatch.setattr(
        "app.api.query_routes.ownership_repository.assert_owns", fake_assert
    )
    return calls


# ─── 모든 read 라우트가 assert_owns 호출 + 거부 시 403 ─────────


async def test_get_cps_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await query_routes.get_cps_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project",
            current_user=_user(),
        )
    assert exc.value.status_code == 403
    # 응답 본문에 victim 데이터 leak 없는지 — assert_owns 가 403 raise 후 q 호출 안 됨
    assert "프로젝트" in str(exc.value.detail)


async def test_get_prd_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await query_routes.get_prd_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project",
            current_user=_user(),
        )
    assert exc.value.status_code == 403


async def test_get_ddd_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await query_routes.get_ddd_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project",
            current_user=_user(),
        )
    assert exc.value.status_code == 403


async def test_get_spack_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await query_routes.get_spack_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project",
            current_user=_user(),
        )
    assert exc.value.status_code == 403


async def test_get_architecture_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await query_routes.get_architecture_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project",
            current_user=_user(),
        )
    assert exc.value.status_code == 403


async def test_get_meeting_log_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await query_routes.get_meeting_log_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project",
            version="v1",
            current_user=_user(),
        )
    assert exc.value.status_code == 403


async def test_get_meeting_versions_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await query_routes.get_meeting_versions_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project",
            current_user=_user(),
        )
    assert exc.value.status_code == 403


# ─── 정상 경로 — 본인 프로젝트면 통과 + repository 호출 ────────


async def test_get_cps_passes_when_owner(allow_ownership, monkeypatch):
    """본인 소유면 assert_owns 가 통과하고 repository 가 호출됨."""
    async def fake_get_master_cps(project, team_id=""):
        return query_routes.CpsMaster(
            master_id="m1", version="v1", content="...", last_updated=None,
            absorbed_cps_ids=[],
        )
    monkeypatch.setattr(
        "app.api.query_routes.q.get_master_cps", fake_get_master_cps
    )
    result = await query_routes.get_cps_route.__wrapped__(
        request=_fake_request(),
        project_name="my_project",
        current_user=_user("owner@x.com"),
    )
    # assert_owns 가 정확한 (email, project) 로 호출됐는지
    assert allow_ownership == [{"email": "owner@x.com", "project": "my_project"}]
    assert result.master_id == "m1"
