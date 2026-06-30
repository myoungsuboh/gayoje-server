"""
CPS 파이프라인 extract/merge 단계 분리 (batch 파이프라이닝 기반).

핵심 불변식:
  - run_cps_extract: 순수 LLM (cps_agent) — Neo4j 쓰기 0건. 누적 master 무관.
  - run_cps_merge: 미리 계산된 graph 를 받아 DB 쓰기 + impact + merge 전부 수행.
  - run_cps_pipeline = extract + merge 합성 → 기존 동작/테스트 동일 (test_pipeline_flow.py).

extract 가 DB 를 안 건드린다는 점이 데이터 안전의 근거 — prefetch 로 미리 돌려도
is_latest/master 선택 로직에 영향 0.
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext, canonicalize_graph
from app.pipelines.cps_pipeline.pipeline import run_cps_extract, run_cps_merge
from app.pipelines.cps_pipeline.types import CpsInput
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio


_VALID_CPS_RESPONSE = json.dumps({
    "_harness_metadata": {"state": "recording", "verification_passed": True},
    "nodes": [
        {"id": "doc_cps_food_v1", "label": "CPS_Document",
         "properties": {"full_markdown": "# CPS\n## Problem\n- X"}},
        {"id": "prb_01", "label": "Problem", "properties": {"summary": "느림"}},
        {"id": "res_01", "label": "Solution", "properties": {"summary": "캐시"}},
    ],
    "relationships": [
        {"source": "res_01", "type": "SOLVES", "target": "prb_01"},
        {"source": "prb_01", "type": "EXTRACTED_FROM", "target": "doc_cps_food_v1"},
    ],
})
_IMPACT_EMPTY = json.dumps({
    "affected_sections": [], "removed_prb_ids": [], "removed_res_ids": [], "analysis": "",
})
_MERGE_OUTPUT = "### 1. Problem\n- 업데이트된 문제\n### 2. Solution\n- 업데이트된 해결"
_MEETING_LONG = "오늘 회의 내용. 50자 이상으로 작성하여 impact 반환 affected_sections 가 빈일 때도 fallback 트리거를 피한다."


async def test_run_cps_extract_does_no_neo4j_writes_and_returns_graph():
    """extract 는 cps_agent LLM 1회만 — Neo4j 쓰기 0건, canonicalized graph 반환."""
    gemini = FakeGemini(responses=[_VALID_CPS_RESPONSE])
    neo4j = FakeNeo4j()
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="t-extract")
    payload = CpsInput(project_name="food", version="v1", date="d", meeting_content=_MEETING_LONG)

    graph = await run_cps_extract(ctx, payload)

    assert neo4j.executed == []           # ← 데이터 안전 핵심: 추출은 DB 안 건드림
    assert len(gemini.calls) == 1         # cps_agent 만
    assert "nodes" in graph
    assert len(graph["nodes"]) == 3
    assert graph.get("_extraction_mode") == "strict"


async def test_run_cps_merge_uses_precomputed_graph_without_calling_cps_agent():
    """merge 는 미리 계산된 graph 를 받아 impact+merge LLM 2회만 — cps_agent 호출 안 함."""
    graph = canonicalize_graph(json.loads(_VALID_CPS_RESPONSE))
    gemini = FakeGemini(responses=[_IMPACT_EMPTY, _MERGE_OUTPUT])  # cps_agent 응답 없음
    neo4j = FakeNeo4j(responses=[[], [], [], []])  # save_log, save_cps, fetch(first_run), merge
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="t-merge")
    payload = CpsInput(project_name="food", version="v1", date="d", meeting_content=_MEETING_LONG)

    result = await run_cps_merge(ctx, payload, graph)

    assert len(gemini.calls) == 2         # impact + merge (cps_agent 아님)
    assert result.mode == "first_run"
    assert result.delta_cps_id == "doc_cps_food_v1"
    assert result.master_cps_id == "doc_cps_master_food"
    assert result.cps_graph == graph      # 입력 graph 가 그대로 결과에 실림
    # 4 DB 쓰기 모두 merge 단계에서 일어남
    assert len(neo4j.executed) == 4
    assert any("Meeting_Log" in e["cypher"] for e in neo4j.executed)
    assert "MERGE (master:CPS_Document" in neo4j.executed[-1]["cypher"]


async def test_run_cps_merge_team_scope_isolates_ids_and_project():
    """[멀티테넌시] team_id 지정 시 master/delta id 와 project property 가 스코프 키로 격리.

    동명 개인 'food' 와 노드가 섞이지 않도록 — master id/delta id/project param 모두
    sentinel 합성 키 기준. LLM 콘텐츠(full_markdown)에는 sentinel 누출 없음.
    """
    from app.core.project_scope import scoped_project, cps_master_id, cps_delta_id

    graph = canonicalize_graph(json.loads(_VALID_CPS_RESPONSE))
    gemini = FakeGemini(responses=[_IMPACT_EMPTY, _MERGE_OUTPUT])
    neo4j = FakeNeo4j(responses=[[], [], [], []])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="t-merge")
    payload = CpsInput(
        project_name="food", version="v1", date="d",
        meeting_content=_MEETING_LONG, team_id="team-9",
    )

    result = await run_cps_merge(ctx, payload, graph)

    key = scoped_project("food", "team-9")
    assert result.master_cps_id == cps_master_id(key)
    assert result.delta_cps_id == cps_delta_id(key, "v1")
    # 개인 'food' 와 명확히 다름 — 격리.
    assert result.master_cps_id != "doc_cps_master_food"
    assert "team-9" in result.master_cps_id

    # 저장 쿼리들의 project param 이 스코프 키 (개인 'food' 아님).
    save_log = next(e for e in neo4j.executed if "Meeting_Log" in e["cypher"])
    assert save_log["params"]["project"] == key
    merge_master = neo4j.executed[-1]
    assert merge_master["params"]["project"] == key
    assert merge_master["params"]["master_id"] == cps_master_id(key)

    # save_cps 의 CPS_Document delta 노드 id 가 스코프 delta id 로 재조정됐는지.
    save_cps = next(e for e in neo4j.executed if "UNWIND" in e["cypher"] and "CPS_Document" in e["cypher"])
    doc_params = [v for v in save_cps["params"].values() if isinstance(v, list)]
    flat = [item for sub in doc_params for item in sub]
    doc_node = next(n for n in flat if isinstance(n, dict) and n.get("id") == cps_delta_id(key, "v1"))
    assert doc_node["props"]["project"] == key
