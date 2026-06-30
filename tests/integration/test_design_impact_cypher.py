"""
실제 Neo4j (testcontainers) 로 design impact / quality Cypher 시맨틱 검증.

기본은 skip. Docker + testcontainers 환경에서:
    pip install testcontainers neo4j
    RUN_TESTCONTAINERS=1 pytest -m testcontainers tests/integration/test_design_impact_cypher.py

[왜 이 테스트가 필요한가 — 회귀 가드]
impact cascade 는 FakeNeo4j 로 검증 불가능한 영역이다 (OPTIONAL MATCH 의 NULL
coalescing, collect(DISTINCT) 의 cross-product dedup, 관계 타입별 traversal).
특히 다음 두 회귀를 영구 가드한다:

  1. [핵심] API 는 (API)-[:IMPLEMENTS]->(Story) 로 연결되는데, L1 traversal 이
     DERIVED_FROM 만 매칭하면 API 가 design_layer 에 절대 안 잡히고 그에 의존하는
     L3(HANDLED_BY)/L4b(CALLS_API)/L5(api_chain)/error_cases 가 전부 dead 가 된다.
     → design_layer 에 API 가 있고, arch/api_chain 이 채워지는지 검증.

  2. tier: IMPLEMENTS(API) 는 명시적 구현 링크라 'confirmed', DERIVED_FROM 은
     confidence(direct/inferred) 기준. rel_type 이 응답에 실려오는지 검증.
"""
from __future__ import annotations

import os

import pytest

testcontainers_neo4j = pytest.importorskip(
    "testcontainers.neo4j",
    reason="testcontainers-neo4j 미설치 — `pip install testcontainers`",
)
neo4j_driver = pytest.importorskip(
    "neo4j", reason="neo4j driver 미설치 — `pip install neo4j`"
)

from app.service.query_repository import (  # noqa: E402
    _GET_DESIGN_IMPACT_CYPHER,
    _GET_DESIGN_QUALITY_CYPHER,
)

pytestmark = pytest.mark.testcontainers


@pytest.fixture(scope="module")
def neo4j_session():
    """Neo4j 5 컨테이너 → driver session (module 범위 재사용)."""
    image = os.getenv("NEO4J_TEST_IMAGE", "neo4j:5.13")
    with testcontainers_neo4j.Neo4jContainer(image) as neo:
        uri = neo.get_connection_url()
        password = (
            neo.NEO4J_ADMIN_PASSWORD
            if hasattr(neo, "NEO4J_ADMIN_PASSWORD")
            else "password"
        )
        driver = neo4j_driver.GraphDatabase.driver(uri, auth=("neo4j", password))
        try:
            with driver.session() as session:
                yield session
        finally:
            driver.close()


# 실제 design 파이프라인이 만드는 엣지 방향과 동일하게 그래프를 구성한다.
#   (API)-[:IMPLEMENTS]->(Story)                  ← L1 (Fix-B 핵심)
#   (Entity)-[:DERIVED_FROM {confidence,quote}]->(Story)  ← L1
#   (Entity)-[:MAPPED_TO {role}]->(Aggregate)     ← L2 / quality
#   (Aggregate)-[:BELONGS_TO]->(BoundedContext)   ← L2b
#   (API)-[:HANDLED_BY]->(ArchService)            ← L3
#   (ArchService)-[:CONNECTS_TO]->(ArchService2)  ← L3b
#   (API2)-[:HANDLED_BY]->(ArchService2)          ← L5 peer
#   (Screen)-[:RENDERS]->(Story)                  ← L4a
#   (Screen)-[:CALLS_API]->(API)                  ← L4b
#   (Story)-[:TRIGGERS]->(DomainEvent)            ← L6
#   (Aggregate)-[:PUBLISHES]->(DomainEvent)       ← L6b
_BUILD_IMPACT_GRAPH = """
CREATE (story:Story {id: 'story_im_1', project: 'tc_impact', summary: 'S', user_edited_at: 1000})
CREATE (api:API {id: 'api_im_1', project: 'tc_impact', endpoint: '/orders', method: 'GET',
                 error_cases: '[{"code":401,"description":"Unauthorized"}]'})
CREATE (ent:Entity {id: 'ent_im_1', project: 'tc_impact', name: 'Order'})
CREATE (agg:Aggregate {id: 'agg_im_1', project: 'tc_impact', name: 'OrderAgg'})
CREATE (ctx:BoundedContext {id: 'ctx_im_1', project: 'tc_impact', name: 'Sales'})
CREATE (svc1:ArchService {id: 'svc_im_1', project: 'tc_impact', name: 'order-svc'})
CREATE (svc2:ArchService {id: 'svc_im_2', project: 'tc_impact', name: 'pay-svc'})
CREATE (api2:API {id: 'api_im_2', project: 'tc_impact', endpoint: '/pay', method: 'POST'})
CREATE (scr:Screen {id: 'scr_im_1', project: 'tc_impact', name: 'OrderScreen'})
CREATE (evt:DomainEvent {id: 'evt_im_1', project: 'tc_impact', name: 'OrderPlaced'})
CREATE (api)-[:IMPLEMENTS]->(story)
CREATE (ent)-[:DERIVED_FROM {confidence: 'direct', quote: 'the order'}]->(story)
CREATE (ent)-[:MAPPED_TO {role: 'aggregate_root'}]->(agg)
CREATE (agg)-[:BELONGS_TO]->(ctx)
CREATE (api)-[:HANDLED_BY]->(svc1)
CREATE (svc1)-[:CONNECTS_TO]->(svc2)
CREATE (api2)-[:HANDLED_BY]->(svc2)
CREATE (scr)-[:RENDERS]->(story)
CREATE (scr)-[:CALLS_API]->(api)
CREATE (story)-[:TRIGGERS]->(evt)
CREATE (agg)-[:PUBLISHES]->(evt)
"""


@pytest.fixture(scope="module")
def impact_row(neo4j_session):
    """impact 그래프 적재 후 _GET_DESIGN_IMPACT_CYPHER 단일 row 반환."""
    neo4j_session.run(_BUILD_IMPACT_GRAPH)
    rows = list(
        neo4j_session.run(
            _GET_DESIGN_IMPACT_CYPHER, project="tc_impact", since=0
        )
    )
    # 편집된 Story 1개 → row 1개
    assert len(rows) == 1, f"expected 1 changed node, got {len(rows)}"
    return rows[0]


def _ids(layer):
    return {item["id"] for item in layer}


def test_impact_design_layer_includes_api_via_implements(impact_row):
    """[회귀 가드 #1] API(IMPLEMENTS) 와 Entity(DERIVED_FROM) 둘 다 design_layer 에."""
    design = impact_row["design_layer"]
    ids = _ids(design)
    assert "api_im_1" in ids, "API 가 design_layer 에 없음 — IMPLEMENTS traversal 회귀!"
    assert "ent_im_1" in ids


def test_impact_api_carries_rel_type_and_error_cases(impact_row):
    """API 항목은 rel_type=IMPLEMENTS + error_cases 보존 (tier=confirmed 근거)."""
    api_item = next(d for d in impact_row["design_layer"] if d["id"] == "api_im_1")
    assert api_item["label"] == "API"
    assert api_item["rel_type"] == "IMPLEMENTS"
    assert "401" in api_item["error_cases"]


def test_impact_entity_carries_derived_from_confidence(impact_row):
    """Entity 항목은 rel_type=DERIVED_FROM + confidence=direct."""
    ent_item = next(d for d in impact_row["design_layer"] if d["id"] == "ent_im_1")
    assert ent_item["rel_type"] == "DERIVED_FROM"
    assert ent_item["confidence"] == "direct"
    assert ent_item["quote"] == "the order"


def test_impact_arch_layer_alive_via_api_handled_by(impact_row):
    """[회귀 가드 #1 연쇄] L3/L3b — API→HANDLED_BY→svc1→CONNECTS_TO→svc2."""
    arch_ids = _ids(impact_row["arch_layer"])
    assert "svc_im_1" in arch_ids
    assert "svc_im_2" in arch_ids


def test_impact_api_chain_alive(impact_row):
    """[회귀 가드 #1 연쇄] L5 — svc2 가 처리하는 peer API(api2)."""
    chain_ids = _ids(impact_row["api_chain_layer"])
    assert "api_im_2" in chain_ids
    # 원본 design API 는 api_chain 에서 제외돼야 함
    assert "api_im_1" not in chain_ids


def test_impact_screen_layer(impact_row):
    """L4a/L4b — Story 렌더링 + API 호출 Screen."""
    assert "scr_im_1" in _ids(impact_row["screen_layer"])


def test_impact_event_layer(impact_row):
    """L6 — Story TRIGGERS DomainEvent."""
    assert "evt_im_1" in _ids(impact_row["event_layer"])


def test_impact_ddd_layer_aggregate_and_context(impact_row):
    """L2/L2b/L6b — Aggregate(MAPPED_TO) + BoundedContext(BELONGS_TO)."""
    ddd_ids = _ids(impact_row["ddd_layer"])
    assert "agg_im_1" in ddd_ids
    assert "ctx_im_1" in ddd_ids


# ── quality 체크 ───────────────────────────────────────────────────────────
_BUILD_QUALITY_GRAPH = """
CREATE (ok:Aggregate {id: 'q_ok', project: 'tc_quality', name: 'OkAgg'})
CREATE (miss:Aggregate {id: 'q_missing', project: 'tc_quality', name: 'MissAgg'})
CREATE (multi:Aggregate {id: 'q_multi', project: 'tc_quality', name: 'MultiAgg'})
CREATE (e1:Entity {id: 'q_e1', project: 'tc_quality'})
CREATE (e2:Entity {id: 'q_e2', project: 'tc_quality'})
CREATE (e3:Entity {id: 'q_e3', project: 'tc_quality'})
CREATE (e1)-[:MAPPED_TO {role: 'aggregate_root'}]->(ok)
CREATE (e2)-[:MAPPED_TO {role: 'aggregate_root'}]->(multi)
CREATE (e3)-[:MAPPED_TO {role: 'aggregate_root'}]->(multi)
"""


@pytest.fixture(scope="module")
def quality_rows(neo4j_session):
    neo4j_session.run(_BUILD_QUALITY_GRAPH)
    return list(neo4j_session.run(_GET_DESIGN_QUALITY_CYPHER, project="tc_quality"))


def test_quality_flags_missing_and_multiple_only(quality_rows):
    """root 0개 → missing, 2개 → multiple, 정확히 1개 → 위반 아님."""
    by_id = {r["aggregate_id"]: r for r in quality_rows}
    assert "q_ok" not in by_id, "root 1개인 Aggregate 가 위반으로 잡힘 (false positive)"
    assert by_id["q_missing"]["violation_type"] == "missing_aggregate_root"
    assert by_id["q_missing"]["root_count"] == 0
    assert by_id["q_multi"]["violation_type"] == "multiple_aggregate_roots"
    assert by_id["q_multi"]["root_count"] == 2
    assert set(by_id["q_multi"]["root_entity_ids"]) == {"q_e2", "q_e3"}
