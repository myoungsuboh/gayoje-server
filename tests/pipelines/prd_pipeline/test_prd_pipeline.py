"""
run_prd_pipeline end-to-end 테스트 — LLM/Neo4j fake.

LLM 호출 순서 (4회):
  call 0: prd_extract   — 자유 텍스트, schema=None
  call 1: prd_graph     — PRD_GRAPH_SCHEMA (Epic/Story 환각 차단 유효)
  call 2: prd_impact    — PRD_IMPACT_SCHEMA
  call 3: prd_merge     — 자유 테스트, schema=None

Neo4j 호출 순서 (3회):
  [0] save_prd_query    — UNWIND 포함 (Epic/Story 노드 있으면 non-empty)
  [1] fetch_prd_master  — {project: ...}
  [2] merge_master_prd  — BASED_ON + design_source_stale
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.prd_pipeline import (
    PrdInput,
    build_merge_master_prd_query,
    call_prd_merge_agent,
    filter_affected_prd_sections,
    parse_cps_for_prd,
    run_prd_pipeline,
)
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio


# ─── 공통 fixture ──────────────────────────────────────────────────

_CPS_GRAPH = {
    "nodes": [
        {
            "id": "doc_cps_food_v1",
            "label": "CPS_Document",
            "properties": {"full_markdown": "# CPS\n## Problem\n- 느림"},
        },
        {
            "id": "prb_01",
            "label": "Problem",
            "properties": {"summary": "서비스 느림"},
        },
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
    "affected_sections": [],
    "removed_epic_ids": [],
    "removed_story_ids": [],
    "analysis": "",
})

_MERGE_TEXT = "### Epic & Story Map\n- 업데이트된 에픽"


# ─── parse_cps_for_prd (순수 함수) ─────────────────────────────────────


def test_parse_cps_extracts_markdown_and_problems():
    result = parse_cps_for_prd(_CPS_GRAPH)
    assert "CPS" in result["pure_markdown"]
    assert "[prb_01]" in result["problems"]
    assert "서비스 느림" in result["problems"]


def test_parse_cps_returns_defaults_on_empty_graph():
    result = parse_cps_for_prd({})
    assert result["pure_markdown"] == "내용 없음"
    assert result["problems"] == "- 매핑된 문제 없음"


def test_parse_cps_no_problems_in_graph():
    graph = {"nodes": [{"id": "doc_x", "label": "CPS_Document", "properties": {"full_markdown": "#x"}}], "relationships": []}
    result = parse_cps_for_prd(graph)
    assert result["pure_markdown"] == "#x"
    assert result["problems"] == "- 매핑된 문제 없음"


# ─── filter_affected_prd_sections (순수 함수) ───────────────────────────


def test_filter_prd_first_run_on_empty_master():
    out = filter_affected_prd_sections("", "내용", {"affected_sections": []})
    assert out["is_first_run"] is True
    assert out["_diagnostic"]["mode"] == "FIRST_RUN"


def test_filter_prd_incremental_matches_affected_section():
    master = (
        "## 📄 PRD\n"
        "### Epic & Story Map\n- 기존 에픽\n"
        "### Screen Architecture\n- 기존 스크린\n"
    )
    impact = {
        "affected_sections": ["Epic & Story Map"],
        "removed_epic_ids": [],
        "removed_story_ids": [],
        "analysis": "변경",
    }
    out = filter_affected_prd_sections(
        master, "새 내용 50자 이상으로 작성하여 fallback 트리거를 피한다.", impact
    )
    assert out["is_first_run"] is False
    assert out["_diagnostic"]["mode"] == "INCREMENTAL"
    assert "Epic & Story Map" in out["affected_section_keys"]


# ─── build_merge_master_prd_query (순수 함수) ───────────────────────────


def test_merge_master_prd_has_based_on_and_stale_marker():
    cypher, params = build_merge_master_prd_query(
        "food", "# merged", "doc_prd_food_v1", cleanup_at_version_count=0
    )
    assert "BASED_ON" in cypher
    assert "design_source_stale" in cypher
    assert params["master_prd_id"] == "doc_prd_master_food"
    assert params["master_cps_id"] == "doc_cps_master_food"
    assert params["merged_content"] == "# merged"  # parameter binding 직접 삽입 없음


def test_merge_master_prd_normalizes_dot_in_project():
    _, params = build_merge_master_prd_query("foo.bar", "# x", None, cleanup_at_version_count=0)
    assert params["master_prd_id"] == "doc_prd_master_foo_bar"
    assert params["master_cps_id"] == "doc_cps_master_foo_bar"


def test_merge_master_prd_includes_synthesized_from_when_delta_given():
    cypher, params = build_merge_master_prd_query(
        "food", "# x", "doc_prd_food_v1", cleanup_at_version_count=0
    )
    assert "SYNTHESIZED_FROM" in cypher
    assert params["latest_delta_id"] == "doc_prd_food_v1"


# ─── [2026-05-26] 데이터 무결성 가드 — master full_markdown wipe 차단 ───


def test_build_merge_master_prd_query_refuses_empty_content():
    """빈 string → ValueError. master 누적 PRD 보호."""
    with pytest.raises(ValueError, match="비어있음"):
        build_merge_master_prd_query("food", "", latest_delta_id=None, cleanup_at_version_count=0)


def test_build_merge_master_prd_query_refuses_whitespace_only():
    with pytest.raises(ValueError, match="비어있음"):
        build_merge_master_prd_query("food", "  \n\t \n", latest_delta_id=None, cleanup_at_version_count=0)


# ─── run_prd_pipeline ────────────────────────────────────────────────


async def test_first_run_prd_ids_and_mode():
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON, _IMPACT_EMPTY, _MERGE_TEXT])
    neo4j = FakeNeo4j(responses=[[], [], []])  # save_prd / fetch_master / merge_master
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="prd-first")

    result = await run_prd_pipeline(
        ctx, PrdInput(project_name="food", version="v1", cps_graph=_CPS_GRAPH)
    )

    assert result.delta_prd_id == "doc_prd_food_v1"
    assert result.master_prd_id == "doc_prd_master_food"
    assert result.mode == "first_run"
    assert result.diagnostic["filter"]["mode"] == "FIRST_RUN"


def test_inventory_from_master_markdown_uses_epic_section():
    """[2026-05-27 R1] master_prd_details(graph)가 비어도 full_markdown 의
    Epic & Story Map 섹션을 인벤토리로 사용 — '(첫 실행)' 으로 떨어지지 않음."""
    from app.pipelines.prd_pipeline import _inventory_from_master_markdown
    md = (
        "## PRD\n\n"
        "### 1. Product Overview\n- 비전\n\n"
        "### 2. Epic & User Story Map\n"
        "#### 📦 Epic 1: 인증\n- **[Story 1.1] 로그인**\n\n"
        "### 3. Screen Architecture\n- 화면\n"
    )
    inv = _inventory_from_master_markdown(md)
    assert "첫 실행" not in inv
    assert "Epic 1" in inv
    assert "Story 1.1" in inv or "로그인" in inv


def test_inventory_from_master_markdown_empty_is_first_run():
    """master_content 가 비면 기존 '(첫 실행)' 신호 유지 (진짜 첫 실행)."""
    from app.pipelines.prd_pipeline import _inventory_from_master_markdown
    assert "첫 실행" in _inventory_from_master_markdown("")
    assert "첫 실행" in _inventory_from_master_markdown("   ")


async def test_merge_agent_uses_markdown_inventory_when_graph_empty():
    """[2026-05-27 R1] graph 인벤토리(master_prd_details)가 비어도 incremental 에서
    full_markdown 의 Epic 섹션이 merge prompt 에 전달돼 '첫 실행' 으로 떨어지지 않음.

    이 신호가 빠지면 merge agent 가 매 회의 새 Epic/Story 를 자유 부여 → 누더기 누적.
    """
    gemini = FakeGemini(responses=["merged out"])
    neo4j = FakeNeo4j()
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="inv-fb")
    filter_data = {
        "master_prd_details": [],  # graph 인벤토리 빔 (구조적 결함 재현)
        "master_content": (
            "### 2. Epic & User Story Map\n"
            "#### 📦 Epic 1: 인증\n- **[Story 1.1] 로그인**\n"
        ),
        "affected_sections_content": "### 2. Epic & User Story Map\n...",
        "latest_content": "delta 내용",
        "impact": {},
    }
    await call_prd_merge_agent(ctx, filter_data)
    prompt = gemini.calls[0]["prompt"]
    # 빈 인벤토리 신호("새 ID 자유 부여")가 prompt 에 들어가면 안 됨 — 그게 누더기 원인.
    # (prd_merge.md 템플릿 본문엔 "첫 실행" 단어가 별도로 존재하므로 그 전문으로 검증.)
    assert "기존 Epic/Story 없음. 새 ID 자유 부여" not in prompt
    # master full_markdown 의 기존 Epic/Story 가 인벤토리로 전달됐는지
    assert "Story 1.1" in prompt or "로그인" in prompt


async def test_prd_pipeline_includes_lint_diagnostic():
    """[2026-05-27 R3] 생성 경로에서 merged_content 를 lint 해 diagnostic 에 노출.

    이전엔 lint_prd 가 수동 엔드포인트에만 연결돼 부실/불일치 PRD 가 무차단
    저장됐다. 이제 master 저장 경로에서 품질을 측정해 diagnostic.prd_lint 로 노출.
    _MERGE_TEXT 엔 Story 표기가 없어 PRD_NO_STORY(error) 가 잡혀야 한다.
    """
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON, _IMPACT_EMPTY, _MERGE_TEXT])
    neo4j = FakeNeo4j(responses=[[], [], []])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="lint1")

    result = await run_prd_pipeline(
        ctx, PrdInput(project_name="food", version="v1", cps_graph=_CPS_GRAPH)
    )
    lint = result.diagnostic["prd_lint"]
    assert "score" in lint
    assert lint["error_count"] >= 1
    assert any(i["code"] == "PRD_NO_STORY" for i in lint["issues"])


async def test_prd_pipeline_schema_assignment_per_call():
    """extract/merge 는 schema=None, graph/impact 는 schema 있음.

    [2026-05-25 perf B] first_run 시 impact + merge skip → 4회 → 2회.
    incremental 시나리오로 만들기 위해 fetch master_content 채움.
    [perf A] graph 와 impact 가 병렬 실행 — call 순서는 task 시작 순서를 따름
    (asyncio.gather 의 첫 인자가 prd_graph). FakeGemini 응답 순서 동일.
    """
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON, _IMPACT_EMPTY, _MERGE_TEXT])
    fetch_response = [{
        "master_id": "doc_prd_master_food",
        "master_content": "### 1. 기존 PRD 본문\n이전 PRD master.",
        "master_prd_details": [
            {"epic_id": "epic_01", "epic_summary": "X", "story_id": "s1", "story_summary": "y", "screen_name": "Z"},
        ],
        "latest_id": "doc_prd_food_v0",
        "latest_content": "직전 PRD",
        "latest_prd_details": [],
        "project_name": "food",
    }]
    # 병렬화로 fetch 가 Save 보다 먼저 호출 — mock 순서 변경.
    neo4j = FakeNeo4j(responses=[fetch_response, [], []])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="prd-schema")

    await run_prd_pipeline(ctx, PrdInput(project_name="food", version="v1", cps_graph=_CPS_GRAPH))

    # 4회 LLM 호출 유지 (incremental). 단 호출 순서는 병렬로 graph↔impact 가
    # 서로 바뀔 수 있으므로 schema 별 매칭으로 검증.
    assert len(gemini.calls) == 4
    schemas = [c.get("response_schema") for c in gemini.calls]
    # extract / merge 는 schema=None (2회)
    none_count = sum(1 for s in schemas if s is None)
    assert none_count == 2
    # graph schema (nodes) 1회
    assert any(s and "nodes" in (s.get("properties") or {}) for s in schemas)
    # impact schema (affected_sections) 1회
    assert any(s and "affected_sections" in (s.get("properties") or {}) for s in schemas)


async def test_prd_pipeline_neo4j_call_order_and_params():
    """[2026-05-25 perf A] graph LLM 과 fetch 병렬 — Cypher 순서 비결정.
    set 검증 + 마지막은 무조건 BASED_ON master merge."""
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON, _IMPACT_EMPTY, _MERGE_TEXT])
    fetch_response = [{
        "master_id": "doc_prd_master_food",
        "master_content": "### 1. master 본문",
        "master_prd_details": [
            {"epic_id": "e1", "epic_summary": "x", "story_id": "s1", "story_summary": "y", "screen_name": "Z"},
        ],
        "latest_id": "doc_prd_food_v0",
        "latest_content": "직전",
        "latest_prd_details": [],
        "project_name": "food",
    }]
    neo4j = FakeNeo4j(responses=[fetch_response, [], []])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="prd-neo4j")

    await run_prd_pipeline(ctx, PrdInput(project_name="food", version="v1", cps_graph=_CPS_GRAPH))

    assert len(neo4j.executed) == 3
    cyphers = [e["cypher"] for e in neo4j.executed]
    # 순서 무관: Save PRD (UNWIND) + fetch (OPTIONAL MATCH master) 둘 다 존재.
    assert any("UNWIND" in c for c in cyphers[:2])
    # 마지막 = master merge + BASED_ON
    assert neo4j.executed[-1]["params"]["master_prd_id"] == "doc_prd_master_food"
    assert "BASED_ON" in cyphers[-1]


async def test_prd_first_run_spec_zero_graceful_no_changes():
    """[2026-05-27] batch 첫 회의(킥오프, 추상)가 Epic/Story 0개여도 batch 를 막지 않음.

    이전엔 first_run + spec 0 → ValueError 로 batch 순차 처리가 첫 회의에서 정지했다.
    킥오프는 으레 추상적이고 batch 는 점진 누적 시연이므로, CPS 는 저장하되 PRD master
    는 만들지 않고(빈 PRD 저장 X) graceful no_changes 반환 → 다음 회의서 누적되면 생성.
    """
    no_spec = json.dumps({
        "nodes": [{"id": "doc_prd_x_v1", "label": "PRD_Document", "properties": {}}],
        "relationships": [],
    })
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, no_spec, _IMPACT_EMPTY])
    # 병렬 path B: fetch_prd_master_and_latest 가 master 비어있는 응답 반환.
    neo4j = FakeNeo4j(responses=[[{
        "master_id": None,
        "master_content": "",
        "master_prd_details": [],
        "latest_id": None,
        "latest_content": "",
        "latest_prd_details": [],
        "project_name": "x",
        "prd_total": 0,
    }]])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="prd-firstrun-empty")

    result = await run_prd_pipeline(
        ctx, PrdInput(project_name="x", version="v1", cps_graph=_CPS_GRAPH)
    )
    # ValueError 없이 graceful no_changes — batch 가 다음 회의로 진행 가능.
    assert result.mode == "no_changes"
    assert result.diagnostic.get("spec_count") == 0
    # 빈 PRD master 를 저장하지 않음 — master merge(BASED_ON) cypher 호출 X.
    cyphers = [e["cypher"] for e in neo4j.executed]
    assert not any("BASED_ON" in c for c in cyphers)


async def test_prd_spec_zero_with_existing_master_returns_no_changes():
    """[2026-05-26] V6 같은 보강·결정 회의록 — 새 Epic/Story 0개 + 기존 master 존재.

    환각 가드 false positive 회피: master 가 있으면 graceful no-op 반환.
    Save / Merge LLM / Master merge cypher 모두 skip → master 그대로 유지.
    batch loop 가 다음 회의로 자연스럽게 이동 가능.
    """
    no_spec = json.dumps({
        "nodes": [{"id": "doc_prd_food_v6", "label": "PRD_Document", "properties": {}}],
        "relationships": [],
    })
    # path B: 기존 master + V1~V5 누적 Epic/Story.
    fetch_response = [{
        "master_id": "doc_prd_master_food",
        "master_content": "### 1. Overview\n기존 누적 PRD\n### 2. Epic & Story Map\n- Epic-01\n- Story-01.1",
        "master_prd_details": [
            {"epic_id": "epic_01", "epic_summary": "X", "story_id": "s1", "story_summary": "y", "screen_name": "Z"},
        ],
        "latest_id": "doc_prd_food_v5",
        "latest_content": "직전 PRD",
        "latest_prd_details": [],
        "project_name": "food",
        "prd_total": 5,
    }]
    # impact LLM 도 병렬로 호출 → 응답 필요. merge LLM 은 호출 안 됨 (skip).
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, no_spec, _IMPACT_EMPTY])
    neo4j = FakeNeo4j(responses=[fetch_response])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="prd-no-changes")

    result = await run_prd_pipeline(
        ctx, PrdInput(project_name="food", version="v6", cps_graph=_CPS_GRAPH)
    )

    # 모드 확인
    assert result.mode == "no_changes"
    assert result.master_prd_id == "doc_prd_master_food"
    assert result.diagnostic.get("spec_count") == 0
    assert "보강" in result.diagnostic.get("reason", "") or "결정" in result.diagnostic.get("reason", "")

    # save_prd / merge_master cypher 호출 안 됨 (fetch 만 1회).
    cyphers = [e["cypher"] for e in neo4j.executed]
    assert not any("UNWIND" in c and "MERGE" in c and "Epic" in c for c in cyphers), \
        "Save PRD 가 호출되면 안 됨 (no_changes 모드)"
    assert not any("MERGE (master:PRD_Document" in c for c in cyphers), \
        "Master merge 가 호출되면 안 됨 (no_changes 모드)"

    # merge LLM 도 호출 안 됨 (extract + graph + impact = 3회).
    assert len(gemini.calls) == 3


async def test_prd_orphan_master_guard_raises_on_data_loss():
    """[2026-05 데이터 손실 방지] master_content="" 인데 prd_total>1 이면 raise.

    시나리오: V1~V16 의 누적 PRD master 가 어떤 이유로 사라진 후 V17 처리 시.
    [2026-05-25 perf A] graph LLM 과 fetch 병렬 — fetch 가 Save 보다 먼저
    호출됨. mock 순서: fetch 응답 (prd_total=5 orphan 신호) 가 1번.
    raise 발생 시점: gather 후 orphan 가드 — Save / merge cypher 안 호출.
    """
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON])
    # 병렬 path: fetch 가 Save 보다 먼저 — 1번째 응답 = fetch 결과.
    neo4j = FakeNeo4j(responses=[
        [{
            "master_id": None,
            "master_content": "",
            "master_prd_details": [],
            "latest_id": "doc_prd_food_v17",
            "latest_content": "stale latest",
            "latest_prd_details": [],
            "project_name": "food",
            "prd_total": 5,  # ← orphan 신호
        }],
        [],  # 만약 race 로 save 가 호출되더라도 안전 fallback
    ])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="prd-orphan")

    with pytest.raises(RuntimeError, match="비정상적으로 사라진"):
        await run_prd_pipeline(
            ctx, PrdInput(project_name="food", version="v17", cps_graph=_CPS_GRAPH)
        )
    # gather 후 orphan 가드 raise — extract + graph + impact + retry (mock 부족 시
    # generate_json_with_retry 가 빈 응답 retry) = 4회. merge 안 감.
    assert len(gemini.calls) == 4
    # merge_master cypher 는 호출 안 됨 (raise 전).
    write_cyphers = [
        e["cypher"] for e in neo4j.executed
        if "MERGE (master:PRD_Document" in e["cypher"]
    ]
    assert write_cyphers == []


async def test_prd_first_run_legitimate_passes_through():
    """진짜 첫 실행 — prd_total=1 (방금 저장한 delta 만) → 가드 안 발동, 정상 진행."""
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON, _IMPACT_EMPTY, _MERGE_TEXT])
    neo4j = FakeNeo4j(responses=[
        [],  # save_prd
        [{
            "master_id": None,
            "master_content": "",
            "master_prd_details": [],
            "latest_id": "doc_prd_food_v1",
            "latest_content": "",
            "latest_prd_details": [],
            "project_name": "food",
            "prd_total": 1,  # 방금 저장한 delta 1건만 — 진짜 첫 실행
        }],
        [],  # merge_master
    ])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="prd-first-legit")

    result = await run_prd_pipeline(
        ctx, PrdInput(project_name="food", version="v1", cps_graph=_CPS_GRAPH)
    )
    assert result.mode == "first_run"  # 정상 first run 인지 확인


async def test_previous_prd_id_wins_as_delta_id():
    gemini = FakeGemini(responses=[_PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON, _IMPACT_EMPTY, _MERGE_TEXT])
    neo4j = FakeNeo4j(responses=[[], [], []])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="prd-prev")

    result = await run_prd_pipeline(
        ctx,
        PrdInput(
            project_name="food",
            version="v2",
            cps_graph=_CPS_GRAPH,
            previous_prd_id="doc_prd_food_v1",
        ),
    )

    assert result.delta_prd_id == "doc_prd_food_v1"
    assert result.master_prd_id == "doc_prd_master_food"


async def test_prd_idempotent_same_input_same_ids():
    """동일 입력 2회 실행 → delta_prd_id / master_prd_id 동일."""
    payload = PrdInput(project_name="x", version="v1", cps_graph=_CPS_GRAPH)

    async def _run():
        g = FakeGemini(responses=[_PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON, _IMPACT_EMPTY, _MERGE_TEXT])
        n = FakeNeo4j(responses=[[], [], []])
        c = PipelineContext(gemini=g, neo4j=n, idempotency_key="k")
        r = await run_prd_pipeline(c, payload)
        return r.delta_prd_id, r.master_prd_id

    assert await _run() == await _run()


# ─── [2026-05-28] Screen + IMPLEMENTED_ON reconcile (markdown ground truth) ──


def test_parse_story_pair_handles_diverse_formats():
    """Story id 형식 변형 흡수 — zero-pad / separator / prefix 무관."""
    from app.pipelines.prd_pipeline import _parse_story_pair
    assert _parse_story_pair("story_1_1") == (1, 1)
    assert _parse_story_pair("story_01_1") == (1, 1)
    assert _parse_story_pair("story_001_001") == (1, 1)
    assert _parse_story_pair("story-2-3") == (2, 3)
    assert _parse_story_pair("Story 10.5") == (10, 5)
    assert _parse_story_pair("epic_01") is None  # 정수 1개만
    assert _parse_story_pair(None) is None
    assert _parse_story_pair("") is None


def test_extract_all_screens_section_and_inline():
    """markdown 의 Section 3 헤더 + Story User Flow inline 양쪽에서 screen 추출."""
    from app.pipelines.prd_pipeline import _extract_all_screens_from_markdown
    md = """
### 2. Epic & User Story Map
#### 📦 Epic 1: 결제
- **[Story 1.1] 케이스 등록**
  - **User Flow**: 1. 사용자가 [테스트 케이스 관리] 화면에서 [등록] 버튼을 클릭. → 2. ...
- **[Story 1.2] 케이스 수정**
  - **User Flow**: 1. [테스트 케이스 관리] 화면에서 항목 선택 → 2. ...

### 3. Screen Architecture
#### 🖥️ [Screen: 테스트 케이스 관리]
- **포함된 기능**:
  - `[Story 1.1]` 케이스 등록
  - `[Story 1.2]` 케이스 수정
#### 🖥️ [Screen: 대시보드]
- **포함된 기능**:
  - `[Story 2.1]` 통계
"""
    screens = _extract_all_screens_from_markdown(md)
    by_name = {s["name"]: s["pairs"] for s in screens}
    # 두 화면 모두 추출
    assert "테스트 케이스 관리" in by_name
    assert "대시보드" in by_name
    # 테스트 케이스 관리: Section 3 의 1.1, 1.2 + inline 1.1, 1.2 = {(1,1), (1,2)}
    assert set(by_name["테스트 케이스 관리"]) == {(1, 1), (1, 2)}
    # 대시보드: Section 3 의 2.1
    assert set(by_name["대시보드"]) == {(2, 1)}


def test_extract_all_screens_backtick_and_quoted_format():
    """[2026-06] 현 prd_extract 포맷 회귀 방지 — '포함된 기능' 의 Story 가 inner-bracket
    없는 백틱('`Story 1.1`'), User Flow 화면명이 따옴표('데이터 소스 관리' 화면)로 등장.
    이전엔 둘 다 못 잡아 pairs=[] → IMPLEMENTED_ON 미생성 → 빈 Relation Graph 버그."""
    from app.pipelines.prd_pipeline import _extract_all_screens_from_markdown
    md = """
#### 📦 Epic 1: 데이터 통합
- **[Story 1.1] 데이터 소스 연결 설정**
  - **User Flow**: 1. 관리자가 '데이터 소스 관리' 화면에서 '새 소스 추가' 버튼을 클릭한다.

### 3. Screen Architecture
#### 🖥️ [Screen: 데이터 소스 관리]
- **포함된 기능**:
  - `Story 1.1` 데이터 소스 연결 설정 (from Epic 1)
#### 🖥️ [Screen: 통합 대시보드]
- **포함된 기능**:
  - `Story 2.1` 통합 대시보드 조회 (from Epic 2)
"""
    by_name = {s["name"]: set(s["pairs"]) for s in _extract_all_screens_from_markdown(md)}
    # 백틱(무괄호) Story 참조 + 따옴표 화면명 둘 다 흡수
    assert by_name.get("데이터 소스 관리") == {(1, 1)}
    assert by_name.get("통합 대시보드") == {(2, 1)}


def test_extract_all_screens_ignores_non_screen_brackets():
    """Role/Story/Epic 키워드로 시작하는 inline 브래킷은 화면명으로 보지 않음."""
    from app.pipelines.prd_pipeline import _extract_all_screens_from_markdown
    md = """
#### 📦 Epic 1: 결제
- **[Story 1.1] x**
  - **User Flow**: 1. [Role A] 사용자가 [메인] 화면에서 [Story 1.2] 참조 → 결제 화면
"""
    screens = _extract_all_screens_from_markdown(md)
    names = {s["name"] for s in screens}
    # [메인] 화면 만 잡혀야 함. [Role A]/[Story 1.2] 는 화면명 아님.
    assert "메인" in names
    assert "Role A" not in names
    assert "Story 1.2" not in names


def test_reconcile_screens_injects_missing_nodes_and_edges():
    """Agent2 가 Screen + IMPLEMENTED_ON 누락한 그래프에 markdown 기준으로 보강."""
    from app.pipelines.prd_pipeline import _reconcile_screens_in_prd_graph
    prd_graph = {
        "nodes": [
            {"id": "doc_prd_x_v1", "label": "PRD_Document", "properties": {}},
            {"id": "epic_01", "label": "Epic", "properties": {"summary": "결제"}},
            {"id": "story_01_1", "label": "Story", "properties": {"summary": "케이스 등록"}},
            {"id": "story_01_2", "label": "Story", "properties": {"summary": "케이스 수정"}},
            # ★ Screen 노드 + IMPLEMENTED_ON 관계 모두 누락 (Agent2 환각)
        ],
        "relationships": [
            {"source": "epic_01", "type": "CONTAINS", "target": "story_01_1"},
            {"source": "epic_01", "type": "CONTAINS", "target": "story_01_2"},
        ],
    }
    md = """
### 3. Screen Architecture
#### 🖥️ [Screen: 테스트 케이스 관리]
- `[Story 1.1]` 등록
- `[Story 1.2]` 수정
"""
    out = _reconcile_screens_in_prd_graph(prd_graph, md)
    # Screen 노드 1개 추가
    screen_nodes = [n for n in out["nodes"] if n["label"] == "Screen"]
    assert len(screen_nodes) == 1
    assert screen_nodes[0]["properties"]["name"] == "테스트 케이스 관리"
    assert screen_nodes[0]["id"].startswith("screen_md_")
    # IMPLEMENTED_ON edge 2개 추가
    impl_edges = [r for r in out["relationships"] if r["type"] == "IMPLEMENTED_ON"]
    assert len(impl_edges) == 2
    edge_targets = {(r["source"], r["target"]) for r in impl_edges}
    sid = screen_nodes[0]["id"]
    assert ("story_01_1", sid) in edge_targets
    assert ("story_01_2", sid) in edge_targets


def test_reconcile_screens_reuses_existing_screen_by_name():
    """Agent2 가 Screen 은 만들었지만 IMPLEMENTED_ON 만 누락한 케이스 — 기존 Screen 재사용."""
    from app.pipelines.prd_pipeline import _reconcile_screens_in_prd_graph
    prd_graph = {
        "nodes": [
            {"id": "story_01_1", "label": "Story", "properties": {}},
            {"id": "screen_01", "label": "Screen", "properties": {"name": "테스트 케이스 관리"}},
        ],
        "relationships": [],
    }
    md = "#### 🖥️ [Screen: 테스트 케이스 관리]\n- `[Story 1.1]` 등록"
    out = _reconcile_screens_in_prd_graph(prd_graph, md)
    # Screen 새로 안 만듦
    screen_nodes = [n for n in out["nodes"] if n["label"] == "Screen"]
    assert len(screen_nodes) == 1
    assert screen_nodes[0]["id"] == "screen_01"
    # IMPLEMENTED_ON edge 추가 — 기존 screen_01 사용
    impl_edges = [r for r in out["relationships"] if r["type"] == "IMPLEMENTED_ON"]
    assert len(impl_edges) == 1
    assert impl_edges[0]["source"] == "story_01_1"
    assert impl_edges[0]["target"] == "screen_01"


def test_reconcile_screens_handles_padded_story_ids_via_fuzzy_match():
    """Agent2 가 Story id 를 3-digit pad 로 저장해도 markdown [Story 1.1] 과 매칭."""
    from app.pipelines.prd_pipeline import _reconcile_screens_in_prd_graph
    prd_graph = {
        "nodes": [
            {"id": "story_001_001", "label": "Story", "properties": {}},  # 3-digit pad
        ],
        "relationships": [],
    }
    md = "#### 🖥️ [Screen: A]\n- `[Story 1.1]` x"
    out = _reconcile_screens_in_prd_graph(prd_graph, md)
    impl_edges = [r for r in out["relationships"] if r["type"] == "IMPLEMENTED_ON"]
    assert len(impl_edges) == 1
    # source 는 prd_graph 의 실제 Story id (3-digit pad) 사용
    assert impl_edges[0]["source"] == "story_001_001"


def test_reconcile_screens_skips_when_story_not_in_graph():
    """markdown 에 [Story X.Y] 있지만 prd_graph 에 해당 Story 없으면 edge 합성 안 함."""
    from app.pipelines.prd_pipeline import _reconcile_screens_in_prd_graph
    prd_graph = {"nodes": [], "relationships": []}
    md = "#### 🖥️ [Screen: A]\n- `[Story 1.1]` x"
    out = _reconcile_screens_in_prd_graph(prd_graph, md)
    # Screen 은 추가 (markdown 에 있으니까)
    screen_nodes = [n for n in out["nodes"] if n["label"] == "Screen"]
    assert len(screen_nodes) == 1
    # IMPLEMENTED_ON 은 Story 가 없으므로 합성 안 함
    impl_edges = [r for r in out["relationships"] if r["type"] == "IMPLEMENTED_ON"]
    assert len(impl_edges) == 0


def test_extract_all_screens_handles_missing_emoji():
    """LLM 이 🖥️ 이모지 누락한 '#### [Screen: 이름]' 도 매칭 — query_repository 와 동일 lenient."""
    from app.pipelines.prd_pipeline import _extract_all_screens_from_markdown
    md = """
### 3. Screen Architecture
#### [Screen: 자동화 규칙 관리 화면]
- **포함된 기능**:
  - `[Story 1.1]` 규칙 추가
  - `[Story 1.2]` 규칙 수정
"""
    screens = _extract_all_screens_from_markdown(md)
    by_name = {s["name"]: s["pairs"] for s in screens}
    # 🖥️ 없어도 '자동화 규칙 관리 화면' 추출되고 페어도 매핑
    assert "자동화 규칙 관리 화면" in by_name
    assert set(by_name["자동화 규칙 관리 화면"]) == {(1, 1), (1, 2)}


def test_extract_all_stories_from_markdown_basic():
    """markdown 의 [Story X.Y] 헤더 → (pair, summary) 정확 추출."""
    from app.pipelines.prd_pipeline import _extract_all_stories_from_markdown
    md = """
#### 📦 Epic 1: 결제
- **[Story 1.1] 케이스 등록**
  - **User Story**: ...
- **[Story 1.2] 케이스 수정**
  - **User Story**: ...

#### 📦 Epic 2: 통계
- **[Story 2.1] 통계 조회**
"""
    out = _extract_all_stories_from_markdown(md)
    pairs = [s["pair"] for s in out]
    assert (1, 1) in pairs
    assert (1, 2) in pairs
    assert (2, 1) in pairs
    summaries = {s["pair"]: s["summary"] for s in out}
    assert "케이스 등록" in summaries[(1, 1)]
    assert "케이스 수정" in summaries[(1, 2)]
    assert "통계 조회" in summaries[(2, 1)]


def test_reconcile_screens_synthesizes_missing_story_from_markdown():
    """Agent2 가 Story 자체를 누락했어도 markdown [Story X.Y] 헤더로 Story 합성 → edge 연결."""
    from app.pipelines.prd_pipeline import _reconcile_screens_in_prd_graph
    # Agent2 가 Screen 도 Story 도 모두 누락한 케이스
    prd_graph = {
        "nodes": [
            {"id": "doc_prd_x_v1", "label": "PRD_Document", "properties": {}},
            {"id": "epic_01", "label": "Epic", "properties": {"summary": "자동화"}},
        ],
        "relationships": [],
    }
    md = """
#### 📦 Epic 1: 자동화
- **[Story 1.1] 규칙 추가**
  - **User Story**: ...

### 3. Screen Architecture
#### 🖥️ [Screen: 자동화 규칙 관리 화면]
- `[Story 1.1]` 규칙 추가
"""
    out = _reconcile_screens_in_prd_graph(prd_graph, md)
    # Story 합성됨
    story_nodes = [n for n in out["nodes"] if n["label"] == "Story"]
    assert len(story_nodes) == 1
    assert story_nodes[0]["id"] == "story_01_1"
    assert story_nodes[0]["properties"].get("summary")
    # Screen 합성됨
    screen_nodes = [n for n in out["nodes"] if n["label"] == "Screen"]
    assert len(screen_nodes) == 1
    assert screen_nodes[0]["properties"]["name"] == "자동화 규칙 관리 화면"
    # IMPLEMENTED_ON edge 연결 — Story 합성으로 가능해진 핵심 fix
    impl_edges = [r for r in out["relationships"] if r["type"] == "IMPLEMENTED_ON"]
    assert len(impl_edges) == 1
    assert impl_edges[0]["source"] == "story_01_1"


def test_reconcile_screens_synthesizes_only_missing_story_pair():
    """Agent2 가 Story 일부만 만든 케이스 — 누락된 페어만 합성, 기존은 유지."""
    from app.pipelines.prd_pipeline import _reconcile_screens_in_prd_graph
    prd_graph = {
        "nodes": [
            {"id": "story_01_1", "label": "Story", "properties": {"summary": "기존"}},
        ],
        "relationships": [],
    }
    md = """
- **[Story 1.1] 기존**
- **[Story 1.2] 추가**

#### 🖥️ [Screen: A]
- `[Story 1.1]` x
- `[Story 1.2]` y
"""
    out = _reconcile_screens_in_prd_graph(prd_graph, md)
    story_ids = sorted(n["id"] for n in out["nodes"] if n["label"] == "Story")
    # story_01_1 (Agent2 출력 유지) + story_01_2 (합성)
    assert story_ids == ["story_01_1", "story_01_2"]
    # 기존 story_01_1 의 summary 가 덮어쓰여지지 않음
    s1 = next(n for n in out["nodes"] if n["id"] == "story_01_1")
    assert s1["properties"]["summary"] == "기존"
    # 두 Story 모두 IMPLEMENTED_ON edge 생김
    impl_edges = [r for r in out["relationships"] if r["type"] == "IMPLEMENTED_ON"]
    assert len(impl_edges) == 2


def test_reconcile_screens_idempotent_no_duplicate_edges():
    """이미 IMPLEMENTED_ON edge 가 있으면 중복 추가 안 함."""
    from app.pipelines.prd_pipeline import _reconcile_screens_in_prd_graph
    prd_graph = {
        "nodes": [
            {"id": "story_01_1", "label": "Story", "properties": {}},
            {"id": "screen_01", "label": "Screen", "properties": {"name": "A"}},
        ],
        "relationships": [
            {"source": "story_01_1", "target": "screen_01", "type": "IMPLEMENTED_ON"},
        ],
    }
    md = "#### 🖥️ [Screen: A]\n- `[Story 1.1]` x"
    out = _reconcile_screens_in_prd_graph(prd_graph, md)
    impl_edges = [r for r in out["relationships"] if r["type"] == "IMPLEMENTED_ON"]
    assert len(impl_edges) == 1  # 중복 추가 X
