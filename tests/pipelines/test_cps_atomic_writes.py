"""
CPS 파이프라인의 Stage 3 (Save CPS + Save Meeting Log) 병렬 실행 회귀.

[설계 변경 — 2026-05-26 perf B]
이전: 두 cypher 가 단일 run_in_transaction 으로 묶임 (atomic).
이후: save_log 가 CPS Agent LLM 호출과 asyncio.gather 로 병렬화.
      save_cps 는 CPS Agent 결과를 기반으로 build → run_cypher.
      → 두 write 는 서로 다른 트랜잭션. atomic 보장 의도적 포기.

[근거]
- save_log 는 raw meeting_content 만 저장 (LLM 결과 무관) — 병렬 가능.
- CPS Agent 가 ~5-10s LLM 호출이라 save_log (~50-100ms) 가 그 동안 끝남.
- 실패 시 orphan meeting_log 가 남을 수 있으나 (a) MERGE 라 idempotent,
  (b) 재시도 시 save_cps 자연 복구, (c) FE 상 cosmetic.

[가드]
- save_cps 와 save_log 모두 실행되는지 확인
- run_in_transaction 으로 묶지 않음 — 별개 트랜잭션
- run_in_transaction 미구현 fake 로도 동작
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.cps_pipeline import CpsInput, run_cps_pipeline


pytestmark = pytest.mark.asyncio


class _FakeNeo4jWithTransaction:
    """run_in_transaction 지원 — 호출 인자 기록."""

    def __init__(self) -> None:
        self.run_cypher_calls: List[Tuple[str, Dict[str, Any]]] = []
        self.tx_calls: List[List[Tuple[str, Dict[str, Any]]]] = []
        self._return_for_get_all_cps = [{
            "master_id": None, "master_content": "",
            "master_probs": [], "latest_id": None,
            "latest_content": "", "latest_probs": [],
            "project_name": "p",
        }]

    async def run_cypher(self, cypher: str, params: Optional[Dict[str, Any]] = None):
        self.run_cypher_calls.append((cypher, params or {}))
        # _GET_ALL_CPS_QUERY 만 응답 반환 — 다른 read 는 빈 결과
        if "OPTIONAL MATCH (m:CPS_Document" in cypher:
            return list(self._return_for_get_all_cps)
        return []

    async def run_in_transaction(
        self, operations: List[Tuple[str, Dict[str, Any]]]
    ):
        self.tx_calls.append(list(operations))
        return [[] for _ in operations]


class _FakeNeo4jWithoutTransaction:
    """옛 어댑터 — run_in_transaction 미구현. 무관 동작 검증용."""

    def __init__(self) -> None:
        self.run_cypher_calls: List[Tuple[str, Dict[str, Any]]] = []
        # run_in_transaction 속성 자체 없음

    async def run_cypher(self, cypher: str, params: Optional[Dict[str, Any]] = None):
        self.run_cypher_calls.append((cypher, params or {}))
        if "OPTIONAL MATCH (m:CPS_Document" in cypher:
            return [{
                "master_id": None, "master_content": "",
                "master_probs": [], "latest_id": None,
                "latest_content": "", "latest_probs": [],
                "project_name": "p",
            }]
        return []


class _FakeGemini:
    """JSON 응답 — 빈 graph 라도 파싱 가능하게."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, prompt: str, *, temperature: float = 0.2):
        self.calls += 1
        from app.clients.gemini_client import GeminiResult, TokenUsage
        # call_cps_agent: nodes 필드 요구. impact: affected_sections. merge: markdown.
        if "STRICT_JSON_ONLY" in prompt and "_harness_metadata" in prompt:
            # CPS Agent — 최소 1 node 로 Save CPS query 가 비지 않게.
            text = (
                '{"_harness_metadata":{},'
                '"nodes":[{"id":"p1","label":"Problem","properties":{"summary":"x"}}],'
                '"relationships":[]}'
            )
        elif "affected_sections" in prompt:
            text = '{"affected_sections":[],"removed_prb_ids":[],"removed_res_ids":[],"analysis":""}'
        else:
            text = "# merged content"
        return GeminiResult(
            text=text, model="fake", finish_reason="stop",
            usage=TokenUsage(),
        )


def _payload() -> CpsInput:
    return CpsInput(
        project_name="p", version="v1", date="2026-01-01",
        meeting_content="hello",
    )


# ─── perf B 병렬 실행 검증 ────────────────────────────────


async def test_save_cps_and_log_both_executed_via_run_cypher():
    """[perf B] save_cps + save_log 둘 다 run_cypher 로 호출.
    더 이상 run_in_transaction 으로 묶지 않음 — 병렬 실행 위한 의도적 분리."""
    neo = _FakeNeo4jWithTransaction()
    ctx = PipelineContext(gemini=_FakeGemini(), neo4j=neo, idempotency_key="t1")
    await run_cps_pipeline(ctx, _payload())

    # save_log + save_cps + fetch + merge 등 모두 run_cypher 경로.
    cyphers = [c for c, _ in neo.run_cypher_calls]
    assert any("Meeting_Log" in c for c in cyphers), \
        f"Save Meeting Log cypher 호출 안 됨: {cyphers}"
    assert any("MERGE (n" in c for c in cyphers), \
        f"Save CPS cypher 호출 안 됨: {cyphers}"
    # run_in_transaction 으로 두 write 를 묶는 호출은 없어야 함 (병렬화 의도)
    assert neo.tx_calls == [], (
        f"save_cps + save_log 를 run_in_transaction 으로 묶음 — perf B 회귀 "
        f"(병렬 실행되어야 함): {[len(c) for c in neo.tx_calls]}"
    )


async def test_works_without_run_in_transaction():
    """ctx.neo4j 가 run_in_transaction 미구현이어도 정상 동작.
    [perf B] 더 이상 run_in_transaction 의존 안 함."""
    neo = _FakeNeo4jWithoutTransaction()
    ctx = PipelineContext(gemini=_FakeGemini(), neo4j=neo, idempotency_key="t3")
    # 예외 없이 완료
    await run_cps_pipeline(ctx, _payload())
    # Save Meeting Log + Save CPS 가 run_cypher 로 호출됐어야 함
    cyphers = [c for c, _ in neo.run_cypher_calls]
    assert any("Meeting_Log" in c for c in cyphers)
    assert any("MERGE (n" in c for c in cyphers)
