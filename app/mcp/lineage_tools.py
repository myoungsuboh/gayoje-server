"""
MCP lineage tools — Cursor / Claude Code 같은 AI 에이전트가 우리 프로젝트의
spec ↔ code 추적성을 직접 조회할 수 있게 노출.

[차별화 — 2026-05]
Cursor / Claude Code MCP 환경에서 사용자가 코드 파일을 편집할 때:
  - 이 파일이 어떤 PRD Story 의 구현인지
  - 이 함수/클래스가 어떤 Aggregate / Service / API spec 의 일부인지
  - upstream chain (Code → Design → Story → Epic) 이 어떻게 되는지
를 즉시 알 수 있게 함. 이게 ChatGPT / 일반 LLM 채팅에서 구현 불가능한 lock-in.

[설계 원칙 — `harness_mcp.py` 와 동일]
- 모든 tool 은 read-only
- 모든 tool 진입에 `require_mcp_user_and_assert_owns(project_name)` 적용
- repository 계층만 호출 — direct neo4j_client 호출은 lineage_health 같은
  cross-cutting tool 에서만

[파일 분리 이유]
`harness_mcp.py` 는 ping/search_skills/get_prd 등 일반 도구. 이 파일은 lineage
도메인 도구. 도구 수가 늘면 도메인별 분리해서 가독성 유지.

[등록 방식]
이 모듈을 import 하기만 하면 `harness_mcp` 인스턴스에 tool 이 등록됨
(side-effect by decorator). `app/api/main.py` 가 `import app.mcp.lineage_tools`
로 트리거.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from app.mcp.auth import require_mcp_user_and_assert_owns
from app.mcp.harness_mcp import harness_mcp

logger = logging.getLogger(__name__)


# ─── File path matching helpers ─────────────────────────────────


def _normalize_file_path(p: str) -> str:
    """입력 file_path 를 매칭하기 좋게 정규화.

    - 백슬래시 → 슬래시 (Windows 경로 흡수)
    - 양끝 공백 / 따옴표 제거
    - 선행 `./` 제거
    - 소문자화 (case-insensitive 매칭)

    절대 경로(`/Users/.../src/x.py`) 도 그대로 둠 — 매칭은 endswith 로 처리.
    """
    if not p:
        return ""
    s = p.strip().strip('"').strip("'").replace("\\", "/")
    if s.startswith("./"):
        s = s[2:]
    return s.lower()


def _path_matches(needle: str, haystack: str) -> bool:
    """needle 과 haystack 둘 다 정규화된 상태라고 가정.

    매칭 정책 (디렉토리 경계 기준 — partial substring 방지):
      - 정확히 같음 → match
      - needle 이 haystack 의 suffix (디렉토리 경계까지) → match
        예: 'order/Service.py' 가 'src/order/Service.py' suffix → OK
            반대로 'der/Service.py' 같은 partial suffix → 차단
      - haystack 이 needle 의 suffix → match (사용자가 절대 경로 보낸 케이스)
      - needle 이 슬래시 없는 순수 파일명일 때만 basename 매칭 허용
        (사용자가 "OrderService.py" 만 입력해도 동작하도록. 단, "der/Service.py"
         같이 부분 경로가 있으면 false positive 방지를 위해 basename 매칭 안 함.)

    이 정책의 trade-off:
      - false positive 줄임 (디렉토리가 명시되면 진짜 디렉토리 매칭만)
      - false negative 일부 발생 가능 — 그래도 명확한 hit 만 반환하는 게 신뢰 ↑
    """
    if not needle or not haystack:
        return False
    if needle == haystack:
        return True
    # suffix 매칭 — 디렉토리 경계 (`/`) 까지. 부분 substring 차단.
    if haystack.endswith("/" + needle) or needle.endswith("/" + haystack):
        return True
    # basename 안전망 — needle 이 순수 파일명일 때만.
    # ("Service.py" 만 보내면 매칭, "der/Service.py" 같은 partial path 는 차단.)
    if "/" not in needle:
        h_base = haystack.rsplit("/", 1)[-1]
        if needle == h_base:
            return True
    return False


# ─── Story ID normalization ────────────────────────────────────


_STORY_ID_RE = re.compile(r"story[-_\s]?(\d+)[._\-](\d+)", re.IGNORECASE)


def _story_id_candidates_from_any(raw: str) -> List[str]:
    """사용자 또는 에이전트가 보낸 임의의 Story id 표현 → Neo4j 후보 IDs.

    수용 형식:
      - 'Story-01.1' / 'Story 01.1' / '[Story-01.1]'
      - 'story_01_1' / 'story_1_1' / 'story_1_01' / 'story_01_01'
      - 'Story 1.1', 'story-1-1'

    Returns: zero-pad / non-zero-pad 4가지 후보 (Neo4j 에 실제 어느 형식이
    저장되어 있어도 매칭되도록 — query_repository._story_id_candidates 와 동일 정책).
    """
    if not raw:
        return []
    m = _STORY_ID_RE.search(raw)
    if not m:
        return []
    major, minor = int(m.group(1)), int(m.group(2))
    forms = {
        f"story_{major}_{minor}",
        f"story_{major:02d}_{minor}",
        f"story_{major}_{minor:02d}",
        f"story_{major:02d}_{minor:02d}",
    }
    return sorted(forms)


# ─── Design lineage (Neo4j) ────────────────────────────────────


_DESIGN_LABELS = ("Entity", "Aggregate", "DomainEntity", "ArchService", "ArchDatabase")


_TRACE_UPSTREAM_CYPHER = """\
// 입력 node 찾기 — design 노드 또는 Story 본인.
OPTIONAL MATCH (n {id: $node_id, project: $project})
WHERE n:Entity OR n:Aggregate OR n:DomainEntity OR n:ArchService OR n:ArchDatabase OR n:Story
WITH n LIMIT 1
WHERE n IS NOT NULL

// design 노드면 DERIVED_FROM → Story / story 자기 자신이면 그대로.
OPTIONAL MATCH (n)-[r:DERIVED_FROM]->(story:Story {project: $project})
WITH n,
     CASE WHEN n:Story THEN [n] ELSE collect(DISTINCT story) END AS stories,
     collect(DISTINCT r) AS lineage_rels

// 각 Story 의 Epic + IMPLEMENTED_ON Screen
UNWIND stories AS s
OPTIONAL MATCH (epic:Epic)-[:CONTAINS]->(s)
OPTIONAL MATCH (s)-[:IMPLEMENTED_ON]->(screen:Screen)
WITH n, stories, lineage_rels,
     collect(DISTINCT epic) AS epics,
     collect(DISTINCT screen) AS screens

RETURN
    n {.id, .name, .description, label: labels(n)[0]} AS node,
    [s IN stories WHERE s IS NOT NULL | s {.id, .name, .description}] AS stories,
    [r IN lineage_rels WHERE r IS NOT NULL | {
        story_id: endNode(r).id,
        confidence: r.confidence,
        quote: r.quote
    }] AS lineage_edges,
    [e IN epics WHERE e IS NOT NULL | e {.id, .name, .description}] AS epics,
    [sc IN screens WHERE sc IS NOT NULL | sc {.id, .name}] AS screens
"""


# list_design_nodes pagination (2026-05 — 미팅 로그 누적으로 project 당 design 노드
# 수가 수천 이상 증가 가능). 한 cypher 호출에서 total count + page 동시 반환.
#
# [Cypher 패턴]
# 1) filter 만족하는 n 모두 collect → total
# 2) SKIP $offset LIMIT $limit 로 page slice
# 한 query 안에서 total + items 동시 산출. 두 번 호출보다 빠르고, race 없음.
_LIST_DESIGN_NODES_CYPHER = """\
MATCH (n)
WHERE n.project = $project
  AND (n:Entity OR n:Aggregate OR n:DomainEntity OR n:ArchService OR n:ArchDatabase)
  AND ($kind_label IS NULL OR labels(n)[0] = $kind_label)
OPTIONAL MATCH (n)-[r:DERIVED_FROM]->(story:Story {project: $project})
WITH n, collect(DISTINCT CASE WHEN story IS NOT NULL THEN
    {id: story.id, name: story.name, confidence: r.confidence}
    ELSE NULL END) AS stories_raw
WITH n, [s IN stories_raw WHERE s IS NOT NULL] AS stories
WITH collect({
    id: n.id,
    name: coalesce(n.name, ''),
    description: coalesce(n.description, ''),
    kind: labels(n)[0],
    stories: stories
}) AS all_items
WITH all_items, size(all_items) AS total
UNWIND all_items AS item
WITH item, total
ORDER BY item.kind, item.name
SKIP $offset
LIMIT $limit
RETURN collect(item) AS items, total
"""


# find_spec_for_file upstream Story 자동 포함 (2026-05) — design 노드들의
# DERIVED_FROM 을 bulk fetch 해서 매치 결과에 inline. agent 의 추가 round-trip
# (별도 trace_upstream 호출) 제거.
#
# 입력: design kind 매치들의 id list. Story 라벨이 아니므로 design 라벨만 매치.
# 출력: { node_id → [{story_id, story_name, confidence, quote}] }
_BULK_UPSTREAM_CYPHER = """\
UNWIND $node_ids AS nid
OPTIONAL MATCH (src {id: nid, project: $project})
WHERE src:Entity OR src:Aggregate OR src:DomainEntity
   OR src:ArchService OR src:ArchDatabase
OPTIONAL MATCH (src)-[r:DERIVED_FROM]->(story:Story {project: $project})
WITH nid, collect(DISTINCT CASE WHEN story IS NOT NULL THEN {
    story_id: story.id,
    story_name: story.name,
    confidence: r.confidence,
    quote: r.quote
} ELSE NULL END) AS stories_raw
RETURN nid AS node_id, [s IN stories_raw WHERE s IS NOT NULL] AS stories
"""


_GET_STORY_CYPHER = """\
MATCH (s:Story {project: $project})
WHERE s.id IN $candidates
WITH s LIMIT 1
OPTIONAL MATCH (epic:Epic)-[:CONTAINS]->(s)
OPTIONAL MATCH (s)-[:IMPLEMENTED_ON]->(screen:Screen)
OPTIONAL MATCH (src)-[r:DERIVED_FROM]->(s)
WHERE src:Entity OR src:Aggregate OR src:DomainEntity OR src:ArchService OR src:ArchDatabase
RETURN
    s {.id, .name, .description, .acceptance_criteria} AS story,
    epic {.id, .name, .description} AS epic,
    [sc IN collect(DISTINCT screen) WHERE sc IS NOT NULL | sc {.id, .name}] AS screens,
    [x IN collect(DISTINCT {node: src, rel: r}) WHERE x.node IS NOT NULL | {
        id: x.node.id,
        name: x.node.name,
        kind: labels(x.node)[0],
        confidence: x.rel.confidence,
        quote: x.rel.quote
    }] AS derived_nodes
"""


# search_spec — fulltext index 우선, CONTAINS fallback.
#
# [Why two paths]
# - 미팅 로그 누적으로 project 당 spec 노드 수가 10K~100K 가능
# - CONTAINS 풀스캔은 그 시점 응답 1초+ 위험
# - fulltext (Lucene) 인덱스 사용 시 ms 단위, 관련도 score 정렬 부가 이득
# - 단, 인덱스 미존재 / 쿼리 문법 오류 / 신규 환경 등 안전망 필요 → CONTAINS fallback
#
# [Project 격리]
# fulltext 인덱스는 라벨/속성 기반 — project 필터링은 결과 후 WHERE 로.
# 사용자 cross-tenant 누설 위험 차단.
_SEARCH_SPEC_FULLTEXT_CYPHER = """\
CALL db.index.fulltext.queryNodes('spec_text_search', $lucene_q) YIELD node, score
WHERE node.project = $project
  AND (
    ($wants_story     AND node:Story) OR
    ($wants_aggregate AND node:Aggregate) OR
    ($wants_entity    AND (node:Entity OR node:DomainEntity)) OR
    ($wants_service   AND node:ArchService) OR
    ($wants_database  AND node:ArchDatabase) OR
    ($wants_api       AND node:API) OR
    ($wants_epic      AND node:Epic)
  )
RETURN
    node.id AS id,
    coalesce(node.name, '') AS name,
    coalesce(node.description, '') AS description,
    labels(node)[0] AS kind,
    score
ORDER BY score DESC
LIMIT $limit
"""

_SEARCH_SPEC_CONTAINS_CYPHER = """\
MATCH (n)
WHERE n.project = $project
  AND (
    ($wants_story     AND n:Story) OR
    ($wants_aggregate AND n:Aggregate) OR
    ($wants_entity    AND (n:Entity OR n:DomainEntity)) OR
    ($wants_service   AND n:ArchService) OR
    ($wants_database  AND n:ArchDatabase) OR
    ($wants_api       AND n:API) OR
    ($wants_epic      AND n:Epic)
  )
  AND (
    toLower(coalesce(n.name, '')) CONTAINS $q OR
    toLower(coalesce(n.description, '')) CONTAINS $q
  )
RETURN
    n.id AS id,
    coalesce(n.name, '') AS name,
    coalesce(n.description, '') AS description,
    labels(n)[0] AS kind
ORDER BY kind, name
LIMIT $limit
"""


# Lucene 특수문자 escape — 사용자 검색어를 fulltext.queryNodes 에 안전하게 전달.
# 미escape 시 사용자가 `:`, `(`, `*` 등 입력하면 Lucene parse error.
_LUCENE_SPECIAL = r'+-&|!(){}[]^"~*?:\/'


def _escape_lucene(q: str) -> str:
    """Lucene 쿼리 문자열 escape. 모든 특수문자 앞에 백슬래시.

    Returns escaped + wildcard-wrapped 쿼리 (`*token*`) — substring 매칭 모방.
    공백 분리 multi-word 는 AND 결합.

    예:
      "주문 처리"   → `*주문* AND *처리*`
      "Order.id"   → `*Order\\.id*`
      ""           → ""
    """
    if not q:
        return ""
    tokens = [t for t in q.strip().split() if t]
    escaped: List[str] = []
    for tok in tokens:
        # 각 특수문자 앞에 \\
        chars = []
        for ch in tok:
            if ch in _LUCENE_SPECIAL:
                chars.append("\\" + ch)
            else:
                chars.append(ch)
        escaped.append("*" + "".join(chars) + "*")
    return " AND ".join(escaped)


# ─── Tools ──────────────────────────────────────────────────────


@harness_mcp.tool(name="find_spec_for_file")
async def find_spec_for_file(
    project_name: str, file_path: str
) -> Dict[str, Any]:
    """파일 경로 → 이 파일을 구현하는 spec 항목들 (Story / Aggregate / API / Service / Entity).

    [언제 호출하나]
    Cursor / Claude Code 에서 사용자가 코드 파일을 열거나 편집할 때. AI 에이전트가
    이 도구를 호출해서 "이 파일이 어떤 spec 의 구현인지" 즉시 확인하고, 답변/수정
    제안을 spec 컨텍스트와 정합하게 함.

    [데이터 출처]
    가장 최근의 analyzeLineage 결과 (LineageResult) 를 참조. 사용자가 한 번도
    analyzeLineage 안 돌렸으면 빈 결과 + 'no_lineage_analysis' reason 반환.

    Args:
        project_name: 본인 소유 프로젝트.
        file_path: 코드 파일 상대/절대 경로. 예: "src/order/OrderService.py",
            "/Users/me/repo/src/order/OrderService.py". 매칭은 정확/suffix/basename
            중 어느 하나라도 만족하면 hit.

    Returns:
        {
            "matches": [
                {
                    "kind": "aggregate" | "story" | "api" | "service",
                    "id": "Order",
                    "name": "Order",
                    "description": "...",
                    "matched_impl": { "filePath": "...", "confidence": "high", "reason": "..." },
                    "endpoint": "POST /orders" (api 만),
                    "method": "POST" (api 만),
                    "tech_stack": "fastapi" (service 만)
                },
                ...
            ],
            "reason": "ok" | "no_lineage_analysis" | "no_match",
            "lineage_id": "lineage-foo-123-abc" | None,
            "lineage_saved_at": 1234567890 | None,
            "hint": "..." (reason != 'ok' 일 때)
        }
    """
    await require_mcp_user_and_assert_owns(project_name)
    from app.service import lineage_repository

    needle = _normalize_file_path(file_path)
    if not needle:
        return {
            "matches": [],
            "reason": "no_match",
            "lineage_id": None,
            "lineage_saved_at": None,
            "hint": "file_path 가 비어있습니다.",
        }

    last = await lineage_repository.get_last_lineage(project_name)
    if not last or not last.data:
        return {
            "matches": [],
            "reason": "no_lineage_analysis",
            "lineage_id": None,
            "lineage_saved_at": None,
            "hint": (
                "이 프로젝트의 lineage 분석 결과가 없습니다. "
                "먼저 analyzeLineage 를 실행해서 spec ↔ code 매핑을 생성하세요."
            ),
        }

    data = last.data
    matches: List[Dict[str, Any]] = []

    def _scan(items: List[Any], kind: str) -> None:
        for item in items or []:
            for impl in item.implementations or []:
                impl_path = _normalize_file_path(impl.filePath)
                if _path_matches(needle, impl_path):
                    entry: Dict[str, Any] = {
                        "kind": kind,
                        "id": item.id,
                        "name": item.name,
                        "description": item.description or "",
                        "matched_impl": {
                            "filePath": impl.filePath,
                            "confidence": impl.confidence,
                            "reason": impl.reason or "",
                            "verified": impl.verified,
                            "repoUrl": impl.repoUrl,
                            "role": impl.role,
                        },
                    }
                    # kind 별 추가 필드 — agent 가 더 풍부한 컨텍스트 받을 수 있게
                    if kind == "api":
                        entry["endpoint"] = item.endpoint
                        entry["method"] = item.method
                    if kind == "service" and item.tech_stack:
                        entry["tech_stack"] = item.tech_stack
                    if kind == "service" and item.type:
                        entry["service_type"] = item.type
                    matches.append(entry)
                    # 같은 spec 항목의 두 번째 impl 매칭은 dedupe — 한 파일 = 한 매치
                    break

    _scan(data.stories, "story")
    _scan(data.aggregates, "aggregate")
    _scan(data.apis, "api")
    _scan(data.services, "service")

    # [2026-05] 매칭된 design kind (aggregate / service) 의 upstream Story 를
    # bulk fetch 해서 inline. agent 의 추가 trace_upstream round-trip 제거.
    # API kind 는 PRD Story 와 직접 DERIVED_FROM 관계가 약하므로 skip.
    design_match_ids = sorted({
        m["id"] for m in matches
        if m["kind"] in ("aggregate", "service") and m.get("id")
    })
    if design_match_ids:
        from app.clients import neo4j_client
        try:
            rows = await neo4j_client.run_cypher(
                _BULK_UPSTREAM_CYPHER,
                {"project": project_name, "node_ids": design_match_ids},
            )
            upstream_by_id: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                upstream_by_id[r.get("node_id")] = r.get("stories") or []
            for m in matches:
                if m["id"] in upstream_by_id:
                    m["stories"] = upstream_by_id[m["id"]]
        except Exception as e:  # noqa: BLE001
            # upstream fetch 실패는 매칭 자체를 막지 않음 — 기본 매치만 반환.
            logger.warning(
                "find_spec_for_file upstream fetch 실패 (project=%s): %s",
                project_name, e,
            )

    return {
        "matches": matches,
        "reason": "ok" if matches else "no_match",
        "lineage_id": last.id,
        "lineage_saved_at": last.saved_at,
        "hint": (
            "이 파일에 매칭되는 spec 항목이 없습니다. "
            "analyzeLineage 가 최신 코드에서 이 파일을 발견하지 못했을 수 있습니다."
        )
        if not matches
        else None,
    }


@harness_mcp.tool(name="trace_upstream")
async def trace_upstream(
    project_name: str, node_id: str
) -> Optional[Dict[str, Any]]:
    """Design 노드 (Entity/Aggregate/Service/Database) 또는 Story → upstream 체인 추적.

    [언제 호출하나]
    Agent 가 특정 design 노드의 출처 (어떤 PRD Story 에서 파생됐는지, 그 Story 가
    어느 Epic 에 속하는지) 를 알아야 할 때. 예: "이 Aggregate 가 왜 만들어졌는지
    설명해 줘" 같은 질문.

    Args:
        project_name: 본인 소유 프로젝트.
        node_id: design 노드 id (예: "Order" 또는 "Aggregate-Order") 또는 Story id
            (예: "story_01_1"). Story id 는 zero-pad 변형 자동 흡수.

    Returns:
        {
            "node": { "id": ..., "name": ..., "description": ..., "label": "Aggregate" },
            "stories": [{ "id": "story_01_1", "name": "주문 처리", "description": "..." }],
            "lineage_edges": [{ "story_id": "story_01_1", "confidence": "direct", "quote": "..." }],
            "epics": [{ "id": "epic_01", "name": "주문 도메인", ... }],
            "screens": [{ "id": "screen_01", "name": "주문 화면" }]
        }
        또는 None (node 못 찾음).
    """
    await require_mcp_user_and_assert_owns(project_name)
    from app.clients import neo4j_client

    # design 노드 id 그대로 시도 + story id 변형 시도 (양쪽 다 받기 위함)
    records = await neo4j_client.run_cypher(
        _TRACE_UPSTREAM_CYPHER,
        {"project": project_name, "node_id": node_id},
    )
    if not records:
        # Story id 같은데 정규화가 다를 가능성 — 후보 IDs 로 retry
        candidates = _story_id_candidates_from_any(node_id)
        for cand in candidates:
            if cand == node_id:
                continue
            records = await neo4j_client.run_cypher(
                _TRACE_UPSTREAM_CYPHER,
                {"project": project_name, "node_id": cand},
            )
            if records:
                break

    if not records:
        return None
    row = records[0]
    node = row.get("node")
    if not node:
        return None
    return {
        "node": node,
        "stories": row.get("stories") or [],
        "lineage_edges": row.get("lineage_edges") or [],
        "epics": row.get("epics") or [],
        "screens": row.get("screens") or [],
    }


@harness_mcp.tool(name="list_design_nodes")
async def list_design_nodes(
    project_name: str,
    kind: Optional[str] = None,
    offset: int = 0,
    limit: int = 100,
) -> Dict[str, Any]:
    """프로젝트의 design 노드 (Entity / Aggregate / Service / Database) 목록 +
    upstream Story 요약 (페이지네이션 지원).

    [언제 호출하나]
    Agent 가 프로젝트 전체 design 의 윤곽을 파악할 때. 예: "이 프로젝트는 어떤
    Aggregate 들로 구성돼 있어?". 미팅 로그 누적으로 design 노드 수가 수백~수천
    이상으로 늘어날 수 있어 페이지네이션 필수.

    Args:
        project_name: 본인 소유 프로젝트.
        kind: 'entity' | 'aggregate' | 'domain_entity' | 'service' | 'database' | None (전체).
            대소문자 무시.
        offset: 페이지 시작 인덱스 (기본 0). 음수는 0 으로 clamp.
        limit: 페이지 크기 (기본 100, 최대 500). 1~500 범위로 clamp.

    Returns:
        {
            "items": [
                {
                    "id": "Order",
                    "name": "Order",
                    "description": "...",
                    "kind": "Aggregate",
                    "stories": [{ "id": "story_01_1", "name": "...", "confidence": "direct" }]
                },
                ...
            ],
            "total": 487,       # 필터 만족하는 전체 개수 (페이지네이션 무관)
            "offset": 0,
            "limit": 100,
            "has_more": true    # offset + limit < total
        }
        잘못된 kind 입력 시 items=[], total=0.
    """
    await require_mcp_user_and_assert_owns(project_name)
    from app.clients import neo4j_client

    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 100), 500))

    # kind 입력 정규화 → Neo4j label
    kind_label: Optional[str] = None
    if kind:
        m = (kind or "").strip().lower()
        kind_map = {
            "entity": "Entity",
            "aggregate": "Aggregate",
            "domain_entity": "DomainEntity",
            "domainentity": "DomainEntity",
            "service": "ArchService",
            "archservice": "ArchService",
            "database": "ArchDatabase",
            "archdatabase": "ArchDatabase",
            "db": "ArchDatabase",
        }
        kind_label = kind_map.get(m)
        if kind_label is None:
            # 잘못된 kind — 빈 결과 반환 (전체 fallback 안 함, 사용자 의도 보존)
            return {
                "items": [],
                "total": 0,
                "offset": safe_offset,
                "limit": safe_limit,
                "has_more": False,
            }

    records = await neo4j_client.run_cypher(
        _LIST_DESIGN_NODES_CYPHER,
        {
            "project": project_name,
            "kind_label": kind_label,
            "offset": safe_offset,
            "limit": safe_limit,
        },
    )
    if not records:
        # cypher 가 매치 0건이면 빈 row 반환 (collect → []). 안전망.
        return {
            "items": [],
            "total": 0,
            "offset": safe_offset,
            "limit": safe_limit,
            "has_more": False,
        }
    row = records[0]
    raw_items = row.get("items") or []
    total = int(row.get("total") or 0)
    items = [
        {
            "id": r.get("id"),
            "name": r.get("name") or "",
            "description": r.get("description") or "",
            "kind": r.get("kind") or "",
            "stories": r.get("stories") or [],
        }
        for r in raw_items
        if r.get("id")
    ]
    return {
        "items": items,
        "total": total,
        "offset": safe_offset,
        "limit": safe_limit,
        # has_more 은 limit 윈도우 기준 — cypher 가 page 를 정확히 SKIP/LIMIT 으로
        # 잘랐다고 가정. 다음 page 가 가능한가만 본다.
        "has_more": (safe_offset + safe_limit) < total,
    }


@harness_mcp.tool(name="get_story")
async def get_story(
    project_name: str, story_id: str
) -> Optional[Dict[str, Any]]:
    """Story 상세 + 이 Story 에서 파생된 design 노드들 + 화면들.

    [언제 호출나]
    Agent 가 특정 Story 의 implementation 영향 범위를 보고 싶을 때. 예: "Story-01.1
    구현하려면 어떤 코드 변경이 필요한지 알려줘".

    Args:
        project_name: 본인 소유 프로젝트.
        story_id: 'Story-01.1' / '[Story 1.1]' / 'story_01_1' / 'story_1_1' 모두 흡수.

    Returns:
        {
            "story": { "id": ..., "name": ..., "description": ..., "acceptance_criteria": ... },
            "epic": { "id": ..., "name": ... } | None,
            "screens": [{ "id": ..., "name": "주문 화면" }],
            "derived_nodes": [
                {
                    "id": "Order",
                    "name": "Order",
                    "kind": "Aggregate",
                    "confidence": "direct",
                    "quote": "PRD 원문 발췌"
                },
                ...
            ]
        }
        또는 None (Story 없음).
    """
    await require_mcp_user_and_assert_owns(project_name)
    from app.clients import neo4j_client

    candidates = _story_id_candidates_from_any(story_id)
    if not candidates:
        # raw id 그대로도 시도 (이미 정확한 Neo4j id 일 수 있음)
        candidates = [story_id]

    records = await neo4j_client.run_cypher(
        _GET_STORY_CYPHER,
        {"project": project_name, "candidates": candidates},
    )
    if not records:
        return None
    row = records[0]
    story = row.get("story")
    if not story or not story.get("id"):
        return None
    return {
        "story": story,
        "epic": row.get("epic"),
        "screens": row.get("screens") or [],
        "derived_nodes": row.get("derived_nodes") or [],
    }


@harness_mcp.tool(name="search_spec")
async def search_spec(
    project_name: str,
    query: str,
    kinds: Optional[List[str]] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """spec 항목 (Story / Epic / Aggregate / Entity / Service / Database / API) 텍스트 검색.

    [언제 호출하나]
    Agent 가 자연어 의도 ("주문 처리 관련 spec", "결제 API") 를 spec 항목으로
    변환할 때. 매칭은 name + description 양쪽.

    [성능 — 2026-05]
    fulltext index (spec_text_search, domain_indexes.py 가 부팅 시 ensure) 1차.
    Lucene 기반이라 미팅 로그가 수십 회 누적된 대형 프로젝트 (10K+ 노드) 에서도
    ms 단위. 결과는 관련도 score 내림차순.

    인덱스 없거나 fulltext 호출 실패 시 CONTAINS 폴백 — 항상 동작 보장.

    Args:
        project_name: 본인 소유 프로젝트.
        query: 검색어 (1자 이상). 공백 분리 다중 단어는 모두 포함 (AND).
        kinds: 'story' / 'epic' / 'aggregate' / 'entity' / 'service' / 'database' / 'api'
            중 일부. None 또는 빈 list 면 전체 종류.
        limit: 최대 결과 수 (기본 50, 최대 200).

    Returns:
        [
            {
                "id": "...",
                "name": "...",
                "description": "...",
                "kind": "Story" | "Aggregate" | ...,
                "score": 1.23,            # fulltext path 만. CONTAINS 폴백 시 누락.
                "search_method": "fulltext" | "contains"
            }
        ]
        결과 없으면 빈 list. fulltext path 는 score 내림차순, CONTAINS path 는 kind+name.
    """
    await require_mcp_user_and_assert_owns(project_name)
    from app.clients import neo4j_client

    q_raw = (query or "").strip()
    if not q_raw:
        return []
    q_lower = q_raw.lower()

    # kinds 정규화 — 어느 라벨을 포함할지 boolean flags 로 전달.
    requested = {(k or "").strip().lower() for k in (kinds or [])}
    if not requested:
        # default: 모든 종류
        requested = {"story", "epic", "aggregate", "entity", "service", "database", "api"}

    safe_limit = max(1, min(int(limit or 50), 200))

    common_params = {
        "project": project_name,
        "wants_story": "story" in requested,
        "wants_epic": "epic" in requested,
        "wants_aggregate": "aggregate" in requested,
        "wants_entity": "entity" in requested,
        "wants_service": "service" in requested,
        "wants_database": "database" in requested,
        "wants_api": "api" in requested,
        "limit": safe_limit,
    }

    # ─── 1차: fulltext index 시도 ───
    lucene_q = _escape_lucene(q_raw)
    if lucene_q:
        try:
            records = await neo4j_client.run_cypher(
                _SEARCH_SPEC_FULLTEXT_CYPHER,
                {**common_params, "lucene_q": lucene_q},
            )
            # 인덱스 존재 + 정상 → fulltext 결과 반환 (빈 결과도 인정 — CONTAINS 폴백 안 함)
            return [
                {
                    "id": r.get("id"),
                    "name": r.get("name") or "",
                    "description": r.get("description") or "",
                    "kind": r.get("kind") or "",
                    "score": float(r.get("score") or 0.0),
                    "search_method": "fulltext",
                }
                for r in records
                if r.get("id")
            ]
        except Exception as e:  # noqa: BLE001
            # 인덱스 미존재 / Lucene parse 실패 등 — CONTAINS 폴백.
            # (운영 보강: domain_indexes 가 fulltext 만들지만 일부 환경에서 미생성 가능.)
            logger.warning(
                "search_spec fulltext 실패 — CONTAINS 폴백 (project=%s, q=%r): %s",
                project_name, q_raw, e,
            )

    # ─── 2차: CONTAINS 폴백 ───
    records = await neo4j_client.run_cypher(
        _SEARCH_SPEC_CONTAINS_CYPHER,
        {**common_params, "q": q_lower},
    )
    return [
        {
            "id": r.get("id"),
            "name": r.get("name") or "",
            "description": r.get("description") or "",
            "kind": r.get("kind") or "",
            "search_method": "contains",
        }
        for r in records
        if r.get("id")
    ]
