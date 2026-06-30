"""
생성 위생 L1-1: placeholder spec 결정적 감지 + PRD spec_count 제외.

빈약한 회의록에서 LLM 이 '정의 불가/미정/명세서 부재' placeholder Epic/Story 로
칸을 채우면, label 만 세는 spec_count 를 우회해 master 에 누더기가 쌓였다. 이 모듈이
그 placeholder 를 LLM 무관 결정적으로 감지해 spec 카운트에서 제외 → no_changes 경로
→ master 오염 차단.
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.prd_pipeline import PrdInput, run_prd_pipeline
from app.pipelines.spec_quality import is_meaningful_spec_node, is_placeholder_text
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio


# ─── 결정적 감지기 단위 ──────────────────────────────────────────


def test_is_placeholder_text_detects_garbage():
    # 운영 누더기 PRD 에서 실제로 나온 패턴들
    assert is_placeholder_text("(CPS 명세서 부재로 추가 Epic 정의 불가)")
    assert is_placeholder_text("정의 불가")
    assert is_placeholder_text("미정")
    assert is_placeholder_text("[Role A]")
    assert is_placeholder_text("[도메인명 - 예: 사용자 계정 관리]")
    assert is_placeholder_text("")
    assert is_placeholder_text("   ")
    assert is_placeholder_text(None)


def test_is_placeholder_text_allows_real_content():
    assert not is_placeholder_text("사용자 계정 관리")
    assert not is_placeholder_text("AI 도구 신청 프로세스")
    assert not is_placeholder_text("토큰 사용량 자동 집계")
    # '미정' 합성어 오탐 방지 — 정확히 '미정'만 placeholder.
    assert not is_placeholder_text("미정산 비용 조회")


def test_is_meaningful_spec_node():
    assert is_meaningful_spec_node({"label": "Epic", "properties": {"summary": "계정 관리"}})
    assert not is_meaningful_spec_node({"label": "Epic", "properties": {"summary": "정의 불가"}})
    assert not is_meaningful_spec_node({"label": "Story", "properties": {"summary": "미정"}})
    assert not is_meaningful_spec_node({"label": "Epic", "properties": {}})


# ─── PRD 라우팅: placeholder-only → no_changes (master 오염 차단) ──

_CPS_GRAPH = {
    "nodes": [
        {"id": "doc_cps_food_v6", "label": "CPS_Document",
         "properties": {"full_markdown": "# CPS\n## Problem\n- 느림"}},
    ],
    "relationships": [],
}
_PRD_EXTRACT_TEXT = "# PRD v6\n## Epic\n- (정의 불가)"
# 모든 Epic/Story 가 placeholder — label 은 Epic/Story 지만 본문은 garbage.
_PLACEHOLDER_PRD_GRAPH = json.dumps({
    "nodes": [
        {"id": "doc_prd_food_v6", "label": "PRD_Document", "properties": {}},
        {"id": "epic_x", "label": "Epic",
         "properties": {"summary": "(CPS 명세서 부재로 추가 Epic 정의 불가)"}},
        {"id": "story_x", "label": "Story", "properties": {"summary": "미정"}},
    ],
    "relationships": [],
})
_IMPACT_EMPTY = json.dumps({
    "affected_sections": [], "removed_epic_ids": [], "removed_story_ids": [], "analysis": "",
})


async def test_placeholder_only_epics_route_to_no_changes():
    """placeholder-only Epic/Story → 실질 spec 0 → no_changes → master 미반영.

    이전엔 label=Epic 이라 spec_count>0 → 진행 → '정의 불가' 가 master 에 누적됐다.
    """
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, _PLACEHOLDER_PRD_GRAPH, _IMPACT_EMPTY])
    fetch_response = [{
        "master_id": "doc_prd_master_food",
        "master_content": "### 1. Overview\n기존 누적 PRD\n### 2. Epic & Story Map\n- Epic-01",
        "master_prd_details": [],
        "latest_id": "doc_prd_food_v5",
        "latest_content": "직전",
        "latest_prd_details": [],
        "project_name": "food",
        "prd_total": 5,
    }]
    neo4j = FakeNeo4j(responses=[fetch_response])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="ph-no-changes")

    result = await run_prd_pipeline(
        ctx, PrdInput(project_name="food", version="v6", cps_graph=_CPS_GRAPH)
    )

    assert result.mode == "no_changes"
    assert result.diagnostic.get("spec_count") == 0
    # master merge cypher 호출 안 됨 — placeholder 가 누적되지 않음
    cyphers = [e["cypher"] for e in neo4j.executed]
    assert not any("MERGE (master:PRD_Document" in c for c in cyphers)
    # merge LLM 도 호출 안 됨 (extract + graph + impact = 3회)
    assert len(gemini.calls) == 3


async def test_real_epics_still_proceed():
    """진짜 Epic/Story 는 정상 진행 — backstop 이 정상 입력 막지 않음(오탐 가드)."""
    real_graph = json.dumps({
        "nodes": [
            {"id": "doc_prd_food_v2", "label": "PRD_Document", "properties": {}},
            {"id": "epic_01", "label": "Epic", "properties": {"summary": "AI 계정 신청 관리"}},
            {"id": "story_01", "label": "Story", "properties": {"summary": "사용자는 신청서를 제출한다"}},
        ],
        "relationships": [{"source": "epic_01", "type": "CONTAINS", "target": "story_01"}],
    })
    merge_text = "### Epic & Story Map\n#### 📦 [Epic-01] 계정 신청\n- **[Story-01.1] 신청서 제출**"
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, real_graph, _IMPACT_EMPTY, merge_text])
    neo4j = FakeNeo4j(responses=[[], [], []])  # fetch(first_run), save, merge
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="real-proceed")

    result = await run_prd_pipeline(
        ctx, PrdInput(project_name="food", version="v2", cps_graph=_CPS_GRAPH)
    )

    assert result.mode in ("first_run", "incremental")  # no_changes 아님
    cyphers = [e["cypher"] for e in neo4j.executed]
    assert any("MERGE (master:PRD_Document" in c for c in cyphers)  # 정상 저장
