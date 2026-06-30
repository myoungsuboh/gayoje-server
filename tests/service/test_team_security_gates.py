"""
팀(Team) 기능 — 핵심 보안 게이트 회귀 테스트 (Phase E).

검증 대상:
- E1/E4: 기존 개인 사용자 회귀 — assert_access(team_id=None) → assert_owns 위임
- E2:     free 유저 초대 수락 → 402 (무과금 악용 차단)
- E3:     구독 만료 유저 팀 프로젝트 접근 → 402 (lazy check)
- E5:     유일 owner 탈퇴 → admin/member 자동 승격 (orphan 방지)

[전략]
neo4j_client.run_cypher 와 usage_repository.get_usage 를 monkeypatch 로 대체.
실제 Neo4j 없이 분기 로직만 결정적으로 검증.
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


# ─── E1/E4: 개인 사용자 회귀 ──────────────────────────────────


@pytest.mark.asyncio
async def test_assert_access_personal_delegates_to_assert_owns(monkeypatch):
    """team_id 없으면 assert_owns 와 동일 — 기존 개인 사용자 동작 보존."""
    called = {}

    async def fake_assert_owns(email, project):
        called["args"] = (email, project)

    monkeypatch.setattr(ownership_repository, "assert_owns", fake_assert_owns)

    await ownership_repository.assert_access("alice@example.com", "proj-x")
    assert called["args"] == ("alice@example.com", "proj-x")


@pytest.mark.asyncio
async def test_assert_access_personal_propagates_403(monkeypatch):
    """개인 소유 아님 → assert_owns 의 403 그대로 전파."""

    async def fake_assert_owns(email, project):
        raise HTTPException(status_code=403, detail="권한 없음")

    monkeypatch.setattr(ownership_repository, "assert_owns", fake_assert_owns)

    with pytest.raises(HTTPException) as ei:
        await ownership_repository.assert_access("bob@example.com", "proj-x")
    assert ei.value.status_code == 403


# ─── E3: 구독 만료 유저 팀 프로젝트 접근 ──────────────────────


@pytest.mark.asyncio
async def test_assert_access_team_paid_member_ok(monkeypatch):
    """팀 멤버 + 유료 플랜 → 통과."""
    async def fake_run(cypher, params=None):
        # 팀 접근 쿼리 → 1행 반환 (멤버십 존재)
        return [{"name": "proj-team"}]

    async def fake_get_usage(email):
        return _usage("pro")

    monkeypatch.setattr(ownership_repository.neo4j_client, "run_cypher", fake_run)
    monkeypatch.setattr(
        "app.service.usage_repository.get_usage", fake_get_usage
    )

    # 예외 없이 통과해야 함
    await ownership_repository.assert_access("user@example.com", "proj-team", team_id="t-1")


@pytest.mark.asyncio
async def test_assert_access_team_free_member_402(monkeypatch):
    """팀 멤버지만 free 플랜 (구독 만료 포함) → 402 lazy check."""
    async def fake_run(cypher, params=None):
        return [{"name": "proj-team"}]  # 멤버십은 존재

    async def fake_get_usage(email):
        return _usage("free")  # 만료되어 free 로 강등된 상태

    monkeypatch.setattr(ownership_repository.neo4j_client, "run_cypher", fake_run)
    monkeypatch.setattr(
        "app.service.usage_repository.get_usage", fake_get_usage
    )

    with pytest.raises(HTTPException) as ei:
        await ownership_repository.assert_access("user@example.com", "proj-team", team_id="t-1")
    assert ei.value.status_code == 402


@pytest.mark.asyncio
async def test_assert_access_team_non_member_403(monkeypatch):
    """팀 멤버가 아니면 → 403 (플랜 체크 이전에 차단)."""
    async def fake_run(cypher, params=None):
        return []  # 멤버십 없음

    monkeypatch.setattr(ownership_repository.neo4j_client, "run_cypher", fake_run)

    with pytest.raises(HTTPException) as ei:
        await ownership_repository.assert_access("stranger@example.com", "proj-team", team_id="t-1")
    assert ei.value.status_code == 403


# ─── E2: free 유저 초대 수락 → 402 ────────────────────────────


@pytest.mark.asyncio
async def test_accept_invite_free_user_402(monkeypatch):
    """free 유저가 초대 수락 시도 → 402. 멤버 등록 cypher 도달 전 차단."""
    invite = {
        "token": "tok-1",
        "team_id": "t-1",
        "team_name": "팀A",
        "invitee_email": "free@example.com",
        "inviter_email": "owner@example.com",
        "role": "member",
        "status": "pending",
        "expires_at": "2026-12-31T00:00:00Z",
    }

    async def fake_get_invite(token):
        return invite

    async def fake_get_usage(email):
        return _usage("free")

    monkeypatch.setattr(team_repository, "get_invite_by_token", fake_get_invite)
    monkeypatch.setattr(
        "app.service.usage_repository.get_usage", fake_get_usage
    )

    with pytest.raises(HTTPException) as ei:
        await team_repository.accept_invite("tok-1", "free@example.com")
    assert ei.value.status_code == 402


@pytest.mark.asyncio
async def test_accept_invite_wrong_email_403(monkeypatch):
    """초대받은 이메일과 다른 유저가 수락 시도 → 403."""
    invite = {
        "token": "tok-1", "team_id": "t-1", "team_name": "팀A",
        "invitee_email": "intended@example.com", "inviter_email": "owner@example.com",
        "role": "member", "status": "pending", "expires_at": "2026-12-31T00:00:00Z",
    }

    async def fake_get_invite(token):
        return invite

    monkeypatch.setattr(team_repository, "get_invite_by_token", fake_get_invite)

    with pytest.raises(HTTPException) as ei:
        await team_repository.accept_invite("tok-1", "attacker@example.com")
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_accept_invite_already_processed_410(monkeypatch):
    """이미 수락/취소된 초대 → 410."""
    invite = {
        "token": "tok-1", "team_id": "t-1", "team_name": "팀A",
        "invitee_email": "u@example.com", "inviter_email": "owner@example.com",
        "role": "member", "status": "accepted", "expires_at": "2026-12-31T00:00:00Z",
    }

    async def fake_get_invite(token):
        return invite

    monkeypatch.setattr(team_repository, "get_invite_by_token", fake_get_invite)

    with pytest.raises(HTTPException) as ei:
        await team_repository.accept_invite("tok-1", "u@example.com")
    assert ei.value.status_code == 410


@pytest.mark.asyncio
async def test_accept_invite_paid_user_success(monkeypatch):
    """유료 유저 정상 수락 → 팀 정보 반환."""
    invite = {
        "token": "tok-1", "team_id": "t-1", "team_name": "팀A",
        "invitee_email": "pro@example.com", "inviter_email": "owner@example.com",
        "role": "member", "status": "pending", "expires_at": "2026-12-31T00:00:00Z",
    }
    calls = []

    async def fake_get_invite(token):
        return invite

    async def fake_get_usage(email):
        return _usage("pro")

    async def fake_run(cypher, params=None):
        calls.append(cypher)
        # is_member 체크 → 빈 결과 (아직 멤버 아님), accept cypher → 결과 반환
        if "MEMBER" in cypher and "CREATE" in cypher:
            return [{"team_id": "t-1", "team_name": "팀A", "role": "member"}]
        return []  # is_member 등

    monkeypatch.setattr(team_repository, "get_invite_by_token", fake_get_invite)
    monkeypatch.setattr(team_repository.neo4j_client, "run_cypher", fake_run)
    monkeypatch.setattr(
        "app.service.usage_repository.get_usage", fake_get_usage
    )

    result = await team_repository.accept_invite("tok-1", "pro@example.com")
    assert result["team_id"] == "t-1"
    assert result["role"] == "member"


# ─── E5: 유일 owner 탈퇴 → 자동 승격 ──────────────────────────


@pytest.mark.asyncio
async def test_remove_sole_owner_promotes_admin(monkeypatch):
    """유일 owner 탈퇴 시 가장 오래된 admin 이 owner 로 자동 승격."""
    cypher_log = []

    async def fake_run(cypher, params=None):
        cypher_log.append(cypher)
        if "count(u) AS total" in cypher:
            return [{"total": 1}]  # owner 1명뿐
        if "role_admin" in str(params) or ("$role_admin" in cypher):
            return [{"promoted": "admin@example.com"}]  # admin 승격 성공
        return []

    # get_member_role 가 owner 반환하도록 (본인 탈퇴 시나리오)
    roles = {"owner@example.com": "owner"}

    async def fake_get_role(email, team_id):
        return roles.get(email)

    monkeypatch.setattr(team_repository.neo4j_client, "run_cypher", fake_run)
    monkeypatch.setattr(team_repository, "get_member_role", fake_get_role)

    # owner 본인 탈퇴
    await team_repository.remove_member("owner@example.com", "t-1", "owner@example.com")

    # 승격 cypher 가 호출됐는지 — admin 승격 쿼리 포함 확인
    promote_called = any("role_admin" in c.lower() or "set m.role" in c.lower() for c in cypher_log)
    assert promote_called, f"승격 cypher 미호출: {cypher_log}"


@pytest.mark.asyncio
async def test_remove_member_insufficient_permission_403(monkeypatch):
    """member 가 다른 멤버 제거 시도 → 403."""
    roles = {
        "member@example.com": "member",
        "other@example.com": "member",
    }

    async def fake_get_role(email, team_id):
        return roles.get(email)

    monkeypatch.setattr(team_repository, "get_member_role", fake_get_role)

    with pytest.raises(HTTPException) as ei:
        await team_repository.remove_member("member@example.com", "t-1", "other@example.com")
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_assert_team_role_insufficient_403(monkeypatch):
    """member 가 admin 권한 필요한 작업 시도 → 403."""
    async def fake_get_role(email, team_id):
        return "member"

    monkeypatch.setattr(team_repository, "get_member_role", fake_get_role)

    with pytest.raises(HTTPException) as ei:
        await team_repository.assert_team_role("u@example.com", "t-1", min_role=team_repository.ROLE_ADMIN)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_assert_team_role_owner_passes_admin_check(monkeypatch):
    """owner 는 admin 요구 작업 통과."""
    async def fake_get_role(email, team_id):
        return "owner"

    monkeypatch.setattr(team_repository, "get_member_role", fake_get_role)

    role = await team_repository.assert_team_role("u@example.com", "t-1", min_role=team_repository.ROLE_ADMIN)
    assert role == "owner"


# ─── 팀 생성 — 유료 플랜 게이트 ───────────────────────────────


@pytest.mark.asyncio
async def test_create_team_free_user_402(monkeypatch):
    """free 유저 팀 생성 시도 → 402."""
    async def fake_get_usage(email):
        return _usage("free")

    monkeypatch.setattr(
        "app.service.usage_repository.get_usage", fake_get_usage
    )

    with pytest.raises(HTTPException) as ei:
        await team_repository.create_team("free@example.com", "새 팀")
    assert ei.value.status_code == 402
