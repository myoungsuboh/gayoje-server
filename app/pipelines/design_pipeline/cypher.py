from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.pipelines.base import escape_cypher_string, extract_json_object
from app.pipelines.design_validator.attributes import serialize_attributes_for_neo4j
from app.pipelines.design_validator.api_payload import serialize_api_payload_for_neo4j
from app.pipelines.design_validator.arch_detail import (
    normalize_connection_auth,
    serialize_deployment_for_neo4j,
    serialize_external_dependencies_for_neo4j,
)
from app.pipelines.design_validator.ddd_detail import serialize_invariants_for_neo4j


# ─── lineage_confidence 정규화 ──────────────────────────────────────────────────────────────
# LLM 이 가끔 'low'/'high'/'medium' 같은 비표준 값을 반환. FE hasLineage 는 'direct'/'inferred'
# 만 인식하므로 파이프라인 저장 전에 화이트리스트로 강제 정규화.
_VALID_CONFIDENCE = frozenset({"direct", "inferred"})


def _norm_confidence(v: Any) -> str:
    """'direct' | 'inferred' 외 모든 값은 'none' 으로 정규화."""
    return v if v in _VALID_CONFIDENCE else "none"


# ─── Cypher helpers ────────────────────────────────────────────────────────────────────────


def _to_cypher_literal(obj: Any) -> str:
    """
    Spack/DDD/Architecture 단계의 `toCypherLiteral()` 헬퍼.
    Cypher 리터럴 (문자열/숫자/불리언/배열/맵) 직렬화.

    ⚠️ 이제 build_save_* 함수는 이 함수를 안 쓰고 $param 바인딩 사용.
    외부 호출자 잠재적 호환성 + 테스트 회귀 보호용으로 함수 자체는 유지.
    """
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float)):
        return str(obj)
    if isinstance(obj, str):
        return f"'{escape_cypher_string(obj)}'"
    if isinstance(obj, list):
        return "[" + ", ".join(_to_cypher_literal(x) for x in obj) + "]"
    if isinstance(obj, dict):
        return (
            "{"
            + ", ".join(f"{k}: {_to_cypher_literal(v)}" for k, v in obj.items())
            + "}"
        )
    return "null"


def _parse_agent_json(text: str) -> Dict[str, Any]:
    """
    Spack/DDD/Architecture 단계의 inline JSON 파서.

    LLM 출력에서 ```json fence 제거 후 첫 `{...}` 추출. 파싱 실패 시 빈 dict.
    """
    obj = extract_json_object(text)
    if not obj:
        # fallback: 빈 결과로 진행 (빈 객체로 fall-through)
        return {}
    return obj


# ─── Lineage Neo4j 저장 헬퍼 (B4 — 2026-05) ──────────────────────────────────────────────
#
# design_validator 가 만든 lineage 객체 ('Story-XX.Y' 형태) 를 PRD pipeline 이
# 만든 Neo4j Story 노드 id ('story_XX_Y' 형태) 로 변환 + 엣지 list 평탄화.

_NORMALIZED_STORY_RE = re.compile(r"^Story-(\d+)\.(\d+)$")


def _to_neo4j_story_id(normalized_story_id: str) -> Optional[str]:
    """
    'Story-01.1' (design_validator 정규화 형태) → 'story_01_1' (prd_graph.md id 콘밤션).
    정규 형태 아니면 None.
    """
    if not normalized_story_id:
        return None
    m = _NORMALIZED_STORY_RE.match(normalized_story_id)
    if not m:
        return None
    return f"story_{int(m.group(1)):02d}_{m.group(2)}"


def _story_match_id(related_story_id: Any) -> str:
    """API/Screen/Event 의 related_story_id 를 Story 노드 id(story_XX_Y) 형태로 변환.

    [2026-06 연결 fix] 프롬프트가 related_story_id 를 'Story-XX.Y' 로 생성하는데,
    Story 노드 id 는 'story_XX_Y' 라서 그대로 매칭하면 IMPLEMENTS/RENDERS/TRIGGERS
    엣지가 절대 안 만들어졌다(연결 0% 고착의 진짜 원인). Entity lineage 는
    _to_neo4j_story_id 로 변환했지만 API/Screen/Event 는 raw 였음.
    변환 불가 형식이면(이미 story_XX_Y 거나 비정상) 원본 유지 → 매칭 시도.
    """
    rsid = str(related_story_id or "")
    return _to_neo4j_story_id(rsid) or rsid


def _extract_lineage_edges(
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    노드 list 에서 lineage 엣지를 평탄화 list 로 추출.

    [정책]
    - confidence == 'none' 인 노드는 엣지 생성 안 함
    - 결정 옵션 3-B: direct + inferred 모두 엣지 (relationship 에 confidence 저장)
    - story_id 가 Neo4j 형태로 변환 안 되면 해당 엣지 skip

    Returns: [{src_id, story_neo4j_id, confidence, quote}, ...]
    """
    edges: List[Dict[str, Any]] = []
    for item in items:
        node_id = item.get("id")
        if not node_id:
            continue
        lineage = item.get("lineage") or {}
        confidence = lineage.get("confidence")
        if confidence not in ("direct", "inferred"):
            continue
        for s in lineage.get("related_stories") or []:
            story_neo4j_id = _to_neo4j_story_id(s.get("story_id") or "")
            if not story_neo4j_id:
                continue
            edges.append({
                "src_id": node_id,
                "story_neo4j_id": story_neo4j_id,
                "confidence": confidence,
                "quote": str(s.get("quote") or ""),
            })
    return edges


def _lineage_cypher_chunk(
    edges_param_name: str,
    src_label: str,
) -> List[str]:
    """
    DERIVED_FROM 엣지 생성 cypher chunk.

    Story 노드가 존재하지 않으면 OPTIONAL MATCH 가 null 이고 FOREACH 가 skip.
    → PRD 가 아직 그래프에 없거나 story_id 가 PRD 에 없는 경우 안전.

    Args:
        edges_param_name: $entity_lineage_edges 같은 param 이름
        src_label: "Entity" / "Aggregate" / "ArchService"
    """
    return [
        f"// --- {src_label} lineage 엣지 (DERIVED_FROM → Story) ---",
        f"WITH count(*) AS _dummy_{src_label.lower()}_lineage_pre",
        f"UNWIND ${edges_param_name} AS le",
        f"MATCH (src:{src_label} {{id: le.src_id, project: $project}})",
        "OPTIONAL MATCH (story:Story {id: le.story_neo4j_id, project: $project})",
        "FOREACH (ignore IN CASE WHEN story IS NOT NULL THEN [1] ELSE [] END | ",
        "  MERGE (src)-[r:DERIVED_FROM]->(story) ",
        "  SET r.confidence = le.confidence, r.quote = le.quote)",
        f"WITH count(*) AS _dummy_{src_label.lower()}_lineage",
        "",
    ]


def build_save_spack_query(
    project_name: str, spack: Dict[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """
    Stage: `Spack Code`.

    Wipe-and-Redraw (parameter binding):
      1. project 단위로 API/Entity/Policy DETACH DELETE
      2. apis, entities, policies UNWIND $param + MERGE
      3. API → Story (IMPLEMENTS), Policy → Entity (GOVERNS) 조건부 관계

    Returns: (cypher_string, params_dict)
    """
    apis = spack.get("apis") or []
    entities = spack.get("entities") or []
    policies = spack.get("policies") or []
    screens = spack.get("screens") or []
    params: Dict[str, Any] = {"project": project_name}

    # [2026-05-27] 빈 생성 결과 가드 — LLM 이 노드를 하나도 못 뽑으면
    # (dirty PRD underextract / 생성 실패) wipe 를 건너뛰어 기존 SPACK 을 보존.
    # 이전엔 빈 결과여도 DETACH DELETE 가 먼저 실행돼 정상 데이터를 영구 삭제 →
    # "최신 업데이트 후 SPACK 탭이 비어버림" 회귀의 원인. 트랜잭션 안에서 실행되므로
    # 유효한 no-op statement 를 반환.
    if not (apis or entities or policies or screens):
        return (
            "RETURN 'Spack Skipped (empty result — existing data preserved)' "
            "AS Status, $project AS ProjectName",
            params,
        )

    q: List[str] = [
        "// --- 1. 기존 Spack 데이터 초기화 (Wipe) ---",
        # [#3 — 2026-05-25] Screen 노드도 wipe 범위에 포함.
        "MATCH (n) WHERE n.project = $project AND (n:API OR n:Entity OR n:Policy OR n:Screen)",
        "DETACH DELETE n",
        "",
        "WITH count(*) AS _dummy1",
        "",
    ]

    if apis:
        # [A-2 — 2026-05-25] API payload (path/query params, request/response body)
        # 객체 → JSON string 직렬화. Neo4j primitive 제약 우회.
        # ★ 원본 apis mutate 금지 — 복사본 (flat_apis) 에만 직렬화 필드 추가.
        flat_apis = []
        for a in apis:
            a_flat = dict(a)
            a_flat.update(serialize_api_payload_for_neo4j(a))
            # [2026-06 연결 fix] Story 매칭용 id 형식 변환 (Story-XX.Y → story_XX_Y).
            a_flat["_story_match_id"] = _story_match_id(a.get("related_story_id"))
            flat_apis.append(a_flat)
        params["apis"] = flat_apis
        q += [
            "UNWIND $apis AS apiData",
            "MERGE (api:API {id: apiData.id, project: $project})",
            "SET api.name = apiData.name, api.method = apiData.method, "
            "api.endpoint = apiData.endpoint, api.description = apiData.description, "
            "api.path_params = apiData.path_params, "
            "api.query_params = apiData.query_params, "
            "api.request_body = apiData.request_body, "
            "api.response_body = apiData.response_body, "
            # [A-3 — 2026-05-25] error_cases + auth (JSON string).
            "api.error_cases = apiData.error_cases, "
            "api.auth = apiData.auth, "
            # [2026-06 연결 fix] related_story_id 를 노드 속성으로도 저장 — 엣지 매칭이
            # 실패해도 점수/검증이 Story 추적성을 읽을 수 있게(안전망).
            "api.related_story_id = apiData.related_story_id, "
            "api.updated_at = timestamp()",
            "WITH api, apiData",
            "OPTIONAL MATCH (s:Story {id: apiData._story_match_id, project: $project})",
            "FOREACH (ignore IN CASE WHEN s IS NOT NULL THEN [1] ELSE [] END | "
            "MERGE (api)-[:IMPLEMENTS]->(s))",
            "",
            "WITH count(*) AS _dummy_api",
            "",
        ]

    if entities:
        # [B4 — 2026-05 lineage] 평탄화된 lineage 필드를 properties 로 저장
        # (검색/필터 cypher 에서 직접 쓰기 위함). full lineage object 는 응답으로만
        # 통과되으로 properties 에는 confidence + story_count 만.
        # ★ 원본 entities mutate 금지 — 응답에 _lineage_* 필드가 노출되거나 downstream
        #   LLM 토큰 낙비 방지. 복사본에만 평탄화 필드 추가.
        flat_entities = []
        for e in entities:
            e_flat = dict(e)
            lineage_obj = e.get("lineage") or {}
            e_flat["_lineage_confidence"] = _norm_confidence(lineage_obj.get("confidence"))
            e_flat["_lineage_story_count"] = len(lineage_obj.get("related_stories") or [])
            # [A-1 — 2026-05-25] attributes 객체 list → JSON string.
            # Neo4j property 는 primitive list 만 허용하므로 직렬화 필수.
            # read 측 (query_repository, lint, fix_spec) 은 normalize_entity_attributes
            # 로 복원. legacy string list 입력도 헬퍼가 흡수.
            e_flat["attributes"] = serialize_attributes_for_neo4j(e.get("attributes"))
            flat_entities.append(e_flat)
        params["entities"] = flat_entities
        params["entity_lineage_edges"] = _extract_lineage_edges(entities)
        q += [
            "UNWIND $entities AS entData",
            "MERGE (ent:Entity {id: entData.id, project: $project})",
            "SET ent.name = entData.name, ent.attributes = entData.attributes, "
            "ent.description = entData.description, "
            "ent.lineage_confidence = entData._lineage_confidence, "
            "ent.lineage_story_count = entData._lineage_story_count, "
            "ent.updated_at = timestamp()",
            "",
            "WITH count(*) AS _dummy_ent",
            "",
        ]
        # [B4] DERIVED_FROM 엣지 — Story 없으면 OPTIONAL MATCH 가 skip
        q += _lineage_cypher_chunk("entity_lineage_edges", "Entity")

    if policies:
        params["policies"] = policies
        q += [
            "UNWIND $policies AS polData",
            "MERGE (pol:Policy {id: polData.id, project: $project})",
            "SET pol.category = polData.category, pol.description = polData.description, "
            "pol.updated_at = timestamp()",
            "WITH pol, polData",
            "OPTIONAL MATCH (e:Entity {name: polData.related_entity, project: $project})",
            "FOREACH (ignore IN CASE WHEN e IS NOT NULL THEN [1] ELSE [] END | "
            "MERGE (pol)-[:GOVERNS]->(e))",
            "",
            "WITH count(*) AS _dummy_pol",
            "",
        ]

    # [#3 — 2026-05-25] Screen 노드 + CALLS_API 관계 + RENDERS Story 관계.
    if screens:
        # [2026-06 연결 fix] Story 매칭용 id 변환 + related_story_id 속성 보존.
        flat_screens = []
        for sc in screens:
            sc_flat = dict(sc)
            sc_flat["_story_match_id"] = _story_match_id(sc.get("related_story_id"))
            flat_screens.append(sc_flat)
        params["screens"] = flat_screens
        q += [
            "UNWIND $screens AS scData",
            "MERGE (sc:Screen {id: scData.id, project: $project})",
            "SET sc.name = scData.name, sc.path = scData.path, "
            "sc.description = scData.description, "
            "sc.next_screens = scData.next_screens, "
            "sc.related_story_id = scData.related_story_id, "
            "sc.updated_at = timestamp()",
            "WITH sc, scData",
            # Screen → Story (RENDERS) — Story 가 어떤 화면에서 구현되나.
            "OPTIONAL MATCH (s:Story {id: scData._story_match_id, project: $project})",
            "FOREACH (ignore IN CASE WHEN s IS NOT NULL THEN [1] ELSE [] END | "
            "MERGE (sc)-[:RENDERS]->(s))",
            "WITH sc, scData",
            # Screen → API (CALLS_API) — 화면이 호출하는 API.
            "UNWIND (CASE WHEN size(scData.calls_apis) > 0 THEN scData.calls_apis ELSE [null] END) AS apiId",
            "WITH sc, apiId WHERE apiId IS NOT NULL",
            "OPTIONAL MATCH (api:API {id: apiId, project: $project})",
            "FOREACH (ignore IN CASE WHEN api IS NOT NULL THEN [1] ELSE [] END | "
            "MERGE (sc)-[:CALLS_API]->(api))",
            "",
            "WITH count(*) AS _dummy_sc",
            "",
        ]

    q.append("RETURN 'Spack Sync Completed' AS Status, $project AS ProjectName")
    return "\n".join(q).strip(), params


def build_save_ddd_query(
    project_name: str, ddd: Dict[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Stage: `DDD Code`. (parameter binding)"""
    contexts = ddd.get("contexts") or []
    aggregates = ddd.get("aggregates") or []
    entities = ddd.get("entities") or []
    events = ddd.get("events") or []
    params: Dict[str, Any] = {"project": project_name}

    # [2026-05-27] 빈 생성 결과 가드 (build_save_spack_query 와 동일 정책).
    # 노드도 cross-link 매핑도 하나 없으면 wipe 건너뛰어 기존 DDD 보존.
    # (매핑만 있는 경우는 기존 노드를 대상으로 하므로 wipe 하지 않고 그대로 진행.)
    if not (
        contexts or aggregates or entities or events
        or (ddd.get("spack_entity_mapping") or [])
    ):
        return (
            "RETURN 'DDD Skipped (empty result — existing data preserved)' "
            "AS Status, $project AS ProjectName",
            params,
        )

    q: List[str] = [
        "// --- 1. 기존 DDD 데이터 초기화 (Wipe) ---",
        "MATCH (n) WHERE n.project = $project AND "
        "(n:BoundedContext OR n:Aggregate OR n:DomainEntity OR n:DomainEvent)",
        "DETACH DELETE n",
        "",
        "WITH count(*) AS _dummy1",
        "",
    ]

    if contexts:
        params["contexts"] = contexts
        q += [
            "UNWIND $contexts AS ctxData",
            "MERGE (ctx:BoundedContext {id: ctxData.id, project: $project})",
            "SET ctx.name = ctxData.name, ctx.description = ctxData.description, "
            "ctx.updated_at = timestamp()",
            "",
            "WITH count(*) AS _dummy_ctx",
            "",
        ]

    if aggregates:
        # [B4 — 2026-05 lineage] 원본 mutate 금지 — 복사본만 평탄화.
        flat_aggregates = []
        for a in aggregates:
            a_flat = dict(a)
            lineage_obj = a.get("lineage") or {}
            a_flat["_lineage_confidence"] = _norm_confidence(lineage_obj.get("confidence"))
            a_flat["_lineage_story_count"] = len(lineage_obj.get("related_stories") or [])
            # [D-1 — 2026-05-25] invariants JSON string 직렬화.
            a_flat["invariants"] = serialize_invariants_for_neo4j(a.get("invariants"))
            flat_aggregates.append(a_flat)
        params["aggregates"] = flat_aggregates
        params["aggregate_lineage_edges"] = _extract_lineage_edges(aggregates)
        q += [
            "UNWIND $aggregates AS aggData",
            "MERGE (agg:Aggregate {id: aggData.id, project: $project})",
            "SET agg.name = aggData.name, agg.description = aggData.description, "
            "agg.lineage_confidence = aggData._lineage_confidence, "
            "agg.lineage_story_count = aggData._lineage_story_count, "
            "agg.invariants = aggData.invariants, "
            "agg.updated_at = timestamp()",
            "WITH agg, aggData",
            "OPTIONAL MATCH (ctx:BoundedContext {id: aggData.context_id, project: $project})",
            "FOREACH (ignore IN CASE WHEN ctx IS NOT NULL THEN [1] ELSE [] END | "
            "MERGE (agg)-[:BELONGS_TO]->(ctx))",
            "",
            "WITH count(*) AS _dummy_agg",
            "",
        ]
        q += _lineage_cypher_chunk("aggregate_lineage_edges", "Aggregate")

    if entities:
        # [C — 2026-05 lineage] DomainEntity 평탄화 (Aggregate 와 동일 패턴).
        flat_dentities = []
        for de in entities:
            de_flat = dict(de)
            lineage_obj = de.get("lineage") or {}
            de_flat["_lineage_confidence"] = _norm_confidence(lineage_obj.get("confidence"))
            de_flat["_lineage_story_count"] = len(lineage_obj.get("related_stories") or [])
            # [D-1 — 2026-05-25] attributes JSON string 직렬화 (SPACK Entity 와 동일).
            de_flat["attributes"] = serialize_attributes_for_neo4j(de.get("attributes"))
            flat_dentities.append(de_flat)
        params["entities"] = flat_dentities
        params["domain_entity_lineage_edges"] = _extract_lineage_edges(entities)
        q += [
            "UNWIND $entities AS entData",
            "MERGE (dent:DomainEntity {id: entData.id, project: $project})",
            "SET dent.name = entData.name, dent.description = entData.description, "
            "dent.lineage_confidence = entData._lineage_confidence, "
            "dent.lineage_story_count = entData._lineage_story_count, "
            "dent.attributes = entData.attributes, "
            "dent.updated_at = timestamp()",
            "WITH dent, entData",
            "OPTIONAL MATCH (agg:Aggregate {id: entData.aggregate_id, project: $project})",
            "FOREACH (ignore IN CASE WHEN agg IS NOT NULL THEN [1] ELSE [] END | "
            "MERGE (dent)-[:PART_OF]->(agg))",
            "",
            "WITH count(*) AS _dummy_ent",
            "",
        ]
        q += _lineage_cypher_chunk("domain_entity_lineage_edges", "DomainEntity")

    if events:
        # [D-1 — 2026-05-25] payload_fields JSON string 직렬화. 원본 mutate 회피.
        flat_events = []
        for ev in events:
            ev_flat = dict(ev)
            ev_flat["payload_fields"] = serialize_attributes_for_neo4j(
                ev.get("payload_fields")
            )
            # [2026-06 연결 fix] Story 매칭용 id 변환.
            ev_flat["_story_match_id"] = _story_match_id(ev.get("related_story_id"))
            flat_events.append(ev_flat)
        params["events"] = flat_events
        q += [
            "UNWIND $events AS evtData",
            "MERGE (evt:DomainEvent {id: evtData.id, project: $project})",
            "SET evt.name = evtData.name, evt.description = evtData.description, "
            "evt.payload_fields = evtData.payload_fields, "
            "evt.related_story_id = evtData.related_story_id, "
            "evt.updated_at = timestamp()",
            "WITH evt, evtData",
            "OPTIONAL MATCH (agg:Aggregate {id: evtData.published_by_aggregate_id, project: $project})",
            "FOREACH (ignore IN CASE WHEN agg IS NOT NULL THEN [1] ELSE [] END | "
            "MERGE (agg)-[:PUBLISHES]->(evt))",
            "WITH evt, evtData",
            "OPTIONAL MATCH (s:Story {id: evtData._story_match_id, project: $project})",
            "FOREACH (ignore IN CASE WHEN s IS NOT NULL THEN [1] ELSE [] END | "
            "MERGE (s)-[:TRIGGERS]->(evt))",
            "",
            "WITH count(*) AS _dummy_evt",
            "",
        ]

    # [2026-05-19] SPACK Entity ↔ DDD location cross-link (Phase 1 cross-jump).
    # LLM 출력의 spack_entity_mapping 을 (Entity)-[:MAPPED_TO]->(Aggregate|DomainEntity)
    # 관계로 저장. FE 가 SPACK Entity 카드에서 DDD 노드로 점프 가능.
    spack_entity_mapping = ddd.get("spack_entity_mapping") or []
    if spack_entity_mapping:
        params["spack_entity_mapping"] = spack_entity_mapping
        # [2026-06 매핑 복원] LLM 이 spack_entity_id / ddd_location 을 한 글자라도
        # 틀리면(번호 drift 등) id-only 매칭이 전부 실패해 MAPPED_TO 가 0개 → 읽기 시
        # entity_mapping_rels 가 비고 → DDD_MAPPING_MISSING_ENTITY(ERROR) 가 전 엔티티에
        # false 로 터진다. "Aggregate/Entity name = SPACK Entity name" 은 프롬프트의
        # 절대 규칙이므로, id 매칭이 실패하면 name(spack_name) 으로 폴백해 올바른 노드를
        # 다시 연결한다. 읽기 시 source_id 는 startNode(Entity).id 라 정확한 id 로 복원됨.
        q += [
            "UNWIND $spack_entity_mapping AS m",
            # source SPACK Entity: id 우선, 실패 시 name(spack_name) 폴백.
            "OPTIONAL MATCH (e_by_id:Entity {id: m.spack_entity_id, project: $project})",
            "OPTIONAL MATCH (e_by_name:Entity {name: m.spack_name, project: $project})",
            "WITH m, coalesce(e_by_id, e_by_name) AS e",
            # target DDD 노드(Aggregate|DomainEntity): id 우선, 실패 시 name 폴백.
            "OPTIONAL MATCH (t_by_id {id: m.ddd_location, project: $project}) "
            "WHERE t_by_id:Aggregate OR t_by_id:DomainEntity",
            "OPTIONAL MATCH (t_by_name {name: m.spack_name, project: $project}) "
            "WHERE t_by_name:Aggregate OR t_by_name:DomainEntity",
            "WITH m, e, coalesce(t_by_id, t_by_name) AS target",
            "FOREACH (ignore IN CASE WHEN e IS NOT NULL AND target IS NOT NULL "
            "THEN [1] ELSE [] END |",
            "  MERGE (e)-[r:MAPPED_TO]->(target)",
            "  SET r.role = m.ddd_role",
            ")",
            "",
            "WITH count(*) AS _dummy_map",
            "",
        ]

    q.append("RETURN 'DDD Sync Completed' AS Status, $project AS ProjectName")
    return "\n".join(q).strip(), params


def build_save_architecture_query(
    project_name: str, arch: Dict[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Stage: `Architecture Code`. (parameter binding)"""
    services = arch.get("services") or []
    databases = arch.get("databases") or []
    connections = arch.get("connections") or []
    params: Dict[str, Any] = {"project": project_name}

    # [2026-05-27] 빈 생성 결과 가드 (build_save_spack_query 와 동일 정책).
    # 노드도 엣지(connections)도 cross-link 매핑도 하나 없으면 wipe 건너뛰어 기존
    # Architecture 보존. (엣지/매핑만 있는 경우는 기존 노드를 대상으로 하므로 진행.)
    if not (
        services or databases or connections
        or (arch.get("api_service_mapping") or [])
    ):
        return (
            "RETURN 'Architecture Skipped (empty result — existing data preserved)' "
            "AS Status, $project AS ProjectName",
            params,
        )

    q: List[str] = [
        "// --- 1. 기존 Architecture 데이터 초기화 (Wipe) ---",
        "MATCH (n) WHERE n.project = $project AND (n:ArchService OR n:ArchDatabase)",
        "DETACH DELETE n",
        "",
        "WITH count(*) AS _dummy1",
        "",
    ]

    if services:
        # [B4 — 2026-05 lineage] 원본 mutate 금지 — 복사본만 평탄화.
        flat_services = []
        for s in services:
            s_flat = dict(s)
            lineage_obj = s.get("lineage") or {}
            s_flat["_lineage_confidence"] = _norm_confidence(lineage_obj.get("confidence"))
            s_flat["_lineage_story_count"] = len(lineage_obj.get("related_stories") or [])
            # [2026-05-19] cross-jump (Phase 1) — owned_aggregates 를 flat 속성으로
            # 노드에 저장해 응답 시 별도 JOIN 없이 노출. 관계는 아래서 별도 생성.
            s_flat["_owned_aggregate_names"] = list(dict.fromkeys(s.get("owned_aggregates") or []))
            # [D-2 — 2026-05-25] deployment / external_dependencies JSON string.
            s_flat["deployment"] = serialize_deployment_for_neo4j(s.get("deployment"))
            s_flat["external_dependencies"] = serialize_external_dependencies_for_neo4j(
                s.get("external_dependencies")
            )
            flat_services.append(s_flat)
        params["services"] = flat_services
        params["service_lineage_edges"] = _extract_lineage_edges(services)
        q += [
            "UNWIND $services AS svcData",
            "MERGE (svc:ArchService {id: svcData.id, project: $project})",
            "SET svc.name = svcData.name, svc.type = svcData.type, "
            "svc.tech_stack = svcData.tech_stack, svc.description = svcData.description, "
            "svc.lineage_confidence = svcData._lineage_confidence, "
            "svc.lineage_story_count = svcData._lineage_story_count, "
            "svc.owned_aggregate_names = svcData._owned_aggregate_names, "
            "svc.deployment = svcData.deployment, "
            "svc.external_dependencies = svcData.external_dependencies, "
            "svc.updated_at = timestamp()",
            "WITH svc, svcData",
            # owned_aggregate_names 의 각 이름 → Aggregate 노드와 OWNED_BY 관계 생성
            # Aggregate 는 DDD save 단계에서 이미 만들어져 있어야 함 (정상 흐름).
            "UNWIND svcData._owned_aggregate_names AS aggName",
            "OPTIONAL MATCH (agg:Aggregate {name: aggName, project: $project})",
            "FOREACH (ignore IN CASE WHEN agg IS NOT NULL THEN [1] ELSE [] END | "
            "MERGE (agg)-[:OWNED_BY]->(svc))",
            "",
            "WITH count(*) AS _dummy_svc",
            "",
        ]
        q += _lineage_cypher_chunk("service_lineage_edges", "ArchService")

    if databases:
        # [C — 2026-05 lineage] Database 평탄화 (Service 와 동일 패턴).
        flat_databases = []
        for d in databases:
            d_flat = dict(d)
            lineage_obj = d.get("lineage") or {}
            d_flat["_lineage_confidence"] = _norm_confidence(lineage_obj.get("confidence"))
            d_flat["_lineage_story_count"] = len(lineage_obj.get("related_stories") or [])
            flat_databases.append(d_flat)
        params["databases"] = flat_databases
        params["database_lineage_edges"] = _extract_lineage_edges(databases)
        q += [
            "UNWIND $databases AS dbData",
            "MERGE (db:ArchDatabase {id: dbData.id, project: $project})",
            "SET db.name = dbData.name, db.type = dbData.type, "
            "db.tech_stack = dbData.tech_stack, db.description = dbData.description, "
            "db.lineage_confidence = dbData._lineage_confidence, "
            "db.lineage_story_count = dbData._lineage_story_count, "
            "db.updated_at = timestamp()",
            "",
            "WITH count(*) AS _dummy_db",
            "",
        ]
        q += _lineage_cypher_chunk("database_lineage_edges", "ArchDatabase")

    if connections:
        # [D-2 — 2026-05-25] connection auth 정규화 (enum). 원본 mutate 회피.
        flat_connections = []
        for c in connections:
            c_flat = dict(c)
            c_flat["auth"] = normalize_connection_auth(c.get("auth"))
            flat_connections.append(c_flat)
        params["connections"] = flat_connections
        q += [
            "UNWIND $connections AS connData",
            "OPTIONAL MATCH (src {id: connData.source_id, project: $project}) "
            "WHERE src:ArchService OR src:ArchDatabase",
            "OPTIONAL MATCH (tgt {id: connData.target_id, project: $project}) "
            "WHERE tgt:ArchService OR tgt:ArchDatabase",
            "FOREACH (ignore IN CASE WHEN src IS NOT NULL AND tgt IS NOT NULL "
            "THEN [1] ELSE [] END |",
            "  MERGE (src)-[rel:CONNECTS_TO]->(tgt)",
            "  SET rel.protocol = connData.protocol, "
            "rel.description = connData.description, "
            "rel.auth = connData.auth",
            ")",
            "",
            "WITH count(*) AS _dummy_conn",
            "",
        ]

    # [2026-05-19] cross-jump (Phase 1) — SPACK API ↔ ArchService 매핑.
    # LLM 출력의 api_service_mapping → (API)-[:HANDLED_BY]->(ArchService) 관계.
    api_service_mapping = arch.get("api_service_mapping") or []
    if api_service_mapping:
        params["api_service_mapping"] = api_service_mapping
        q += [
            "UNWIND $api_service_mapping AS m",
            "OPTIONAL MATCH (api:API {id: m.api_id, project: $project})",
            "OPTIONAL MATCH (svc:ArchService {id: m.service_id, project: $project})",
            "FOREACH (ignore IN CASE WHEN api IS NOT NULL AND svc IS NOT NULL "
            "THEN [1] ELSE [] END |",
            "  MERGE (api)-[r:HANDLED_BY]->(svc)",
            "  SET r.reason = m.reason",
            ")",
            "",
            "WITH count(*) AS _dummy_apimap",
            "",
        ]

    q.append("RETURN 'Architecture Sync Completed' AS Status, $project AS ProjectName")
    return "\n".join(q).strip(), params


# ─── [2026-05 Phase 3.6] Design source-stale reset ─────────────────
#
# createSpack 트랜잭션 마지막에 끼워서 — design 재생성 성공 = stale 해소.
#
# [2026-06-05 버그픽스 — stale 배너가 재생성 후에도 부활]
# 이전: MATCH (p:Project {name: $project, owner_email: $email}). 그런데 design
# 파이프라인은 DesignInput(email 필드 없음)을 받아 호출부가 email="" 를 넘겼고,
# Project 노드의 owner_email 은 실제 이메일이라 MATCH 가 0건 → reset 이 no-op →
# design_source_stale 이 영영 true → 사용자가 재생성해도 페이지 재방문 시 배너 부활.
# 또한 stale 을 SET 하는 PRD merge( MERGE (p:Project {name}) )와 READ 엔드포인트
# ( MATCH (p:Project {name}) )는 둘 다 name-only 라, owner_email 을 reset 에만
# 추가한 게 비대칭 버그였다. 팀 격리는 이미 scoped name($project)이 담당한다
# (scoped_project(name, team_id)). → SET·READ 와 동일하게 name-only 로 통일.
RESET_DESIGN_STALE_QUERY = """\
MATCH (p:Project {name: $project})
SET p.design_source_stale = false,
    p.design_last_generated_at = timestamp()
RETURN p.name AS project
"""


def build_reset_design_stale_query(project_name: str, email: str = "") -> Tuple[str, Dict[str, Any]]:
    """[Phase 3.6] design 재생성 트랜잭션에 묶을 (cypher, params) 반환.

    name-only 스코핑 — SET(PRD merge)·READ(source-stale 엔드포인트)와 동일.
    팀 격리는 scoped name 이 담당. email 인자는 하위호환용으로 받기만 하고 미사용.
    """
    return RESET_DESIGN_STALE_QUERY, {"project": project_name}
