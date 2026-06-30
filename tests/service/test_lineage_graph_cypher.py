"""
[2026-06 회귀 가드] Design↔PRD lineage 그래프 cypher 결함 재발 방지.

발견된 버그:
 (1) DERIVED_FROM 만 매치 → API(IMPLEMENTS 로 Story 연결) lineage 엣지 전부 누락.
 (2) `UNWIND rels AS r` + `WITH ... r ... collect()` 라 그룹키에 r 이 들어가 rel 1개당
     1행(각 행 엣지 1개)을 반환 → _build_graph_from_records 의 _first_row 가 첫 행만 읽어
     '노드 다수 + 엣지 단 1개' 로 깨짐.

cypher 는 Neo4j 없이는 실행 검증이 불가하므로(integration 부재가 미탐의 원인이었음),
구조적 불변식을 정적으로 가드한다 — 검증된 _GET_PROJECT_GRAPH_CYPHER 와 동일한
list-comprehension 집계를 쓰고, IMPLEMENTS 를 포함하는지.
"""
from app.service.query_repository import (
    _LINEAGE_GRAPH_ALL_CYPHER,
    _LINEAGE_GRAPH_BY_STORY_CYPHER,
)


def test_lineage_cyphers_match_both_derived_from_and_implements():
    # API 는 IMPLEMENTS 로 Story 에 연결 — 둘 다 매치해야 API lineage 엣지가 안 누락됨.
    for cy in (_LINEAGE_GRAPH_ALL_CYPHER, _LINEAGE_GRAPH_BY_STORY_CYPHER):
        assert "DERIVED_FROM|IMPLEMENTS" in cy


def test_lineage_cyphers_aggregate_edges_in_one_row():
    # 엣지는 list comprehension `[r IN rels | {...}]` 으로 한 행에 집계해야 한다
    # (검증된 _GET_PROJECT_GRAPH_CYPHER 와 동일 패턴). rel 단위 UNWIND 다중행이면
    # _first_row 가 엣지 1개만 읽어 깨진다.
    for cy in (_LINEAGE_GRAPH_ALL_CYPHER, _LINEAGE_GRAPH_BY_STORY_CYPHER):
        assert "[r IN rels |" in cy, "엣지는 list comprehension 으로 집계해야 함"
