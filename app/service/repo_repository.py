"""
Project Repo CRUD — addProjectRepo / getProjectRepos / deleteProjectRepo.

[엔드포인트 매핑]
- addProjectRepo    → `add_repo`
- getProjectRepos   → `get_repos`
- deleteProjectRepo → `delete_repo`

Repo 노드 schema:
- project, url, role, label, addedAt, updatedAt
- (Project)-[:HAS_REPO]->(Repo)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.clients import neo4j_client
from app.core.project_scope import scoped_project

logger = logging.getLogger(__name__)


class RepoIn(BaseModel):
    project_name: str
    url: str
    role: str = "primary"  # primary / docs / mirror 등
    label: str = ""
    # 팀 프로젝트 컨텍스트 — 없으면 개인 프로젝트 (라우트 ownership 분기용).
    team_id: Optional[str] = None


class RepoOut(BaseModel):
    url: str
    role: Optional[str] = None
    label: Optional[str] = None
    added_at: Optional[int] = None
    updated_at: Optional[int] = None


_ADD_REPO_CYPHER = """\
MERGE (p:Project { name: $project })
MERGE (r:Repo { project: $project, url: $url })
SET r.role = $role,
    r.label = $label,
    r.addedAt = COALESCE(r.addedAt, $now),
    r.updatedAt = $now
MERGE (p)-[:HAS_REPO]->(r)
RETURN r {
    .url, .role, .label, .addedAt, .updatedAt
} AS repo
"""


_GET_REPOS_CYPHER = """\
MATCH (r:Repo { project: $project })
RETURN r { .url, .role, .label, .addedAt, .updatedAt } AS repo
ORDER BY r.role, r.addedAt DESC
"""


_DELETE_REPO_CYPHER = """\
MATCH (r:Repo { project: $project, url: $url })
DETACH DELETE r
"""


def _to_repo_out(row: Dict[str, Any]) -> Optional[RepoOut]:
    r = row.get("repo") or {}
    if not r.get("url"):
        return None
    return RepoOut(
        url=r["url"],
        role=r.get("role"),
        label=r.get("label"),
        added_at=int(r["addedAt"]) if r.get("addedAt") is not None else None,
        updated_at=int(r["updatedAt"]) if r.get("updatedAt") is not None else None,
    )


async def add_repo(payload: RepoIn) -> RepoOut:
    """upsert Repo + Project 노드. addProjectRepo 엔드포인트 구현."""
    now = int(time.time() * 1000)
    records = await neo4j_client.run_cypher(
        _ADD_REPO_CYPHER,
        {
            "project": scoped_project(payload.project_name, payload.team_id),
            "url": payload.url,
            "role": payload.role,
            "label": payload.label,
            "now": now,
        },
    )
    row = records[0] if records else {}
    out = _to_repo_out(row)
    if out is None:
        raise RuntimeError("add_repo: Neo4j 응답에서 repo 누락")
    return out


async def get_repos(project_name: str, team_id: str = "") -> List[RepoOut]:
    """프로젝트의 모든 Repo 조회."""
    records = await neo4j_client.run_cypher(
        _GET_REPOS_CYPHER, {"project": scoped_project(project_name, team_id)}
    )
    out: List[RepoOut] = []
    for r in records:
        ro = _to_repo_out(r)
        if ro is not None:
            out.append(ro)
    return out


async def delete_repo(project_name: str, url: str, team_id: str = "") -> bool:
    """url 로 Repo 삭제. (matched 했는지 여부는 Neo4j 가 별도 반환 안 함 → 항상 True 로 응답)."""
    await neo4j_client.run_cypher(
        _DELETE_REPO_CYPHER, {"project": scoped_project(project_name, team_id), "url": url}
    )
    return True
