"""
run_cps_pipeline end-to-end 흐름 — LLM/Neo4j fake 로.

시나리오:
  - first_run: master 없음 → mode='first_run', merge_agent output 가 그대로 merged_content
  - incremental: master 있음 → mode='incremental', filter_affected_sections 점수
  - schema 전달: cps_agent / impact_analyzer 는 schema, merge_agent 는 None
  - 환각 차단: Problem/Solution/Requirement 0개 → ValueError
  - empty response: 첫 시도 + retry 모두 빈 → ValueError
  - no-tx fallback: run_in_transaction 없는 fake → 순차 run_cypher
  - canonicalize 적용: CRLF→LF 가 Cypher params 까지 전파
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.cps_pipeline.pipeline import run_cps_pipeline
from app.pipelines.cps_pipeline.types import CpsInput
from tests.conftest import FakeGemini, FakeNeo4j


pytestmark = pytest.mark.asyncio


# ─── 고정 응답 fixture ──────────────────────────────────────────────────


_VALID_CPS_RESPONSE = json.dumps({
    "_harness_metadata": {"state": "recording", "verification_passed": True},
    "nodes": [
        {
            "id": "doc_cps_food_v1",
            "label": "CPS_Document",
            "properties": {"full_markdown": "# CPS\n## Problem\n- X"},
        },
        {
            "id": "prb_01",
            "label": "Problem",
            "properties": {"summary": "느림"},
        },
        {
            "id": "res_01",
            "label": "Solution",
            "properties": {"summary": "캐시"},
        },
    ],
    "relationships": [
        {"source": "res_01", "type": "SOLVES", "target": "prb_01"},
        {"source": "prb_01", "type": "EXTRACTED_FROM", "target": "doc_cps_food_v1"},
    ],
})

_IMPACT_EMPTY = json.dumps({
    "affected_sections": [],
    "removed_prb_ids": [],
    "removed_res_ids": [],
    "analysis": "",
})

_IMPACT_PROBLEM = json.dumps({
    "affected_sections": ["Problem"],
    "removed_prb_ids": [],
    "removed_res_ids": [],
    "analysis": "신규 문제 추가",
})

_MERGE_OUTPUT = "### 1. Problem\n- 업데이트된 문제\n### 2. Solution\n- 업데이트된 해결"

_MEETING_LONG = "오늘 회의 내용. 50자 이상으로 작성하여 impact 반환 affected_sections 가 빈일 때도 fallback 트리거를 피한다."

_EXISTING_MASTER = (
    "## 📄 CPS 명세서: food (v1)\n"
    "### 1. Context (배경)\n"
    "- 기존 컨텍스트\n"
    "### 2. Problem (핵심 문제)\n"
    "- 기존 문제\n"
    "### 3. Solution (구현 방향)\n"
    "- 기존 해결\n"
)


# ─── first_run 시나리오 ────────────────────────────────────────────────


async def test_first_run_returns_first_run_mode():
    """master 없을 때 mode='first_run', merge_agent 출력이 그대로 master 로 저장."""
    gemini = FakeGemini(responses=[_VALID_CPS_RESPONSE, _IMPACT_EMPTY, _MERGE_OUTPUT])
    neo4j = FakeNeo4j(responses=[
        [],  # save_cps
        [],  # save_log
        [],  # _GET_ALL_CPS_QUERY (first_run — records 비어 fetch 기본값)
        [],  # merge_master
    ])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="test-first-run")

    payload = CpsInput(
        project_name="food",
        version="v1",
        date="2026-05-19",
        meeting_content=_MEETING_LONG,
    )
    result = await run_cps_pipeline(ctx, payload)

    assert result.meeting_log_id == "log_food_v1"
    assert result.delta_cps_id == "doc_cps_food_v1"
    assert result.master_cps_id == "doc_cps_master_food"

    assert result.mode == "first_run"
    assert result.diagnostic["filter"]["mode"] == "FIRST_RUN"
    # [2026-05-25 perf B] first_run 시 merge LLM skip → reassemble 직접 latest passthrough.
    assert result.diagnostic["reassemble"]["mode"] == "FIRST_RUN_PASSTHROUGH"

    assert "nodes" in result.cps_graph
    assert len(result.cps_graph["nodes"]) == 3


async def test_passes_schemas_to_gemini_correctly():
    """cps_agent / impact_analyzer 는 response_schema, merge_agent 는 None.
    Phase A 핵심 검증.

    [2026-05-25 perf B] first_run 시 impact + merge LLM skip 되므로,
    incremental 시나리오로 만들기 위해 fetch_master_and_latest 가 master_content
    있는 응답 반환하도록 mock 갱신.
    """
    gemini = FakeGemini(responses=[_VALID_CPS_RESPONSE, _IMPACT_EMPTY, _MERGE_OUTPUT])
    # fetch_master_and_latest 응답에 master_content 채워서 incremental path 진입.
    fetch_response = [{
        "master_id": "doc_cps_master_food",
        "master_content": "### 1. 기존 master 내용\n이전 미팅에서 누적된 master.",
        "master_probs": [{"id": "p1", "summary": "old prob"}],
        "latest_id": "doc_cps_food_v0",
        "latest_content": "이전 latest",
    }]
    neo4j = FakeNeo4j(responses=[[], [], fetch_response, []])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="test-schema")

    await run_cps_pipeline(
        ctx,
        CpsInput(
            project_name="food",
            version="v1",
            date="2026-05-19",
            meeting_content=_MEETING_LONG,
        ),
    )

    assert len(gemini.calls) == 3

    call0_schema = gemini.calls[0]["response_schema"]
    assert call0_schema is not None
    assert "_harness_metadata" in call0_schema["properties"]
    assert "nodes" in call0_schema["properties"]

    call1_schema = gemini.calls[1]["response_schema"]
    assert call1_schema is not None
    assert "affected_sections" in call1_schema["properties"]

    assert gemini.calls[2]["response_schema"] is None


async def test_save_log_and_save_cps_both_executed():
    """[2026-05-26 perf B] save_log 가 CPS Agent 와 병렬 → executed[0],
    save_cps 는 CPS Agent 완료 후 → executed[1]. 둘 다 실행되는지 확인."""
    gemini = FakeGemini(responses=[_VALID_CPS_RESPONSE, _IMPACT_EMPTY, _MERGE_OUTPUT])
    neo4j = FakeNeo4j(responses=[[], [], [], []])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="test-tx")

    await run_cps_pipeline(
        ctx,
        CpsInput(
            project_name="food", version="v1", date="d", meeting_content=_MEETING_LONG,
        ),
    )

    assert len(neo4j.executed) == 4
    # 병렬화로 save_log 가 CPS Agent 와 동시 실행 — 순서는 event loop 스케줄에 의존.
    # 첫 2개 안에 save_log + save_cps 가 모두 있어야 함.
    first_two_cyphers = {neo4j.executed[0]["cypher"], neo4j.executed[1]["cypher"]}
    assert any("UNWIND" in c for c in first_two_cyphers)
    assert any("Meeting_Log" in c for c in first_two_cyphers)
    log_idx = next(
        i for i in range(2) if "Meeting_Log" in neo4j.executed[i]["cypher"]
    )
    assert neo4j.executed[log_idx]["params"]["log_id"] == "log_food_v1"
    assert neo4j.executed[2]["params"] == {"project": "food"}
    assert "MERGE (master:CPS_Document" in neo4j.executed[3]["cypher"]
    assert neo4j.executed[3]["params"]["master_id"] == "doc_cps_master_food"


async def test_canonicalizes_meeting_content_into_cypher_params():
    """CRLF / trailing whitespace 가 save_log Cypher params raw_content 에서 제거됨.
    [2026-05-26 perf B] save_log 는 병렬 실행 — 위치 무관 검색."""
    crlf_content = (
        "회의록\r\n   trailing 공백   \r\n50자 이상으로 충분히 길게 작성하여 잘 텔스트한다."
    )
    gemini = FakeGemini(responses=[_VALID_CPS_RESPONSE, _IMPACT_EMPTY, _MERGE_OUTPUT])
    neo4j = FakeNeo4j(responses=[[], [], [], []])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="test-canon")

    await run_cps_pipeline(
        ctx,
        CpsInput(
            project_name="food", version="v1", date="d", meeting_content=crlf_content,
        ),
    )

    save_log = next(
        e for e in neo4j.executed if "Meeting_Log" in e["cypher"]
    )
    raw = save_log["params"]["raw_content"]
    assert "\r" not in raw
    assert "   trailing 공백   " not in raw
    assert "trailing 공백" in raw


# ─── incremental 시나리오 ──────────────────────────────────────────────


async def test_incremental_with_existing_master_uses_filter_data():
    """master_content 있을 때 mode='incremental', filter_affected_sections 가 접근됨."""
    gemini = FakeGemini(responses=[_VALID_CPS_RESPONSE, _IMPACT_PROBLEM, _MERGE_OUTPUT])
    neo4j = FakeNeo4j(responses=[
        [],  # save_cps
        [],  # save_log
        [{  # _GET_ALL_CPS_QUERY — master 있음
            "master_id": "doc_cps_master_food",
            "master_content": _EXISTING_MASTER,
            "master_probs": [
                {"id": "prb_old", "summary": "기존 문제", "resolved_by": "기존 해결"},
            ],
            "latest_id": "doc_cps_food_v1",
            "latest_content": "이전 회의록 내용 50자 이상으로 작성. impact 검증 용.",
            "latest_probs": [],
            "project_name": "food",
        }],
        [],  # merge_master
    ])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="test-inc")

    result = await run_cps_pipeline(
        ctx,
        CpsInput(
            project_name="food",
            version="v2",
            date="2026-05-19",
            meeting_content=_MEETING_LONG,
            previous_cps_id="doc_cps_food_v1",
        ),
    )

    assert result.mode == "incremental"
    assert result.diagnostic["filter"]["mode"] == "INCREMENTAL"
    # filter_affected_sections 는 master 섹션 헤더 전체 문자열을 key 로 반환하므로
    # (예: "Problem (핵심 문제)") 부분 문자열로 확인한다.
    assert any("Problem" in s for s in result.diagnostic["filter"]["affected_sections"])
    # previous_cps_id 가 derived_cps_id 에 그대로 올라옴
    assert result.delta_cps_id == "doc_cps_food_v1"


# ─── 오류 시나리오 ────────────────────────────────────────────────────


async def test_zero_spec_falls_back_to_skip_stub():
    """[2026-05-25 fallback] LLM 이 Problem/Solution 0개여도 raise 안 함.
    Strict + Lenient 모두 0개면 skip stub 으로 통과 — BATCH 멈춤 차단."""
    no_spec_response = json.dumps({
        "nodes": [
            {"id": "doc_cps_x_v1", "label": "CPS_Document", "properties": {}},
        ],
        "relationships": [],
    })
    # 2개 응답 — strict + lenient 모두 같은 빈 응답
    gemini = FakeGemini(responses=[no_spec_response, no_spec_response])
    neo4j = FakeNeo4j()
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="test-skip-stub")

    # raise 안 함 — pipeline 정상 완료
    result = await run_cps_pipeline(
        ctx,
        CpsInput(
            project_name="x", version="v1", date="d",
            meeting_content=_MEETING_LONG,
        ),
    )
    # pipeline 이 정상 완료 — skip stub 결과
    assert result is not None


async def test_raises_when_empty_response_even_after_retry():
    """첫 시도 + retry 모두 빈 → generate_json_with_retry 가 {} 반환 → ValueError."""
    gemini = FakeGemini(responses=["", ""])
    neo4j = FakeNeo4j()
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="test-empty")

    with pytest.raises(ValueError, match="unparseable JSON"):
        await run_cps_pipeline(
            ctx,
            CpsInput(
                project_name="x", version="v1", date="d",
                meeting_content=_MEETING_LONG,
            ),
        )


# ─── no-tx fallback 시나리오 ──────────────────────────────────────────────


class _Neo4jWithoutTx:
    """run_in_transaction 메서드 없는 fake — 순차 run_cypher fallback 경로 검증."""

    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.executed: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def run_cypher(self, cypher: str, params: Optional[Dict[str, Any]] = None):
        self.executed.append({"cypher": cypher, "params": params or {}})
        if self._responses:
            return self._responses.pop(0)
        return []


async def test_works_with_neo4j_lacking_run_in_transaction():
    """[2026-05-26 perf B] perf B 이후 cps_pipeline 은 항상 run_cypher 만 사용
    (run_in_transaction 무관). run_in_transaction 없는 fake 로도 정상 동작."""
    gemini = FakeGemini(responses=[_VALID_CPS_RESPONSE, _IMPACT_EMPTY, _MERGE_OUTPUT])
    neo4j = _Neo4jWithoutTx(responses=[[], [], [], []])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="test-no-tx")

    await run_cps_pipeline(
        ctx,
        CpsInput(
            project_name="food", version="v1", date="d", meeting_content=_MEETING_LONG,
        ),
    )

    assert len(neo4j.executed) == 4
    # 병렬화 — 처음 2개 안에 save_log + save_cps. 순서 무관.
    first_two = {neo4j.executed[0]["cypher"], neo4j.executed[1]["cypher"]}
    assert any("UNWIND" in c for c in first_two)
    assert any("Meeting_Log" in c for c in first_two)
    assert neo4j.executed[2]["params"] == {"project": "food"}
    assert "MERGE (master:CPS_Document" in neo4j.executed[3]["cypher"]
