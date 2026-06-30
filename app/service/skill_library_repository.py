"""
Skill Library Repository — 유저 단위 스킬 보관함 (프로젝트와 독립).

[배경]
기존 `(Skill {project, id, ...})` 는 프로젝트 단위라 재사용 불가.
사용자가 잘 만든 스킬을 다른 프로젝트에 다시 입력해야 하는 불편 해소를 위해
**유저 라이브러리** 도입.

[데이터 모델]
    (User {email})
      -[:OWNS_SKILL_FOLDER]->
        (SkillFolder {
          id (UUID), name, description, color, category,
          owner_email, created_at, updated_at
        })
        -[:CONTAINS]->
          (LibrarySkill {
            id (UUID),
            name, scope, priority, trigger_condition,
            instructions[], tags[],
            owner_email, created_at, updated_at
          })

[설계 결정]
- 폴더는 사용자별. 다른 사용자 라이브러리 접근 차단 (owner_email + relationship 양쪽 검증).
- 모든 LibrarySkill 은 폴더 안에 — "미분류" 폴더가 기본 자동 생성됨.
- 이름은 한글/영문/숫자/공백/'-'/'_' 만. `app.core.name_validation` 가 검증.
- 한도: `app.core.quota` 의 `library_skills` (Free 100 / Pro 1000) — 라우트가 확인.

[기존 Skill 노드와 관계]
LibrarySkill ↔ Skill 은 별개 노드. import/export 라우트가 복사만 함. ArchService
의 GOVERNED_BY 관계는 import 후 Skill 노드에서 자동 재계산.

[멱등성]
- 폴더 / 스킬 id 는 backend 가 randomUUID 부여. 클라이언트는 id 보낼 필요 없음.
- 같은 이름 폴더 여러 개 허용 (사용자 자유). 검증은 사용자 책임.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# ===== 도메인 모델 =====


@dataclass(frozen=True)
class SkillFolderRow:
    id: str
    name: str
    description: str
    color: str
    category: str
    owner_email: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class LibrarySkillRow:
    id: str
    name: str
    scope: str
    priority: str
    trigger_condition: str
    instructions: List[str]
    tags: List[str]
    folder_id: Optional[str]  # 어느 폴더에 들어있는지 (None 은 없음 — 보통 발생 안 함)
    owner_email: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ===== Cypher =====


_ENSURE_FOLDER_CONSTRAINT_CYPHER = """\
// SkillFolder.id UNIQUE — id 는 backend 가 randomUUID 로 부여하므로 충돌 거의 없지만 안전.
CREATE CONSTRAINT skill_folder_id_unique IF NOT EXISTS
FOR (f:SkillFolder) REQUIRE f.id IS UNIQUE
"""

_ENSURE_LIBRARY_SKILL_CONSTRAINT_CYPHER = """\
CREATE CONSTRAINT library_skill_id_unique IF NOT EXISTS
FOR (s:LibrarySkill) REQUIRE s.id IS UNIQUE
"""

_ENSURE_FOLDER_OWNER_INDEX_CYPHER = """\
// 사용자별 라이브러리 조회 성능.
CREATE INDEX skill_folder_owner_email IF NOT EXISTS
FOR (f:SkillFolder) ON (f.owner_email)
"""

_ENSURE_LIBRARY_SKILL_OWNER_INDEX_CYPHER = """\
CREATE INDEX library_skill_owner_email IF NOT EXISTS
FOR (s:LibrarySkill) ON (s.owner_email)
"""


# ─── 폴더 ──────────────────────────────────────────


_CREATE_FOLDER_CYPHER = """\
// 사용자가 존재할 때만 폴더 생성. 사용자 없으면 빈 결과 → 라우트 404.
MATCH (u:User {email: $owner_email})
CREATE (f:SkillFolder {
    id: randomUUID(),
    name: $name,
    description: $description,
    color: $color,
    category: $category,
    owner_email: $owner_email,
    created_at: datetime(),
    updated_at: datetime()
})
MERGE (u)-[:OWNS_SKILL_FOLDER]->(f)
RETURN {
    id: f.id,
    name: f.name,
    description: COALESCE(f.description, ''),
    color: COALESCE(f.color, ''),
    category: COALESCE(f.category, ''),
    owner_email: f.owner_email,
    created_at: toString(f.created_at),
    updated_at: toString(f.updated_at)
} AS folder
"""


_UPDATE_FOLDER_CYPHER = """\
// 폴더 메타 수정. owner_email 매칭으로 다른 사용자 폴더 수정 방지.
// 입력 필드 중 None / '' 이 아닌 것만 갱신 — partial update.
MATCH (f:SkillFolder {id: $id, owner_email: $owner_email})
SET f.name = CASE WHEN $name IS NOT NULL AND $name <> '' THEN $name ELSE f.name END,
    f.description = CASE WHEN $description IS NOT NULL THEN $description ELSE COALESCE(f.description, '') END,
    f.color = CASE WHEN $color IS NOT NULL THEN $color ELSE COALESCE(f.color, '') END,
    f.category = CASE WHEN $category IS NOT NULL THEN $category ELSE COALESCE(f.category, '') END,
    f.updated_at = datetime()
RETURN {
    id: f.id,
    name: f.name,
    description: COALESCE(f.description, ''),
    color: COALESCE(f.color, ''),
    category: COALESCE(f.category, ''),
    owner_email: f.owner_email,
    created_at: toString(f.created_at),
    updated_at: toString(f.updated_at)
} AS folder
"""


_DELETE_FOLDER_CASCADE_CYPHER = """\
// cascade=true: 폴더 + 안 스킬 모두 삭제.
MATCH (f:SkillFolder {id: $id, owner_email: $owner_email})
OPTIONAL MATCH (f)-[:CONTAINS]->(s:LibrarySkill)
WITH f, collect(s) AS skills, count(s) AS deleted_skill_count
FOREACH (sk IN skills | DETACH DELETE sk)
DETACH DELETE f
RETURN deleted_skill_count
"""


_DELETE_FOLDER_MOVE_TO_UNFILED_CYPHER = """\
// cascade=false: 폴더만 삭제하고 안 스킬은 "미분류" 폴더로 이동.
// "미분류" 폴더는 is_system=true 플래그로 표시 — 사용자가 같은 이름 폴더 만들어도
// 별개 노드 (사용자 폴더는 is_system 없거나 false).
//
// [BUG FIX 2026-05] 자기-자신 삭제 방어:
//   사용자가 시스템 '미분류' 폴더(is_system=true)를 cascade=false 로 삭제 시도 시,
//   기존 cypher 는 MERGE 가 동일 노드 매칭 → 안 스킬을 같은 폴더에 재연결 후 DETACH
//   DELETE → 스킬도 함께 사라짐. (f.id = unfiled.id 일 때 안전 분기 필요.)
MATCH (u:User {email: $owner_email})
MATCH (f:SkillFolder {id: $id, owner_email: $owner_email})

// 시스템 미분류 폴더 ensure — is_system:true 로 사용자 동명 폴더와 분리.
MERGE (unfiled:SkillFolder {owner_email: $owner_email, is_system: true})
ON CREATE SET
    unfiled.id = randomUUID(),
    unfiled.name = '미분류',
    unfiled.description = '폴더가 삭제되어 자동 이동된 스킬',
    unfiled.color = '#6b7280',
    unfiled.category = '',
    unfiled.created_at = datetime(),
    unfiled.updated_at = datetime()
MERGE (u)-[:OWNS_SKILL_FOLDER]->(unfiled)

// 자기-자신 케이스: 삭제 대상 f 가 시스템 미분류 폴더면 cascade 모드로 전환 (안 스킬도 삭제).
WITH f, unfiled, (f.id = unfiled.id) AS is_self_target

// 안 스킬 수집 (자기-자신이 아닐 때만 옮길 대상)
OPTIONAL MATCH (f)-[old:CONTAINS]->(s:LibrarySkill)
WITH f, unfiled, is_self_target, collect(s) AS skills, collect(old) AS old_rels

// 자기-자신이면 안 스킬 함께 삭제. 아니면 unfiled 로 이동.
FOREACH (sk IN CASE WHEN is_self_target THEN skills ELSE [] END | DETACH DELETE sk)
FOREACH (r IN CASE WHEN is_self_target THEN [] ELSE old_rels END | DELETE r)
FOREACH (sk IN CASE WHEN is_self_target THEN [] ELSE skills END | MERGE (unfiled)-[:CONTAINS]->(sk))

WITH f, unfiled, is_self_target, size(skills) AS affected_count
DETACH DELETE f
RETURN
    affected_count AS moved_skill_count,
    unfiled.id AS unfiled_folder_id,
    is_self_target
"""


# ─── 스킬 ──────────────────────────────────────────


_CREATE_SKILL_CYPHER = """\
// 폴더 안에 스킬 생성. 폴더 없으면 빈 결과 → 라우트 404.
MATCH (f:SkillFolder {id: $folder_id, owner_email: $owner_email})
CREATE (s:LibrarySkill {
    id: randomUUID(),
    name: $name,
    scope: $scope,
    priority: $priority,
    trigger_condition: $trigger_condition,
    instructions: $instructions,
    tags: $tags,
    owner_email: $owner_email,
    created_at: datetime(),
    updated_at: datetime()
})
MERGE (f)-[:CONTAINS]->(s)
RETURN {
    id: s.id,
    name: s.name,
    scope: COALESCE(s.scope, ''),
    priority: COALESCE(s.priority, 'Medium'),
    trigger_condition: COALESCE(s.trigger_condition, ''),
    instructions: COALESCE(s.instructions, []),
    tags: COALESCE(s.tags, []),
    folder_id: f.id,
    owner_email: s.owner_email,
    created_at: toString(s.created_at),
    updated_at: toString(s.updated_at)
} AS skill
"""


_UPDATE_SKILL_CYPHER = """\
// 스킬 메타 수정 + 선택적으로 folder 이동.
// folder_id 가 비어있지 않으면 기존 [:CONTAINS] 끊고 새 폴더에 연결.
// folder_id None / '' 이면 폴더 그대로 유지.
//
// [BUG FIX 2026-05] 이전 버전은 subquery 패턴(CALL+RETURN)이었는데 안의 WHERE 가
// 0 row 반환 시 outer query 도 0 row 되어 메타만 수정한 경우에도 함수가 None 반환 →
// 라우트 404. OPTIONAL MATCH + FOREACH 패턴으로 변경:
//   - newF 매칭 (folder_id 비어있거나 newF 없으면 null)
//   - newF 가 not null 일 때만 옛 [:CONTAINS] 끊고 새 폴더에 연결
//   - outer row 보존 (0 row 안 됨)
MATCH (s:LibrarySkill {id: $id, owner_email: $owner_email})
SET s.name = CASE WHEN $name IS NOT NULL AND $name <> '' THEN $name ELSE s.name END,
    s.scope = CASE WHEN $scope IS NOT NULL THEN $scope ELSE COALESCE(s.scope, '') END,
    s.priority = CASE WHEN $priority IS NOT NULL AND $priority <> '' THEN $priority ELSE COALESCE(s.priority, 'Medium') END,
    s.trigger_condition = CASE WHEN $trigger_condition IS NOT NULL THEN $trigger_condition ELSE COALESCE(s.trigger_condition, '') END,
    s.instructions = CASE WHEN $instructions IS NOT NULL THEN $instructions ELSE COALESCE(s.instructions, []) END,
    s.tags = CASE WHEN $tags IS NOT NULL THEN $tags ELSE COALESCE(s.tags, []) END,
    s.updated_at = datetime()

// 새 폴더 매칭 (folder_id 빈 값이거나 매칭 안 되면 null — 이 경우 폴더 이동 안 함).
WITH s, $folder_id AS new_folder_id
OPTIONAL MATCH (newF:SkillFolder {owner_email: s.owner_email})
    WHERE new_folder_id IS NOT NULL
      AND new_folder_id <> ''
      AND newF.id = new_folder_id

// 옛 [:CONTAINS] 관계는 newF 가 매칭됐을 때만 수집·삭제 (안 매칭 시 빈 리스트 유지).
WITH s, newF
OPTIONAL MATCH (oldF:SkillFolder)-[oldRel:CONTAINS]->(s)
    WHERE newF IS NOT NULL
WITH s, newF, collect(oldRel) AS old_rels
FOREACH (r IN old_rels | DELETE r)
FOREACH (_ IN CASE WHEN newF IS NOT NULL THEN [1] ELSE [] END |
    MERGE (newF)-[:CONTAINS]->(s)
)

// 최종 폴더 (메타 수정 + 이동 후) 확인 — RETURN 에 folder_id 포함.
WITH s
OPTIONAL MATCH (f:SkillFolder)-[:CONTAINS]->(s)
RETURN {
    id: s.id,
    name: s.name,
    scope: COALESCE(s.scope, ''),
    priority: COALESCE(s.priority, 'Medium'),
    trigger_condition: COALESCE(s.trigger_condition, ''),
    instructions: COALESCE(s.instructions, []),
    tags: COALESCE(s.tags, []),
    folder_id: f.id,
    owner_email: s.owner_email,
    created_at: toString(s.created_at),
    updated_at: toString(s.updated_at)
} AS skill
"""


_DELETE_SKILL_CYPHER = """\
MATCH (s:LibrarySkill {id: $id, owner_email: $owner_email})
WITH s.id AS deleted_id, s
DETACH DELETE s
RETURN deleted_id
"""


# ─── 조회 ──────────────────────────────────────────


_LIST_LIBRARY_CYPHER = """\
// 사용자 라이브러리 전체. 폴더 + 안 스킬 한 번에 반환.
MATCH (u:User {email: $owner_email})-[:OWNS_SKILL_FOLDER]->(f:SkillFolder)
OPTIONAL MATCH (f)-[:CONTAINS]->(s:LibrarySkill)
WITH f, collect(
    CASE WHEN s IS NULL THEN NULL ELSE {
        id: s.id,
        name: s.name,
        scope: COALESCE(s.scope, ''),
        priority: COALESCE(s.priority, 'Medium'),
        trigger_condition: COALESCE(s.trigger_condition, ''),
        instructions: COALESCE(s.instructions, []),
        tags: COALESCE(s.tags, []),
        folder_id: f.id,
        owner_email: s.owner_email,
        created_at: toString(s.created_at),
        updated_at: toString(s.updated_at)
    } END
) AS skills_raw
WITH f, [sk IN skills_raw WHERE sk IS NOT NULL] AS skills
RETURN {
    folder: {
        id: f.id,
        name: f.name,
        description: COALESCE(f.description, ''),
        color: COALESCE(f.color, ''),
        category: COALESCE(f.category, ''),
        owner_email: f.owner_email,
        created_at: toString(f.created_at),
        updated_at: toString(f.updated_at)
    },
    skills: skills
} AS entry
ORDER BY f.name ASC
"""


_COUNT_LIBRARY_SKILLS_CYPHER = """\
// 사용자의 라이브러리 스킬 총 개수 — quota 검증에 사용.
MATCH (s:LibrarySkill {owner_email: $owner_email})
RETURN count(s) AS total
"""


# ===== Helpers =====


def _first(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return records[0] if records else None


def _row_to_folder(d: Dict[str, Any]) -> Optional[SkillFolderRow]:
    if not d or not d.get("id"):
        return None
    return SkillFolderRow(
        id=d["id"],
        name=d.get("name") or "",
        description=d.get("description") or "",
        color=d.get("color") or "",
        category=d.get("category") or "",
        owner_email=d.get("owner_email") or "",
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


def _row_to_skill(d: Dict[str, Any]) -> Optional[LibrarySkillRow]:
    if not d or not d.get("id"):
        return None
    return LibrarySkillRow(
        id=d["id"],
        name=d.get("name") or "",
        scope=d.get("scope") or "",
        priority=d.get("priority") or "Medium",
        trigger_condition=d.get("trigger_condition") or "",
        instructions=list(d.get("instructions") or []),
        tags=list(d.get("tags") or []),
        folder_id=d.get("folder_id"),
        owner_email=d.get("owner_email") or "",
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


# ===== 초기화 =====


async def ensure_constraints() -> None:
    """앱 부팅 시 1회 호출. SkillFolder/LibrarySkill 의 UNIQUE 제약 + 인덱스 ensure.

    실패해도 부팅 막지 않음 (Neo4j 미연결 환경 호환).
    """
    statements = [
        _ENSURE_FOLDER_CONSTRAINT_CYPHER,
        _ENSURE_LIBRARY_SKILL_CONSTRAINT_CYPHER,
        _ENSURE_FOLDER_OWNER_INDEX_CYPHER,
        _ENSURE_LIBRARY_SKILL_OWNER_INDEX_CYPHER,
    ]
    for stmt in statements:
        try:
            await neo4j_client.run_cypher(stmt)
        except Exception as e:  # noqa: BLE001 — 부팅 가드
            logger.warning("skill_library: ensure_constraints 실패 (skip): %s", e)


# ===== 폴더 CRUD =====


async def create_folder(
    *,
    owner_email: str,
    name: str,
    description: str = "",
    color: str = "",
    category: str = "",
) -> Optional[SkillFolderRow]:
    """폴더 생성. 사용자 없으면 None (라우트가 404 매핑)."""
    records = await neo4j_client.run_cypher(
        _CREATE_FOLDER_CYPHER,
        {
            "owner_email": owner_email,
            "name": name,
            "description": description,
            "color": color,
            "category": category,
        },
    )
    row = _first(records)
    return _row_to_folder((row or {}).get("folder") or {})


async def update_folder(
    *,
    owner_email: str,
    folder_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    color: Optional[str] = None,
    category: Optional[str] = None,
) -> Optional[SkillFolderRow]:
    """폴더 메타 수정. 본인 소유 + 폴더 존재할 때만 적용. 그 외 None."""
    records = await neo4j_client.run_cypher(
        _UPDATE_FOLDER_CYPHER,
        {
            "owner_email": owner_email,
            "id": folder_id,
            "name": name,
            "description": description,
            "color": color,
            "category": category,
        },
    )
    row = _first(records)
    return _row_to_folder((row or {}).get("folder") or {})


async def delete_folder(
    *, owner_email: str, folder_id: str, cascade: bool
) -> Dict[str, Any]:
    """
    폴더 삭제.
    - cascade=True: 폴더 + 안 스킬 모두 삭제.
    - cascade=False: 폴더만 삭제, 안 스킬은 시스템 '미분류' 폴더 (is_system=true) 로 이동.

    [자기-자신 케이스]
    cascade=False 인데 삭제 대상이 시스템 '미분류' 폴더 본인이면 cypher 가 자동 cascade
    전환 (안 스킬도 함께 삭제). 응답에서 mode='cascade' 로 알림. 사용자 차단 안 함 —
    의도적으로 비울 수 있는 행동.

    Returns:
      { mode: 'cascade', deleted_skill_count: int }     — cascade=True 또는 자기-자신
      { mode: 'moved', moved_skill_count: int,
        unfiled_folder_id: str }                         — cascade=False + 일반
      { mode: 'not_found' }                              — 폴더 없음 / 다른 사용자
    """
    if cascade:
        records = await neo4j_client.run_cypher(
            _DELETE_FOLDER_CASCADE_CYPHER,
            {"owner_email": owner_email, "id": folder_id},
        )
        row = _first(records)
        if not row:
            return {"mode": "not_found"}
        return {
            "mode": "cascade",
            "deleted_skill_count": int(row.get("deleted_skill_count") or 0),
        }
    else:
        records = await neo4j_client.run_cypher(
            _DELETE_FOLDER_MOVE_TO_UNFILED_CYPHER,
            {"owner_email": owner_email, "id": folder_id},
        )
        row = _first(records)
        if not row:
            return {"mode": "not_found"}
        # 자기-자신 케이스 — cypher 가 자동 cascade 처리
        if row.get("is_self_target"):
            return {
                "mode": "cascade",
                "deleted_skill_count": int(row.get("moved_skill_count") or 0),
            }
        return {
            "mode": "moved",
            "moved_skill_count": int(row.get("moved_skill_count") or 0),
            "unfiled_folder_id": row.get("unfiled_folder_id"),
        }


# ===== 스킬 CRUD =====


async def create_skill(
    *,
    owner_email: str,
    folder_id: str,
    name: str,
    scope: str = "",
    priority: str = "Medium",
    trigger_condition: str = "",
    instructions: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
) -> Optional[LibrarySkillRow]:
    """폴더 안에 스킬 생성. 폴더 없거나 다른 사용자 폴더면 None."""
    records = await neo4j_client.run_cypher(
        _CREATE_SKILL_CYPHER,
        {
            "owner_email": owner_email,
            "folder_id": folder_id,
            "name": name,
            "scope": scope,
            "priority": priority,
            "trigger_condition": trigger_condition,
            "instructions": instructions or [],
            "tags": tags or [],
        },
    )
    row = _first(records)
    return _row_to_skill((row or {}).get("skill") or {})


async def update_skill(
    *,
    owner_email: str,
    skill_id: str,
    name: Optional[str] = None,
    scope: Optional[str] = None,
    priority: Optional[str] = None,
    trigger_condition: Optional[str] = None,
    instructions: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    folder_id: Optional[str] = None,
) -> Optional[LibrarySkillRow]:
    """
    스킬 메타 수정 + 폴더 이동.

    folder_id 가 None 이면 폴더 그대로. 비어있지 않은 값이면 그 폴더로 이동
    (다른 사용자 폴더면 매칭 실패로 이동 안 됨, 메타만 수정).
    """
    records = await neo4j_client.run_cypher(
        _UPDATE_SKILL_CYPHER,
        {
            "owner_email": owner_email,
            "id": skill_id,
            "name": name,
            "scope": scope,
            "priority": priority,
            "trigger_condition": trigger_condition,
            "instructions": instructions,
            "tags": tags,
            "folder_id": folder_id,
        },
    )
    row = _first(records)
    return _row_to_skill((row or {}).get("skill") or {})


async def delete_skill(*, owner_email: str, skill_id: str) -> bool:
    """스킬 삭제. True if deleted."""
    records = await neo4j_client.run_cypher(
        _DELETE_SKILL_CYPHER,
        {"owner_email": owner_email, "id": skill_id},
    )
    row = _first(records)
    return bool(row and row.get("deleted_id"))


# ===== 조회 =====


@dataclass(frozen=True)
class LibraryEntry:
    """한 폴더 + 그 안 스킬들."""

    folder: SkillFolderRow
    skills: List[LibrarySkillRow]


async def list_library(owner_email: str) -> List[LibraryEntry]:
    """사용자 라이브러리 전체 — 폴더 트리 + 안 스킬."""
    records = await neo4j_client.run_cypher(
        _LIST_LIBRARY_CYPHER, {"owner_email": owner_email}
    )
    out: List[LibraryEntry] = []
    for r in records:
        entry = r.get("entry") or {}
        folder = _row_to_folder(entry.get("folder") or {})
        if not folder:
            continue
        skills = []
        for sk_row in entry.get("skills") or []:
            sk = _row_to_skill(sk_row)
            if sk:
                skills.append(sk)
        out.append(LibraryEntry(folder=folder, skills=skills))
    return out


async def count_skills(owner_email: str) -> int:
    """사용자가 보유한 LibrarySkill 총 개수. quota 검증용."""
    records = await neo4j_client.run_cypher(
        _COUNT_LIBRARY_SKILLS_CYPHER, {"owner_email": owner_email}
    )
    row = _first(records)
    return int((row or {}).get("total") or 0)


# ===== 자동 초기화 (빈 라이브러리) =====


# 사용자 결정 (2026-05) — 첫 진입 시 자동 생성될 기본 폴더 5종.
# preset 컬러 6종 중 5개 매핑. 카테고리는 기존 RuleGenerator 의 CATEGORY_MAP 호환.
_DEFAULT_FOLDERS: List[Dict[str, str]] = [
    {"name": "Frontend", "color": "#3b82f6", "category": "frontEnd",
     "description": "프론트엔드 표준 스킬"},
    {"name": "Backend", "color": "#10b981", "category": "backEnd",
     "description": "백엔드 표준 스킬"},
    {"name": "DB", "color": "#f59e0b", "category": "db",
     "description": "데이터베이스 / 데이터 모델링 스킬"},
    {"name": "Mobile", "color": "#8b5cf6", "category": "mobile",
     "description": "모바일 앱 (iOS / Android) 스킬"},
    {"name": "기타", "color": "#6b7280", "category": "",
     "description": "분류되지 않은 스킬"},
]


# ===== Import / Export (프로젝트 ↔ 라이브러리) =====


# [BUG FIX 2026-05] 두 단계 분리:
#   1. _CHECK_FOLDER_OWNED_CYPHER 로 폴더 존재/소유 확인 (호출자 None 반환)
#   2. _COPY_FROM_PROJECT_CYPHER 로 import (skill_ids 0 매칭이어도 정상 진행)
# 이전 단일 cypher 는 skill_ids 가 project 에 없으면 outer row 0 → None 잘못 반환.

_CHECK_FOLDER_OWNED_CYPHER = """\
MATCH (f:SkillFolder {id: $folder_id, owner_email: $owner_email})
RETURN f.id AS folder_id
"""

# 폴더 존재가 호출자에서 검증됐다고 가정. skill_ids 가 0 매칭이어도 0 row OK
# (호출자가 빈 imported list 로 처리).
_COPY_FROM_PROJECT_CYPHER = """\
MATCH (f:SkillFolder {id: $folder_id, owner_email: $owner_email})
MATCH (src:Skill {project: $project_name})
WHERE src.id IN $skill_ids
CREATE (lib:LibrarySkill {
    id: randomUUID(),
    name: src.name,
    scope: COALESCE(src.scope, ''),
    priority: COALESCE(src.priority, 'Medium'),
    trigger_condition: COALESCE(src.trigger_condition, ''),
    instructions: COALESCE(src.instructions, []),
    tags: [t IN COALESCE(src.tags, []) WHERE NOT t STARTS WITH 'cat:'],
    owner_email: $owner_email,
    created_at: datetime(),
    updated_at: datetime()
})
MERGE (f)-[:CONTAINS]->(lib)
RETURN collect({
    source_skill_id: src.id,
    library_skill_id: lib.id,
    name: src.name
}) AS imported
"""


# 라이브러리 스킬 → 프로젝트 Skill 복사 (overwrite/create 패턴).
# project 안에 같은 id Skill 있으면 SET 으로 덮어쓰기 (MERGE 동작).
# GOVERNED_BY 관계는 별도 cypher 로 재계산 (기존 Skill 의 ArchService 매칭 로직과 동일).
_COPY_TO_PROJECT_MERGE_CYPHER = """\
// 본인 LibrarySkill 만 조회 (owner_email 매칭으로 다른 사용자 라이브러리 차단)
MATCH (lib:LibrarySkill {owner_email: $owner_email})
WHERE lib.id IN $library_skill_ids
WITH collect(lib) AS libs

// 각 LibrarySkill 을 project 의 Skill 노드로 MERGE.
// id_overrides 매핑: rename 정책 시 새 id 사용. {orig_id: new_id} dict 형태.
UNWIND libs AS lib
WITH lib, COALESCE($id_overrides[lib.id], lib.id) AS target_id
// [동적 카테고리] 폴더 category 를 cat: 마커로 주입 — export(get_skill_path / FE getCategoryFromSkill)가 동적 폴더명을 그대로 보존
OPTIONAL MATCH (folder:SkillFolder)-[:CONTAINS]->(lib)
WITH lib, target_id,
     CASE WHEN folder IS NOT NULL AND coalesce(folder.category, '') <> ''
          THEN ['cat:' + folder.category] ELSE [] END AS cat_marker
MERGE (s:Skill {project: $project_name, id: target_id})
SET s.name = lib.name,
    s.scope = lib.scope,
    s.priority = lib.priority,
    s.trigger_condition = lib.trigger_condition,
    s.instructions = lib.instructions,
    s.tags = cat_marker + [t IN coalesce(lib.tags, []) WHERE NOT t STARTS WITH 'cat:'],
    s.updated_at = timestamp()

// 기존 GOVERNED_BY 끊고 ArchService.tech_stack 매칭으로 재연결
WITH s, lib
OPTIONAL MATCH (s)<-[old:GOVERNED_BY]-()
DELETE old
WITH s, lib
OPTIONAL MATCH (arch:ArchService {project: $project_name})
WHERE ANY(tag IN s.tags WHERE arch.tech_stack CONTAINS tag)
FOREACH (_ IN CASE WHEN arch IS NULL THEN [] ELSE [1] END |
    MERGE (arch)-[:GOVERNED_BY]->(s)
)

RETURN collect(DISTINCT s.id) AS created_ids
"""


# 충돌 검사 — 어떤 id 가 이미 project 에 존재하는지.
_FIND_CONFLICTING_SKILL_IDS_CYPHER = """\
MATCH (s:Skill {project: $project_name})
WHERE s.id IN $skill_ids
RETURN collect(s.id) AS conflicting_ids
"""


@dataclass(frozen=True)
class ImportResult:
    """프로젝트 → 라이브러리 import 결과."""

    imported: List[Dict[str, str]]  # [{source_skill_id, library_skill_id, name}]
    new_total_skill_count: int      # 라이브러리의 새 총 스킬 수


@dataclass(frozen=True)
class ExportResult:
    """라이브러리 → 프로젝트 export 결과."""

    imported_ids: List[str]                 # 새로 만들어졌거나 덮어쓴 id
    skipped_ids: List[str]                  # skip 정책 적용된 id
    renamed: List[Dict[str, str]]           # [{old_id, new_id}]


async def find_conflicting_skill_ids(
    project_name: str, skill_ids: List[str]
) -> List[str]:
    """project 에 이미 있는 Skill id 들 반환. export 충돌 검사용."""
    if not skill_ids:
        return []
    records = await neo4j_client.run_cypher(
        _FIND_CONFLICTING_SKILL_IDS_CYPHER,
        {"project_name": project_name, "skill_ids": skill_ids},
    )
    row = _first(records)
    return list((row or {}).get("conflicting_ids") or [])


async def copy_skills_from_project(
    *,
    owner_email: str,
    project_name: str,
    skill_ids: List[str],
    folder_id: str,
) -> Optional[ImportResult]:
    """
    프로젝트의 Skill 노드들을 라이브러리 폴더로 복사.

    [정책]
    - skill_ids 중 project 에 없는 id 는 silently skip (cypher MATCH 매칭 안 됨).
    - 한도 검증은 호출자(라우트) 책임 — count_skills + len(skill_ids) 비교 후 호출.
    - 폴더 본인 소유가 아니면 None 반환 (호출자가 404 매핑).

    [구현 — 2단계]
    1. _CHECK_FOLDER_OWNED_CYPHER 로 폴더 존재 확인 (없으면 None).
    2. _COPY_FROM_PROJECT_CYPHER 로 실제 import. skill_ids 가 project 에 0 매칭이어도
       imported=[] 정상 반환 (404 잘못 매핑 방지 — Critical Fix 2026-05).

    Returns:
        ImportResult 또는 None (폴더 없음).
    """
    # 1. 폴더 존재 + owner 매칭 확인
    folder_check = await neo4j_client.run_cypher(
        _CHECK_FOLDER_OWNED_CYPHER,
        {"owner_email": owner_email, "folder_id": folder_id},
    )
    if not _first(folder_check):
        return None  # 폴더 없거나 다른 사용자 소유

    # 2. skill_ids 빈 list 면 cypher 호출 skip
    if not skill_ids:
        return ImportResult(
            imported=[],
            new_total_skill_count=await count_skills(owner_email),
        )

    # 3. 실제 import — skill_ids 가 project 에 0 매칭이어도 cypher 가 0 row 반환.
    #    그래도 폴더는 있으니 imported=[] 로 정상 처리.
    records = await neo4j_client.run_cypher(
        _COPY_FROM_PROJECT_CYPHER,
        {
            "owner_email": owner_email,
            "project_name": project_name,
            "skill_ids": skill_ids,
            "folder_id": folder_id,
        },
    )
    row = _first(records)
    imported_raw = (row or {}).get("imported") or []
    imported = [
        {
            "source_skill_id": item.get("source_skill_id") or "",
            "library_skill_id": item.get("library_skill_id") or "",
            "name": item.get("name") or "",
        }
        for item in imported_raw
        if item and item.get("library_skill_id")
    ]
    return ImportResult(
        imported=imported,
        new_total_skill_count=await count_skills(owner_email),
    )


async def copy_skills_to_project(
    *,
    owner_email: str,
    project_name: str,
    library_skill_ids: List[str],
    conflict_strategy: str,  # 'overwrite' | 'skip' | 'rename'
) -> ExportResult:
    """
    라이브러리 스킬 → 프로젝트 Skill 노드 복사.

    [정책별 처리]
    - 'overwrite': 충돌 ID 도 덮어쓰기 (MERGE + SET).
    - 'skip': 충돌 ID 는 제외하고 나머지만 생성.
    - 'rename': 충돌 ID 에 '-copy-{n}' suffix 부여 후 생성.

    [Why 코드에서 분기]
    충돌 처리를 cypher 안에 다 넣으면 복잡. 코드에서 1) 충돌 ID 먼저 조회 → 2) 정책별
    ID 리스트 분리 → 3) MERGE cypher 1회 호출. 명확하고 디버그 쉬움.

    [GOVERNED_BY]
    Skill 생성 후 기존 [:GOVERNED_BY] 끊고 tech_stack 매칭으로 재연결 (cypher 안에서).
    """
    if not library_skill_ids:
        return ExportResult(imported_ids=[], skipped_ids=[], renamed=[])
    if conflict_strategy not in ("overwrite", "skip", "rename"):
        raise ValueError(f"invalid conflict_strategy: {conflict_strategy}")

    # 본인 라이브러리 스킬만 조회 — 동시에 그 id 들 알아냄
    skill_rows = await _fetch_library_skill_ids_owned(owner_email, library_skill_ids)
    if not skill_rows:
        return ExportResult(imported_ids=[], skipped_ids=[], renamed=[])
    owned_ids = [row["id"] for row in skill_rows]  # owner 매칭된 id 만

    # 충돌 검사 — project 에 이미 같은 id 있는지
    conflicting = set(await find_conflicting_skill_ids(project_name, owned_ids))

    id_overrides: Dict[str, str] = {}
    skipped_ids: List[str] = []
    renamed: List[Dict[str, str]] = []

    if conflict_strategy == "overwrite":
        # 충돌 무시, 그대로 진행 (MERGE 가 SET 으로 덮어씀)
        target_ids = owned_ids
    elif conflict_strategy == "skip":
        target_ids = [i for i in owned_ids if i not in conflicting]
        skipped_ids = [i for i in owned_ids if i in conflicting]
    else:  # rename
        target_ids = []
        for orig in owned_ids:
            if orig in conflicting:
                new_id = await _allocate_renamed_id(project_name, orig)
                id_overrides[orig] = new_id
                renamed.append({"old_id": orig, "new_id": new_id})
                target_ids.append(orig)  # cypher 에 원래 id 넘기고 override 로 매핑
            else:
                target_ids.append(orig)

    if not target_ids:
        return ExportResult(imported_ids=[], skipped_ids=skipped_ids, renamed=renamed)

    records = await neo4j_client.run_cypher(
        _COPY_TO_PROJECT_MERGE_CYPHER,
        {
            "owner_email": owner_email,
            "project_name": project_name,
            "library_skill_ids": target_ids,
            "id_overrides": id_overrides,
        },
    )
    row = _first(records)
    imported_ids = list((row or {}).get("created_ids") or [])
    return ExportResult(
        imported_ids=imported_ids,
        skipped_ids=skipped_ids,
        renamed=renamed,
    )


# ─── Import/Export helpers ─────────────────────────────────


_FETCH_OWNED_LIBRARY_SKILL_IDS_CYPHER = """\
MATCH (s:LibrarySkill {owner_email: $owner_email})
WHERE s.id IN $library_skill_ids
RETURN collect({id: s.id}) AS owned
"""


async def _fetch_library_skill_ids_owned(
    owner_email: str, library_skill_ids: List[str]
) -> List[Dict[str, str]]:
    """본인 소유의 LibrarySkill id 만 필터링. 다른 사용자 라이브러리 ID 시도 차단."""
    if not library_skill_ids:
        return []
    records = await neo4j_client.run_cypher(
        _FETCH_OWNED_LIBRARY_SKILL_IDS_CYPHER,
        {"owner_email": owner_email, "library_skill_ids": library_skill_ids},
    )
    row = _first(records)
    return list((row or {}).get("owned") or [])


async def _allocate_renamed_id(project_name: str, original_id: str) -> str:
    """
    rename 정책 시 충돌 안 나는 id 할당.
    `{original}-copy` 시도 → 있으면 `-copy-2`, `-copy-3` ... 으로 증가.

    [성능]
    충돌 많을수록 cypher 호출 늘어남. 운영에서 dozens 정도면 OK.
    개선: 한 cypher 안에서 모든 후보 검사. 추후 필요 시.
    """
    suffix_n = 1
    while True:
        candidate = f"{original_id}-copy" if suffix_n == 1 else f"{original_id}-copy-{suffix_n}"
        existing = await find_conflicting_skill_ids(project_name, [candidate])
        if not existing:
            return candidate
        suffix_n += 1
        if suffix_n > 50:  # 무한 루프 방지 — 50개나 충돌하면 비정상
            # UUID 8자 suffix 로 fallback
            import uuid
            return f"{original_id}-copy-{uuid.uuid4().hex[:8]}"


async def ensure_default_folders_if_empty(owner_email: str) -> List[SkillFolderRow]:
    """
    사용자 라이브러리가 완전히 비어있으면 (folder 0개) 기본 폴더 5개 자동 생성.

    [멱등성]
    이미 폴더 1개 이상 있으면 skip — 사용자가 의도적으로 다 삭제한 상태도 존중.
    return 값으로 새로 생성된 폴더 리스트 (또는 empty list).

    [Why 빈 상태에서만]
    사용자가 폴더를 다 삭제한 후에도 매번 자동 생성하면 짜증. "라이브러리 한 번도
    안 쓴 사용자에게만" 한정 → folder count 0 일 때만.
    """
    existing = await list_library(owner_email)
    if existing:
        return []

    created: List[SkillFolderRow] = []
    for spec in _DEFAULT_FOLDERS:
        folder = await create_folder(
            owner_email=owner_email,
            name=spec["name"],
            description=spec["description"],
            color=spec["color"],
            category=spec["category"],
        )
        if folder:
            created.append(folder)
    return created
