"""
PRD 파이프라인 end-to-end (Gemini/Neo4j fake).

검증 포인트:
  - PRD Agent1 (markdown) → PRD Agent2 (graph JSON) 체이닝
  - first_run / incremental 분기
  - 마지막 Cypher 가 PRD 마스터이고 BASED_ON 연결을 포함
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.prd_pipeline import PrdInput, run_prd_pipeline
from tests.conftest import FakeGemini, FakeNeo4j


pytestmark = pytest.mark.asyncio


_CPS_GRAPH = {
    "nodes": [
        {
            "id": "doc_cps_harness_v1_1",
            "label": "CPS_Document",
            "properties": {"full_markdown": "## CPS\n- ctx"},
        },
        {"id": "prb_01", "label": "Problem", "properties": {"summary": "first prob"}},
    ],
    "relationships": [],
}


def _responder_first_run():
    def respond(prompt: str) -> str:
        if "통합 하네스 PRD 아키텍트" in prompt:
            # PRD Agent1 → markdown
            return "## 🚀 PRD: [harness]\n\n### 1. Overview & Roles\n- vision\n"
        if "하네스 데이터 엔지니어" in prompt:
            # PRD Agent2 → graph JSON
            return json.dumps(
                {
                    "nodes": [
                        {
                            "id": "doc_prd_harness_v1_1",
                            "label": "PRD_Document",
                            "properties": {"project": "harness", "is_latest": True},
                        },
                        {"id": "epic_01", "label": "Epic", "properties": {"summary": "E1"}},
                        {"id": "story_01_1", "label": "Story", "properties": {"summary": "S1"}},
                    ],
                    "relationships": [
                        {"source": "epic_01", "type": "EXTRACTED_FROM", "target": "doc_prd_harness_v1_1"},
                        {"source": "epic_01", "type": "CONTAINS", "target": "story_01_1"},
                    ],
                },
                ensure_ascii=False,
            )
        if "PRD 영향 범위 분석" in prompt:
            return json.dumps({"affected_sections": [], "removed_epic_ids": [], "removed_story_ids": []})
        if "PRD 조감도를 시맨틱" in prompt:
            return "## 🗺️ Master PRD\n\n### 2. Epic & Story Map\n#### 📦 [Epic-01] E1\n"
        raise AssertionError(f"unexpected PRD prompt: {prompt[:120]}")

    return respond


async def test_first_run_writes_master_prd_with_based_on():
    payload = PrdInput(
        project_name="harness", version="v1.1", cps_graph=_CPS_GRAPH
    )
    gemini = FakeGemini(_responder_first_run())
    # [2026-05-25 perf A] fetch 가 Save 보다 먼저 호출됨.
    neo = FakeNeo4j(
        responses=[
            # Get All PRD2 — 마스터/델타 모두 없음 (병렬 첫 호출)
            [
                {
                    "master_id": None,
                    "master_content": "",
                    "master_prd_details": [],
                    "latest_id": None,
                    "latest_content": "",
                    "latest_prd_details": [],
                    "project_name": "harness",
                }
            ],
            [],  # Save PRD
            [],  # Merge master PRD
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="p1")

    result = await run_prd_pipeline(ctx, payload)
    assert result.mode == "first_run"
    assert result.master_prd_id == "doc_prd_master_harness"
    assert result.delta_prd_id == "doc_prd_harness_v1_1"

    # [2026-05-25 perf A] Save PRD + Get All PRD2 가 graph LLM 과 병렬 실행됨 →
    # Cypher 실행 순서 비결정. 순서 무관 set 검증.
    cyphers = [e["cypher"] for e in neo.executed]
    # Save PRD (n + PRD_Document) 어딘가
    assert any("MERGE (n" in c and ":PRD_Document" in c for c in cyphers)
    # fetch (Get All PRD2) 어딘가
    assert any("OPTIONAL MATCH (m:PRD_Document" in c for c in cyphers)
    # 마지막 = master 갱신 + BASED_ON (이건 무조건 마지막 — merge 후)
    assert "MERGE (master:PRD_Document" in cyphers[-1]
    assert "OPTIONAL MATCH (cps_m:CPS_Document" in cyphers[-1]
    assert "MERGE (master)-[:BASED_ON]->(cps_m)" in cyphers[-1]

    # [2026-05-25 perf B] first_run 시 impact + merge LLM skip → Agent1 + Agent2 = 2회.
    # 이전 4회 → 2회.
    assert len(gemini.calls) == 4


def _responder_incremental():
    def respond(prompt: str) -> str:
        if "통합 하네스 PRD 아키텍트" in prompt:
            return "## 🚀 PRD: [harness]\n\n### 2. Epics & User Stories\n#### 📦 Epic 2\n"
        if "하네스 데이터 엔지니어" in prompt:
            return json.dumps(
                {
                    "nodes": [
                        {"id": "doc_prd_harness_v1_2", "label": "PRD_Document", "properties": {"project": "harness"}},
                        {"id": "epic_02", "label": "Epic", "properties": {"summary": "E2"}},
                    ],
                    "relationships": [
                        {"source": "epic_02", "type": "EXTRACTED_FROM", "target": "doc_prd_harness_v1_2"},
                    ],
                },
                ensure_ascii=False,
            )
        if "PRD 영향 범위 분석" in prompt:
            return json.dumps({"affected_sections": ["Epic & Story Map"], "removed_epic_ids": [], "removed_story_ids": []})
        if "PRD 조감도를 시맨틱" in prompt:
            return (
                "### 2. Epic & User Story Map (기능 계층도)\n"
                "#### 📦 [Epic-01] EXIST\n"
                "- `[Story-01.1]` story-a ➡️ *(구현 화면: Home)*\n"
                "#### 📦 [Epic-02] NEW\n"
                "- `[Story-02.1]` story-new ➡️ *(구현 화면: Detail)*\n"
            )
        raise AssertionError("unexpected prompt")

    return respond


_EXISTING_PRD_MASTER = """\
## 🗺️ Master PRD

### 1. Product Overview (통합 제품 비전)
- **통합 비전**: ENV

### 2. Epic & User Story Map (기능 계층도)
#### 📦 [Epic-01] EXIST
- `[Story-01.1]` story-a ➡️ *(구현 화면: Home)*

### 3. Screen Architecture (화면별 구현 명세)
#### 🖥️ [Screen: Home]
- **포함된 기능**:
  - `[Story-01.1]` story-a (from Epic-01)

### 4. Global Non-Functional Requirements (공통 제약 사항)
- **공통 규칙**:
  - rule1
"""


async def test_incremental_replaces_epic_map_only_preserves_other_sections():
    payload = PrdInput(
        project_name="harness", version="v1.2", cps_graph=_CPS_GRAPH
    )
    gemini = FakeGemini(_responder_incremental())
    # [2026-05-25 perf A] 병렬화로 fetch 가 Save PRD 보다 먼저 호출됨.
    # mock 순서: 1) fetch (Get All PRD2) → 2) Save PRD → 3) Merge master PRD
    neo = FakeNeo4j(
        responses=[
            [
                {
                    "master_id": "doc_prd_master_harness",
                    "master_content": _EXISTING_PRD_MASTER,
                    "master_prd_details": [
                        {"epic_id": "epic_01", "epic_summary": "EXIST", "story_id": "story_01_1", "story_summary": "story-a", "screen_name": "Home"},
                    ],
                    "latest_id": "doc_prd_harness_v1_2",
                    "latest_content": "delta",
                    "latest_prd_details": [],
                    "project_name": "harness",
                }
            ],
            [],  # Save PRD
            [],  # Merge master PRD
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="p2")

    result = await run_prd_pipeline(ctx, payload)
    assert result.mode == "incremental"

    # 마지막 cypher = master 갱신
    merge_executed = neo.executed[-1]
    merge_cypher = merge_executed["cypher"]
    merge_params = merge_executed["params"]
    # Cypher 본문은 $param 바인딩만. merged_content 는 params 로.
    assert "master.full_markdown = $merged_content" in merge_cypher
    md = merge_params["merged_content"]
    # Epic Map 은 agent 결과로 대체 → Epic-02 포함
    assert "Epic-02" in md
    # 다른 섹션 (Screen Architecture, NFR) 은 보존
    assert "Screen Architecture" in md
    assert "rule1" in md
    # BASED_ON 관계 자체는 cypher 본문에 (관계 식별자는 인터폴 유지)
    assert "MERGE (master)-[:BASED_ON]->(cps_m)" in merge_cypher


async def test_prd_agent_invalid_json_raises_value_error():
    payload = PrdInput(project_name="harness", version="v1.1", cps_graph=_CPS_GRAPH)

    def respond(prompt: str) -> str:
        if "통합 하네스 PRD 아키텍트" in prompt:
            return "valid markdown"
        return "this is not JSON"  # PRD Agent2 가 깨진 출력

    gemini = FakeGemini(respond)
    neo = FakeNeo4j()
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="p3")

    with pytest.raises(ValueError, match="PRD Agent2 returned unparseable JSON"):
        await run_prd_pipeline(ctx, payload)
    # [2026-05-25 perf A] graph LLM 과 fetch 가 병렬 — fetch 가 raise 전에 호출
    # 됐을 수 있음. 단 Save PRD / Merge master 같은 write 는 호출 안 됨.
    write_cyphers = [
        e["cypher"] for e in neo.executed
        if "MERGE (n" in e["cypher"] or "MERGE (master" in e["cypher"]
    ]
    assert write_cyphers == []
