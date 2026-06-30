"""
프로젝트 소유권(ownership) 관리 — User ↔ Project.

[설계 — Phase 2D 모델]
- Project 는 (owner_email, name) 조합으로 unique. 글로벌 name UNIQUE 제거.
- 소유 관계: `(:User {email})-[:OWNS {created_at}]->(:Project {name, owner_email})`.
- **유저별 격리 정책**: 같은 이름이라도 다른 유저면 독립 프로젝트.
  - `(owner_email, name)` composite UNIQUE 제약.

[설계 — Phase 2 멀티테넌시 마이그레이션]
SaaS 전환을 위해 `Project.name` 글로벌 UNIQUE 를 `(owner_email, name)` 합성
UNIQUE 로 전환 중. 모든 cypher 의 `WHERE n.project = $project_name` 패턴을
`WHERE n.project_id = $project_id` 로 옮기는 게 최종 목표.

Phase 2A (현재): **additive only** — Project 노드에 다음 추가
  - `id` (UUID, 글로벌 UNIQUE 제약) — 안정적 식별자
  - `owner_email` — OWNS 관계의 정보를 빠른 lookup 용으로 노드에 복제
새 헬퍼 `resolve_project_id(email, name) → project_id` 도입.
기존 `claim_project` / `assert_owns` 의 동작/시그니처는 그대로.

Phase 2B~2D 에서 모든 cypher 가 project_id 를 사용하도록 점진 마이그.
Phase 2D 완료: `Project.name` 글로벌 UNIQUE → `(owner_email, name)` composite UNIQUE.

[규약]
- 모든 mutation 진입 API (postMeeting / addProjectRepo / postSkill 의 CREATE 분기)
  는 진입 시 `claim_project(email, project)` 호출 →
    - 미존재 또는 본인 소유 → OK
    - 다른 유저 소유 → ProjectOwnershipConflict (409 매핑)
- 모든 read/access API 는 진입 시 `assert_owns(email, project)` 호출 → 403.
- 회원 탈퇴 시 Project 노드 자체는 남기지만 OWNS 관계만 삭제.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import HTTPException, status

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


class ProjectOwnershipConflict(Exception):
    """다른 유저가 이미 OWNS 중인 프로젝트 이름을 차지하려 함."""

    def __init__(self, project: str, current_owner_hint: Optional[str] = None) -> None:
        super().__init__(
            f"Project '{project}' is already owned by another user."
        )
        self.project = project
        self.current_owner_hint = current_owner_hint


# Phase 2D: 기존 글로벌 name UNIQUE 제약 DROP 후 (owner_email, name) composite 로 교체.
_DROP_OLD_NAME_CONSTRAINT_CYPHER = """\
DROP CONSTRAINT project_name_unique IF EXISTS
"""

_ENSURE_CONSTRAINT_CYPHER = """\
// Phase 2D: (owner_email, name) composite UNIQUE — 유저별 프로젝트 이름 격리.
CREATE CONSTRAINT project_owner_name_unique IF NOT EXISTS
FOR (p:Project) REQUIRE (p.owner_email, p.name) IS UNIQUE
"""


# Phase 2A 추가 — Project.id (UUID) 글로벌 UNIQUE 제약.
# project_id 가 안정적 식별자가 되므로 Phase 2D 에서 name UNIQUE 떼도 무방.
_ENSURE_PROJECT_ID_CONSTRAINT_CYPHER = """\
CREATE CONSTRAINT project_id_unique IF NOT EXISTS
FOR (p:Project) REQUIRE p.id IS UNIQUE
"""


# Phase 2A 마이그레이션 — 기존 Project 노드의 id / owner_email 백필.
# 모두 idempotent (IS NULL 체크) — 부팅마다 안전.
_BACKFILL_PROJECT_ID_CYPHER = """\
// id 누락된 기존 Project 에 UUID 부여
MATCH (p:Project)
WHERE p.id IS NULL
SET p.id = randomUUID()
RETURN count(p) AS backfilled
"""


_BACKFILL_PROJECT_OWNER_EMAIL_CYPHER = """\
// owner_email 누락된 Project 에 OWNS 관계의 owner email 복제
MATCH (u:User)-[:OWNS]->(p:Project)
WHERE p.owner_email IS NULL
SET p.owner_email = u.email
RETURN count(p) AS backfilled
"""


# Phase 2D: 유저별 프로젝트 이름 격리.
# 동일 이름이라도 다른 유저의 프로젝트와 독립적으로 생성 가능.
# (owner_email, name) composite UNIQUE 제약이 본인 중복만 방지.
_CLAIM_PROJECT_CYPHER = """\
MATCH (u:User {email: $email})
MERGE (p:Project {name: $project, owner_email: $email})
ON CREATE SET p.created_at = datetime(),
              p.id = randomUUID()
ON MATCH SET p.id = coalesce(p.id, randomUUID())
MERGE (u)-[r:OWNS]->(p)
ON CREATE SET r.created_at = datetime()
RETURN p.id AS project_id, p.name AS project_name
"""



_LIST_OWNED_PROJECTS_CYPHER = """\
MATCH (u:User {email: $email})-[r:OWNS]->(p:Project)
RETURN p.id AS id, p.name AS name, toString(r.created_at) AS owned_at
ORDER BY r.created_at DESC
"""


_COUNT_OWNED_PROJECTS_CYPHER = """\
MATCH (u:User {email: $email})-[:OWNS]->(p:Project)
RETURN count(p) AS total
"""


_ASSERT_OWNS_CYPHER = """\
MATCH (u:User {email: $email})-[:OWNS]->(p:Project {name: $project, owner_email: $email})
RETURN p.name AS name LIMIT 1
"""


# Phase 2A — (email, project_name) → project_id 해석. Phase 2B+ 의 모든 cypher 가
# project_id 로 격리 검색하도록 변환되는 동안 라우트 단에서 호출.
_RESOLVE_PROJECT_ID_CYPHER = """\
MATCH (u:User {email: $email})-[:OWNS]->(p:Project {name: $project, owner_email: $email})
RETURN p.id AS project_id
LIMIT 1
"""


_DELETE_OWNERSHIPS_FOR_USER_CYPHER = """\
// 회원 탈퇴 시: OWNS 관계만 끊고 Project 노드는 유지.
MATCH (u:User {email: $email})-[r:OWNS]->(:Project)
DELETE r
RETURN 1 AS removed
"""


_DELETE_PROJECT_OWNERSHIP_CYPHER = """\
# Phase 2D: 유저별 격리 — owner_email 조건 추가로 다른 유저 동명 프로젝트 보호.
MATCH (p:Project {name: $project, owner_email: $email})
DETACH DELETE p
"""


# ─── Team 프로젝트 — A2/A3/A4 ──────────────────────────────────

# (team_id, name) composite UNIQUE — 팀 내 프로젝트 이름 격리.
_ENSURE_TEAM_PROJECT_CONSTRAINT_CYPHER = """\
CREATE CONSTRAINT project_team_name_unique IF NOT EXISTS
FOR (p:Project) REQUIRE (p.team_id, p.name) IS UNIQUE
"""

_CLAIM_TEAM_PROJECT_CYPHER = """\
MATCH (u:User {email: $email})-[:MEMBER]->(t:Team {id: $team_id})
MERGE (p:Project {name: $project, team_id: $team_id})
ON CREATE SET p.created_at = datetime(),
              p.id = randomUUID(),
              p.team_id = $team_id,
              p.created_by = $email
ON MATCH SET p.id = coalesce(p.id, randomUUID())
MERGE (t)-[r:HAS_PROJECT]->(p)
ON CREATE SET r.created_at = datetime()
RETURN p.id AS project_id, p.name AS project_name
"""

_ASSERT_TEAM_ACCESS_CYPHER = """\
MATCH (u:User {email: $email})-[:MEMBER]->(t:Team {id: $team_id})-[:HAS_PROJECT]->(p:Project {name: $project, team_id: $team_id})
RETURN p.name AS name LIMIT 1
"""

_LIST_TEAM_PROJECTS_CYPHER = """\
MATCH (u:User {email: $email})-[:MEMBER]->(t:Team {id: $team_id})-[r:HAS_PROJECT]->(p:Project)
RETURN p.id AS id, p.name AS name, p.team_id AS team_id,
       toString(r.created_at) AS created_at
ORDER BY r.created_at DESC
"""

_DELETE_TEAM_PROJECT_CYPHER = """\
MATCH (t:Team {id: $team_id})-[:HAS_PROJECT]->(p:Project {name: $project, team_id: $team_id})
DETACH DELETE p
"""


# ─── App 부팅 헬퍼 ────────────────────────────────────────────


async def ensure_project_constraint() -> None:
    """
    Project.name UNIQUE 제약 + Phase 2A 멀티테넌시 마이그레이션 ensure.

    실패해도 부팅을 막지 않음 (Neo4j 미연결/일시 장애 대비).

    [Phase 2A 추가]
      1. Project.id (UUID) 글로벌 UNIQUE 제약 생성
      2. 기존 Project 노드의 id / owner_email backfill (idempotent)

    각 단계 독립 try/except — 한 단계 실패가 다른 단계를 막지 않음.
    """
    # Phase 2D: 기존 글로벌 name UNIQUE 제약 DROP
    try:
        await neo4j_client.run_cypher(_DROP_OLD_NAME_CONSTRAINT_CYPHER)
        logger.info("ownership: 기존 Project.name 글로벌 UNIQUE 제약 DROP 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("ownership: 기존 name UNIQUE DROP 실패 (이미 없거나 오류) (%s)", e)

    # Phase 2D: (owner_email, name) composite UNIQUE 제약
    try:
        await neo4j_client.run_cypher(_ENSURE_CONSTRAINT_CYPHER)
        logger.info("ownership: (owner_email, name) composite UNIQUE 제약 ensure 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("ownership: composite UNIQUE 제약 실패 (%s)", e)

    # Phase 2A — id UNIQUE 제약
    try:
        await neo4j_client.run_cypher(_ENSURE_PROJECT_ID_CONSTRAINT_CYPHER)
        logger.info("ownership: Project.id UNIQUE 제약 ensure 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("ownership: id UNIQUE 제약 실패 (%s)", e)

    # Phase 2A — backfill (legacy Project 노드에 id/owner_email 채움)
    try:
        rows = await neo4j_client.run_cypher(_BACKFILL_PROJECT_ID_CYPHER)
        n_id = int((rows[0] or {}).get("backfilled", 0)) if rows else 0
        if n_id > 0:
            logger.info("ownership: Project.id backfill — %d nodes", n_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("ownership: id backfill 실패 (%s)", e)

    try:
        rows = await neo4j_client.run_cypher(_BACKFILL_PROJECT_OWNER_EMAIL_CYPHER)
        n_oe = int((rows[0] or {}).get("backfilled", 0)) if rows else 0
        if n_oe > 0:
            logger.info("ownership: Project.owner_email backfill — %d nodes", n_oe)
    except Exception as e:  # noqa: BLE001
        logger.warning("ownership: owner_email backfill 실패 (%s)", e)

    # Team project — (team_id, name) composite UNIQUE
    try:
        await neo4j_client.run_cypher(_ENSURE_TEAM_PROJECT_CONSTRAINT_CYPHER)
        logger.info("ownership: (team_id, name) composite UNIQUE 제약 ensure 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("ownership: team project UNIQUE 제약 실패 (%s)", e)


# ─── 소유권 강제 ─────────────────────────────────────────────


async def claim_project(email: str, project: str) -> Optional[str]:
    """
    프로젝트 단일 소유 강제 claim. mutation 진입점에서 호출.

    - 미존재 → 신규 생성 + 본인 OWNS
    - 본인 소유 → no-op (멱등)
    - 다른 유저 소유 → `ProjectOwnershipConflict` raise

    Returns:
        project_id (UUID) on success. None 만 반환되는 경우는 빈 인자 best-effort
        skip 케이스. 기존 호출자는 반환값을 무시해도 됨 (역호환).

    호출자(router) 가 ProjectOwnershipConflict 를 잡아 409 로 매핑.

    [Quota 가드 — 2026-05]
    신규 생성일 때만 등급별 max_projects 한도 검사. 기존 본인 소유는 멱등이라
    가드 우회. quota.assert_projects_within_limit 가 402 raise.
    """
    if not email or not project:
        # 빈 인자는 best-effort 무시 (호출자 잘못)
        return None

    # [멀티테넌시 위조 차단] 예약 sentinel 포함 이름은 생성 거부 — 개인 이름으로 팀
    # 문서 스코프 키를 위조해 도달하는 것을 원천 차단 (claim 이 유일한 생성 게이트).
    from app.core.project_scope import assert_safe_project_name
    assert_safe_project_name(project)

    # 신규 생성 진입점만 한도 검사. 본인 소유 (멱등 호출)은 통과.
    # 지연 import — ownership_repository <-> quota 양방향 의존성 회피.
    if not await is_owner(email, project):
        from app.core import quota
        await quota.assert_projects_within_limit(email)

    rows = await neo4j_client.run_cypher(
        _CLAIM_PROJECT_CYPHER, {"email": email, "project": project}
    )
    if rows:
        return rows[0].get("project_id")

    # MERGE 실패 — User 노드 미존재 등 예외 케이스.
    logger.warning("claim_project: MERGE returned 0 rows (email=%s, project=%s)", email, project)
    return None


# ─── Phase 2A 신규: project_id 해석 ──────────────────────────


async def resolve_project_id(email: str, project: str) -> Optional[str]:
    """
    (email, project_name) → project_id (UUID).

    본인 소유 프로젝트만 해석. 미존재 또는 다른 사용자 소유 → None.
    `assert_owns` 와 동일한 격리 정책.

    Phase 2B~ 의 라우트 단에서: project_name 입력 받고 → 이 함수로 UUID 해석 →
    다운스트림 (pipeline / repository) 에 project_id 만 전달.

    Returns:
        project_id 문자열 or None. 호출자가 None 일 때 404 로 매핑하면 됨.
    """
    if not email or not project:
        return None
    rows = await neo4j_client.run_cypher(
        _RESOLVE_PROJECT_ID_CYPHER, {"email": email, "project": project}
    )
    if not rows:
        return None
    return rows[0].get("project_id")


async def record_ownership(email: str, project: str) -> None:
    """
    [DEPRECATED] 단일 소유 정책 도입 후 사용 금지. `claim_project` 사용 권장.

    하위 호환: claim 시도 후 충돌은 best-effort 무시 (로그만).
    호출자 코드 점진 마이그레이션을 위해 잔존.
    """
    try:
        await claim_project(email, project)
    except ProjectOwnershipConflict as e:
        logger.warning("record_ownership conflict (legacy call): %s", e)
    except Exception as e:  # noqa: BLE001
        logger.warning("record_ownership failed: %s", e)


async def count_user_projects(email: str) -> int:
    """현재 사용자가 OWNS 한 프로젝트 수. quota.assert_projects_within_limit 가 사용.

    Returns:
        프로젝트 개수. 사용자 노드 없거나 OWNS 관계 없으면 0.
    """
    if not email:
        return 0
    rows = await neo4j_client.run_cypher(
        _COUNT_OWNED_PROJECTS_CYPHER, {"email": email}
    )
    if not rows:
        return 0
    return int(rows[0].get("total") or 0)


async def list_owned_projects(email: str) -> List[dict]:
    """
    현재 유저가 OWNS 한 프로젝트 목록 (최근 등록 순).

    Phase 2A: 응답에 `id` (project_id UUID) 추가. 기존 키 `name`, `owned_at`
    그대로 유지 — FE 가 id 키를 추가로 받아도 무시 가능 (additive).
    """
    if not email:
        return []
    rows = await neo4j_client.run_cypher(_LIST_OWNED_PROJECTS_CYPHER, {"email": email})
    return [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "owned_at": r.get("owned_at"),
        }
        for r in rows
        if r.get("name")
    ]


async def is_owner(email: str, project: str) -> bool:
    """email 이 project 의 owner 인지 boolean."""
    if not email or not project:
        return False
    rows = await neo4j_client.run_cypher(
        _ASSERT_OWNS_CYPHER, {"email": email, "project": project}
    )
    return bool(rows)


async def assert_owns(email: str, project: str) -> None:
    """
    소유권 없으면 403 HTTPException raise. read/write 라우트가 진입 시 호출.

    분리 정책: 404 가 아닌 403 으로 보냄 — 존재 여부 자체도 leak 안 되도록
    (project 이름 enumeration 차단).
    """
    if not project:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project_name 이 필요합니다.",
        )
    if not await is_owner(email, project):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="해당 프로젝트에 대한 권한이 없습니다.",
        )


async def remove_user_ownerships(email: str) -> int:
    """회원 탈퇴 시 호출. 끊은 관계 수 반환 (Project 노드는 보존)."""
    if not email:
        return 0
    rows = await neo4j_client.run_cypher(
        _DELETE_OWNERSHIPS_FOR_USER_CYPHER, {"email": email}
    )
    return len(rows)


async def delete_project_node(email: str, project: str) -> None:
    """프로젝트 삭제 시 OWNS 관계 + Project 노드 제거. (Phase 2D: 유저 격리)"""
    if not project:
        return
    await neo4j_client.run_cypher(
        _DELETE_PROJECT_OWNERSHIP_CYPHER, {"email": email, "project": project}
    )


# ─── A3: 통합 접근 검증 (개인 + 팀) ────────────────────────────


async def assert_access(email: str, project: str, team_id: Optional[str] = None) -> None:
    """
    프로젝트 접근 권한 확인. read/write 라우트 진입 시 assert_owns 대신 사용.

    - team_id 없음 → 개인 소유 확인 (assert_owns 와 동일, 기존 호환).
    - team_id 있음 → 팀 멤버십 확인 + 유료 플랜 확인.

    403 또는 402 raise.
    """
    if not team_id:
        await assert_owns(email, project)
        return

    if not project:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project_name 이 필요합니다.",
        )

    rows = await neo4j_client.run_cypher(
        _ASSERT_TEAM_ACCESS_CYPHER, {"email": email, "project": project, "team_id": team_id}
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="해당 프로젝트에 대한 권한이 없습니다.",
        )

    # 유료 플랜 lazy check — 구독 만료 시 즉시 차단
    from app.service.usage_repository import get_usage
    from app.core.subscription import PAID_SUBSCRIPTIONS
    usage = await get_usage(email)
    sub = (usage.subscription_type if usage else "free")
    if sub not in PAID_SUBSCRIPTIONS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="팀 기능은 유료 플랜 (Pro 이상) 이 필요합니다.",
        )


async def can_access(email: str, project: str, team_id: Optional[str] = None) -> bool:
    """read 전용 접근 판정 — 403 을 raise 하지 않고 bool 반환.

    assert_access 와 동일한 관계 검사(개인 OWNS / 팀 멤버십)를 쓰되, 접근 불가를
    예외가 아닌 False 로 돌려준다. read 라우트가 비소유자(아직 claim 안 된 본인 신규
    프로젝트 포함)에게 403 대신 빈응답을 주도록 — pre-claim 403 콘솔 노이즈 제거용.

    [보안 — 중요] 도메인 read 쿼리는 전역 project 이름으로 조회하므로(테넌트 격리가
    이 게이트에 의존, security_tenant_isolation_gap 참고), 호출자는 can_access 가
    False 면 **핸들러를 실행하지 말고** 빈응답을 반환해야 한다. True 일 때만 실데이터
    핸들러를 태운다. (False 인데 핸들러를 태우면 동명 타 유저 데이터가 노출 = IDOR.)

    팀 유료 플랜(402) 게이트는 read 라도 유지 — 멤버십은 있으나 플랜 미달이면
    HTTPException(402) 를 그대로 raise. 개인 미소유 또는 팀 비멤버 → False.
    """
    if not project:
        return False
    if not team_id:
        return await is_owner(email, project)

    rows = await neo4j_client.run_cypher(
        _ASSERT_TEAM_ACCESS_CYPHER, {"email": email, "project": project, "team_id": team_id}
    )
    if not rows:
        return False

    # 멤버십 OK → 유료 플랜만 별도 강제 (read 라도 팀 게이트 우회 금지).
    from app.service.usage_repository import get_usage
    from app.core.subscription import PAID_SUBSCRIPTIONS
    usage = await get_usage(email)
    sub = (usage.subscription_type if usage else "free")
    if sub not in PAID_SUBSCRIPTIONS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="팀 기능은 유료 플랜 (Pro 이상) 이 필요합니다.",
        )
    return True


# ─── A4: 팀 프로젝트 생성/목록/삭제 ──────────────────────────


async def claim_team_project(email: str, team_id: str, project: str) -> Optional[str]:
    """
    팀 프로젝트 claim. 팀 멤버이면 누구나 생성 가능.
    (team_id, name) composite UNIQUE — 팀 내 중복 이름 방지.

    Returns: project_id (UUID)
    """
    if not email or not team_id or not project:
        return None

    # [멀티테넌시 위조 차단] 예약 sentinel 포함 이름 거부 (claim 게이트).
    from app.core.project_scope import assert_safe_project_name
    assert_safe_project_name(project)

    rows = await neo4j_client.run_cypher(
        _CLAIM_TEAM_PROJECT_CYPHER, {"email": email, "team_id": team_id, "project": project}
    )
    if not rows:
        logger.warning("claim_team_project: MERGE 실패 (email=%s team_id=%s project=%s)", email, team_id, project)
        return None
    return rows[0].get("project_id")


async def claim(email: str, project: str, team_id: Optional[str] = None) -> Optional[str]:
    """
    통합 claim 진입점 — mutation 라우트가 team_id 유무에 따라 분기.

    - team_id 없음 → 개인 프로젝트 claim (claim_project, 기존 동작 그대로).
    - team_id 있음 → 팀 멤버십 + 유료 플랜 확인 후 팀 프로젝트 claim.

    팀 분기는 claim_team_project 전에 assert_team_access 로 멤버십/플랜을
    강제하여, 비멤버가 team_id 만 위조해 팀 프로젝트를 만들 수 없게 한다.
    """
    if not team_id:
        return await claim_project(email, project)

    # 팀 멤버십 + 유료 플랜 게이트 (위조 team_id 차단).
    await assert_team_access(email, team_id)
    return await claim_team_project(email, team_id, project)


async def assert_team_access(email: str, team_id: str) -> None:
    """팀 멤버십 + 유료 플랜 확인. 비멤버 → 403, free 플랜 → 402."""
    if not team_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="team_id 가 필요합니다.",
        )
    from app.service import team_repository
    await team_repository.assert_team_role(email, team_id)
    # 유료 플랜 lazy check — 구독 만료 시 즉시 차단.
    from app.service.usage_repository import get_usage
    from app.core.subscription import PAID_SUBSCRIPTIONS
    usage = await get_usage(email)
    sub = (usage.subscription_type if usage else "free")
    if sub not in PAID_SUBSCRIPTIONS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="팀 기능은 유료 플랜 (Pro 이상) 이 필요합니다.",
        )


async def list_team_projects(email: str, team_id: str) -> List[dict]:
    """팀 프로젝트 목록. 팀 멤버만 조회 가능."""
    rows = await neo4j_client.run_cypher(
        _LIST_TEAM_PROJECTS_CYPHER, {"email": email, "team_id": team_id}
    )
    return [
        {"id": r.get("id"), "name": r.get("name"),
         "team_id": r.get("team_id"), "created_at": r.get("created_at")}
        for r in (rows or [])
        if r.get("name")
    ]


async def delete_team_project_node(email: str, team_id: str, project: str) -> None:
    """팀 프로젝트 삭제. admin 이상 권한 체크는 라우트 단에서."""
    if not project or not team_id:
        return
    await neo4j_client.run_cypher(
        _DELETE_TEAM_PROJECT_CYPHER, {"team_id": team_id, "project": project}
    )
