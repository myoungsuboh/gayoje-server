"""
delete_meeting_pipeline 의 atomic 트랜잭션 회귀 가드.

[배경 — 2026-05]
이전 흐름의 위험: delete commit → LLM rebuild → save master. LLM 또는 save 가
실패하면 delta 삭제는 이미 commit 됐는데 master 미갱신 → inconsistent state
영구 잔존.

이후 흐름:
  1. fetch 시뮬레이션 (삭제 *전*, payload.version 제외)
  2. LLM rebuild (트랜잭션 시작 전)
  3. atomic [delete + save_cps + save_prd] 단일 트랜잭션

[검증]
- LLM 호출 실패 시 delete 가 일어나지 않음
- run_in_transaction 이 모든 write (delete + save) 를 한 번에 받음
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.delete_pipeline import (
    DeleteMeetingInput,
    run_delete_meeting_pipeline,
)


pytestmark = pytest.mark.asyncio


class _FakeNeo4jTxAware:
    """run_in_transaction 지원 — 트랜잭션 호출 인자 기록."""

    def __init__(self, fetch_response: Dict[str, Any]) -> None:
        self.fetch_response = fetch_response
        self.run_cypher_calls: List[Tuple[str, Dict[str, Any]]] = []
        self.tx_calls: List[List[Tuple[str, Dict[str, Any]]]] = []

    async def run_cypher(self, cypher: str, params: Dict[str, Any] | None = None):
        self.run_cypher_calls.append((cypher, params or {}))
        # fetch_remaining cypher 인지 식별 — CPS_Document + WHERE excluded
        if "excluded_cps_id" in cypher or "cps_list" in cypher:
            return [self.fetch_response]
        return []

    async def run_in_transaction(
        self, operations: List[Tuple[str, Dict[str, Any]]]
    ):
        self.tx_calls.append(list(operations))
        return [[] for _ in operations]


class _FakeGeminiFail:
    """LLM 호출 시 RuntimeError raise — rebuild 실패 시뮬레이션."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, prompt: str, *, temperature: float = 0.2):
        self.calls += 1
        raise RuntimeError("LLM down")


class _FakeGeminiOk:
    """rebuild 두 번 다 정상 응답."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, prompt: str, *, temperature: float = 0.2):
        self.calls += 1
        from app.clients.gemini_client import GeminiResult, TokenUsage
        return GeminiResult(
            text="## 재구성 완료\n",
            model="fake", finish_reason="stop",
            usage=TokenUsage(),
        )


def _payload() -> DeleteMeetingInput:
    return DeleteMeetingInput(project_name="harness", version="v1.0")


# ─── LLM 실패 시 delete 가 실행되지 않음 ──────────────────


async def test_llm_failure_prevents_delete():
    """[핵심 회귀 가드] LLM rebuild 가 raise 하면 delete 자체가 호출되지 않음."""
    neo = _FakeNeo4jTxAware(fetch_response={
        "cps_list": [{"version": "v0.9", "content": "x"}],
        "prd_list": [],
    })
    gemini = _FakeGeminiFail()
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="t1")

    with pytest.raises(RuntimeError, match="LLM down"):
        await run_delete_meeting_pipeline(ctx, _payload())

    # fetch 만 호출됨. delete 는 트랜잭션도 run_cypher 도 안 거침.
    assert len(neo.tx_calls) == 0, "LLM 실패 후에도 트랜잭션이 발사됨"
    delete_called = any(
        "DETACH DELETE" in c and "Meeting_Log" in c
        for c, _ in neo.run_cypher_calls
    )
    assert not delete_called, (
        "LLM 실패 시 delete cypher 가 호출됨 — inconsistent state 발생 위험!"
    )


# ─── 정상 흐름 — delete + save 가 단일 트랜잭션으로 묶임 ──


async def test_delete_and_save_in_single_transaction():
    """정상 흐름에서 delete + save_cps 가 run_in_transaction 한 번에 들어감."""
    neo = _FakeNeo4jTxAware(fetch_response={
        "cps_list": [{"version": "v0.9", "content": "cps body"}],
        "prd_list": [],
    })
    gemini = _FakeGeminiOk()
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="t2")

    result = await run_delete_meeting_pipeline(ctx, _payload())

    assert result.status == "success"
    # 정확히 1번의 run_in_transaction 호출
    assert len(neo.tx_calls) == 1, (
        f"트랜잭션 호출 수 비정상: {len(neo.tx_calls)}"
    )
    ops = neo.tx_calls[0]
    # 최소 delete + save_cps 2개 operation
    assert len(ops) >= 2, f"트랜잭션 안 operation 수 부족: {len(ops)}"
    # 첫 번째 = delete
    assert "DETACH DELETE" in ops[0][0] and "Meeting_Log" in ops[0][0]
    # 두 번째 이후 어딘가에 save_cps
    save_cps_in_tx = any(
        "CPS_Document" in c and "MERGE (master:CPS_Document" in c
        for c, _ in ops
    )
    assert save_cps_in_tx, "save_cps cypher 가 트랜잭션 안에 없음"


async def test_empty_remaining_runs_delete_alone():
    """남은 delta 가 0 이면 LLM 호출 없이 delete 만 단일 cypher 로 실행."""
    neo = _FakeNeo4jTxAware(fetch_response={"cps_list": [], "prd_list": []})
    gemini = _FakeGeminiFail()  # 호출되면 안 됨
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="t3")

    result = await run_delete_meeting_pipeline(ctx, _payload())

    assert result.cps_master_rebuilt is False
    assert result.prd_master_rebuilt is False
    assert gemini.calls == 0, "Empty 분기에서 LLM 호출됨"
    # delete 는 단일 run_cypher 로 (no rebuild → 트랜잭션 불필요)
    assert len(neo.tx_calls) == 0
    delete_called = any(
        "DETACH DELETE" in c and "Meeting_Log" in c
        for c, _ in neo.run_cypher_calls
    )
    assert delete_called, "Empty 분기에서 delete 가 실행 안 됨"


# ─── [데이터 손실 가드 — 2026-05-27] 손상 delta 에서 master 삭제 차단 ──
async def test_remaining_nodes_with_empty_content_blocks_delete():
    """남은 delta '노드'는 존재하는데 본문(full_markdown)이 비어 있으면(데이터 손상),
    삭제 시 master 까지 소실되므로 raise 로 차단 — master/delta 보존.

    이전 버그: content 필터로 has_any=False → '남은 미팅 없음' 오판 → master 삭제.
    """
    neo = _FakeNeo4jTxAware(fetch_response={
        "cps_list": [{"version": "v0.9", "content": ""}],     # 노드 존재, 본문 빔
        "prd_list": [{"version": "v0.9", "content": None}],   # 노드 존재, 본문 None
    })
    gemini = _FakeGeminiOk()  # rebuild 전에 차단되므로 호출되면 안 됨
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="t-corrupt")

    with pytest.raises(RuntimeError, match="본문|손상|비어"):
        await run_delete_meeting_pipeline(ctx, _payload())

    # delete 가 트랜잭션으로도 단독 run_cypher 로도 실행되면 안 됨 (master 보존)
    assert len(neo.tx_calls) == 0
    delete_called = any(
        "DETACH DELETE" in c and "Meeting_Log" in c
        for c, _ in neo.run_cypher_calls
    )
    assert not delete_called, "손상 delta 상태에서 delete 실행됨 — master 손실 위험!"
    assert gemini.calls == 0, "차단은 rebuild 전에 일어나야 함"
