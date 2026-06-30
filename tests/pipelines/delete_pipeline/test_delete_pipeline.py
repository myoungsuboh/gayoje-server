"""
delete_pipeline 테스트.

[2026-05 트랜잭션 안전성 반영 테스트]
이전 흐름: delete commit → fetch_remaining → LLM rebuild → save master.
  LLM 실패 시 delta 삭제는 이미 commit → inconsistent state 영구 잠존.
이후 흐름: 삭제 전 시점 fetch → LLM rebuild → delete + save atomic.
  LLM 실패 → delete 자체가 발생 안 함 → 사용자 재시도 가능.

테스트 시나리오:
  - delete_project: 2번 cypher (DETACH DELETE + Project 노드)
  - empty branch: 남은 delta 없음 → LLM 0회
  - full rebuild: CPS+PRD delta 있음 → LLM 2회 + atomic tx
  - partial rebuild: CPS만 → LLM 1회 + atomic tx
  - LLM 실패 시 delete 발생 안 함 (필수 회귀 로직)
"""
from __future__ import annotations

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

pytestmark = pytest.mark.asyncio

_LONG = "충분히 긴 CPS 내용. 50자 이상으로 작성하여 테스트 통과 조건을 체크한다."


# ─── 순수 함수 ──────────────────────────────────────────────────────


def test_derive_ids_normalizes_dots():
    ids = _derive_ids("food.delivery", "v1.5")
    assert ids["log_id"] == "log_food_delivery_v1_5"
    assert ids["cps_delta_id"] == "doc_cps_food_delivery_v1_5"
    assert ids["prd_delta_id"] == "doc_prd_food_delivery_v1_5"
    assert ids["cps_master_id"] == "doc_cps_master_food_delivery"
    assert ids["prd_master_id"] == "doc_prd_master_food_delivery"


def test_join_deltas_wraps_each_delta_with_markers():
    deltas = [
        {"version": "v1", "content": "CPS 내용 v1"},
        {"version": "v2", "content": "CPS 내용 v2"},
    ]
    out = _join_deltas(deltas, "CPS")
    assert out.count("CPS DELTA START") == 2
    assert "CPS 내용 v1" in out
    assert "CPS 내용 v2" in out


def test_prepare_rebuild_data_empty_lists():
    prep = _prepare_rebuild_data({"cps_list": [], "prd_list": []})
    assert prep["has_any"] is False
    assert prep["remaining_cps_count"] == 0
    assert prep["remaining_prd_count"] == 0


def test_prepare_rebuild_data_cps_only():
    prep = _prepare_rebuild_data({
        "cps_list": [{"id": "x", "version": "v1", "content": "c"}],
        "prd_list": [],
    })
    assert prep["has_any"] is True
    assert prep["has_cps"] is True
    assert prep["has_prd"] is False
    assert prep["remaining_cps_count"] == 1


# ─── delete_project ─────────────────────────────────────────────────


async def test_delete_project_two_cyphers_and_returns_count():
    """[2026-05-18 버그 수정] Project 노드 좌비 방지 — 2번째 cypher 검증."""
    neo4j = FakeNeo4j(responses=[
        [{"deleted_count": 42}],  # DETACH DELETE 도메인 노드
        [],                        # Project 노드 + OWNS 관계 제거
    ])
    ctx = PipelineContext(gemini=None, neo4j=neo4j, idempotency_key="dp")

    result = await delete_project(ctx, "food")

    assert result["status"] == "deleted"
    assert result["child_count"] == 42
    assert len(neo4j.executed) == 2
    assert "DETACH DELETE" in neo4j.executed[0]["cypher"]
    assert neo4j.executed[0]["params"] == {"project": "food"}
    # 두 번째: Project 노드 제거
    assert ":Project" in neo4j.executed[1]["cypher"]


async def test_delete_project_empty_name_raises():
    neo4j = FakeNeo4j()
    ctx = PipelineContext(gemini=None, neo4j=neo4j, idempotency_key="dp2")
    with pytest.raises(ValueError):
        await delete_project(ctx, "")


# ─── run_delete_meeting_pipeline — empty 분기 ────────────────────────────


async def test_delete_meeting_no_remaining_delta_skips_llm():
    def _no_llm(_):
        raise AssertionError("남은 delta 없으면 LLM 호출 없어야 함")

    neo4j = FakeNeo4j(responses=[
        [],   # _fetch_remaining → records 비어 → cps_list=[], prd_list=[]
        [],   # _DELETE_MEETING_CYPHER
    ])
    ctx = PipelineContext(gemini=FakeGemini(_no_llm), neo4j=neo4j, idempotency_key="dm-empty")

    result = await run_delete_meeting_pipeline(
        ctx, DeleteMeetingInput(project_name="food", version="v1")
    )

    assert result.status == "success"
    assert result.cps_master_rebuilt is False
    assert result.prd_master_rebuilt is False
    assert result.remaining_cps_count == 0
    # fetch + delete = 2
    assert len(neo4j.executed) == 2


# ─── run_delete_meeting_pipeline — full rebuild ─────────────────────────


async def test_delete_meeting_rebuilds_cps_and_prd_atomically():
    """CPS+PRD delta 있으면 LLM 2회 + delete+save_cps+save_prd atomic tx."""
    gemini = FakeGemini(responses=["# 재구성 CPS", "# 재구성 PRD"])
    neo4j = FakeNeo4j(responses=[
        [{"cps_list": [{"id": "doc_cps_food_v1", "version": "v1", "content": _LONG}],
          "prd_list": [{"id": "doc_prd_food_v1", "version": "v1", "content": _LONG}]}],
        [],   # delete (tx op 0)
        [],   # save_cps (tx op 1)
        [],   # save_prd (tx op 2)
    ])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="dm-full")

    result = await run_delete_meeting_pipeline(
        ctx, DeleteMeetingInput(project_name="food", version="v2")
    )

    assert result.status == "success"
    assert result.cps_master_rebuilt is True
    assert result.prd_master_rebuilt is True
    assert result.remaining_cps_count == 1
    assert result.remaining_prd_count == 1
    # LLM: rebuild_cps + rebuild_prd
    assert len(gemini.calls) == 2
    # neo4j: fetch + 3 atomic ops = 4
    assert len(neo4j.executed) == 4
    # atomic ops 순서: delete → save_cps → save_prd
    assert "OPTIONAL MATCH (log:Meeting_Log" in neo4j.executed[1]["cypher"]
    assert "last_rebuild_reason" in neo4j.executed[2]["cypher"]
    assert "last_rebuild_reason" in neo4j.executed[3]["cypher"]
    assert "BASED_ON" in neo4j.executed[3]["cypher"]  # save_prd 는 CPS 링크


async def test_delete_meeting_rebuilds_cps_only_when_no_prd_delta():
    gemini = FakeGemini(responses=["# CPS만"])
    neo4j = FakeNeo4j(responses=[
        [{"cps_list": [{"id": "doc_cps_food_v1", "version": "v1", "content": _LONG}],
          "prd_list": []}],
        [],   # delete
        [],   # save_cps
    ])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="dm-cps-only")

    result = await run_delete_meeting_pipeline(
        ctx, DeleteMeetingInput(project_name="food", version="v2")
    )

    assert result.cps_master_rebuilt is True
    assert result.prd_master_rebuilt is False
    assert len(gemini.calls) == 1
    assert len(neo4j.executed) == 3  # fetch + delete + save_cps


async def test_delete_meeting_llm_failure_prevents_delete():
    """퍵슬 안전성 핸심 테스트: LLM 실패 → delete cypher 실행 안 됨."""
    def _explode(_):
        raise RuntimeError("용량 초과!")

    neo4j = FakeNeo4j(responses=[
        [{"cps_list": [{"id": "doc_cps_food_v1", "version": "v1", "content": _LONG}],
          "prd_list": []}],
    ])
    ctx = PipelineContext(gemini=FakeGemini(_explode), neo4j=neo4j, idempotency_key="dm-fail")

    with pytest.raises(RuntimeError, match="용량 초과"):
        await run_delete_meeting_pipeline(
            ctx, DeleteMeetingInput(project_name="food", version="v1")
        )

    # fetch_remaining 한 번만 실행 — delete 는 발생 안 함
    assert len(neo4j.executed) == 1


async def test_delete_meeting_id_generation_dot_project():
    """dot 포함 project_name → log_id / cps_id 구분자 정상 정규화."""
    def _no_llm(_):
        raise AssertionError("LLM called")

    neo4j = FakeNeo4j(responses=[[], []])
    ctx = PipelineContext(gemini=FakeGemini(_no_llm), neo4j=neo4j, idempotency_key="dm-dot")

    result = await run_delete_meeting_pipeline(
        ctx, DeleteMeetingInput(project_name="foo.bar", version="v1")
    )

    # delete cypher params
    delete_params = neo4j.executed[1]["params"]
    assert delete_params["log_id"] == "log_foo_bar_v1"
    assert delete_params["cps_master_id"] == "doc_cps_master_foo_bar"
