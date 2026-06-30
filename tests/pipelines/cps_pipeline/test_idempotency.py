"""
CPS Pipeline 멱등성 검증.

Neo4j MERGE 실제 멱등성은 testcontainers (Phase 6). 여기서는 코드 레벨:
  - 결과 dataclass 의 ID 들이 결정적 (project_name + version 에만 의존)
  - canonicalize_graph 가 노드 순서 차이를 흡수
  - build_save_cps_query 가 blocked / unsafe label 드롭
  - build_save_meeting_log_query 의 ID 계산 규칙
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.cps_pipeline.cypher import (
    build_save_cps_query,
    build_save_meeting_log_query,
)
from app.pipelines.cps_pipeline.pipeline import run_cps_pipeline
from app.pipelines.cps_pipeline.types import CpsInput
from tests.conftest import FakeGemini, FakeNeo4j


pytestmark = pytest.mark.asyncio


_VALID_CPS = json.dumps({
    "_harness_metadata": {"state": "recording"},
    "nodes": [
        {"id": "doc_cps_x_v1", "label": "CPS_Document", "properties": {}},
        {"id": "prb_01", "label": "Problem", "properties": {"summary": "X"}},
        {"id": "res_01", "label": "Solution", "properties": {"summary": "Y"}},
    ],
    "relationships": [
        {"source": "res_01", "type": "SOLVES", "target": "prb_01"},
        {"source": "prb_01", "type": "EXTRACTED_FROM", "target": "doc_cps_x_v1"},
    ],
})
_IMPACT_EMPTY = json.dumps({"affected_sections": [], "removed_prb_ids": [], "removed_res_ids": [], "analysis": ""})
_MERGE_TEXT = "### 1. Problem\n- A\n### 2. Solution\n- B"
_MEETING_LONG = "테스트 회의 내용 50자 이상으로 충분히 길게 작성하여 fallback 회피."


# ─── 재실행 멱등성 ─────────────────────────────────────────────────


async def test_same_input_produces_same_ids():
    """동일 CpsInput 2회 호출 → meeting_log_id / delta_cps_id / master_cps_id 동일."""
    payload = CpsInput(
        project_name="x",
        version="v1",
        date="2026-05-19",
        meeting_content=_MEETING_LONG,
    )

    async def _run():
        gemini = FakeGemini(responses=[_VALID_CPS, _IMPACT_EMPTY, _MERGE_TEXT])
        neo4j = FakeNeo4j(responses=[[], [], [], []])
        ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="k")
        result = await run_cps_pipeline(ctx, payload)
        return result.meeting_log_id, result.delta_cps_id, result.master_cps_id

    first = await _run()
    second = await _run()
    assert first == second
    # 구체 값 검증
    assert first == ("log_x_v1", "doc_cps_x_v1", "doc_cps_master_x")


async def test_previous_cps_id_overrides_derived_id():
    """previous_cps_id 가 들어오면 derived_cps_id 가 각 입력 그대로."""
    payload = CpsInput(
        project_name="x",
        version="v2",
        date="d",
        meeting_content=_MEETING_LONG,
        previous_cps_id="doc_cps_x_v1",
    )
    gemini = FakeGemini(responses=[_VALID_CPS, _IMPACT_EMPTY, _MERGE_TEXT])
    neo4j = FakeNeo4j(responses=[[], [], [], []])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="k")

    result = await run_cps_pipeline(ctx, payload)
    # previous_cps_id 이 있으면 그게 delta_cps_id
    assert result.delta_cps_id == "doc_cps_x_v1"
    # master 는 항상 project 기반 (version 무관)
    assert result.master_cps_id == "doc_cps_master_x"
    # log 는 version 기반
    assert result.meeting_log_id == "log_x_v2"


# ─── build_save_meeting_log_query ─────────────────────────────────────────


def test_save_meeting_log_normalizes_version_dot_to_underscore():
    """version 의 dot 이 underscore 로 변환된 log_id / target_cps_id."""
    payload = CpsInput(
        project_name="food",
        version="v1.5",
        date="d",
        meeting_content="c",
    )
    _, params = build_save_meeting_log_query(payload)
    assert params["log_id"] == "log_food_v1_5"
    assert params["target_cps_id"] == "doc_cps_food_v1_5"
    assert params["version"] == "v1.5"  # 원본 보존
    assert params["project"] == "food"


def test_save_meeting_log_uses_previous_cps_id_when_provided():
    """previous_cps_id 있으면 target_cps_id 는 그것."""
    payload = CpsInput(
        project_name="food",
        version="v2",
        date="d",
        meeting_content="c",
        previous_cps_id="doc_cps_food_v1",
    )
    _, params = build_save_meeting_log_query(payload)
    assert params["target_cps_id"] == "doc_cps_food_v1"
    assert params["log_id"] == "log_food_v2"  # log_id 는 여전히 현재 version


# ─── build_save_cps_query ───────────────────────────────────────────────


def test_save_cps_query_groups_nodes_by_label():
    """같은 라벨 노드들은 하나의 UNWIND 블록."""
    g = {
        "nodes": [
            {"id": "prb_01", "label": "Problem", "properties": {"summary": "p1"}},
            {"id": "prb_02", "label": "Problem", "properties": {"summary": "p2"}},
            {"id": "res_01", "label": "Solution", "properties": {"summary": "s1"}},
        ],
        "relationships": [],
    }
    cypher, params = build_save_cps_query(g)
    # Problem 1개, Solution 1개 블록
    assert cypher.count("노드 생성: Problem") == 1
    assert cypher.count("노드 생성: Solution") == 1
    # params 에 Problem 논더윈드 노드 2개
    problem_params = [v for k, v in params.items() if k.startswith("n_")]
    # 첫 블록 또는 둘째 블록 중 Problem 의 list len == 2
    has_two_prb = any(
        isinstance(v, list) and len(v) == 2 and all("prb_" in item["id"] for item in v)
        for v in problem_params
    )
    assert has_two_prb


def test_save_cps_query_drops_blocked_labels():
    """Project / User 라벨 노드는 _BLOCKED_LABELS 로 drop."""
    g = {
        "nodes": [
            {"id": "prb_01", "label": "Problem", "properties": {"summary": "X"}},
            {"id": "proj_a", "label": "Project", "properties": {}},  # blocked
            {"id": "user_b", "label": "User", "properties": {}},  # blocked
        ],
        "relationships": [
            {"source": "prb_01", "target": "proj_a", "type": "BELONGS_TO"},  # blocked 참조 → drop
            {"source": "prb_01", "target": "user_b", "type": "OWNED_BY"},
        ],
    }
    cypher, _ = build_save_cps_query(g)
    assert "Problem" in cypher
    assert "노드 생성: Project" not in cypher
    assert "노드 생성: User" not in cypher
    # 관계도 blocked 참조라 함께 drop
    assert "BELONGS_TO" not in cypher
    assert "OWNED_BY" not in cypher


def test_save_cps_query_drops_unsafe_labels_and_types():
    """is_safe_cypher_identifier 통과 못 하는 라벨/관계 타입은 drop."""
    g = {
        "nodes": [
            {"id": "prb_01", "label": "Problem", "properties": {"summary": "p1"}},
            {"id": "bad1", "label": "Bad-Label", "properties": {"summary": "x"}},  # hyphen 포함
            {"id": "bad2", "label": "123Numeric", "properties": {"summary": "x"}},  # 숫자 시작
        ],
        "relationships": [
            {"source": "prb_01", "target": "prb_01", "type": "REL_OK"},
            {"source": "prb_01", "target": "prb_01", "type": "Bad Type"},  # 공백
        ],
    }
    cypher, _ = build_save_cps_query(g)
    assert "Problem" in cypher
    assert "REL_OK" in cypher
    assert "Bad-Label" not in cypher
    assert "123Numeric" not in cypher
    assert "Bad Type" not in cypher


def test_save_cps_query_empty_graph_returns_empty_string():
    """노드 0개 → 빈 cypher string."""
    cypher, params = build_save_cps_query({"nodes": [], "relationships": []})
    assert cypher == ""
    assert params == {}


def test_save_cps_query_skips_nodes_without_id_or_label():
    """id / label 이 비어있는 노드 스킵."""
    g = {
        "nodes": [
            {"id": "prb_01", "label": "Problem", "properties": {"summary": "p1"}},
            {"label": "Problem", "properties": {"summary": "noid"}},  # id 없음
            {"id": "x", "properties": {"summary": "nolabel"}},  # label 없음
        ],
        "relationships": [],
    }
    cypher, params = build_save_cps_query(g)
    # 하나의 Problem 블록
    assert cypher.count("노드 생성: Problem") == 1
    # n_0 list 길이 1
    prb_list = next((v for k, v in params.items() if k.startswith("n_")), None)
    assert prb_list is not None
    assert len(prb_list) == 1
    assert prb_list[0]["id"] == "prb_01"
