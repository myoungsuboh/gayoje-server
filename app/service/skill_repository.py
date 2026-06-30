"""
Skill Repository — Rule Generator 의 Skill 노드 CRUD (Neo4j 직접).

[엔드포인트 매핑]
- postSkill        → `create_skills` (bulk MERGE + ArchService.tech_stack 매칭으로 GOVERNED_BY 자동 생성)
- getSkill         → `get_skill`
- getAllSkill      → `get_all_skills`
- deleteSkill      → `delete_skill`
- getDuplicateSkill → `find_duplicate_skill`

[Skill 도메인 모델]
- id, project, name, scope, priority(High/Medium/Low), trigger_condition,
  instructions(list), tags(list of tech stack tags), updated_at

[관계]
(ArchService)-[:GOVERNED_BY]->(Skill)
스킬의 tags 와 ArchService.tech_stack 이 매칭되면 자동 연결.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# ===== 도메인 모델 =====


class SkillInput(BaseModel):
    """클라이언트가 postSkill 로 보내는 단일 스킬 정의."""

    id: str
    name: str
    scope: str = ""
    priority: str = "Medium"  # High | Medium | Low
    trigger_condition: str = ""
    instructions: List[str] = []
    tags: List[str] = []
    # [B1 — 2026-06-13] 추천 승인율 추적: AI 추천 수락 여부·신뢰도 기록.
    source: Optional[str] = None                 # "ai_recommend" | None
    recommended_confidence: Optional[float] = None  # 추천 시 confidence 값


class SkillOut(BaseModel):
    """getSkill 응답."""

    id: str
    name: str
    scope: Optional[str] = None
    priority: Optional[str] = None
    trigger: Optional[str] = None  # trigger_condition (기존 alias 이름 유지)
    instructions: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    applied_services: List[str] = []
    source: Optional[str] = None
    recommended_confidence: Optional[float] = None


class SkillSummary(BaseModel):
    """getAllSkill 한 줄 요약."""

    id: str
    name: str
    scope: Optional[str] = None
    priority: Optional[str] = None
    tags: Optional[List[str]] = None
    rule_count: int = 0  # '규칙수' (size(instructions))
    applied_services: List[str] = []
    source: Optional[str] = None


class SkillFull(BaseModel):
    """getAllSkillDetail 응답 — 규칙 본문(instructions)·trigger 까지 포함한 전체.

    바이브 코딩 zip 의 skills/*.md 를 만들 때 사용한다. getAllSkill(요약) 은
    rule_count(개수)만 줘서 .md 가 빈 껍데기가 되던 문제를 해결하기 위해 분리.
    """

    id: str
    name: str
    scope: Optional[str] = None
    priority: Optional[str] = None
    trigger_condition: str = ""
    instructions: List[str] = []
    tags: Optional[List[str]] = None


# ===== Cypher =====


# postSkill — bulk upsert + 기존 GOVERNED_BY 삭제 후 tech_stack 매칭으로 재연결.
# 'Post Skill' 단계와 동등. 문자열 인터폴레이션 대신
# `$skills` / `$project` 파라미터 바인딩 사용 (Cypher injection 안전).
_POST_SKILL_CYPHER = """\
UNWIND $skills AS sData
MERGE (s:Skill {id: sData.id, project: $project})
SET s.name = sData.name,
    s.scope = sData.scope,
    s.priority = sData.priority,
    s.trigger_condition = sData.trigger_condition,
    s.instructions = sData.instructions,
    s.tags = sData.tags,
    s.source = sData.source,
    s.recommended_confidence = sData.recommended_confidence,
    s.updated_at = timestamp()

WITH s, sData
OPTIONAL MATCH (s)<-[old:GOVERNED_BY]-()
DELETE old

WITH s, sData
MATCH (arch:ArchService {project: $project})
WHERE ANY(tag IN sData.tags WHERE arch.tech_stack CONTAINS tag)
MERGE (arch)-[:GOVERNED_BY]->(s)

RETURN collect(DISTINCT s.id) AS ids
"""


_GET_SKILL_CYPHER = """\
MATCH (s:Skill {project: $project, id: $id})
OPTIONAL MATCH (arch:ArchService)-[:GOVERNED_BY]->(s)
RETURN
    s.id AS id,
    s.name AS name,
    s.scope AS scope,
    s.priority AS priority,
    s.trigger_condition AS trigger,
    s.instructions AS instructions,
    s.tags AS tags,
    s.source AS source,
    s.recommended_confidence AS recommended_confidence,
    collect(DISTINCT arch.name) AS applied_services
ORDER BY s.priority DESC, s.name ASC
"""


_GET_ALL_SKILLS_CYPHER = """\
MATCH (s:Skill {project: $project})
OPTIONAL MATCH (arch:ArchService)-[:GOVERNED_BY]->(s)
RETURN
    s.id AS id,
    s.name AS name,
    s.scope AS scope,
    s.priority AS priority,
    s.tags AS tags,
    s.source AS source,
    size(s.instructions) AS rule_count,
    collect(DISTINCT arch.name) AS applied_services
ORDER BY
    CASE s.priority
        WHEN 'High' THEN 1
        WHEN 'Medium' THEN 2
        WHEN 'Low' THEN 3
        ELSE 4 END,
    s.name ASC
"""


_DELETE_SKILL_CYPHER = """\
// 노드와 모든 관계 삭제. 반환은 삭제된 id (없으면 빈 결과).
MATCH (s:Skill {id: $id, project: $project})
WITH s.id AS deletedId, s
DETACH DELETE s
RETURN deletedId AS deleted_id
"""


# fillSkillTriggers — 프로젝트의 모든 Skill 을 trigger 생성에 필요한 필드까지 전부.
# getAllSkill(요약) 과 달리 instructions/trigger_condition 도 반환 (LLM 입력용).
_GET_SKILLS_FOR_TRIGGER_FILL_CYPHER = """\
MATCH (s:Skill {project: $project})
RETURN
    s.id AS id,
    s.name AS name,
    s.scope AS scope,
    s.trigger_condition AS trigger_condition,
    s.instructions AS instructions,
    s.tags AS tags
ORDER BY s.name ASC
"""


# getAllSkillDetail — 규칙 본문 포함 전체 (vibe zip 의 skills/*.md 생성용).
_GET_ALL_SKILLS_FULL_CYPHER = """\
MATCH (s:Skill {project: $project})
RETURN
    s.id AS id,
    s.name AS name,
    s.scope AS scope,
    s.priority AS priority,
    s.trigger_condition AS trigger_condition,
    s.instructions AS instructions,
    s.tags AS tags
ORDER BY s.priority DESC, s.name ASC
"""


_DUPLICATE_SKILL_CYPHER = """\
// 같은 project 내 동일 이름 존재 여부.
MATCH (s:Skill {project: $project, name: $name})
RETURN count(s) > 0 AS is_duplicate,
       collect(s.id) AS existing_ids
"""


_DUPLICATE_SKILL_BY_ID_CYPHER = """\
// 같은 project 내 동일 ID 존재 여부.
MATCH (s:Skill {project: $project, id: $id})
RETURN count(s) > 0 AS is_duplicate,
       collect(s.id) AS existing_ids
"""


# ===== Helpers =====


def _first(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return records[0] if records else None


# ===== CRUD =====


async def create_skills(
    project_name: str, skills: List[SkillInput]
) -> Dict[str, Any]:
    """
    Endpoint: postSkill.

    bulk upsert. 기존 동일 id 의 GOVERNED_BY 관계는 모두 끊고
    현재 ArchService.tech_stack 매칭으로 재연결.
    """
    if not skills:
        return {"ids": []}

    skills_payload = [
        {
            "id": s.id,
            "name": s.name,
            "scope": s.scope,
            "priority": s.priority,
            "trigger_condition": s.trigger_condition,
            "instructions": s.instructions,
            "tags": s.tags,
            "source": s.source,
            "recommended_confidence": s.recommended_confidence,
        }
        for s in skills
    ]
    records = await neo4j_client.run_cypher(
        _POST_SKILL_CYPHER,
        {"project": project_name, "skills": skills_payload},
    )
    row = _first(records)
    return {"ids": (row or {}).get("ids") or []}


async def get_skill(project_name: str, skill_id: str) -> Optional[SkillOut]:
    """Endpoint: getSkill."""
    records = await neo4j_client.run_cypher(
        _GET_SKILL_CYPHER, {"project": project_name, "id": skill_id}
    )
    row = _first(records)
    if not row or not row.get("id"):
        return None
    return SkillOut(
        id=row["id"],
        name=row.get("name") or "",
        scope=row.get("scope"),
        priority=row.get("priority"),
        trigger=row.get("trigger"),
        instructions=row.get("instructions") or [],
        tags=row.get("tags") or [],
        applied_services=[a for a in (row.get("applied_services") or []) if a],
        source=row.get("source"),
        recommended_confidence=row.get("recommended_confidence"),
    )


async def get_all_skills(project_name: str) -> List[SkillSummary]:
    """Endpoint: getAllSkill."""
    records = await neo4j_client.run_cypher(
        _GET_ALL_SKILLS_CYPHER, {"project": project_name}
    )
    out: List[SkillSummary] = []
    for r in records:
        if not r.get("id"):
            continue
        out.append(
            SkillSummary(
                id=r["id"],
                name=r.get("name") or "",
                scope=r.get("scope"),
                priority=r.get("priority"),
                tags=r.get("tags") or [],
                rule_count=int(r.get("rule_count") or 0),
                applied_services=[a for a in (r.get("applied_services") or []) if a],
                source=r.get("source"),
            )
        )
    return out


async def get_all_skills_full(project_name: str) -> List[SkillFull]:
    """Endpoint: getAllSkillDetail — 규칙 본문(instructions)·trigger 포함 전체.

    바이브 코딩 zip 의 skills/*.md 를 채우기 위해 사용. getAllSkill 요약과 달리
    instructions/trigger_condition 을 그대로 반환한다.
    """
    records = await neo4j_client.run_cypher(
        _GET_ALL_SKILLS_FULL_CYPHER, {"project": project_name}
    )
    out: List[SkillFull] = []
    for r in records:
        if not r.get("id"):
            continue
        out.append(
            SkillFull(
                id=r["id"],
                name=r.get("name") or "",
                scope=r.get("scope"),
                priority=r.get("priority"),
                trigger_condition=r.get("trigger_condition") or "",
                instructions=r.get("instructions") or [],
                tags=r.get("tags") or [],
            )
        )
    return out


async def delete_skill(project_name: str, skill_id: str) -> bool:
    """Endpoint: deleteSkill. True if deleted, False if not found."""
    records = await neo4j_client.run_cypher(
        _DELETE_SKILL_CYPHER, {"project": project_name, "id": skill_id}
    )
    row = _first(records)
    return bool(row and row.get("deleted_id"))


async def find_duplicate_skill(
    project_name: str, skill_name: str
) -> Dict[str, Any]:
    """
    Endpoint: getDuplicateSkill (이름 기반).
    Returns: { is_duplicate: bool, existing_ids: [str] }
    """
    records = await neo4j_client.run_cypher(
        _DUPLICATE_SKILL_CYPHER, {"project": project_name, "name": skill_name}
    )
    row = _first(records) or {}
    return {
        "is_duplicate": bool(row.get("is_duplicate")),
        "existing_ids": list(row.get("existing_ids") or []),
    }


async def get_skills_for_trigger_fill(
    project_name: str,
) -> List[Dict[str, Any]]:
    """
    fillSkillTriggers 용 — 프로젝트의 모든 Skill 을 trigger 생성에 필요한 필드까지
    포함해 반환 (id/name/scope/trigger_condition/instructions/tags).

    getAllSkill(SkillSummary 요약) 과 분리: trigger 생성은 instructions 본문이
    필요한데 요약에는 없어서 별도 조회.
    """
    records = await neo4j_client.run_cypher(
        _GET_SKILLS_FOR_TRIGGER_FILL_CYPHER, {"project": project_name}
    )
    out: List[Dict[str, Any]] = []
    for r in records:
        if not r.get("id"):
            continue
        out.append(
            {
                "id": r["id"],
                "name": r.get("name") or "",
                "scope": r.get("scope") or "",
                "trigger_condition": r.get("trigger_condition") or "",
                "instructions": r.get("instructions") or [],
                "tags": r.get("tags") or [],
            }
        )
    return out


async def find_duplicate_skill_by_id(
    project_name: str, skill_id: str
) -> Dict[str, Any]:
    """
    Skill ID 중복 체크 (frontend 의 RuleGenerator '중복 체크' 버튼이 사용).
    Returns: { is_duplicate: bool, existing_ids: [str] }
    """
    records = await neo4j_client.run_cypher(
        _DUPLICATE_SKILL_BY_ID_CYPHER, {"project": project_name, "id": skill_id}
    )
    row = _first(records) or {}
    return {
        "is_duplicate": bool(row.get("is_duplicate")),
        "existing_ids": list(row.get("existing_ids") or []),
    }
