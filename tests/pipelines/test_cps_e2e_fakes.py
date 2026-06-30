"""
End-to-end with fakes — Gemini/Neo4j 를 가짜로 두고 파이프라인 전체 흐름을 검증.

검증 포인트:
  - 첫 실행: Master 가 없을 때 LLM 출력을 그대로 master 로 저장 (FIRST_RUN_PASSTHROUGH)
  - 증분: Master 가 있을 때 영향 섹션만 교체 (INCREMENTAL_REASSEMBLED)
  - Save CPS 보다 doc 노드 MATCH 가 뒤에 실행되어야 한다 (현재 stage 순서 보존)
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.cps_pipeline import CpsInput, run_cps_pipeline
from tests.conftest import FakeGemini, FakeNeo4j


pytestmark = pytest.mark.asyncio


def _responder_first_run():
    """
    프롬프트 본문으로 단계를 식별해 응답을 구분.
    """

    def respond(prompt: str) -> str:
        if "당신은 '통합 하네스 아키텍트'" in prompt:
            # CPS Agent → 그래프 JSON
            return json.dumps(
                {
                    "_harness_metadata": {"state": "recording"},
                    "nodes": [
                        {
                            "id": "doc_cps_harness_v1_1",
                            "label": "CPS_Document",
                            "properties": {"project": "harness", "version": "v1.1", "is_latest": True},
                        },
                        {
                            "id": "prb_01",
                            "label": "Problem",
                            "properties": {"summary": "first prob"},
                        },
                        {
                            "id": "res_01",
                            "label": "Solution",
                            "properties": {"summary": "first sol"},
                        },
                    ],
                    "relationships": [
                        {"source": "prb_01", "type": "EXTRACTED_FROM", "target": "doc_cps_harness_v1_1"},
                        {"source": "res_01", "type": "SOLVES", "target": "prb_01"},
                    ],
                },
                ensure_ascii=False,
            )
        if "문서 영향 범위 분석" in prompt:
            return json.dumps({"affected_sections": [], "removed_prb_ids": [], "removed_res_ids": [], "analysis": "first"})
        if "시맨틱(의미 기반)으로 병합" in prompt:
            # 첫 실행 → 전체 문서 출력
            return "## 📄 CPS 명세서: harness (v1.1)\n\n### 2. Problem\n- **[PRB-01] first prob**: x\n"
        raise AssertionError(f"unexpected prompt: {prompt[:120]}")

    return respond


async def test_first_run_writes_master_via_passthrough():
    payload = CpsInput(
        project_name="harness",
        version="v1.1",
        date="2026-05-12",
        meeting_content="첫 미팅 내용",
        previous_cps_id=None,
    )
    gemini = FakeGemini(_responder_first_run())
    # Get All CPS2 응답: 마스터/델타 모두 없음
    neo = FakeNeo4j(
        responses=[
            # Save CPS query (no records expected, returns empty)
            [],
            # Save Meeting Log query
            [],
            # Get All CPS2
            [{"master_id": None, "master_content": "", "master_probs": [], "latest_id": None, "latest_content": "", "latest_probs": [], "project_name": "harness"}],
            # Merge master query
            [],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="t1")

    result = await run_cps_pipeline(ctx, payload)

    assert result.mode == "first_run"
    assert result.master_cps_id == "doc_cps_master_harness"
    assert result.delta_cps_id == "doc_cps_harness_v1_1"
    assert result.meeting_log_id == "log_harness_v1_1"

    cyphers = [e["cypher"] for e in neo.executed]
    # [2026-05-26 perf B] save_log 가 CPS Agent 와 병렬 실행 — 앞 2개의 순서는
    # event loop 스케줄에 의존. save_log + save_cps 가 처음 2개에 있어야 함.
    first_two = {cyphers[0], cyphers[1]}
    assert any("MERGE (n0:CPS_Document" in c for c in first_two), \
        f"save_cps 가 첫 2개에 없음: {first_two}"
    assert any("MERGE (log:Meeting_Log" in c for c in first_two), \
        f"save_log 가 첫 2개에 없음: {first_two}"
    assert "OPTIONAL MATCH (m:CPS_Document" in cyphers[2]
    assert "MERGE (master:CPS_Document" in cyphers[3]

    # CPS Agent (1) + impact (1) + merge (1) = 3회.
    assert len(gemini.calls) == 3


def _responder_incremental():
    def respond(prompt: str) -> str:
        if "당신은 '통합 하네스 아키텍트'" in prompt:
            return json.dumps(
                {
                    "nodes": [
                        {"id": "doc_cps_harness_v1_2", "label": "CPS_Document", "properties": {"project": "harness", "is_latest": True}},
                        {"id": "prb_03", "label": "Problem", "properties": {"summary": "new prob"}},
                    ],
                    "relationships": [
                        {"source": "prb_03", "type": "EXTRACTED_FROM", "target": "doc_cps_harness_v1_2"},
                    ],
                },
                ensure_ascii=False,
            )
        if "문서 영향 범위 분석" in prompt:
            return json.dumps({"affected_sections": ["Problem"], "removed_prb_ids": [], "removed_res_ids": []})
        if "시맨틱(의미 기반)으로 병합" in prompt:
            # 영향 섹션만 출력 (Problem)
            return (
                "### 2. Problem (핵심 문제)\n"
                "- **[PRB-01] A**: a-detail\n"
                "- **[PRB-02] B**: b-detail\n"
                "- **[PRB-03] new prob**: new-detail\n"
            )
        raise AssertionError("unexpected prompt")

    return respond


_EXISTING_MASTER = """\
## 📄 CPS 명세서: harness

### 1. Context (배경 및 상황)
- **비즈니스 환경**: ENV
- **도입 배경**: BG

### 2. Problem (핵심 문제)
- **[PRB-01] A**: a-detail
- **[PRB-02] B**: b-detail

### 3. Solution (최종 해결책 및 기획 방향)
- **목표 시스템 모델**: model
- **핵심 기능 명세**:
  - `[RES-01] f1`: [매핑: PRB-01 / x]

### 4. Pending & Action Items
- **미결정 사항**:
  - q1
- **Next Steps**:
  - [ ] `alice`: do it
"""


async def test_incremental_preserves_unaffected_sections():
    payload = CpsInput(
        project_name="harness",
        version="v1.2",
        date="2026-05-12",
        meeting_content="새 문제 PRB-03 발견",
        previous_cps_id=None,
    )
    gemini = FakeGemini(_responder_incremental())
    neo = FakeNeo4j(
        responses=[
            [],  # Save CPS
            [],  # Save Meeting Log
            # Get All CPS2: 기존 마스터 + delta 있음
            [
                {
                    "master_id": "doc_cps_master_harness",
                    "master_content": _EXISTING_MASTER,
                    "master_probs": [
                        {"id": "prb_01", "summary": "A", "resolved_by": None},
                        {"id": "prb_02", "summary": "B", "resolved_by": None},
                    ],
                    "latest_id": "doc_cps_harness_v1_2",
                    "latest_content": "delta content",
                    "latest_probs": [{"id": "prb_03", "summary": "new prob", "resolved_by": None}],
                    "project_name": "harness",
                }
            ],
            [],  # Merge master
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="t2")

    result = await run_cps_pipeline(ctx, payload)
    assert result.mode == "incremental"

    # 마지막 cypher = master 갱신
    merge_executed = neo.executed[-1]
    merge_cypher = merge_executed["cypher"]
    merge_params = merge_executed["params"]
    # Cypher 본문엔 $param 바인딩만. master_id/project/merged_content 는 params 에.
    assert "MERGE (master:CPS_Document {id: $master_id})" in merge_cypher
    assert "master.full_markdown = $merged_content" in merge_cypher
    assert merge_params["master_id"] == "doc_cps_master_harness"
    # merged_content (params) 에 영향 외 섹션이 보존되고 새 항목 포함
    md = merge_params["merged_content"]
    assert "Context (배경 및 상황)" in md
    assert "RES-01" in md
    assert "[ ] `alice`: do it" in md
    assert "PRB-03" in md


async def test_cps_agent_invalid_json_raises_value_error():
    payload = CpsInput(
        project_name="harness", version="v1.1", date="", meeting_content="x"
    )

    def respond(prompt: str) -> str:
        return "this is not JSON at all"

    gemini = FakeGemini(respond)
    neo = FakeNeo4j()
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="t3")

    with pytest.raises(ValueError, match="CPS Agent returned unparseable JSON"):
        await run_cps_pipeline(ctx, payload)
    # [2026-05-26 perf B] save_log 가 CPS Agent 와 병렬 — CPS Agent raise 전에
    # save_log 가 실행됐을 수 있음 (orphan meeting_log). save_cps / fetch /
    # merge 같은 후속 write 는 실행 안 됨.
    write_cyphers = [
        e["cypher"] for e in neo.executed
        if "MERGE (n0:" in e["cypher"] or "MERGE (master" in e["cypher"]
        or "OPTIONAL MATCH (m:" in e["cypher"]
    ]
    assert write_cyphers == [], (
        f"CPS Agent raise 시 후속 write 호출됨: {write_cyphers}"
    )


async def test_cps_orphan_master_guard_raises_on_data_loss():
    """[2026-05 데이터 손실 방지] PRD 와 동일 패턴 — master 비었지만 cps_total>1 → raise.

    V1~V16 누적된 master 가 사라진 상태에서 V17 처리 시 누적 Problem/Solution
    데이터 손실 시나리오. 가드가 batch 중단.
    """
    payload = CpsInput(
        project_name="harness", version="v17", date="2026-05-21", meeting_content="새 내용"
    )

    def respond(prompt: str) -> str:
        if "당신은 '통합 하네스 아키텍트'" in prompt:
            return json.dumps({
                "_harness_metadata": {},
                "nodes": [
                    {"id": "doc_cps_harness_v17", "label": "CPS_Document",
                     "properties": {"project": "harness", "version": "v17"}},
                    {"id": "prb_x", "label": "Problem", "properties": {"summary": "x"}},
                ],
                "relationships": [
                    {"source": "prb_x", "type": "EXTRACTED_FROM", "target": "doc_cps_harness_v17"},
                ],
            }, ensure_ascii=False)
        raise AssertionError("guard should fire before impact/merge prompts")

    gemini = FakeGemini(respond)
    # fetch master 가 master 없음 + cps_total=5 신호 → 가드 발동
    neo = FakeNeo4j(responses=[
        [],  # save_cps
        [],  # save_meeting_log
        [{
            "master_id": None,
            "master_content": "",
            "master_probs": [],
            "latest_id": "doc_cps_harness_v17",
            "latest_content": "stale",
            "latest_probs": [],
            "project_name": "harness",
            "cps_total": 5,  # ← orphan 신호
        }],
    ])
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cps-orphan")

    with pytest.raises(RuntimeError, match="비정상적으로 사라진"):
        await run_cps_pipeline(ctx, payload)
    # impact/merge 단계 들어가기 전 raise — gemini 1회 호출만 (CPS Agent)
    assert len(gemini.calls) == 1
    # save_cps + save_log + fetch_master — merge 안 감
    assert len(neo.executed) == 3
