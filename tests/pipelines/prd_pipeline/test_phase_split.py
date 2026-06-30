"""
PRD 파이프라인 extract/merge 단계 분리 (batch 파이프라이닝 기반).

핵심 불변식:
  - run_prd_extract: parse + prd_extract + prd_graph (LLM 2회) — Neo4j 쓰기/읽기 0건.
    누적 master 무관 → prefetch 가능.
  - run_prd_merge: 미리 계산된 extract 를 받아 fetch + impact + filter + merge + lint
    + DB 쓰기 전부 수행.
  - run_prd_pipeline = extract + merge 합성 → 기존 동작/테스트 동일 (test_prd_pipeline.py).

[순서 보존] LLM 순서 extract→graph→impact→merge, Neo4j 순서 fetch→save→merge 는
기존(gather) 과 동일하게 유지 → 기존 테스트 회귀 없음.
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext, canonicalize_graph
from app.pipelines.prd_pipeline import (
    PrdInput,
    parse_cps_for_prd,
    run_prd_extract,
    run_prd_merge,
)
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio


_CPS_GRAPH = {
    "nodes": [
        {"id": "doc_cps_food_v1", "label": "CPS_Document",
         "properties": {"full_markdown": "# CPS\n## Problem\n- 느림"}},
        {"id": "prb_01", "label": "Problem", "properties": {"summary": "서비스 느림"}},
    ],
    "relationships": [],
}
_PRD_EXTRACT_TEXT = "# PRD v1\n## Epic\n- 인증"
_PRD_GRAPH_JSON = json.dumps({
    "_harness_metadata": {"state": "recording"},
    "nodes": [
        {"id": "doc_prd_food_v1", "label": "PRD_Document", "properties": {}},
        {"id": "epic_01", "label": "Epic", "properties": {"summary": "인증"}},
        {"id": "story_01", "label": "Story", "properties": {"summary": "로그인"}},
    ],
    "relationships": [
        {"source": "epic_01", "type": "EXTRACTED_FROM", "target": "doc_prd_food_v1"},
        {"source": "story_01", "type": "BELONGS_TO", "target": "epic_01"},
    ],
})
_IMPACT_EMPTY = json.dumps({
    "affected_sections": [], "removed_epic_ids": [], "removed_story_ids": [], "analysis": "",
})
_MERGE_TEXT = "### Epic & Story Map\n- 업데이트된 에픽\n- **[Story 1.1] 로그인**"


async def test_run_prd_extract_does_no_neo4j_and_returns_artifacts():
    """extract 는 prd_extract + prd_graph LLM 2회만 — Neo4j 호출 0건."""
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON])
    neo4j = FakeNeo4j()
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="t-prd-extract")

    out = await run_prd_extract(
        ctx, PrdInput(project_name="food", version="v1", cps_graph=_CPS_GRAPH)
    )

    assert neo4j.executed == []           # ← 추출은 DB 안 건드림 (읽기도 0 — fetch 는 merge 단계)
    assert len(gemini.calls) == 2         # prd_extract + prd_graph
    assert out["prd_markdown"] == _PRD_EXTRACT_TEXT
    assert "nodes" in out["prd_graph"]
    assert any(n.get("label") == "Epic" for n in out["prd_graph"]["nodes"])
    assert out["parsed"]["pure_markdown"]  # parse_cps_for_prd 결과 포함


async def test_run_prd_merge_uses_precomputed_extract_without_re_extracting():
    """merge 는 미리 계산된 extract 를 받아 impact + merge LLM 2회만 — 재추출 안 함."""
    extract = {
        "parsed": parse_cps_for_prd(_CPS_GRAPH),
        "prd_markdown": _PRD_EXTRACT_TEXT,
        "prd_graph": canonicalize_graph(json.loads(_PRD_GRAPH_JSON)),
    }
    gemini = FakeGemini(responses=[_IMPACT_EMPTY, _MERGE_TEXT])  # prd_extract/graph 응답 없음
    neo4j = FakeNeo4j(responses=[[], [], []])  # fetch(first_run), save_prd, merge_master
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="t-prd-merge")

    result = await run_prd_merge(
        ctx, PrdInput(project_name="food", version="v1", cps_graph=_CPS_GRAPH), extract
    )

    assert len(gemini.calls) == 2         # impact + merge (extract/graph 아님)
    assert result.mode == "first_run"
    assert result.delta_prd_id == "doc_prd_food_v1"
    assert result.master_prd_id == "doc_prd_master_food"
    assert "prd_lint" in result.diagnostic  # merge 단계가 lint 까지 수행
    # Neo4j 순서 보존: fetch → save → merge
    assert len(neo4j.executed) == 3
    assert any("UNWIND" in e["cypher"] for e in neo4j.executed)
    assert "MERGE (master:PRD_Document" in neo4j.executed[-1]["cypher"]
