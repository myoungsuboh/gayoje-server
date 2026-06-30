"""
Graph traversal repository — Trace (upstream lineage) v1.

[배경]
PR1~PR11 은 Neo4j 를 "label 별 collect" 수준으로만 활용 → 그래프 DB 의 핵심인
multi-hop traversal 미사용. 이 모듈은 그래프의 BFS-style 역추적 (upstream
lineage) 을 1차 기능으로 추가한다.

[지원 시작 노드 — v1]
- API           : 가장 흔한 질문 "이 API 어디서?" 5-hop trace
- Story         : Epic 부터 위로
- Epic          : PRD/CPS 까지
- Problem       : CPS/Meeting 까지
- Solution      : Problem 거쳐 위로 (graph label 은 :Solution — 시맨틱 의미는 resolution)

(보류) Aggregate / DomainEvent / ArchService — 이들은 createDesign 의 출력이라
회의 단일 출처가 모호함. 후속 PR 에서 REPRESENTS 관계 도입 시 확장.

[설계 원칙]
1. **Per-kind dispatch** — 시작 노드 종류별 cypher 분리. 가독성 우선.
2. **Project 격리 강제** — 모든 OPTIONAL MATCH 에 `{project: $project}` 명시.
3. **Best-effort** — 어떤 hop 이 끊겨도 cypher 는 항상 한 row 반환. 끊긴 부분은
   해당 collection 만 빔.
4. **Null 안전** — `collect(DISTINCT x)` 는 x 가 null 이면 빈 컬렉션 생성.
5. **Label 결정** — 노드 종류별로 가장 의미있는 텍스트를 라벨로 (API 는 method+
   endpoint, Story 는 summary 등). `_to_artifact_ref()` 가 이 변환을 담당.

[보안]
모든 cypher 는 `$param` 바인딩만 사용. LLM 출력 보간 0건.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# ===== Pydantic 응답 모델 =====


class ArtifactRef(BaseModel):
    """
    그래프의 한 노드를 UI 표시용으로 정규화한 ref.

    Fields:
        kind     : 'api' | 'story' | 'epic' | 'problem' | 'resolution' |
                   'prd' | 'cps' | 'meeting' | 'aggregate' | 'event' | 'service'
        id       : Neo4j 노드의 `id` property (LLM 가 부여한 식별자)
        label    : UI 표시용 짧은 텍스트. 노드 종류별로 다른 property 에서 추출:
                   - API        → "{method} {endpoint}"
                   - Story/Epic → summary
                   - Problem/Solution → summary
                   - PRD/CPS    → version
                   - Meeting    → "{version} ({date})"
        project  : 노드의 project property (멀티테넌시 검증 용도, FE 는 무시)
    """
    kind: str
    id: str
    label: str
    project: Optional[str] = None


class UpstreamTrace(BaseModel):
    """
    upstream 역추적 결과 — 한 시작 노드에서 위로 거슬러 모은 카테고리별 노드들.

    Fields:
        target         : 시작 노드 (조회 대상).
        stories~meetings : 카테고리별 collect (순서 = "위로 갈수록 더 상류").
                          노드가 없는 카테고리는 빈 리스트.
        not_found      : 시작 노드 자체를 못 찾았으면 True (404 분기용).
    """
    target: Optional[ArtifactRef] = None
    stories: List[ArtifactRef] = []
    epics: List[ArtifactRef] = []
    problems: List[ArtifactRef] = []
    resolutions: List[ArtifactRef] = []
    prd_documents: List[ArtifactRef] = []
    cps_documents: List[ArtifactRef] = []
    meetings: List[ArtifactRef] = []
    not_found: bool = False


# ===== 시작 노드 종류 =====


# UI / FE 가 보내는 kind 문자열 → cypher dispatch key.
# 가능한 값을 enum 대신 화이트리스트 set 으로 검증 (Pydantic 의존성 가벼움).
SUPPORTED_TRACE_KINDS: frozenset = frozenset(
    {"api", "story", "epic", "problem", "resolution"}
)


# ===== Cypher (per-kind upstream) =====
#
# 모든 cypher 의 RETURN 형식 통일:
#   RETURN target, stories, epics, problems, resolutions, prds, cps_raw, logs_raw
#
# `cps_raw` / `logs_raw` 는 list concat 직후라 중복 가능 → Python 에서 dedup.


_TRACE_FROM_API = """\
MATCH (start:API {id: $start_id, project: $project})
OPTIONAL MATCH (start)-[:IMPLEMENTS]->(story:Story {project: $project})
OPTIONAL MATCH (story)<-[:CONTAINS]-(epic:Epic {project: $project})
OPTIONAL MATCH (epic)-[:SOLVES]->(prob:Problem {project: $project})
OPTIONAL MATCH (prob)<-[:SOLVES]-(res:Solution {project: $project})
OPTIONAL MATCH (epic)-[:EXTRACTED_FROM]->(prd:PRD_Document {project: $project})
OPTIONAL MATCH (prob)-[:EXTRACTED_FROM]->(cps_p:CPS_Document {project: $project})
OPTIONAL MATCH (prd)-[:BASED_ON]->(cps_d:CPS_Document {project: $project})
OPTIONAL MATCH (cps_p)-[:EXTRACTED_FROM]->(log_p:Meeting_Log {project: $project})
OPTIONAL MATCH (cps_d)-[:EXTRACTED_FROM]->(log_d:Meeting_Log {project: $project})
RETURN
    start AS target,
    collect(DISTINCT story) AS stories,
    collect(DISTINCT epic) AS epics,
    collect(DISTINCT prob) AS problems,
    collect(DISTINCT res) AS resolutions,
    collect(DISTINCT prd) AS prds,
    collect(DISTINCT cps_p) + collect(DISTINCT cps_d) AS cps_raw,
    collect(DISTINCT log_p) + collect(DISTINCT log_d) AS logs_raw
"""


_TRACE_FROM_STORY = """\
MATCH (start:Story {id: $start_id, project: $project})
OPTIONAL MATCH (start)<-[:CONTAINS]-(epic:Epic {project: $project})
OPTIONAL MATCH (epic)-[:SOLVES]->(prob:Problem {project: $project})
OPTIONAL MATCH (prob)<-[:SOLVES]-(res:Solution {project: $project})
OPTIONAL MATCH (epic)-[:EXTRACTED_FROM]->(prd:PRD_Document {project: $project})
OPTIONAL MATCH (prob)-[:EXTRACTED_FROM]->(cps_p:CPS_Document {project: $project})
OPTIONAL MATCH (prd)-[:BASED_ON]->(cps_d:CPS_Document {project: $project})
OPTIONAL MATCH (cps_p)-[:EXTRACTED_FROM]->(log_p:Meeting_Log {project: $project})
OPTIONAL MATCH (cps_d)-[:EXTRACTED_FROM]->(log_d:Meeting_Log {project: $project})
RETURN
    start AS target,
    [] AS stories,
    collect(DISTINCT epic) AS epics,
    collect(DISTINCT prob) AS problems,
    collect(DISTINCT res) AS resolutions,
    collect(DISTINCT prd) AS prds,
    collect(DISTINCT cps_p) + collect(DISTINCT cps_d) AS cps_raw,
    collect(DISTINCT log_p) + collect(DISTINCT log_d) AS logs_raw
"""


_TRACE_FROM_EPIC = """\
MATCH (start:Epic {id: $start_id, project: $project})
OPTIONAL MATCH (start)-[:SOLVES]->(prob:Problem {project: $project})
OPTIONAL MATCH (prob)<-[:SOLVES]-(res:Solution {project: $project})
OPTIONAL MATCH (start)-[:EXTRACTED_FROM]->(prd:PRD_Document {project: $project})
OPTIONAL MATCH (prob)-[:EXTRACTED_FROM]->(cps_p:CPS_Document {project: $project})
OPTIONAL MATCH (prd)-[:BASED_ON]->(cps_d:CPS_Document {project: $project})
OPTIONAL MATCH (cps_p)-[:EXTRACTED_FROM]->(log_p:Meeting_Log {project: $project})
OPTIONAL MATCH (cps_d)-[:EXTRACTED_FROM]->(log_d:Meeting_Log {project: $project})
RETURN
    start AS target,
    [] AS stories,
    [] AS epics,
    collect(DISTINCT prob) AS problems,
    collect(DISTINCT res) AS resolutions,
    collect(DISTINCT prd) AS prds,
    collect(DISTINCT cps_p) + collect(DISTINCT cps_d) AS cps_raw,
    collect(DISTINCT log_p) + collect(DISTINCT log_d) AS logs_raw
"""


_TRACE_FROM_PROBLEM = """\
MATCH (start:Problem {id: $start_id, project: $project})
OPTIONAL MATCH (start)<-[:SOLVES]-(res:Solution {project: $project})
OPTIONAL MATCH (start)-[:EXTRACTED_FROM]->(cps:CPS_Document {project: $project})
OPTIONAL MATCH (cps)-[:EXTRACTED_FROM]->(log:Meeting_Log {project: $project})
RETURN
    start AS target,
    [] AS stories,
    [] AS epics,
    [] AS problems,
    collect(DISTINCT res) AS resolutions,
    [] AS prds,
    collect(DISTINCT cps) AS cps_raw,
    collect(DISTINCT log) AS logs_raw
"""


_TRACE_FROM_RESOLUTION = """\
MATCH (start:Solution {id: $start_id, project: $project})
OPTIONAL MATCH (start)-[:SOLVES]->(prob:Problem {project: $project})
OPTIONAL MATCH (start)-[:EXTRACTED_FROM]->(cps:CPS_Document {project: $project})
OPTIONAL MATCH (prob)-[:EXTRACTED_FROM]->(cps_p:CPS_Document {project: $project})
OPTIONAL MATCH (cps)-[:EXTRACTED_FROM]->(log:Meeting_Log {project: $project})
OPTIONAL MATCH (cps_p)-[:EXTRACTED_FROM]->(log_p:Meeting_Log {project: $project})
RETURN
    start AS target,
    [] AS stories,
    [] AS epics,
    collect(DISTINCT prob) AS problems,
    [] AS resolutions,
    [] AS prds,
    collect(DISTINCT cps) + collect(DISTINCT cps_p) AS cps_raw,
    collect(DISTINCT log) + collect(DISTINCT log_p) AS logs_raw
"""


_CYPHER_BY_KIND: Dict[str, str] = {
    "api": _TRACE_FROM_API,
    "story": _TRACE_FROM_STORY,
    "epic": _TRACE_FROM_EPIC,
    "problem": _TRACE_FROM_PROBLEM,
    "resolution": _TRACE_FROM_RESOLUTION,
}


# ===== Node → ArtifactRef 변환 =====


def _props(node: Any) -> Dict[str, Any]:
    """Neo4j Node 또는 dict 를 dict 로 표준화. None 이면 빈 dict."""
    if node is None:
        return {}
    if isinstance(node, dict):
        return node
    try:
        return dict(node)  # type: ignore[arg-type]
    except Exception:
        return {}


def _node_labels(node: Any) -> Sequence[str]:
    """노드의 Neo4j label 셋. driver 가 frozenset 또는 list 로 줌. None 안전."""
    if node is None:
        return ()
    # neo4j.graph.Node 객체
    labels = getattr(node, "labels", None)
    if labels is not None:
        try:
            return tuple(labels)
        except TypeError:
            return ()
    # dict 인 경우 (예: 테스트 fake) — properties 에 _label 키가 있다고 보고 폴백
    if isinstance(node, dict):
        lbl = node.get("_label")
        return (lbl,) if lbl else ()
    return ()


# 노드 라벨 우선순위 — 한 노드에 여러 라벨이 있을 때 의미있는 것 선택.
# 현재 시스템은 한 노드 = 한 라벨이라 사실상 첫 번째만 쓰면 됨.
_LABEL_TO_KIND: Dict[str, str] = {
    "API": "api",
    "Story": "story",
    "Epic": "epic",
    "Problem": "problem",
    "Solution": "resolution",
    "PRD_Document": "prd",
    "CPS_Document": "cps",
    "Meeting_Log": "meeting",
    "Aggregate": "aggregate",
    "DomainEvent": "event",
    "ArchService": "service",
    "Entity": "entity",
    "BoundedContext": "context",
}


def _derive_label_text(kind: str, props: Dict[str, Any]) -> str:
    """
    노드 종류별로 가장 의미있는 표시 텍스트 추출.

    PRD prompts 가 emit 하는 노드 property:
      - API     : name, method, endpoint
      - Story   : summary, priority   (name 없음!)
      - Epic    : summary             (name 없음!)
      - Problem : summary
      - Solution : summary
      - PRD/CPS_Document : version, full_markdown
      - Meeting_Log      : version, date

    fallback 순서: 카테고리별 우선 필드 → name → summary → id.
    """
    if kind == "api":
        method = (props.get("method") or "").upper()
        endpoint = props.get("endpoint") or ""
        if method and endpoint:
            return f"{method} {endpoint}"
        return props.get("name") or "(API)"
    if kind == "meeting":
        ver = props.get("version") or ""
        date = props.get("date") or ""
        if ver and date:
            return f"{ver} ({date})"
        return ver or date or "(Meeting)"
    if kind in ("prd", "cps"):
        return props.get("version") or "(Document)"
    # 나머지 (story/epic/problem/resolution/aggregate/event/service/context/entity)
    return (
        props.get("name")
        or props.get("summary")
        or props.get("description")
        or "(unlabeled)"
    )


def _to_artifact_ref(node: Any) -> Optional[ArtifactRef]:
    """
    Neo4j node 를 ArtifactRef 로. 라벨 매핑 안 되거나 id 없으면 None.

    [라벨 검증]
    _LABEL_TO_KIND 화이트리스트 통과한 노드만 ArtifactRef 로 변환.
    LLM 가 변형 라벨 emit 한 경우 silently drop (응답에서 빠짐).
    """
    if node is None:
        return None
    labels = _node_labels(node)
    kind: Optional[str] = None
    for lbl in labels:
        k = _LABEL_TO_KIND.get(lbl)
        if k:
            kind = k
            break
    if kind is None:
        return None

    props = _props(node)
    nid = props.get("id")
    if not nid:
        return None

    return ArtifactRef(
        kind=kind,
        id=str(nid),
        label=_derive_label_text(kind, props),
        project=props.get("project"),
    )


def _to_refs(nodes: Any) -> List[ArtifactRef]:
    """노드 리스트 → ArtifactRef 리스트. None / 무효 노드는 제외 + id 기준 dedup."""
    if not isinstance(nodes, list):
        return []
    out: List[ArtifactRef] = []
    seen: set = set()
    for n in nodes:
        ref = _to_artifact_ref(n)
        if ref is None:
            continue
        if ref.id in seen:
            continue
        seen.add(ref.id)
        out.append(ref)
    return out


# ===== Public API =====


async def trace_upstream(
    kind: str, start_id: str, project: str, team_id: str = ""
) -> UpstreamTrace:
    """
    시작 노드 (kind, start_id) 에서 위로 거슬러 올라가 회의까지 도달.

    Args:
        kind       : 'api' | 'story' | 'epic' | 'problem' | 'resolution'
        start_id   : 노드의 id property (Neo4j 노드 id 아님 — LLM 가 부여한 식별자)
        project    : 프로젝트명 — 모든 traversal 단계에서 격리에 사용
        team_id    : 팀 컨텍스트 (빈 문자열=개인). project 를 스코프 키로 변환해
                     동명 개인/팀 프로젝트의 그래프가 섞이지 않게 한다.

    Returns:
        UpstreamTrace. 시작 노드 못 찾으면 `not_found=True`, 나머지 모두 빈 리스트.

    Raises:
        ValueError : `kind` 가 SUPPORTED_TRACE_KINDS 외부 (라우트 단에서 미리 차단해도 안전망).
    """
    k = (kind or "").lower().strip()
    if k not in SUPPORTED_TRACE_KINDS:
        raise ValueError(
            f"지원하지 않는 trace 시작 노드 종류: {kind!r}. "
            f"허용: {sorted(SUPPORTED_TRACE_KINDS)}"
        )
    if not start_id or not start_id.strip():
        raise ValueError("start_id 는 비어 있을 수 없습니다.")
    if not project or not project.strip():
        raise ValueError("project 는 비어 있을 수 없습니다.")

    # [멀티테넌시] traversal 의 모든 {project: $project} 필터가 스코프 키로 격리.
    from app.core.project_scope import scoped_project
    project = scoped_project(project, team_id)

    cypher = _CYPHER_BY_KIND[k]
    rows = await neo4j_client.run_cypher(
        cypher, {"start_id": start_id, "project": project}
    )

    if not rows:
        # MATCH (start) 가 실패하면 행 자체가 없을 수 있음 (Neo4j 동작).
        return UpstreamTrace(target=None, not_found=True)

    row = rows[0]
    target_node = row.get("target")
    target_ref = _to_artifact_ref(target_node)
    if target_ref is None:
        # 행은 있지만 target 노드가 라벨 매핑 안 되는 케이스 — 사실상 not_found 와 동일.
        return UpstreamTrace(target=None, not_found=True)

    return UpstreamTrace(
        target=target_ref,
        stories=_to_refs(row.get("stories")),
        epics=_to_refs(row.get("epics")),
        problems=_to_refs(row.get("problems")),
        resolutions=_to_refs(row.get("resolutions")),
        prd_documents=_to_refs(row.get("prds")),
        cps_documents=_to_refs(row.get("cps_raw")),
        meetings=_to_refs(row.get("logs_raw")),
        not_found=False,
    )
