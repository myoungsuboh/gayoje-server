"""
ownership_repository 단위 테스트.

핵심 회귀 시나리오:
1. 신규 프로젝트 claim → 정상 등록 (rows 반환, project_id UUID 반환 — Phase 2A)
2. 본인이 이미 소유한 프로젝트 재-claim → 멱등 OK
3. 다른 유저 소유 프로젝트 claim → `ProjectOwnershipConflict` raise
4. assert_owns: 비-소유자 → 403 HTTPException
5. list_owned_projects: 최근 등록 순 반환 (Phase 2A — id 필드 포함)
6. record_ownership (deprecated alias): 충돌 시 silent (예외 안 던짐) — 하위 호환

Phase 2A 신규:
7. resolve_project_id: (email, name) → project_id (UUID) 또는 None
8. ensure_project_constraint: 4 단계 모두 호출 (name UNIQUE / id UNIQUE / 2 backfill)
9. claim_project: 신규 사용자가 받는 project_id 반환 (역호환)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.service import ownership_repository
from app.service.ownership_repository import ProjectOwnershipConflict


pytestmark = pytest.mark.asyncio


# ─── Fake neo4j_client.run_cypher ───────────────────────────────


class _FakeRunCypher:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(
        self,
        cypher: str,
        params: Optional[Dict[str, Any]] = None,
        database: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self.calls.append({"cypher": cypher, "params": params or {}, "database": database})
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses: Optional[List[List[Dict[str, Any]]]] = None) -> _FakeRunCypher:
        fake = _FakeRunCypher(responses=responses)
        monkeypatch.setattr(
            "app.service.ownership_repository.neo4j_client.run_cypher", fake
        )
        return fake

    return _setup


@pytest.fixture(autouse=True)
def _disable_quota_guard(monkeypatch):
    """
    ownership 단위 테스트는 quota 가드의 동작이 아닌 ownership 자체에 집중.
    `claim_project` 는 신규 생성 시 `quota.assert_projects_within_limit` 를 부르는데,
    여기서 noop 으로 격리해서 cypher 시퀀스가 단순 (is_owner + claim) 만 남도록 한다.
    가드 자체의 회귀 검증은 `tests/core/test_quota_projects.py` 에서 별도로 처리.
    """
    async def _noop(email: str) -> None:
        return None
    monkeypatch.setattr("app.core.quota.assert_projects_within_limit", _noop)


# ─── claim_project ──────────────────────────────────────────────


async def test_claim_project_new_or_self_owned_returns_row(fake_run):
    """신규 또는 본인 소유 → 정상 종료.

    [신규 흐름]
      1. is_owner cypher: 빈 응답 → 가드 호출 (autouse fixture 로 noop)
      2. claim cypher: 1 row 반환 → 정상 종료
    """
    fake = fake_run(
        responses=[
            [],                              # is_owner: 본인 소유 아님 → 가드 호출
            [{"project_name": "p1"}],        # claim: 정상 등록
        ]
    )
    await ownership_repository.claim_project("alice@example.com", "p1")
    assert len(fake.calls) == 2
    # claim cypher 의 params 가 올바른지 (2번째 호출)
    assert fake.calls[1]["params"] == {"email": "alice@example.com", "project": "p1"}


async def test_claim_project_other_owner_raises_conflict(fake_run):
    """
    Phase 2D: 다른 유저 bob의 동명 프로젝트가 있더라도, 글로벌 unique constraint가
    없으므로 claim_project는 충돌을 일으키지 않고 None(실패 케이스 시뮬레이션) 또는 ID를 반환.
    """
    fake = fake_run(
        responses=[
            [],                                       # is_owner: 소유주 아님
            [],                                       # claim: 실패 시뮬레이션 (0 rows)
        ]
    )
    res = await ownership_repository.claim_project("alice@example.com", "p1")
    assert res is None
    assert len(fake.calls) == 2


async def test_claim_project_conflict_without_peek_hint(fake_run):
    """Phase 2D: peek 힌트 조회 없이 claim 실패 시 None 반환."""
    fake = fake_run(responses=[[], []])  # is_owner + claim
    res = await ownership_repository.claim_project("alice@example.com", "p1")
    assert res is None


async def test_claim_project_empty_args_silent(fake_run):
    fake = fake_run()
    await ownership_repository.claim_project("", "p1")
    await ownership_repository.claim_project("a@b.com", "")
    assert len(fake.calls) == 0  # 빈 인자는 no-op


# ─── record_ownership (deprecated) ──────────────────────────────


async def test_record_ownership_silently_swallows_conflict(fake_run):
    """
    deprecated alias 는 충돌도 silent — 호출자 코드의 점진 마이그레이션 위함.
    예외가 밖으로 새지 않아야 함.
    """
    fake_run(responses=[[], []])  # claim 빈 + peek 빈 → conflict
    # 예외 없이 정상 반환되어야 함
    await ownership_repository.record_ownership("alice@example.com", "p1")


# ─── assert_owns ────────────────────────────────────────────────


async def test_assert_owns_owner_passes(fake_run):
    fake_run(responses=[[{"name": "p1"}]])
    await ownership_repository.assert_owns("alice@example.com", "p1")  # no raise


async def test_assert_owns_not_owner_raises_403(fake_run):
    fake_run(responses=[[]])
    with pytest.raises(HTTPException) as exc_info:
        await ownership_repository.assert_owns("alice@example.com", "p1")
    assert exc_info.value.status_code == 403


async def test_assert_owns_empty_project_raises_400(fake_run):
    fake_run()
    with pytest.raises(HTTPException) as exc_info:
        await ownership_repository.assert_owns("alice@example.com", "")
    assert exc_info.value.status_code == 400


# ─── list_owned_projects ────────────────────────────────────────


async def test_list_owned_projects_returns_rows(fake_run):
    fake_run(responses=[
        [
            {"name": "p1", "owned_at": "2025-01-01T00:00:00Z"},
            {"name": "p2", "owned_at": "2024-12-31T00:00:00Z"},
        ]
    ])
    out = await ownership_repository.list_owned_projects("alice@example.com")
    assert len(out) == 2
    assert out[0]["name"] == "p1"
    assert out[1]["owned_at"] == "2024-12-31T00:00:00Z"


async def test_list_owned_projects_empty_email(fake_run):
    fake = fake_run()
    out = await ownership_repository.list_owned_projects("")
    assert out == []
    assert len(fake.calls) == 0  # 호출도 안 함


# ─── count_user_projects (quota 가드용) ─────────────────────────


async def test_count_user_projects_returns_total(fake_run):
    """quota.assert_projects_within_limit 의 보조 함수 — 단순 count 반환."""
    fake_run(responses=[[{"total": 5}]])
    count = await ownership_repository.count_user_projects("alice@example.com")
    assert count == 5


async def test_count_user_projects_returns_zero_for_no_owners(fake_run):
    """OWNS 관계 없으면 0 반환 — None 회피."""
    fake_run(responses=[[{"total": 0}]])
    assert await ownership_repository.count_user_projects("alice@example.com") == 0


async def test_count_user_projects_empty_email_no_cypher(fake_run):
    """빈 인자는 cypher 호출 없이 0 반환 — 비용 절약."""
    fake = fake_run()
    assert await ownership_repository.count_user_projects("") == 0
    assert len(fake.calls) == 0


async def test_count_user_projects_handles_missing_response(fake_run):
    """비정상 빈 응답 → 0 (방어)."""
    fake_run(responses=[[]])
    assert await ownership_repository.count_user_projects("ghost@example.com") == 0


# ─── ensure_project_constraint ──────────────────────────────────


async def test_ensure_project_constraint_runs_all_phases(fake_run):
    """
    ensure_project_constraint 가 전 단계 cypher 실행.

    순서: name DROP → owner_name UNIQUE → id UNIQUE → id backfill →
          owner_email backfill → team_name UNIQUE (팀 프로젝트 격리).
    """
    fake = fake_run(
        responses=[
            [],                       # name DROP
            [],                       # owner_name UNIQUE
            [],                       # id UNIQUE
            [{"backfilled": 3}],      # id backfill
            [{"backfilled": 2}],      # owner_email backfill
            [],                       # team_name UNIQUE
        ]
    )
    await ownership_repository.ensure_project_constraint()
    assert len(fake.calls) == 6
    assert "project_name_unique" in fake.calls[0]["cypher"]
    assert "project_owner_name_unique" in fake.calls[1]["cypher"]
    assert "project_id_unique" in fake.calls[2]["cypher"]
    assert "p.id IS NULL" in fake.calls[3]["cypher"]
    assert "p.owner_email IS NULL" in fake.calls[4]["cypher"]
    assert "project_team_name_unique" in fake.calls[5]["cypher"]


async def test_ensure_project_constraint_continues_on_partial_failure(monkeypatch):
    """
    한 단계가 실패해도 나머지 단계가 그대로 진행 — 부팅을 막지 않음.

    각 단계 try/except 가 독립적이라 첫 단계 raise 후에도 다음 단계 시도.
    """

    class _FlakyFake:
        def __init__(self):
            self.calls = []

        async def __call__(self, cypher, params=None, database=None):
            self.calls.append(cypher)
            # 모든 단계가 실패해도 함수 자체는 raise 안 함
            raise RuntimeError("simulated neo4j down")

    flaky = _FlakyFake()
    monkeypatch.setattr(
        "app.service.ownership_repository.neo4j_client.run_cypher", flaky
    )
    # 예외 새지 않아야 함 (부팅 보호)
    await ownership_repository.ensure_project_constraint()
    # 전 단계 모두 시도됨 (team_name UNIQUE 포함 6단계)
    assert len(flaky.calls) == 6


# ─── Phase 2A: claim_project 가 project_id 반환 ─────────────────


async def test_claim_project_returns_project_id_on_success(fake_run):
    """Phase 2A: 새 cypher 가 RETURN project_id 추가 → claim_project 가 UUID 반환.

    cypher 호출 순서: is_owner (빈) + claim (project_id 반환).
    """
    fake_run(
        responses=[
            [],                                                       # is_owner: 빈
            [{"project_id": "uuid-1234", "project_name": "p1"}],      # claim
        ]
    )
    pid = await ownership_repository.claim_project("alice@example.com", "p1")
    assert pid == "uuid-1234"


async def test_claim_project_returns_none_on_empty_args(fake_run):
    """빈 인자는 best-effort skip — None 반환 (역호환)."""
    fake_run()
    assert await ownership_repository.claim_project("", "p1") is None
    assert await ownership_repository.claim_project("a@b.com", "") is None


# ─── Phase 2A: resolve_project_id ───────────────────────────────


async def test_resolve_project_id_returns_uuid_for_owned(fake_run):
    fake_run(responses=[[{"project_id": "uuid-1234"}]])
    pid = await ownership_repository.resolve_project_id("alice@example.com", "p1")
    assert pid == "uuid-1234"


async def test_resolve_project_id_returns_none_for_missing(fake_run):
    fake_run(responses=[[]])
    assert await ownership_repository.resolve_project_id("alice@example.com", "nope") is None


async def test_resolve_project_id_returns_none_for_other_user_project(fake_run):
    """다른 사용자 소유 프로젝트는 MATCH 자체가 실패 → 빈 결과."""
    fake_run(responses=[[]])
    pid = await ownership_repository.resolve_project_id("alice@example.com", "bobs")
    assert pid is None


async def test_resolve_project_id_empty_args_skip(fake_run):
    fake = fake_run()
    assert await ownership_repository.resolve_project_id("", "p1") is None
    assert await ownership_repository.resolve_project_id("a", "") is None
    assert fake.calls == []  # cypher 호출 없음


# ─── Phase 2A: list_owned_projects 응답에 id 포함 ──────────────


async def test_list_owned_projects_includes_project_id(fake_run):
    """Phase 2A: 응답에 id (project_id UUID) 추가."""
    fake_run(
        responses=[
            [
                {"id": "uuid-1", "name": "p1", "owned_at": "2025-01-01T00:00:00Z"},
                {"id": "uuid-2", "name": "p2", "owned_at": "2024-12-31T00:00:00Z"},
            ]
        ]
    )
    out = await ownership_repository.list_owned_projects("alice@example.com")
    assert len(out) == 2
    assert out[0]["id"] == "uuid-1"
    assert out[0]["name"] == "p1"
    assert out[1]["id"] == "uuid-2"


async def test_list_owned_projects_id_can_be_none_for_legacy(fake_run):
    """Phase 2A backfill 이전의 legacy 데이터 — id 가 None 으로 나올 수 있음 (방어)."""
    fake_run(responses=[[{"name": "legacy_p", "owned_at": "2024-01-01T00:00:00Z"}]])
    out = await ownership_repository.list_owned_projects("alice@example.com")
    assert len(out) == 1
    assert out[0]["id"] is None  # 누락 시 None
    assert out[0]["name"] == "legacy_p"


# ─── can_access — read 전용 게이트 (2026-06) ────────────────────
# assert_owns(403 raise) 와 달리 bool 반환. read 라우트가 비소유면 핸들러 미실행 +
# 200-empty 로 가도록 — pre-claim 403 노이즈 제거 + 동명 타 유저 데이터 노출(IDOR) 차단.


async def test_can_access_personal_owner_true(fake_run):
    """개인 프로젝트 소유자 → True."""
    fake_run(responses=[[{"name": "p1"}]])  # is_owner: row 있음
    assert await ownership_repository.can_access("alice@example.com", "p1") is True


async def test_can_access_personal_non_owner_false_no_raise(fake_run):
    """개인 프로젝트 비소유 → False (assert_owns 와 달리 403 raise 안 함).

    read 게이트의 핵심 — 비소유면 핸들러 미실행 + 200-empty 로 가야 하므로
    예외가 아닌 False 를 줘야 한다.
    """
    fake_run(responses=[[]])  # is_owner: row 없음
    result = await ownership_repository.can_access("bob@example.com", "p1")
    assert result is False  # HTTPException 안 던짐


async def test_can_access_empty_project_false(fake_run):
    fake = fake_run()
    assert await ownership_repository.can_access("alice@example.com", "") is False
    assert fake.calls == []  # 빈 project → cypher 호출 없음


async def test_can_access_team_non_member_false(fake_run):
    """팀 비멤버 → False (멤버십 cypher 0 행). get_usage 미도달."""
    fake_run(responses=[[]])  # _ASSERT_TEAM_ACCESS_CYPHER: 0 행
    assert await ownership_repository.can_access("bob@example.com", "p1", team_id="team-1") is False


async def test_can_access_team_member_free_plan_raises_402(fake_run, monkeypatch):
    """팀 멤버이나 무료 플랜 → 402 raise (read 라도 팀 유료 게이트 유지)."""
    fake_run(responses=[[{"name": "p1"}]])  # 멤버십 있음

    class _Usage:
        subscription_type = "free"

    async def _get_usage(email):
        return _Usage()

    monkeypatch.setattr("app.service.usage_repository.get_usage", _get_usage)
    monkeypatch.setattr("app.core.subscription.PAID_SUBSCRIPTIONS", {"pro", "team", "enterprise"})

    with pytest.raises(HTTPException) as ei:
        await ownership_repository.can_access("alice@example.com", "p1", team_id="team-1")
    assert ei.value.status_code == 402


async def test_can_access_team_member_paid_plan_true(fake_run, monkeypatch):
    """팀 멤버 + 유료 플랜 → True."""
    fake_run(responses=[[{"name": "p1"}]])

    class _Usage:
        subscription_type = "pro"

    async def _get_usage(email):
        return _Usage()

    monkeypatch.setattr("app.service.usage_repository.get_usage", _get_usage)
    monkeypatch.setattr("app.core.subscription.PAID_SUBSCRIPTIONS", {"pro", "team", "enterprise"})

    assert await ownership_repository.can_access("alice@example.com", "p1", team_id="team-1") is True
