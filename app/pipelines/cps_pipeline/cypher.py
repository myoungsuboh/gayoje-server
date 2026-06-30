from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.pipelines.base import is_safe_cypher_identifier
from app.pipelines.cps_pipeline.types import CpsInput

logger = logging.getLogger(__name__)

_BLOCKED_LABELS = frozenset({"Project", "User"})

# [2026-05-26] LLM 환각 silent skeleton 차단.
# spec 노드 (Problem/Solution/Epic/Story 등) 가 핵심 필드 없이 id+label 만으로
# 저장되면 fetch 시 의미 없는 stub 가 누적됨. "AI Agent" 프로젝트 사고에서
# 모든 CPS_Document 의 properties 가 비어있던 상태와 유사한 잠재 경로.
# 보수적 가드: 어떤 식별 가능 필드도 없으면 drop + warning.
_SPEC_LABELS = frozenset({
    "Problem", "Solution", "Requirement",
    "Epic", "Story", "Screen",
    "NFR", "NonFunctionalRequirement",
    "OpenQuestion", "Dependency", "OutOfScope", "Role", "Actor",
})
_SPEC_KEY_FIELDS = ("summary", "name", "description", "title", "body", "content")


def _is_meaningful_spec_node(node: Dict[str, Any]) -> bool:
    """spec 노드가 식별 가능한 본문 필드를 가지는지.

    spec 외 (CPS_Document / PRD_Document / Project / User 등) 는 자체 가드가
    있으므로 여기선 통과. spec 노드만 핵심 필드 비어있으면 drop.
    """
    label = node.get("label")
    if label not in _SPEC_LABELS:
        return True
    props = node.get("properties") or {}
    if not isinstance(props, dict):
        return False
    for key in _SPEC_KEY_FIELDS:
        val = props.get(key)
        if isinstance(val, str) and val.strip():
            return True
    return False


def _sanitize_prop_value(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, list):
        out_list: List[Any] = []
        for it in v:
            if isinstance(it, (bool, int, float, str)):
                out_list.append(it)
            else:
                out_list.append(json.dumps(it, ensure_ascii=False))
        return out_list
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return json.dumps(v, ensure_ascii=False)


def _sanitize_props(props: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not props:
        return {}
    out: Dict[str, Any] = {}
    for k, v in props.items():
        if not isinstance(k, str) or not k:
            continue
        out[k] = _sanitize_prop_value(v)
    return out


def _ensure_project_on_nodes(
    graph: Dict[str, Any], project_name: Optional[str]
) -> Dict[str, Any]:
    if not project_name or not isinstance(graph, dict):
        return graph
    nodes = graph.get("nodes") or []
    if not isinstance(nodes, list):
        return graph

    fixed_nodes: List[Dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            fixed_nodes.append(n)
            continue
        props = n.get("properties")
        if not isinstance(props, dict):
            props = {}
        if not props.get("project"):
            props = {**props, "project": project_name}
        fixed_nodes.append({**n, "properties": props})

    return {**graph, "nodes": fixed_nodes}


def build_save_meeting_log_query(
    payload: CpsInput,
) -> Tuple[str, Dict[str, Any]]:
    log_id = payload.log_id()
    target_cps_id = payload.derived_cps_id()
    q = (
        "// --- 미팅 로그 노드 생성 ---\n"
        "MERGE (log:Meeting_Log {id: $log_id})\n"
        "SET log.project = $project,\n"
        "    log.version = $version,\n"
        "    log.date = $date,\n"
        "    log.raw_content = $raw_content,\n"
        "    log.created_at = timestamp()\n\n"
        "// --- CPS 문서와 연결 ---\n"
        "WITH log\n"
        "MATCH (doc:CPS_Document {id: $target_cps_id})\n"
        "MERGE (doc)-[:EXTRACTED_FROM]->(log)"
    )
    params: Dict[str, Any] = {
        "log_id": log_id,
        # [멀티테넌시] project property 는 스코프 키 — 동명 팀/개인 격리.
        "project": payload.project_key(),
        "version": payload.version,
        "date": payload.date,
        "raw_content": payload.meeting_content,
        "target_cps_id": target_cps_id,
    }
    return q.strip(), params


def build_save_cps_query(
    graph: Dict[str, Any],
    project_name: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    graph = _ensure_project_on_nodes(graph, project_name)

    dropped_node_ids: set = set()
    nodes_by_label: Dict[str, List[Dict[str, Any]]] = {}
    for n in graph.get("nodes") or []:
        if not n.get("id") or not n.get("label"):
            continue
        label = n["label"]
        if not is_safe_cypher_identifier(label):
            logger.warning(
                "build_save_cps_query: dropping node with unsafe label: %r", label
            )
            continue
        if label in _BLOCKED_LABELS:
            logger.warning(
                "build_save_cps_query: %r 라벨 노드는 별도 시스템(ownership/user)이 관리 — "
                "graph save 에서 제외 (id=%r)",
                label, n["id"],
            )
            dropped_node_ids.add(str(n["id"]))
            continue
        # [2026-05-26] LLM 환각 차단 — spec 노드인데 핵심 필드 0개면 stub 의심.
        if not _is_meaningful_spec_node(n):
            logger.warning(
                "build_save_cps_query: spec node 가 핵심 필드 없이 LLM 출력됨 — drop "
                "(label=%r id=%r). LLM 환각 또는 출력 형식 깨짐 의심.",
                label, n["id"],
            )
            dropped_node_ids.add(str(n["id"]))
            continue
        nodes_by_label.setdefault(label, []).append(n)

    rels_by_type: Dict[str, List[Dict[str, Any]]] = {}
    for r in graph.get("relationships") or []:
        if not (r.get("source") and r.get("target") and r.get("type")):
            continue
        rtype = r["type"]
        if not is_safe_cypher_identifier(rtype):
            logger.warning(
                "build_save_cps_query: dropping relationship with unsafe type: %r",
                rtype,
            )
            continue
        src, tgt = str(r["source"]), str(r["target"])
        if src in dropped_node_ids or tgt in dropped_node_ids:
            logger.warning(
                "build_save_cps_query: blocked-label 노드를 참조하는 관계 제외 "
                "(type=%r src=%r tgt=%r)",
                rtype, src, tgt,
            )
            continue
        rels_by_type.setdefault(rtype, []).append(r)

    chunks: List[str] = []
    params: Dict[str, Any] = {}
    block_idx = 0

    # [멀티테넌시 정합성 — 2026-06] spec 노드(prb_01/epic_01/story_01_1 …)는 LLM 이
    # project-prefix 없는 id 로 생성한다. 과거엔 MERGE 가 id 만으로 매칭해, 서로 다른
    # 프로젝트의 동일 id(prb_01) 가 **전역 단일 노드를 공유** → 마지막 writer 가 project
    # property·summary 를 덮어써 cross-project 오염/누락(get_project_graph 의
    # `WHERE n.project=$project` 에서 노드 증발)이 발생했다. Design 노드는 이미
    # {id, project} 로 MERGE 하므로(design_pipeline/cypher.py), 여기서도 동일하게 스코프해
    # 일관성을 맞춘다.
    #
    # [null-safe] project_name 이 있을 때만 스코프한다 — 프로덕션 호출은 항상
    # project_name(스코프 키)을 넘기고(_ensure_project_on_nodes 로 모든 노드에 project
    # property 부여), project 미지정 레거시/테스트 경로는 기존 id-only 동작을 byte-identical
    # 하게 보존한다(`{project: null}` 패턴이 매칭 0건이 되는 함정 회피).
    scoped = bool(project_name)

    for label, items in nodes_by_label.items():
        param_key = f"n_{block_idx}"
        node_params: List[Dict[str, Any]] = []
        for n in items:
            props = _sanitize_props(n.get("properties"))
            # [2026-05-27 b] Document 본문(full_markdown)이 빈/whitespace 면 props 에서
            # 제거 — MERGE (doc) SET doc += props 시 기존 full_markdown 을 빈 값으로
            # 덮어써 누적 본문을 손상시키는 것을 방지(이후 deleteMeeting 가 손상 delta 를
            # 만나 master 까지 날리는 사고의 선행 원인 차단). 빈 노드 신규 생성은 허용 —
            # 기존 흐름 보존.
            if label in ("CPS_Document", "PRD_Document"):
                fm = props.get("full_markdown")
                if not (isinstance(fm, str) and fm.strip()):
                    props.pop("full_markdown", None)
            node_params.append({"id": str(n["id"]), "props": props})
        params[param_key] = node_params
        block: List[str] = []
        block.append(f"// --- 노드 생성: {label} ---")
        if block_idx > 0:
            block.append(f"WITH count(*) AS _dummy{block_idx}")
        block.append(f"UNWIND ${param_key} AS nItem{block_idx}")
        if scoped:
            # 스코프 키는 모든 노드 공통($save_project) — 단일 save 의 노드는 동일 project.
            block.append(
                f"MERGE (n{block_idx}:{label} "
                f"{{id: nItem{block_idx}.id, project: $save_project}})"
            )
        else:
            block.append(
                f"MERGE (n{block_idx}:{label} {{id: nItem{block_idx}.id}})"
            )
        block.append(f"SET n{block_idx} += nItem{block_idx}.props")
        chunks.append("\n".join(block))
        block_idx += 1

    for rtype, rels in rels_by_type.items():
        param_key = f"r_{block_idx}"
        params[param_key] = [
            {
                "source": str(r["source"]),
                "target": str(r["target"]),
                "props": _sanitize_props(r.get("properties")),
            }
            for r in rels
        ]
        block = []
        block.append(f"// --- 관계 생성: {rtype} ---")
        if block_idx > 0:
            block.append(f"WITH count(*) AS _dummy{block_idx}")
        block.append(f"UNWIND ${param_key} AS rItem{block_idx}")
        if scoped:
            # endpoint 도 project 로 한정 — cross-project 동일 id 노드에 잘못 연결되는 것
            # 방지 + 스코프된 노드를 정확히 매칭(연결 0% 회귀 방지).
            block.append(
                f"MATCH (s{block_idx} "
                f"{{id: rItem{block_idx}.source, project: $save_project}})"
            )
            block.append(
                f"MATCH (t{block_idx} "
                f"{{id: rItem{block_idx}.target, project: $save_project}})"
            )
        else:
            block.append(f"MATCH (s{block_idx} {{id: rItem{block_idx}.source}})")
            block.append(f"MATCH (t{block_idx} {{id: rItem{block_idx}.target}})")
        block.append(
            f"MERGE (s{block_idx})-[r{block_idx}:{rtype}]->(t{block_idx})"
        )
        block.append(f"SET r{block_idx} += rItem{block_idx}.props")
        chunks.append("\n".join(block))
        block_idx += 1

    # 빈 그래프(chunks 없음)면 save_project 도 넣지 않아 "빈 그래프 → 빈 query + 빈 params"
    # 계약을 보존한다. 스코프 패턴($save_project 참조)이 실제로 emit 됐을 때만 바인딩.
    if scoped and chunks:
        params["save_project"] = project_name

    return ("\n\n".join(chunks), params)


def build_merge_master_query(
    project_name: str, merged_content: str, latest_delta_id: Optional[str]
) -> Tuple[str, Dict[str, Any]]:
    # [2026-05-26 데이터 무결성 가드] merged_content 가 빈 string 또는 whitespace
    # 만 있으면 master.full_markdown 이 wipe 됨 → 누적 데이터 영구 손실.
    # cypher 호출 전에 차단 — 호출자(pipeline) 의 책임이지만 defense in depth.
    # 정상 path (merge_agent 정상 출력) 에서는 발동 안 함. 발동 = 상위 단계 bug.
    if not merged_content or not merged_content.strip():
        raise ValueError(
            f"build_merge_master_query: merged_content 가 비어있음 (project={project_name}). "
            "master.full_markdown 를 wipe 하면 누적 CPS 데이터 영구 손실 — 차단."
        )

    # [멀티테넌시] project_name 은 호출자(pipeline)가 넘긴 *스코프 키*.
    # master id 빌더로 통일 (개인=기존 형식, 팀=격리).
    from app.core.project_scope import cps_master_id
    master_id = cps_master_id(project_name)

    parts = [
        "// --- 마스터 CPS 갱신 ---",
        "MERGE (master:CPS_Document {id: $master_id})",
        "SET master.project = $project,",
        "    master.version = 'Final',",
        "    master.type = 'Master',",
        "    master.is_latest = true,",
        "    master.full_markdown = $merged_content,",
        "    master.updated_at = timestamp()",
    ]
    params: Dict[str, Any] = {
        "master_id": master_id,
        "project": project_name,
        "merged_content": merged_content,
    }

    if latest_delta_id:
        parts.append("")
        parts.append("// --- 최신 Delta 편입 및 is_latest 해제 ---")
        parts.append("WITH master")
        parts.append("MATCH (latest:CPS_Document {id: $latest_delta_id})")
        parts.append("MERGE (master)-[:SYNTHESIZED_FROM]->(latest)")
        parts.append("SET latest.is_latest = false")
        params["latest_delta_id"] = latest_delta_id

    return "\n".join(parts).strip(), params
