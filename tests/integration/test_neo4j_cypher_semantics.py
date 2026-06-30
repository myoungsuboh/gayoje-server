"""
실제 Neo4j 인스턴스 (testcontainers) 로 Cypher 의미 검증.

기본은 skip. Docker + testcontainers 설치 환경에서:
    pip install testcontainers neo4j
    RUN_TESTCONTAINERS=1 pytest -m testcontainers

[검증 목표]
  - build_save_cps_query 가 생성한 UNWIND Cypher 가 실제 동작
  - MERGE 의 실제 멱등성 — 동일 graph 2회 적재 → 같은 노드 수
  - canonicalize_graph 가 입력 순서 다르더라도 실제 DB 상태 동일
  - SOLVES 관계 방향 정확 (Solution -> Problem)

[와 testcontainers 인가]
FakeNeo4j 는 cypher 문자열만 기록. MERGE 의 실제 멱등성, OPTIONAL MATCH
의 NULL coalescing, UNWIND 의 팬 필드, FOREACH 의 조건부 실행 은 fake 가 모사
불가능. testcontainers 는 이 곳을 메워주는 e2e 안전망.

[PR D 스콤프]
핵심 보증 수준 테스트 (3-4개) 만 널는다. 더 널은 커버리지는 PR F+ 에서
단계적으로 확장 (testcontainers 트랜잭션, master 재합성 등).
"""
from __future__ import annotations

import os

import pytest

# 의존 import 의 ImportError 는 테스트 수집 시점에 module-level skip 으로 전환.
# testcontainers / neo4j 패키지가 설치 안 된 로컬에서도 import 자체는 안전.
testcontainers_neo4j = pytest.importorskip(
    "testcontainers.neo4j",
    reason="testcontainers-neo4j 미설치 — `pip install testcontainers`",
)
neo4j_driver = pytest.importorskip(
    "neo4j", reason="neo4j driver 미설치 — `pip install neo4j`"
)

from app.pipelines.base import canonicalize_graph  # noqa: E402
from app.pipelines.cps_pipeline.cypher import build_save_cps_query  # noqa: E402
from app.service.query_repository import (  # noqa: E402
    _GET_PROJECT_GRAPH_FALLBACK_CYPHER,
)

# 모든 테스트는 testcontainers marker — conftest.py 가 RUN_TESTCONTAINERS!=1 이면
# 자동 skip. 이중 가드 (importorskip + marker) 로 CI/로컬 주앮을 메움.
pytestmark = pytest.mark.testcontainers


@pytest.fixture(scope="module")
def neo4j_session():
    """
    Neo4j 5 컨테이너 띄워서 driver session yield — module 범위 (테스트당 재사용).
    setup 속도: ~20-30초. 로컬 dev 에서는 RUN_TESTCONTAINERS=1 필수 용도 제한.
    """
    image = os.getenv("NEO4J_TEST_IMAGE", "neo4j:5.13")
    with testcontainers_neo4j.Neo4jContainer(image) as neo:
        uri = neo.get_connection_url()
        # Neo4jContainer 는 기본 password 서정을 잘 해주는데 driver auth 는 명시적 필요.
        password = neo.NEO4J_ADMIN_PASSWORD if hasattr(neo, "NEO4J_ADMIN_PASSWORD") else "password"
        driver = neo4j_driver.GraphDatabase.driver(uri, auth=("neo4j", password))
        try:
            with driver.session() as session:
                # 각 test 전 초기화 없이 module 차원 재사용 — 테스트가 어떤 프로젝트
                # 이름을 쓰는지 명시적으로 분리 — 아래 테스트들의 'project' params 이 서로 겹치지
                # 않는 것을 확인.
                yield session
        finally:
            driver.close()


def test_save_cps_query_creates_problem_node(neo4j_session):
    """UNWIND 기반 MERGE 가 실제 노드 생성."""
    g = {
        "nodes": [
            {"id": "prb_tc_01", "label": "Problem", "properties": {"summary": "테스트"}},
        ],
        "relationships": [],
    }
    cypher, params = build_save_cps_query(g)
    neo4j_session.run(cypher, **params)

    result = neo4j_session.run(
        "MATCH (n:Problem {id: 'prb_tc_01'}) RETURN n.summary AS s"
    ).single()
    assert result is not None
    assert result["s"] == "테스트"


def test_save_cps_query_is_idempotent_on_rerun(neo4j_session):
    """동일 graph 2회 적재 → MERGE 멱등성 으로 노드 1개."""
    g = {
        "nodes": [{"id": "prb_tc_idem", "label": "Problem", "properties": {"summary": "X"}}],
        "relationships": [],
    }
    cypher, params = build_save_cps_query(g)
    neo4j_session.run(cypher, **params)
    neo4j_session.run(cypher, **params)  # 2회 실행

    cnt = neo4j_session.run(
        "MATCH (n:Problem {id: 'prb_tc_idem'}) RETURN count(n) AS c"
    ).single()["c"]
    assert cnt == 1


def test_save_cps_query_node_order_does_not_affect_db_state(neo4j_session):
    """canonicalize_graph 대 입력 순서 차이가 실제 DB 상태에도 흔적 없음.
    멱등성 + DB 레벨 확인 (canonicalize_graph 가 입력 순서 정리 후 적재)."""
    g_a = {
        "nodes": [
            {"id": "prb_tc_A", "label": "Problem", "properties": {"summary": "A"}},
            {"id": "prb_tc_B", "label": "Problem", "properties": {"summary": "B"}},
        ],
        "relationships": [],
    }
    g_b = {
        "nodes": [
            {"id": "prb_tc_B", "label": "Problem", "properties": {"summary": "B"}},
            {"id": "prb_tc_A", "label": "Problem", "properties": {"summary": "A"}},
        ],
        "relationships": [],
    }
    cypher_a, params_a = build_save_cps_query(canonicalize_graph(g_a))
    cypher_b, params_b = build_save_cps_query(canonicalize_graph(g_b))

    # canonicalize 거친 후 cypher 자체가 동일해야함
    assert cypher_a == cypher_b
    neo4j_session.run(cypher_a, **params_a)

    nodes = list(
        neo4j_session.run(
            "MATCH (n:Problem) WHERE n.id IN ['prb_tc_A', 'prb_tc_B'] RETURN n.id AS id ORDER BY n.id"
        )
    )
    assert [r["id"] for r in nodes] == ["prb_tc_A", "prb_tc_B"]


def test_save_cps_query_creates_solves_relationship_with_correct_direction(neo4j_session):
    """Solution -[:SOLVES]-> Problem — 관계 방향이 실제 DB 에도 올바로 적재.
    관계 방향은 LLM/code/DB 세 계층 모두 일치해야 하는 부분."""
    g = {
        "nodes": [
            # [2026-06] summary 부여 — _is_meaningful_spec_node 가드(핵심 필드 0개 spec
            # 노드 drop) 도입 후, 빈 properties 면 노드가 drop 돼 빈 query 가 됐다.
            {"id": "prb_tc_rel", "label": "Problem", "properties": {"summary": "문제"}},
            {"id": "res_tc_rel", "label": "Solution", "properties": {"summary": "해법"}},
        ],
        "relationships": [
            {"source": "res_tc_rel", "type": "SOLVES", "target": "prb_tc_rel"},
        ],
    }
    cypher, params = build_save_cps_query(g)
    neo4j_session.run(cypher, **params)

    # (Solution)-[:SOLVES]->(Problem) 방향이 정확한지 확인
    rel = neo4j_session.run(
        "MATCH (s:Solution {id: 'res_tc_rel'})-[r:SOLVES]->(p:Problem {id: 'prb_tc_rel'}) "
        "RETURN count(r) AS c"
    ).single()["c"]
    assert rel == 1
    # 역방향은 없어야 함
    rev = neo4j_session.run(
        "MATCH (p:Problem {id: 'prb_tc_rel'})-[r:SOLVES]->(s:Solution {id: 'res_tc_rel'}) "
        "RETURN count(r) AS c"
    ).single()["c"]
    assert rev == 0


def test_save_cps_query_scopes_same_id_across_projects(neo4j_session):
    """[멀티테넌시 회귀 가드] 서로 다른 프로젝트가 동일 spec id(prb_01)를 써도
    각자 별도 노드로 격리된다 (project 복합 키 MERGE).

    과거 id-only MERGE 에선 두 프로젝트의 prb_01 이 전역 단일 노드를 공유해
    뒤 writer 가 앞 writer 의 project/summary 를 덮어썼다 (cross-project 오염).
    """
    g_a = {
        "nodes": [{"id": "prb_01", "label": "Problem", "properties": {"summary": "A의 문제"}}],
        "relationships": [],
    }
    g_b = {
        "nodes": [{"id": "prb_01", "label": "Problem", "properties": {"summary": "B의 문제"}}],
        "relationships": [],
    }
    cy_a, pa = build_save_cps_query(g_a, project_name="proj_a")
    cy_b, pb = build_save_cps_query(g_b, project_name="proj_b")
    neo4j_session.run(cy_a, **pa)
    neo4j_session.run(cy_b, **pb)  # 같은 id, 다른 project

    # 동일 id 지만 project 별로 2개 노드 — 충돌 없음.
    total = neo4j_session.run(
        "MATCH (n:Problem {id: 'prb_01'}) RETURN count(n) AS c"
    ).single()["c"]
    assert total == 2
    # A 프로젝트 노드는 A 내용 그대로 (B 가 덮어쓰지 않음).
    a_sum = neo4j_session.run(
        "MATCH (n:Problem {id: 'prb_01', project: 'proj_a'}) RETURN n.summary AS s"
    ).single()["s"]
    assert a_sum == "A의 문제"
    b_sum = neo4j_session.run(
        "MATCH (n:Problem {id: 'prb_01', project: 'proj_b'}) RETURN n.summary AS s"
    ).single()["s"]
    assert b_sum == "B의 문제"


def test_get_project_graph_read_isolates_same_id_across_projects(neo4j_session):
    """[A — 읽기 경로 격리 회귀 가드] A1(노드 MERGE 스코프)이 노드 *수* 만이 아니라
    실제 읽기 API(get_project_graph)에서도 프로젝트별로 격리됨을 end-to-end 로 증명.

    과거 id-only MERGE 에선 두 프로젝트가 prb_01 전역 노드를 공유 → 뒤 writer 가
    project 를 덮어써 앞 프로젝트의 `WHERE n.project=$project` 읽기에서 노드가 증발
    했다. A1 이후엔 각 프로젝트가 자기 스코프 노드를 가지므로, get_project_graph 의
    실제 Cypher(_GET_PROJECT_GRAPH_FALLBACK_CYPHER, APOC 불필요)로 읽어도 자기
    프로젝트 노드/내용만 보여야 한다.
    """
    g_a = {
        "nodes": [
            {"id": "prb_iso", "label": "Problem", "properties": {"summary": "A 격리"}},
            {"id": "res_iso", "label": "Solution", "properties": {"summary": "A 해법"}},
        ],
        "relationships": [
            {"source": "res_iso", "type": "SOLVES", "target": "prb_iso"},
        ],
    }
    g_b = {
        "nodes": [
            {"id": "prb_iso", "label": "Problem", "properties": {"summary": "B 격리"}},
        ],
        "relationships": [],
    }
    cy_a, pa = build_save_cps_query(g_a, project_name="iso_a")
    cy_b, pb = build_save_cps_query(g_b, project_name="iso_b")
    neo4j_session.run(cy_a, **pa)
    neo4j_session.run(cy_b, **pb)  # 같은 prb_iso id, 다른 project

    # get_project_graph 의 실제 읽기 Cypher 로 iso_a 만 조회.
    row_a = neo4j_session.run(
        _GET_PROJECT_GRAPH_FALLBACK_CYPHER, project="iso_a"
    ).single()
    nodes_a = {n["id"]: n["properties"].get("summary") for n in row_a["nodes"]}
    # A 는 자기 prb_iso(=A 내용) + res_iso 둘 다 보여야 하고, summary 는 A 것.
    assert nodes_a.get("prb_iso") == "A 격리"  # B 가 덮어쓰지 않음
    assert "res_iso" in nodes_a
    # SOLVES 엣지도 A 스코프 안에서 정확히 연결.
    edges_a = {(e["source_id"], e["target_id"], e["type"]) for e in row_a["edges"]}
    assert ("res_iso", "prb_iso", "SOLVES") in edges_a

    # iso_b 읽기는 자기 prb_iso(=B 내용)만, A 의 res_iso/SOLVES 는 안 보임.
    row_b = neo4j_session.run(
        _GET_PROJECT_GRAPH_FALLBACK_CYPHER, project="iso_b"
    ).single()
    nodes_b = {n["id"]: n["properties"].get("summary") for n in row_b["nodes"]}
    assert nodes_b.get("prb_iso") == "B 격리"
    assert "res_iso" not in nodes_b  # A 의 노드는 B 읽기에 누출 안 됨
    edges_b = {(e["source_id"], e["target_id"], e["type"]) for e in row_b["edges"]}
    assert ("res_iso", "prb_iso", "SOLVES") not in edges_b
