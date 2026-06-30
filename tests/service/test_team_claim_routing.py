"""
팀(Team) 기능 — Phase F: 통합 claim() 라우팅 + assert_team_access 게이트 회귀 테스트.

검증 대상:
- F2: claim(email, project, team_id=None) → 개인 claim_project 위임 (기존 동작)
- F2: claim(..., team_id=X) → 멤버십/플랜 게이트 후 claim_team_project
- 위조 team_id 차단 — 비멤버 → 403, free 플랜 → 402
- F3: 라우트가 payload.team_id 를 assert_access / claim 으로 전달

[전략]
neo4j_client.run_cypher / usage_repository.get_usage / team_repository 분기를
monkeypatch 로 대체. 실제 Neo4j 없이 분기 로직만 결정적으로 검증.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.service import ownership_repository, team_repository
from app.service.usage_repository import Usage


def _usage(subscription_type: str) -> Usage:
    return Usage(
        email="user@example.com",
        subscription_type=subscription_type,
        meeting_count=0,
        total_tokens=0,
        total_chars=0,
    )


# ─── F2: claim() 개인 분기 — 기존 동작 보존 ────────────────────


@pytest.mark.asyncio
async def test_claim_personal_delegates_to_claim_project(monkeypatch):
    """team_id 없으면 claim_project 로 위임 — 팀 게이트 미발동."""
    called = {}

    async def fake_claim_project(email, project):
        called["args"] = (email, project)
        return "pid-personal"

    async def boom_team_access(email, team_id):
        raise AssertionError("개인 claim 인데 팀 게이트가 호출됨")

    monkeypatch.setattr(ownership_repository, "claim_project", fake_claim_project)
    monkeypatch.setattr(ownership_repository, "assert_team_access", boom_team_access)

    pid = await ownership_repository.claim("alice@example.com", "proj-x")
    assert pid == "pid-personal"
    assert called["args"] == ("alice@example.com", "proj-x")


# ─── F2: claim() 팀 분기 — 게이트 통과 후 팀 프로젝트 생성 ──────


@pytest.mark.asyncio
async def test_claim_team_passes_gate_then_claims_team_project(monkeypatch):
    """유료 멤버 → assert_team_access 통과 → claim_team_project 호출."""
    order = []

    async def fake_assert_role(email, team_id, min_role=team_repository.ROLE_MEMBER):
        order.append("role")
        return team_repository.ROLE_MEMBER

    async def fake_get_usage(email):
        order.append("usage")
        return _usage("pro")

    async def fake_claim_team(email, team_id, project):
        order.append("claim_team")
        return "pid-team"

    async def boom_personal(email, project):
        raise AssertionError("팀 claim 인데 개인 claim_project 가 호출됨")

    monkeypatch.setattr(team_repository, "assert_team_role", fake_assert_role)
    monkeypatch.setattr("app.service.usage_repository.get_usage", fake_get_usage)
    monkeypatch.setattr(ownership_repository, "claim_team_project", fake_claim_team)
    monkeypatch.setattr(ownership_repository, "claim_project", boom_personal)

    pid = await ownership_repository.claim("bob@example.com", "proj-y", "team-1")
    assert pid == "pid-team"
    # 게이트(role + usage) 가 claim_team 보다 먼저 실행돼야 함.
    assert order == ["role", "usage", "claim_team"]


# ─── 위조 team_id 차단 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_team_non_member_blocked_403(monkeypatch):
    """비멤버가 team_id 위조 → assert_team_role 403 → claim_team_project 미호출."""

    async def fake_assert_role(email, team_id, min_role=team_repository.ROLE_MEMBER):
        raise HTTPException(status_code=403, detail="팀 멤버가 아닙니다.")

    async def boom_claim_team(email, team_id, project):
        raise AssertionError("비멤버인데 팀 프로젝트가 생성됨")

    monkeypatch.setattr(team_repository, "assert_team_role", fake_assert_role)
    monkeypatch.setattr(ownership_repository, "claim_team_project", boom_claim_team)

    with pytest.raises(HTTPException) as ei:
        await ownership_repository.claim("mallory@evil.com", "proj-z", "team-1")
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_claim_team_free_member_blocked_402(monkeypatch):
    """멤버지만 free 플랜 → 402 (무과금 악용 차단) → claim_team_project 미호출."""

    async def fake_assert_role(email, team_id, min_role=team_repository.ROLE_MEMBER):
        return team_repository.ROLE_MEMBER

    async def fake_get_usage(email):
        return _usage("free")

    async def boom_claim_team(email, team_id, project):
        raise AssertionError("free 멤버인데 팀 프로젝트가 생성됨")

    monkeypatch.setattr(team_repository, "assert_team_role", fake_assert_role)
    monkeypatch.setattr("app.service.usage_repository.get_usage", fake_get_usage)
    monkeypatch.setattr(ownership_repository, "claim_team_project", boom_claim_team)

    with pytest.raises(HTTPException) as ei:
        await ownership_repository.claim("poor@example.com", "proj-z", "team-1")
    assert ei.value.status_code == 402


# ─── assert_team_access 직접 검증 ─────────────────────────────


@pytest.mark.asyncio
async def test_assert_team_access_empty_team_id_400():
    with pytest.raises(HTTPException) as ei:
        await ownership_repository.assert_team_access("a@b.com", "")
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_assert_team_access_paid_member_ok(monkeypatch):
    async def fake_assert_role(email, team_id, min_role=team_repository.ROLE_MEMBER):
        return team_repository.ROLE_ADMIN

    async def fake_get_usage(email):
        return _usage("pro_plus")

    monkeypatch.setattr(team_repository, "assert_team_role", fake_assert_role)
    monkeypatch.setattr("app.service.usage_repository.get_usage", fake_get_usage)

    # 예외 없이 통과해야 함.
    await ownership_repository.assert_team_access("admin@example.com", "team-1")
