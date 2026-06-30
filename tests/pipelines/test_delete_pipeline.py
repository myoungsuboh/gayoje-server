"""
delete_pipeline 테스트:
- delete_project: 단순 Cypher 호출 + 빈 project 입력 시 ValueError
- delete_meeting: 11-stage 흐름 + 빈 분기 (no rebuild / cps only / prd only / 둘 다)
- _derive_ids: dot → underscore 변환
- _join_deltas: 마커 wrapping 회귀
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.delete_pipeline import (
    DeleteMeetingInput,
    _derive_ids,
    _join_deltas,
    _prepare_rebuild_data,
    delete_project,
    run_delete_meeting_pipeline,
)
from tests.conftest import FakeGemini, FakeNeo4j


# ─── _derive_ids ────────────────────────────────────────────────


def test_derive_ids_normalizes_dots():
    out = _derive_ids("harness.v2", "v1.2.3")
    assert out["log_id"] == "log_harness_v2_v1_2_3"
    assert out["cps_delta_id"] == "doc_cps_harness_v2_v1_2_3"
    assert out["prd_delta_id"] == "doc_prd_harness_v2_v1_2_3"
    assert out["cps_master_id"] == "doc_cps_master_harness_v2"
    assert out["prd_master_id"] == "doc_prd_master_harness_v2"


def test_derive_ids_simple():
    out = _derive_ids("harness", "v1")
    assert out["log_id"] == "log_harness_v1"


# ─── _join_deltas ───────────────────────────────────────────────


def test_join_deltas_wraps_with_markers():
    deltas = [
        {"version": "v1.0", "content": "first body"},
        {"version": "v1.1", "content": "second body"},
    ]
    out = _join_deltas(deltas, "CPS")
    assert ">>>>> CPS DELTA START (version: v1.0) >>>>>" in out
    assert ">>>>> CPS DELTA START (version: v1.1) >>>>>" in out
    assert "<<<<< CPS DELTA END <<<<<" in out
    assert "first body" in out
    assert "second body" in out


def test_join_deltas_empty_returns_empty_string():
    assert _join_deltas([], "CPS") == ""


def test_join_deltas_falls_back_to_index_when_no_version():
    deltas = [{"content": "x"}]
    out = _join_deltas(deltas, "PRD")
    assert "version: #1" in out


# ─── _prepare_rebuild_data ──────────────────────────────────────


def test_prepare_rebuild_filters_empty_content():
    remaining = {
        "cps_list": [
            {"version": "v1.0", "content": "real"},
            {"version": "v1.1", "content": None},  # filtered out
            {"version": "v1.2", "content": ""},  # filtered out
        ],
        "prd_list": [],
    }
    out = _prepare_rebuild_data(remaining)
    assert out["has_cps"] is True
    assert out["has_prd"] is False
    assert out["has_any"] is True
    assert out["remaining_cps_count"] == 1
    assert out["remaining_prd_count"] == 0


def test_prepare_rebuild_all_empty():
    out = _prepare_rebuild_data({"cps_list": [], "prd_list": []})
    assert out["has_any"] is False
    assert out["cps_content"] == ""


# ─── delete_project ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_project_returns_count():
    neo = FakeNeo4j(responses=[[{"deleted_count": 42}]])
    ctx = PipelineContext(
        gemini=FakeGemini(lambda p: "no call"), neo4j=neo, idempotency_key="dp1"
    )
    out = await delete_project(ctx, "harness")
    assert out == {"status": "deleted", "project_name": "harness", "child_count": 42}
    # parameter binding
    assert neo.executed[0]["params"] == {"project": "harness"}


@pytest.mark.asyncio
async def test_delete_project_empty_name_raises():
    ctx = PipelineContext(
        gemini=FakeGemini(lambda p: "x"), neo4j=FakeNeo4j(), idempotency_key="dp2"
    )
    with pytest.raises(ValueError, match="비어 있을 수 없습니다"):
        await delete_project(ctx, "")


@pytest.mark.asyncio
async def test_delete_project_cypher_injection_safe():
    """입력값이 Cypher 본문에 직접 보간되면 안 됨."""
    neo = FakeNeo4j(responses=[[{"deleted_count": 0}]])
    ctx = PipelineContext(
        gemini=FakeGemini(lambda p: "x"), neo4j=neo, idempotency_key="dp3"
    )
    dangerous = "x' OR true OR '"
    await delete_project(ctx, dangerous)
    assert dangerous not in neo.executed[0]["cypher"]
    assert neo.executed[0]["params"]["project"] == dangerous


@pytest.mark.asyncio
async def test_delete_project_also_removes_project_node():
    """
    회귀: 사용자가 프로젝트 삭제해도 `:Project` 노드 + `OWNS` 관계가 남아
    `/auth/me/projects` 가 좀비 프로젝트를 계속 반환하던 버그.
    delete_project 가 도메인 데이터 Cypher 뿐 아니라 Project 노드 정리 Cypher
    까지 두 번 호출해야 한다.

    [Phase 2D 멀티테넌시] Project 노드 매칭은 (name, owner_email) 합성 — ctx 의
    user_email 이 Cypher params 로 전파돼야 다른 유저의 동명 프로젝트 보호.
    """
    neo = FakeNeo4j(responses=[[{"deleted_count": 5}], []])
    ctx = PipelineContext(
        gemini=FakeGemini(lambda p: "x"),
        neo4j=neo,
        idempotency_key="dp5",
        user_email="alice@example.com",
    )
    out = await delete_project(ctx, "test")

    # 두 query 모두 실행됐는지 + 파라미터 binding 검증
    assert len(neo.executed) == 2, (
        f"delete_project 는 도메인 데이터 + Project 노드 두 query 실행 — 실제 {len(neo.executed)} 회"
    )
    # 1) 도메인 데이터 Cypher (project property 매칭)
    assert "WHERE root.project = $project" in neo.executed[0]["cypher"]
    assert neo.executed[0]["params"] == {"project": "test"}
    # 2) Project 노드 Cypher — (name, owner_email) 매칭. ctx.user_email 이 전파돼야 함.
    assert "MATCH (p:Project {name: $project, owner_email: $email})" in neo.executed[1]["cypher"]
    assert "DETACH DELETE p" in neo.executed[1]["cypher"]
    assert neo.executed[1]["params"] == {"project": "test", "email": "alice@example.com"}
    # child_count 는 첫 Cypher 결과에서 유지
    assert out["child_count"] == 5


@pytest.mark.asyncio
async def test_delete_project_uses_ctx_user_email_not_empty():
    """[회귀 차단] ctx.user_email 이 비면 Cypher 가 owner_email='' 로 매칭 → 운영
    Project 노드가 한 개도 안 매치(좀비 재발). 이 테스트는 ctx 에 명시 email 을
    설정하면 그 값이 Cypher 까지 도달함을 직접 확인."""
    neo = FakeNeo4j(responses=[[{"deleted_count": 0}], []])
    ctx = PipelineContext(
        gemini=FakeGemini(lambda p: "x"),
        neo4j=neo,
        idempotency_key="dp-user",
        user_email="bob@example.com",
    )
    await delete_project(ctx, "foo")
    project_node_call = neo.executed[1]
    assert project_node_call["params"]["email"] == "bob@example.com"
    assert project_node_call["params"]["email"] != ""


@pytest.mark.asyncio
async def test_delete_project_count_captured_before_delete():
    """
    회귀 테스트: deleted_count 가 DETACH DELETE 전에 캡처되어야 함.

    Cypher 안에 `size(...) AS deleted_count` 가 UNWIND/DELETE 보다 먼저 등장해야
    하고, DELETE 뒤에 count() 같은 deleted variable 참조가 없어야 한다.
    (Cypher 5+ 에서 DETACH DELETE 이후 변수 참조는 0 반환 또는 에러)
    """
    neo = FakeNeo4j(responses=[[{"deleted_count": 7}]])
    ctx = PipelineContext(
        gemini=FakeGemini(lambda p: "x"), neo4j=neo, idempotency_key="dp4"
    )
    out = await delete_project(ctx, "harness")
    assert out["child_count"] == 7

    cypher = neo.executed[0]["cypher"]
    # 검증 1: size() 가 DETACH DELETE 보다 먼저 등장 (캡처 후 삭제)
    size_pos = cypher.find("AS deleted_count")
    delete_pos = cypher.find("DETACH DELETE n")
    assert 0 < size_pos < delete_pos, (
        "deleted_count 가 DETACH DELETE 전에 캡처돼야 함"
    )
    # 검증 2: DETACH DELETE 뒤에 count() 호출이 없어야 함 (deleted variable 참조 금지)
    after_delete = cypher[delete_pos:]
    assert "count(" not in after_delete, (
        "DETACH DELETE 뒤에 count() 호출 = deleted variable 참조 = Cypher 에러 위험"
    )
    # 검증 3: RETURN 은 미리 캡처된 deleted_count 만 사용
    assert "RETURN deleted_count" in cypher


# ─── delete_meeting (full + branches) ───────────────────────────


_CPS_REBUILT = "## 📄 CPS 명세서 (재구성)\n### 1. Context\n- x"
_PRD_REBUILT = "## 🗺️ Master PRD 조감도 (재구성)\n### 1. Product Overview\n- x"


def _both_rebuild_responder():
    """CPS + PRD 둘 다 rebuild 호출되는 시나리오의 LLM 응답."""

    def respond(prompt: str) -> str:
        if "CPS 명세서를 처음부터 재구성" in prompt:
            return _CPS_REBUILT
        if "PRD 조감도를 처음부터 재구성" in prompt:
            return _PRD_REBUILT
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    return respond


@pytest.mark.asyncio
async def test_delete_meeting_full_rebuild_both():
    """남은 CPS + PRD delta 가 모두 있을 때 → 두 master 모두 rebuild.

    [2026-05 트랜잭션 안전성 — 호출 순서 변경]
    이전: delete → fetch → rebuild → save.
    이후: fetch (시뮬레이션) → rebuild → atomic(delete + save).
    """
    gemini = FakeGemini(_both_rebuild_responder())
    neo = FakeNeo4j(
        responses=[
            # Step 1: Get Remaining Deltas (시뮬레이션, 삭제 전)
            [
                {
                    "cps_list": [{"version": "v1.0", "content": "cps body"}],
                    "prd_list": [{"version": "v1.0", "content": "prd body"}],
                }
            ],
            # Step 4 atomic 트랜잭션 fallback (FakeNeo4j 는 run_in_transaction 미구현)
            # → 순차 run_cypher 로 변환됨: delete, save_cps, save_prd
            [{"deleted_phases": 5}],
            [{"saved_id": "doc_cps_master_harness"}],
            [{"saved_id": "doc_prd_master_harness"}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="dm1")
    result = await run_delete_meeting_pipeline(
        ctx, DeleteMeetingInput(project_name="harness", version="v1.1")
    )
    assert result.status == "success"
    assert result.cps_master_rebuilt is True
    assert result.prd_master_rebuilt is True
    assert result.remaining_cps_count == 1
    assert result.remaining_prd_count == 1
    # Gemini 2회 호출 (CPS + PRD)
    assert len(gemini.calls) == 2


@pytest.mark.asyncio
async def test_delete_meeting_no_remaining_skips_llm():
    """남은 delta 없으면 LLM 호출 0회 + no-rebuild 메시지."""
    gemini = FakeGemini(lambda p: "should not be called")
    neo = FakeNeo4j(
        responses=[
            # fetch (시뮬레이션) → 빈 결과 → has_any=False → delete 만 실행
            [{"cps_list": [], "prd_list": []}],
            [{"deleted_phases": 5}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="dm2")
    result = await run_delete_meeting_pipeline(
        ctx, DeleteMeetingInput(project_name="harness", version="v1.1")
    )
    assert result.cps_master_rebuilt is False
    assert result.prd_master_rebuilt is False
    assert "남은 Delta 없음" in result.message
    assert len(gemini.calls) == 0


@pytest.mark.asyncio
async def test_delete_meeting_cps_only_rebuild():
    """CPS delta 만 남고 PRD delta 0개 → CPS 만 rebuild."""

    def respond(prompt: str) -> str:
        if "CPS 명세서를 처음부터 재구성" in prompt:
            return _CPS_REBUILT
        raise AssertionError("PRD agent should not be called")

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(
        responses=[
            # fetch (시뮬레이션) → CPS 만 있음
            [
                {
                    "cps_list": [{"version": "v1.0", "content": "cps body"}],
                    "prd_list": [],
                }
            ],
            # atomic fallback 순차: delete, save_cps
            [{"deleted_phases": 5}],
            [{"saved_id": "doc_cps_master_harness"}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="dm3")
    result = await run_delete_meeting_pipeline(
        ctx, DeleteMeetingInput(project_name="harness", version="v1.1")
    )
    assert result.cps_master_rebuilt is True
    assert result.prd_master_rebuilt is False
    assert len(gemini.calls) == 1


@pytest.mark.asyncio
async def test_delete_meeting_preserves_master_prd_when_prd_deltas_empty_content():
    """[R3] 남은 PRD delta '노드'는 있으나 본문(full_markdown)이 모두 비어 has_prd=False 이고,
    CPS 는 본문이 있어(has_cps=True) 재구성 분기로 진입하는 경우.

    버그(수정 전): 재구성 분기가 _DELETE_MEETING_CYPHER 로 master PRD(step5)까지 삭제하는데
    prd_content_to_save=None 이라 SAVE_PRD 를 안 함 → master PRD 영구 소실(CPS 가득/PRD 빈의
    delete 경로 변종). 수정 후: 재구성 분기는 delta 만 삭제하고 master 는 보존(SAVE 의 MERGE 가
    재구성 대상만 덮어씀) → 재구성 못 하는 master PRD 는 기존 누적본 그대로 보존."""

    def respond(prompt: str) -> str:
        if "CPS 명세서를 처음부터 재구성" in prompt:
            return _CPS_REBUILT
        raise AssertionError("PRD agent should not be called (has_prd=False)")

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(
        responses=[
            # fetch: CPS 본문 있음 / PRD 노드는 있으나 본문 비어(None·"") → 필터 후 has_prd=False
            [
                {
                    "cps_list": [{"version": "v1.0", "content": "cps body"}],
                    "prd_list": [
                        {"version": "v1.0", "content": None},
                        {"version": "v1.2", "content": ""},
                    ],
                }
            ],
            # 재구성 분기 atomic fallback 순차: delete(deltas-only), save_cps
            [{"deleted_phases": 3}],
            [{"saved_id": "doc_cps_master_harness"}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="dm-preserve")
    result = await run_delete_meeting_pipeline(
        ctx, DeleteMeetingInput(project_name="harness", version="v1.1")
    )

    assert result.cps_master_rebuilt is True
    assert result.prd_master_rebuilt is False
    assert len(gemini.calls) == 1  # CPS 만 재구성

    # 핵심: 삭제 cypher 가 master 를 지우지 않아야 함(보존). SAVE 의 MERGE 가 재구성 대상만 덮어씀.
    delete_cypher = neo.executed[1]["cypher"]
    assert "$prd_master_id" not in delete_cypher, "master PRD 를 삭제하면 재구성 안 돼 영구 소실"
    assert "$cps_master_id" not in delete_cypher, "재구성 분기는 master 를 보존(SAVE MERGE 가 덮어씀)"
    # 재구성 안 하는 master PRD 는 SAVE 도 안 됨 → 기존 누적본 보존
    assert not any("MERGE (master:PRD_Document" in e["cypher"] for e in neo.executed)


@pytest.mark.asyncio
async def test_delete_meeting_agent_empty_output_raises_to_prevent_data_loss():
    """LLM 이 빈 응답을 주면 RuntimeError — DELETE 가 SAVE 없이 진행되면 Master 데이터
    영구 소실 발생. c75a062 (2026-05) 의 fix 이후, 빈 응답은 재시도 가능 오류로 처리.
    """

    def respond(prompt: str) -> str:
        return "   "  # whitespace only

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(
        responses=[
            # fetch (시뮬레이션) — RuntimeError 가 fetch 후 LLM 호출 시점에 raise.
            [
                {
                    "cps_list": [{"version": "v1.0", "content": "x"}],
                    "prd_list": [{"version": "v1.0", "content": "y"}],
                }
            ],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="dm4")
    with pytest.raises(RuntimeError, match="비어"):
        await run_delete_meeting_pipeline(
            ctx, DeleteMeetingInput(project_name="harness", version="v1.1")
        )
    # CPS rebuild 호출만 한 후 빈 응답 → 즉시 raise, PRD 호출 / delete 발생 안 함.
    assert len(gemini.calls) == 1
    assert len(neo.executed) == 1  # fetch 만


@pytest.mark.asyncio
async def test_delete_meeting_empty_input_raises():
    ctx = PipelineContext(
        gemini=FakeGemini(lambda p: "x"), neo4j=FakeNeo4j(), idempotency_key="dm5"
    )
    with pytest.raises(ValueError):
        await run_delete_meeting_pipeline(
            ctx, DeleteMeetingInput(project_name="", version="v1.0")
        )
    with pytest.raises(ValueError):
        await run_delete_meeting_pipeline(
            ctx, DeleteMeetingInput(project_name="x", version="")
        )


@pytest.mark.asyncio
async def test_delete_meeting_prd_rebuild_creates_based_on_link():
    """
    회귀 테스트 (PR12 fix): PRD master rebuild Cypher 에 CPS 와의 BASED_ON 관계가
    포함돼야 함. 'Save Rebuilt PRD Code' 단계와 동등.
    """

    def respond(prompt: str) -> str:
        if "PRD 조감도를 처음부터 재구성" in prompt:
            return "## 🗺️ Master PRD 조감도\n"
        # CPS 도 rebuild
        if "CPS 명세서를 처음부터 재구성" in prompt:
            return _CPS_REBUILT
        raise AssertionError("unexpected")

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(
        responses=[
            # fetch (시뮬레이션)
            [
                {
                    "cps_list": [{"version": "v1.0", "content": "cps"}],
                    "prd_list": [{"version": "v1.0", "content": "prd"}],
                }
            ],
            # atomic fallback 순차: delete, save_cps, save_prd
            [{"deleted_phases": 5}],
            [{"saved_id": "doc_cps_master_harness"}],
            [{"saved_id": "doc_prd_master_harness"}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="dm-prd-link")
    await run_delete_meeting_pipeline(
        ctx, DeleteMeetingInput(project_name="harness", version="v1.1")
    )
    # Save PRD Cypher (마지막 호출) 검증
    save_prd = neo.executed[-1]
    cypher = save_prd["cypher"]
    # MERGE master:PRD_Document
    assert "MERGE (master:PRD_Document" in cypher
    # BASED_ON 연결
    assert "OPTIONAL MATCH (cps_m:CPS_Document" in cypher
    assert "MERGE (master)-[:BASED_ON]->(cps_m)" in cypher
    # parameter binding
    assert save_prd["params"]["cps_master_id"] == "doc_cps_master_harness"
    assert save_prd["params"]["master_id"] == "doc_prd_master_harness"


@pytest.mark.asyncio
async def test_delete_meeting_cypher_uses_param_binding():
    """Delete Cypher 가 dangerous 입력을 보간하지 않고 $param 으로만 바인딩."""
    gemini = FakeGemini(lambda p: "no call")
    neo = FakeNeo4j(
        responses=[
            # fetch (시뮬레이션) — 빈 결과로 has_any=False → delete only
            [{"cps_list": [], "prd_list": []}],
            [{"deleted_phases": 0}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="dm6")
    dangerous_project = "x' OR true OR '"
    dangerous_version = "v' DETACH DELETE n //"
    await run_delete_meeting_pipeline(
        ctx,
        DeleteMeetingInput(project_name=dangerous_project, version=dangerous_version),
    )
    # Delete Cypher 검색 (fetch 다음에 실행됨)
    delete_call = next(
        c for c in neo.executed
        if "DETACH DELETE" in c["cypher"] and "Meeting_Log" in c["cypher"]
    )
    # Cypher 본문에 dangerous 가 보간되면 안 됨
    assert dangerous_project not in delete_call["cypher"]
    assert dangerous_version not in delete_call["cypher"]
    # 모든 id 가 params 로 전달됨
    params = delete_call["params"]
    assert params["log_id"].startswith("log_")
    assert dangerous_project.replace(".", "_") in params["log_id"]
