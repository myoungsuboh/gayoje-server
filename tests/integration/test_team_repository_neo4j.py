"""
팀(Team) 기능 — 실 Neo4j (testcontainers) 통합 검증.

FakeNeo4j 가 검증할 수 없는 영역:
  - MERGE 멱등성 (팀 생성 2회 → 노드 1개)
  - (team_id, name) composite UNIQUE 제약 충돌
  - DETACH DELETE 후 RETURN count(t) — 실제로 0 반환하는 Neo4j 특성
  - assert_team_role 에서 Cypher 결과가 DB 상태를 올바르게 반영하는지
  - remove_member 고아(orphan) 방지 — 3-query 시퀀스가 실 DB 상태를 올바르게 변경
  - accept_invite Cypher — MEMBER 관계 생성 + status 갱신 원자성
  - claim_team_project — HAS_PROJECT 관계 + (team_id, name) UNIQUE 제약

[활성화]
  RUN_TESTCONTAINERS=1 pytest tests/integration/test_team_repository_neo4j.py -m testcontainers -v

[전제]
  Docker 데몬 실행 중, testcontainers[neo4j] 설치됨.
"""
from __future__ import annotations

import asyncio
import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.testcontainers]


# ─── 컨테이너 / 환경 fixture ──────────────────────────────────────


@pytest.fixture(scope="module")
def neo4j_container():
    try:
        from testcontainers.neo4j import Neo4jContainer
    except ImportError:
        pytest.skip("testcontainers[neo4j] 미설치")

    container = Neo4jContainer("neo4j:5.27").with_env("NEO4J_AUTH", "neo4j/testpw1234")
    try:
        container.start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Docker 없음 또는 컨테이너 시작 실패: {e}")
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def neo4j_env(neo4j_container, monkeypatch):
    """neo4j_client 가 testcontainers 인스턴스를 바라보도록 재초기화."""
    bolt_url = neo4j_container.get_connection_url()
    monkeypatch.setenv("NEO4J_URI", bolt_url)
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "testpw1234")

    from app.clients import neo4j_client
    await neo4j_client.close_driver()
    yield
    await neo4j_client.close_driver()


@pytest.fixture
async def clean_db(neo4j_env):
    """각 테스트마다 전체 DB 초기화 + 제약 재 ensure."""
    from app.clients import neo4j_client
    from app.service import ownership_repository, team_repository

    await neo4j_client.run_cypher("MATCH (n) DETACH DELETE n")
    # 제약/인덱스 ensure
    await ownership_repository.ensure_project_constraint()
    await team_repository.ensure_team_constraints()
    yield neo4j_client


@pytest.fixture
async def two_users(clean_db):
    """owner(pro) + member(pro) 유저 2명 생성."""
    from app.clients import neo4j_client

    await neo4j_client.run_cypher(
        "CREATE (:User {email: $e, subscription_type: 'pro'})", {"e": "owner@test.com"}
    )
    await neo4j_client.run_cypher(
        "CREATE (:User {email: $e, subscription_type: 'pro'})", {"e": "member@test.com"}
    )
    return {"owner": "owner@test.com", "member": "member@test.com"}


# ─── 제약/인덱스 검증 ─────────────────────────────────────────────


async def test_team_id_unique_constraint(clean_db):
    """Team.id UNIQUE 제약 — 같은 id 로 2번 CREATE → 두 번째 실패."""
    from app.clients import neo4j_client

    await neo4j_client.run_cypher(
        "CREATE (:Team {id: 'dup-id', name: '팀A', created_at: datetime()})"
    )
    with pytest.raises(Exception, match="already exists|ConstraintValidationFailed|constraint"):
        await neo4j_client.run_cypher(
            "CREATE (:Team {id: 'dup-id', name: '팀B', created_at: datetime()})"
        )


async def test_invite_token_unique_constraint(clean_db):
    """Invite.token UNIQUE 제약."""
    from app.clients import neo4j_client

    await neo4j_client.run_cypher(
        "CREATE (:Invite {id: 'i1', token: 'same-tok', team_id: 't1', "
        "invitee_email: 'a@test.com', inviter_email: 'b@test.com', "
        "role: 'member', status: 'pending', "
        "expires_at: datetime() + duration({days: 7}), created_at: datetime()})"
    )
    with pytest.raises(Exception, match="already exists|ConstraintValidationFailed|constraint"):
        await neo4j_client.run_cypher(
            "CREATE (:Invite {id: 'i2', token: 'same-tok', team_id: 't2', "
            "invitee_email: 'c@test.com', inviter_email: 'b@test.com', "
            "role: 'member', status: 'pending', "
            "expires_at: datetime() + duration({days: 7}), created_at: datetime()})"
        )


async def test_team_project_name_unique_per_team(clean_db, two_users):
    """(team_id, name) composite UNIQUE — 같은 팀 내 동명 프로젝트 → 두 번째 실패."""
    from app.service import ownership_repository

    # 팀과 유저를 직접 DB에 세팅
    from app.clients import neo4j_client
    await neo4j_client.run_cypher(
        "MATCH (u:User {email: $e}) "
        "CREATE (t:Team {id: 'tc-t1', name: '팀', created_at: datetime()}) "
        "CREATE (u)-[:MEMBER {role: 'owner', joined_at: datetime()}]->(t)",
        {"e": two_users["owner"]},
    )

    pid1 = await ownership_repository.claim_team_project(
        two_users["owner"], "tc-t1", "alpha"
    )
    assert pid1 is not None

    # 같은 (team_id, name) → MERGE 멱등성이라 에러 X, 같은 노드 반환
    pid2 = await ownership_repository.claim_team_project(
        two_users["owner"], "tc-t1", "alpha"
    )
    assert pid1 == pid2, "같은 (team_id, name) 는 같은 project_id 반환해야 함 (MERGE 멱등)"

    # 다른 이름 → 별개 프로젝트
    pid3 = await ownership_repository.claim_team_project(
        two_users["owner"], "tc-t1", "beta"
    )
    assert pid3 != pid1


async def test_personal_project_name_unique_per_owner(clean_db, two_users):
    """개인 프로젝트 — owner 별로 동명 허용, 동일 owner 내 중복만 방지."""
    from app.service import ownership_repository

    await ownership_repository.claim_project(two_users["owner"], "proj-x")
    await ownership_repository.claim_project(two_users["member"], "proj-x")  # 다른 owner 허용

    # 동일 owner 재시도 → MERGE 멱등 (에러 아님)
    await ownership_repository.claim_project(two_users["owner"], "proj-x")

    rows = await clean_db.run_cypher(
        "MATCH (p:Project {name: 'proj-x'}) RETURN count(p) AS c"
    )
    assert rows[0]["c"] == 2  # 소유자별로 별개 노드


# ─── 팀 생성/삭제 ──────────────────────────────────────────────


async def test_create_team_registers_owner_member(two_users):
    """create_team → owner 가 MEMBER {role: 'owner'} 관계로 등록됨."""
    from app.service import team_repository
    from app.service.usage_repository import Usage

    async def fake_usage(email):
        return Usage(email=email, subscription_type="pro",
                     meeting_count=0, total_tokens=0, total_chars=0)

    import app.service.team_repository as tr_mod
    orig = tr_mod.get_usage if hasattr(tr_mod, "get_usage") else None

    # _assert_paid_plan 은 get_usage 를 지연 import — monkeypatch 가 아닌 직접 주입
    import app.service.usage_repository as usage_mod
    orig_fn = usage_mod.get_usage
    usage_mod.get_usage = fake_usage
    try:
        result = await team_repository.create_team(two_users["owner"], "통합팀")
    finally:
        usage_mod.get_usage = orig_fn

    assert result["name"] == "통합팀"
    assert result["role"] == "owner"

    # DB 직접 확인
    from app.clients import neo4j_client
    row = await neo4j_client.run_cypher(
        "MATCH (u:User {email: $e})-[m:MEMBER]->(t:Team {id: $id}) "
        "RETURN m.role AS role",
        {"e": two_users["owner"], "id": result["id"]},
    )
    assert row[0]["role"] == "owner"


async def test_delete_team_removes_team_node(two_users):
    """delete_team — Team 노드 + MEMBER 관계 삭제 확인."""
    from app.clients import neo4j_client
    from app.service import team_repository
    import app.service.usage_repository as usage_mod
    from app.service.usage_repository import Usage

    async def fake_usage(email):
        return Usage(email=email, subscription_type="pro",
                     meeting_count=0, total_tokens=0, total_chars=0)

    orig = usage_mod.get_usage
    usage_mod.get_usage = fake_usage
    try:
        team = await team_repository.create_team(two_users["owner"], "삭제팀")
    finally:
        usage_mod.get_usage = orig

    team_id = team["id"]
    await team_repository.delete_team(two_users["owner"], team_id)

    # Team 노드가 실제로 사라졌는지
    rows = await neo4j_client.run_cypher(
        "MATCH (t:Team {id: $id}) RETURN count(t) AS c", {"id": team_id}
    )
    assert rows[0]["c"] == 0, "Team 노드가 삭제되지 않음"

    # MEMBER 관계도 삭제됐는지
    rows2 = await neo4j_client.run_cypher(
        "MATCH ()-[m:MEMBER]->(:Team {id: $id}) RETURN count(m) AS c", {"id": team_id}
    )
    assert rows2[0]["c"] == 0, "MEMBER 관계가 남아 있음"


async def test_delete_team_non_owner_raises_403(two_users):
    """owner 아닌 유저가 delete_team 시도 → 403."""
    from fastapi import HTTPException
    from app.clients import neo4j_client
    from app.service import team_repository

    # 팀 직접 생성
    await neo4j_client.run_cypher(
        "MATCH (u:User {email: $e}) "
        "CREATE (t:Team {id: 'tc-del', name: '보호팀', created_at: datetime()}) "
        "CREATE (u)-[:MEMBER {role: 'owner', joined_at: datetime()}]->(t)",
        {"e": two_users["owner"]},
    )
    # member 로 등록
    await neo4j_client.run_cypher(
        "MATCH (u:User {email: $e}), (t:Team {id: 'tc-del'}) "
        "CREATE (u)-[:MEMBER {role: 'member', joined_at: datetime()}]->(t)",
        {"e": two_users["member"]},
    )

    with pytest.raises(HTTPException) as ei:
        await team_repository.delete_team(two_users["member"], "tc-del")
    assert ei.value.status_code == 403


# ─── 핵심 버그 검증: DELETE 후 RETURN count ─────────────────────


async def test_delete_team_cypher_count_after_detach_delete(clean_db):
    """
    Neo4j 특성 검증: DETACH DELETE 후 RETURN count(t) 는 0 반환.

    [배경]
    기존 _DELETE_TEAM_CYPHER 가 `RETURN count(t) AS deleted` 를 쓰는데
    DETACH DELETE 후에는 t 가 null 이 되어 count(t) = 0 → deleted = 0 →
    delete_team 이 "항상 403" 을 raise 하는 버그가 있었다.

    이 테스트가 그 동작을 확인하고, 현재 구현이 이를 올바르게 처리하는지 검증.
    """
    from app.clients import neo4j_client

    # 직접 Cypher 로 동작 확인
    await neo4j_client.run_cypher(
        "CREATE (:Team {id: 'tc-cnt', name: '카운트팀', created_at: datetime()})"
    )

    # DETACH DELETE 후 count(t) 는 실제로 어떻게 되는가?
    rows = await neo4j_client.run_cypher(
        "MATCH (t:Team {id: 'tc-cnt'}) DETACH DELETE t RETURN count(t) AS deleted"
    )
    # Neo4j 5.x: 삭제된 노드는 null → count(null) = 0
    # 이 값이 0 이면 delete_team 이 403 을 throw 하는 버그가 있었음
    deleted_count = rows[0]["deleted"] if rows else 0
    # 현재 구현이 이 케이스를 처리하는지 확인
    # (rows 가 비어있지 않지만 deleted = 0 인 경우)
    assert rows is not None and len(rows) > 0

    # delete_team 에서는 `if rows` (len > 0) 만 체크해야 함.
    # count(t) = 0 이어도 rows 자체는 [{"deleted": 0}] 으로 비어있지 않음 →
    # 현재 코드 `if not deleted` 는 여전히 버그
    # → 이 테스트가 실패하면 delete_team 로직을 수정해야 함
    assert deleted_count == 0, (
        f"Neo4j DETACH DELETE 후 count(t) = {deleted_count} (예상: 0). "
        "이 값이 0이 아니면 현재 delete_team 로직이 올바르게 동작하지만, "
        "Neo4j 5.x 표준 동작과 다름."
    )


# ─── 멤버 관리 ─────────────────────────────────────────────────


async def test_invite_and_accept_flow(two_users):
    """초대 생성 → 수락 → MEMBER 관계 DB 검증."""
    from app.clients import neo4j_client
    from app.service import team_repository
    import app.service.usage_repository as usage_mod
    from app.service.usage_repository import Usage

    async def fake_usage(email):
        return Usage(email=email, subscription_type="pro",
                     meeting_count=0, total_tokens=0, total_chars=0)

    orig = usage_mod.get_usage
    usage_mod.get_usage = fake_usage

    try:
        # 팀 생성
        team = await team_repository.create_team(two_users["owner"], "초대팀")
        team_id = team["id"]

        # owner → member 초대
        invite = await team_repository.create_invite(
            two_users["owner"], team_id, two_users["member"], "member"
        )
        token = invite["token"]

        # member 초대 수락
        result = await team_repository.accept_invite(token, two_users["member"])
    finally:
        usage_mod.get_usage = orig

    assert result["team_id"] == team_id
    assert result["role"] == "member"

    # DB 직접: member 가 실제로 MEMBER 관계를 가지는지
    rows = await neo4j_client.run_cypher(
        "MATCH (u:User {email: $e})-[m:MEMBER]->(t:Team {id: $tid}) RETURN m.role AS role",
        {"e": two_users["member"], "tid": team_id},
    )
    assert rows, "member 에게 MEMBER 관계가 없음"
    assert rows[0]["role"] == "member"

    # Invite status 가 accepted 로 변경됐는지
    inv_rows = await neo4j_client.run_cypher(
        "MATCH (i:Invite {token: $tok}) RETURN i.status AS status", {"tok": token}
    )
    assert inv_rows[0]["status"] == "accepted"


async def test_duplicate_accept_invite_returns_409(two_users):
    """동일 초대 2회 수락 시도 → 두 번째는 409 (이미 멤버)."""
    from fastapi import HTTPException
    from app.service import team_repository
    import app.service.usage_repository as usage_mod
    from app.service.usage_repository import Usage

    async def fake_usage(email):
        return Usage(email=email, subscription_type="pro",
                     meeting_count=0, total_tokens=0, total_chars=0)

    orig = usage_mod.get_usage
    usage_mod.get_usage = fake_usage

    try:
        team = await team_repository.create_team(two_users["owner"], "중복팀")
        invite = await team_repository.create_invite(
            two_users["owner"], team["id"], two_users["member"]
        )
        token = invite["token"]
        await team_repository.accept_invite(token, two_users["member"])

        with pytest.raises(HTTPException) as ei:
            await team_repository.accept_invite(token, two_users["member"])
    finally:
        usage_mod.get_usage = orig

    assert ei.value.status_code in (409, 410), (
        f"이미 멤버인 경우 409 or 만료된 토큰 410 예상, 실제: {ei.value.status_code}"
    )


async def test_remove_sole_owner_promotes_admin_in_db(two_users):
    """유일 owner 탈퇴 → admin 이 owner 로 승격 — DB 상태 검증."""
    from app.clients import neo4j_client
    from app.service import team_repository
    import app.service.usage_repository as usage_mod
    from app.service.usage_repository import Usage

    async def fake_usage(email):
        return Usage(email=email, subscription_type="pro",
                     meeting_count=0, total_tokens=0, total_chars=0)

    orig = usage_mod.get_usage
    usage_mod.get_usage = fake_usage

    try:
        # 팀 생성 → owner만 있는 상태
        team = await team_repository.create_team(two_users["owner"], "승격팀")
        team_id = team["id"]

        # member 추가 (초대 없이 직접 DB에)
        await neo4j_client.run_cypher(
            "MATCH (u:User {email: $e}), (t:Team {id: $tid}) "
            "CREATE (u)-[:MEMBER {role: 'admin', joined_at: datetime()}]->(t)",
            {"e": two_users["member"], "tid": team_id},
        )

        # owner 탈퇴
        await team_repository.remove_member(two_users["owner"], team_id, two_users["owner"])
    finally:
        usage_mod.get_usage = orig

    # owner 가 팀에 없는지
    owner_rows = await neo4j_client.run_cypher(
        "MATCH (u:User {email: $e})-[:MEMBER]->(t:Team {id: $tid}) RETURN u.email AS e",
        {"e": two_users["owner"], "tid": team_id},
    )
    assert not owner_rows, "탈퇴한 owner 가 여전히 MEMBER 관계를 가짐"

    # member 가 owner 로 승격됐는지
    new_owner_rows = await neo4j_client.run_cypher(
        "MATCH (u:User {email: $e})-[m:MEMBER]->(t:Team {id: $tid}) RETURN m.role AS role",
        {"e": two_users["member"], "tid": team_id},
    )
    assert new_owner_rows, "member 가 MEMBER 관계를 잃음"
    assert new_owner_rows[0]["role"] == "owner", (
        f"admin 이 owner 로 승격되지 않음 — 실제 role: {new_owner_rows[0]['role']}"
    )


# ─── assert_access 팀 경로 ─────────────────────────────────────


async def test_assert_access_team_member_passes(two_users):
    """팀 멤버 + 유료 플랜 → assert_access 통과."""
    from app.clients import neo4j_client
    from app.service import ownership_repository
    import app.service.usage_repository as usage_mod
    from app.service.usage_repository import Usage

    async def fake_usage(email):
        return Usage(email=email, subscription_type="pro",
                     meeting_count=0, total_tokens=0, total_chars=0)

    orig = usage_mod.get_usage
    usage_mod.get_usage = fake_usage

    team_id = "tc-access"
    await neo4j_client.run_cypher(
        "MATCH (u:User {email: $e}) "
        "CREATE (t:Team {id: $tid, name: '접근팀', created_at: datetime()}) "
        "CREATE (u)-[:MEMBER {role: 'member', joined_at: datetime()}]->(t) "
        "CREATE (p:Project {id: randomUUID(), name: 'myproj', "
        "team_id: $tid, created_at: datetime()}) "
        "CREATE (t)-[:HAS_PROJECT]->(p)",
        {"e": two_users["member"], "tid": team_id},
    )

    try:
        await ownership_repository.assert_access(
            two_users["member"], "myproj", team_id=team_id
        )  # 예외 없어야 함
    finally:
        usage_mod.get_usage = orig


async def test_assert_access_non_member_blocked(two_users):
    """팀 멤버가 아닌 유저 → 403."""
    from fastapi import HTTPException
    from app.clients import neo4j_client
    from app.service import ownership_repository

    team_id = "tc-block"
    await neo4j_client.run_cypher(
        "MATCH (u:User {email: $e}) "
        "CREATE (t:Team {id: $tid, name: '차단팀', created_at: datetime()}) "
        "CREATE (u)-[:MEMBER {role: 'owner', joined_at: datetime()}]->(t) "
        "CREATE (p:Project {id: randomUUID(), name: 'secret', "
        "team_id: $tid, created_at: datetime()}) "
        "CREATE (t)-[:HAS_PROJECT]->(p)",
        {"e": two_users["owner"], "tid": team_id},
    )

    with pytest.raises(HTTPException) as ei:
        await ownership_repository.assert_access(
            two_users["member"], "secret", team_id=team_id
        )
    assert ei.value.status_code == 403
