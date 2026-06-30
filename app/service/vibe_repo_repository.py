"""
Vibe Repo Library — 사용자 단위의 "내가 바이브코딩한 repo" 즐겨찾기.

[배경]
기존 `repo_repository.py` 는 Project 종속 (`(:Project)-[:HAS_REPO]->(:Repo)`).
프로젝트와 무관하게 "내가 바이브코딩으로 만든 GitHub repo URL 모음" 이 필요.
다른 사람(동료) 의 URL 도 추가 가능 — GitHub 자체가 협업 도구라 URL 만 있으면 됨.

[모델]
(:User {email})-[:HAS_VIBE_REPO {added_at, updated_at}]->(:VibeRepo {
    user_email,        # 소유자 식별 (per-user 노드)
    url,               # 정규화된 https://github.com/{owner}/{repo}
    owner_handle,      # GitHub username 추출값 (검색 편의)
    label,             # 사용자 표시명
    description,       # 메모
    is_mine,           # true: 내가 만든 것, false: 동료 것
})

per-user 노드 — 같은 URL 도 사용자별로 분리. label/description 이 user-private.

[URL 정규화]
app.clients.github_client.parse_github_url 재사용 — 어떤 형식이든
`https://github.com/{owner}/{repo}` 표준 형식으로.

[보안]
모든 mutation/read 는 호출자가 `current_user.email` 을 통과 필수.
Cypher 는 전부 $param 바인딩.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.clients import neo4j_client
from app.clients.github_client import GitHubError, parse_github_url

logger = logging.getLogger(__name__)


# ===== 도메인 모델 =====


class VibeRepoInput(BaseModel):
    """라이브러리 추가 요청 body."""

    url: str = Field(..., min_length=1)
    label: str = ""
    description: str = ""
    is_mine: bool = True  # true: 내 vibe-coded, false: 동료 것


class VibeRepoOut(BaseModel):
    """라이브러리 항목 응답."""

    url: str
    owner_handle: Optional[str] = None
    label: Optional[str] = None
    description: Optional[str] = None
    is_mine: bool = True
    added_at: Optional[int] = None
    updated_at: Optional[int] = None


# ===== Cypher =====


_ADD_VIBE_REPO_CYPHER = """\
MATCH (u:User {email: $email})
MERGE (r:VibeRepo {user_email: $email, url: $url})
ON CREATE SET
    r.owner_handle = $owner_handle,
    r.label = $label,
    r.description = $description,
    r.is_mine = $is_mine,
    r.added_at = $now,
    r.updated_at = $now
ON MATCH SET
    r.label = $label,
    r.description = $description,
    r.is_mine = $is_mine,
    r.owner_handle = $owner_handle,
    r.updated_at = $now
MERGE (u)-[rel:HAS_VIBE_REPO]->(r)
ON CREATE SET rel.added_at = $now
RETURN r {
    .url, .owner_handle, .label, .description, .is_mine, .added_at, .updated_at
} AS repo
"""


_GET_VIBE_REPOS_CYPHER = """\
MATCH (u:User {email: $email})-[:HAS_VIBE_REPO]->(r:VibeRepo)
RETURN r {
    .url, .owner_handle, .label, .description, .is_mine, .added_at, .updated_at
} AS repo
ORDER BY r.is_mine DESC, r.updated_at DESC
"""


_DELETE_VIBE_REPO_CYPHER = """\
// user 소유 확인 후 노드 + 관계 삭제.
// 다른 사용자의 동일 URL 노드는 그대로 (per-user 모델).
MATCH (u:User {email: $email})-[:HAS_VIBE_REPO]->(r:VibeRepo {user_email: $email, url: $url})
WITH r, r.url AS deleted_url
DETACH DELETE r
RETURN deleted_url AS deleted_url
"""


_GET_BY_URL_CYPHER = """\
MATCH (u:User {email: $email})-[:HAS_VIBE_REPO]->(r:VibeRepo {user_email: $email, url: $url})
RETURN r {
    .url, .owner_handle, .label, .description, .is_mine, .added_at, .updated_at
} AS repo
LIMIT 1
"""


# ===== Helpers =====


def normalize_github_url(raw_url: str) -> tuple[str, str]:
    """
    GitHub URL 정규화 + owner_handle 추출.

    Returns: (normalized_url, owner_handle)
    Raises: ValueError — 파싱 실패 시 (호출자가 422 매핑)
    """
    try:
        ident = parse_github_url(raw_url)
    except GitHubError as e:
        raise ValueError(f"GitHub URL 형식이 올바르지 않습니다: {raw_url}") from e
    return f"https://github.com/{ident.owner}/{ident.repo}", ident.owner


def _first(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return records[0] if records else None


def _to_out(row: Dict[str, Any]) -> Optional[VibeRepoOut]:
    r = row.get("repo") or {}
    if not r.get("url"):
        return None
    return VibeRepoOut(
        url=r["url"],
        owner_handle=r.get("owner_handle"),
        label=r.get("label") or "",
        description=r.get("description") or "",
        is_mine=bool(r.get("is_mine", True)),
        added_at=int(r["added_at"]) if r.get("added_at") is not None else None,
        updated_at=int(r["updated_at"]) if r.get("updated_at") is not None else None,
    )


# ===== CRUD =====


async def add_vibe_repo(email: str, payload: VibeRepoInput) -> VibeRepoOut:
    """
    라이브러리에 repo 추가 (upsert — 같은 URL 재호출 시 label/description 갱신).

    Raises:
      ValueError: URL 정규화 실패 시 (호출자가 422 매핑)
      RuntimeError: Neo4j 에서 user 노드를 찾지 못한 경우 (호출자 책임 아니)
    """
    if not email:
        raise ValueError("email 이 비어 있습니다.")

    normalized_url, owner_handle = normalize_github_url(payload.url)
    now = int(time.time() * 1000)

    records = await neo4j_client.run_cypher(
        _ADD_VIBE_REPO_CYPHER,
        {
            "email": email,
            "url": normalized_url,
            "owner_handle": owner_handle,
            "label": payload.label or "",
            "description": payload.description or "",
            "is_mine": bool(payload.is_mine),
            "now": now,
        },
    )
    row = _first(records)
    out = _to_out(row or {})
    if out is None:
        raise RuntimeError(f"VibeRepo upsert failed: user not found? email={email}")
    return out


async def get_vibe_repos(email: str) -> List[VibeRepoOut]:
    """
    내 라이브러리 전체. is_mine=true 가 먼저, 그 안에서 최신 갱신순.
    """
    if not email:
        return []
    records = await neo4j_client.run_cypher(_GET_VIBE_REPOS_CYPHER, {"email": email})
    out: List[VibeRepoOut] = []
    for r in records:
        o = _to_out(r)
        if o is not None:
            out.append(o)
    return out


async def delete_vibe_repo(email: str, url: str) -> bool:
    """
    라이브러리에서 한 항목 제거. 매칭이 있었으면 True.

    URL 은 호출자가 정규화 후 넘기는 게 안전 — 여기서도 fallback 정규화.
    """
    if not email or not url:
        return False
    try:
        normalized_url, _ = normalize_github_url(url)
    except ValueError:
        # 정규화 실패 — 원본 URL 로 시도 (예: 잘못 저장된 항목 정리용)
        normalized_url = url

    records = await neo4j_client.run_cypher(
        _DELETE_VIBE_REPO_CYPHER,
        {"email": email, "url": normalized_url},
    )
    return bool(_first(records))


async def get_vibe_repo_by_url(email: str, url: str) -> Optional[VibeRepoOut]:
    """단일 항목 조회 (디버깅/검증용). 없으면 None."""
    if not email or not url:
        return None
    try:
        normalized_url, _ = normalize_github_url(url)
    except ValueError:
        normalized_url = url
    records = await neo4j_client.run_cypher(
        _GET_BY_URL_CYPHER, {"email": email, "url": normalized_url}
    )
    row = _first(records)
    return _to_out(row or {})
