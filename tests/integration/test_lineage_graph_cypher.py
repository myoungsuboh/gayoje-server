"""
실제 Neo4j (testcontainers) 로 lineage 그래프 Cypher 시맨틱 검증.

기본은 skip. Docker + testcontainers 환경에서:
    pip install testcontainers neo4j
    RUN_TESTCONTAINERS=1 pytest -m testcontainers tests/integration/test_lineage_graph_cypher.py

[왜 이 테스트가 필요한가 — 회귀 가드]
get_design_lineage_graph 의 ALL / BY_STORY Cypher 는 FakeNeo4j(단위) 로 검증 불가능한
다중행 집계 결함을 가졌었다. 영구 가드하는 2건:

  1. [핵심] 이전엔 `UNWIND rels AS r` + `WITH ... r ... collect()` 라 그룹키에 r 이
     들어가 rel 1개당 1행(각 행 엣지 1개)을 반환했고, _build_graph_from_records 의
     _first_row 가 첫 행만 읽어 '노드 다수 + 엣지 단 1개' 로 화면이 깨졌다.
     FakeNeo4j 는 canned 단일행만 돌려줘 이 다중행 결함을 구조적으로 못 잡는다.
     → 여러 Story·여러 rel 을 시드해도 **정확히 1행** 이 나오고 그 1행에 모든 엣지가
        집계되는지(엣지 1개로 줄지 않는지) 검증.

  2. API 는 (API)-[:IMPLEMENTS]->(Story) 로 연결되는데 DERIVED_FROM 만 매치하면
     API lineage 엣지가 통째로 누락된다. → IMPLEMENTS 엣지가 실려오는지 검증.
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
    _LINEAGE_GRAPH_ALL_CYPHER,
    _LINEAGE_GRAPH_BY_STORY_CYPHER,
)

pytestmark = pytest.mark.testcontainers

_PROJECT = "tc_lineage"


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


# 실제 design/PRD 파이프라인이 만드는 lineage 엣지 방향과 동일하게 그래프를 구성한다.
#   (Epic)-[:CONTAINS]->(Story)                            ← Epic 묶음
#   (Entity)-[:DERIVED_FROM {confidence,quote}]->(Story)   ← design 도출
#   (ArchService)-[:DERIVED_FROM]->(Story)                 ← design 도출
#   (API)-[:IMPLEMENTS]->(Story)                           ← API 구현 링크
#
# 일부러 Story 2개 + rel 6개를 만든다 — 다중행 집계 결함(UNWIND→rel당 1행)이 살아
# 있었다면 이 시드에서 ALL Cypher 가 6행을 반환(각 행 엣지 1개)했을 것이다.
_BUILD_LINEAGE_GRAPH = """
CREATE (epic:Epic   {id: 'epic_lg_1',  project: 'tc_lineage', title: 'Checkout'})
CREATE (s1:Story    {id: 'story_lg_1', project: 'tc_lineage', summary: 'place order'})
CREATE (s2:Story    {id: 'story_lg_2', project: 'tc_lineage', summary: 'pay order'})
CREATE (e1:Entity   {id: 'ent_lg_1',   project: 'tc_lineage', name: 'Order'})
CREATE (e2:Entity   {id: 'ent_lg_2',   project: 'tc_lineage', name: 'Payment'})
CREATE (svc:ArchService {id: 'svc_lg_1', project: 'tc_lineage', name: 'order-svc'})
CREATE (api:API     {id: 'api_lg_1',   project: 'tc_lineage', endpoint: '/orders', method: 'POST'})
CREATE (epic)-[:CONTAINS]->(s1)
CREATE (epic)-[:CONTAINS]->(s2)
CREATE (e1)-[:DERIVED_FROM {confidence: 'direct', quote: 'the order'}]->(s1)
CREATE (svc)-[:DERIVED_FROM {confidence: 'inferred'}]->(s1)
CREATE (api)-[:IMPLEMENTS]->(s1)
CREATE (e2)-[:DERIVED_FROM {confidence: 'direct'}]->(s2)
"""


def _edge_key(edge):
    return (edge["source_id"], edge["target_id"], edge["type"])


@pytest.fixture(scope="module")
def all_rows(neo4j_session):
    """lineage 그래프 적재 후 ALL Cypher 의 모든 행 반환."""
    neo4j_session.run(_BUILD_LINEAGE_GRAPH)
    return list(neo4j_session.run(_LINEAGE_GRAPH_ALL_CYPHER, project=_PROJECT))


@pytest.fixture(scope="module")
def by_story_rows(neo4j_session, all_rows):
    """story_lg_1 focus 모드 행 반환 (all_rows 가 먼저 시드를 적재하도록 의존)."""
    return list(
        neo4j_session.run(
            _LINEAGE_GRAPH_BY_STORY_CYPHER,
            project=_PROJECT,
            focus_story_id="story_lg_1",
        )
    )


# ── ALL 모드 ────────────────────────────────────────────────────────────────
def test_all_returns_exactly_one_row(all_rows):
    """[핵심 회귀 가드] rel 6개를 시드해도 정확히 1행 — 다중행(UNWIND) 결함 차단.

    _build_graph_from_records 는 _first_row 만 읽으므로, 2행 이상이면 엣지가
    누락된다. 옛 결함 cypher 는 여기서 6행을 반환했다.
    """
    assert len(all_rows) == 1, (
        f"ALL lineage cypher 가 {len(all_rows)}행 반환 — "
        "다중행 집계 결함(rel당 1행) 회귀!"
    )


def test_all_aggregates_every_edge_not_just_one(all_rows):
    """[핵심 회귀 가드] 단일 행에 모든 엣지가 집계 — '엣지 1개만' 버그 차단."""
    edges = all_rows[0]["edges"]
    keys = {_edge_key(e) for e in edges}
    expected = {
        ("ent_lg_1", "story_lg_1", "DERIVED_FROM"),
        ("svc_lg_1", "story_lg_1", "DERIVED_FROM"),
        ("api_lg_1", "story_lg_1", "IMPLEMENTS"),
        ("ent_lg_2", "story_lg_2", "DERIVED_FROM"),
        ("epic_lg_1", "story_lg_1", "CONTAINS"),
        ("epic_lg_1", "story_lg_2", "CONTAINS"),
    }
    assert keys == expected, f"엣지 누락/초과: got {keys}, want {expected}"


def test_all_includes_implements_api_edge(all_rows):
    """API 의 IMPLEMENTS 엣지가 실려온다 (DERIVED_FROM 만 매치하면 누락)."""
    edges = all_rows[0]["edges"]
    impl = [e for e in edges if e["type"] == "IMPLEMENTS"]
    assert len(impl) == 1
    assert impl[0]["source_id"] == "api_lg_1"
    assert impl[0]["target_id"] == "story_lg_1"


def test_all_derived_from_carries_confidence_and_quote(all_rows):
    """DERIVED_FROM 엣지는 properties 에 confidence + quote 보존."""
    edges = all_rows[0]["edges"]
    ent_edge = next(
        e for e in edges if _edge_key(e) == ("ent_lg_1", "story_lg_1", "DERIVED_FROM")
    )
    assert ent_edge["properties"]["confidence"] == "direct"
    assert ent_edge["properties"]["quote"] == "the order"


def test_all_returns_every_node(all_rows):
    """노드도 단일 행에 전부 집계 (Epic + Story×2 + Entity×2 + Service + API = 7)."""
    nodes = all_rows[0]["nodes"]
    ids = {n["id"] for n in nodes}
    assert ids == {
        "epic_lg_1",
        "story_lg_1",
        "story_lg_2",
        "ent_lg_1",
        "ent_lg_2",
        "svc_lg_1",
        "api_lg_1",
    }


# ── BY_STORY (focus) 모드 ─────────────────────────────────────────────────────
def test_by_story_returns_exactly_one_row(by_story_rows):
    """focus 모드도 rel 4개에 정확히 1행 — 다중행 결함 차단."""
    assert len(by_story_rows) == 1, (
        f"BY_STORY cypher 가 {len(by_story_rows)}행 반환 — 다중행 회귀!"
    )


def test_by_story_aggregates_focused_edges(by_story_rows):
    """story_lg_1 에 연결된 엣지만, 전부 집계 (DERIVED_FROM×2 + IMPLEMENTS + CONTAINS)."""
    edges = by_story_rows[0]["edges"]
    keys = {_edge_key(e) for e in edges}
    assert keys == {
        ("ent_lg_1", "story_lg_1", "DERIVED_FROM"),
        ("svc_lg_1", "story_lg_1", "DERIVED_FROM"),
        ("api_lg_1", "story_lg_1", "IMPLEMENTS"),
        ("epic_lg_1", "story_lg_1", "CONTAINS"),
    }
    # story_lg_2 의 엣지는 새지 않아야 함
    assert ("ent_lg_2", "story_lg_2", "DERIVED_FROM") not in keys


def test_by_story_includes_implements_api_edge(by_story_rows):
    """focus 모드에서도 API IMPLEMENTS 엣지 포함."""
    edges = by_story_rows[0]["edges"]
    assert any(
        e["type"] == "IMPLEMENTS" and e["source_id"] == "api_lg_1" for e in edges
    )
