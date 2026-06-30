"""인터뷰 end-to-end 행동 eval (약점 #2).

기존 test_interview.py 는 단일 턴 단위 검증. 이 모듈은 **여러 턴을 누적하며 전체
루프**(준비도 게이트·grounding·갭·조기종료 차단)가 의도대로 동작하는지 시나리오로
측정·고정한다.

[정직한 한계] FakeGemini 로 모델 응답을 스크립트한다 — 즉 '시스템 로직'을 측정하지
'LLM 출력 품질'을 측정하진 않는다. 라이브 LLM 품질 eval 은 키·judge 가 필요한 별개
작업. 이 스위트는 통합 회귀를 잠그고, 게이트/궤적이 시나리오대로 흐르는지 보장한다.
"""
from __future__ import annotations

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.interview import InterviewMessage, run_interview_turn
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio

_SYNTH = "# 프로젝트 개요\n합성된 회의록"


def _ctx(gemini) -> PipelineContext:
    return PipelineContext(gemini=gemini, neo4j=FakeNeo4j(responses=[]), idempotency_key="eval")


def _scores(**dims) -> str:
    base = {"goal": 0.0, "features": 0.0, "data": 0.0, "users": 0.0, "constraints": 0.0, "usage": 0.0}
    base.update(dims)
    return "|".join(f"{k}={v}" for k, v in base.items())


def _ask(scores: str, msg: str = "다음 질문?") -> str:
    return f"PHASE: ask\nMESSAGE: {msg}\nSUGGESTIONS:\nCOVERAGE:\nSCORES: {scores}"


def _done(scores: str, msg: str = "정리할게요!") -> str:
    return f"PHASE: done\nMESSAGE: {msg}\nSUGGESTIONS:\nCOVERAGE:\nSCORES: {scores}"


async def _simulate(model_turns, *, graph_readiness: float = 1.0, gap_questions=None):
    """여러 턴 시뮬레이션. 각 턴마다 사용자 발화를 누적하고 model_turns[i] 를 모델
    응답으로 사용. done 이면 즉시 (turn, trajectory) 반환. trajectory=[(phase, readiness)]."""
    history: list = []
    trajectory: list = []
    turn = None
    for i, resp in enumerate(model_turns):
        history.append(InterviewMessage(role="user", content=f"사용자 답변 {i}"))
        gemini = FakeGemini(responses=[resp, _SYNTH])  # done 이면 2번째(합성)도 소비
        turn = await run_interview_turn(
            _ctx(gemini), history, gap_questions=gap_questions, graph_readiness=graph_readiness,
        )
        trajectory.append((turn.phase, turn.readiness))
        history.append(InterviewMessage(role="assistant", content=turn.assistant_message))
        if turn.phase == "done":
            break
    return turn, trajectory


# ─── 시나리오 ──────────────────────────────────────────────────────────────

async def test_eval_premature_done_blocked_before_min_turns():
    """1턴에 모델이 done+만점을 줘도 최소 턴(3) 전이면 마무리 안 함."""
    turn, traj = await _simulate([_done(_scores(goal=1, features=1, data=1, users=1, constraints=1, usage=1))])
    assert turn.phase == "ask"          # 조기 종료 차단
    assert traj == [("ask", turn.readiness)]


async def test_eval_vague_conversation_never_finalizes_early():
    """모호한 답변(낮은 점수)으로 4턴 — 준비도 미달이라 계속 질문."""
    turn, traj = await _simulate([_ask(_scores(goal=0.3)) for _ in range(4)])
    assert turn.phase == "ask"
    assert all(p == "ask" for p, _ in traj)
    assert all(r < 0.8 for _, r in traj)   # 준비도 임계 미달 유지


async def test_eval_complete_conversation_finalizes_with_content():
    """점수가 오르고 3턴째 만점 done → 마무리 + 회의록 합성."""
    full = _scores(goal=1, features=1, data=1, users=1, constraints=1, usage=1)
    turn, traj = await _simulate([_ask(_scores(goal=0.5)), _ask(_scores(goal=1, features=0.5)), _done(full)])
    assert turn.phase == "done"
    assert turn.readiness == 1.0
    assert "프로젝트 개요" in turn.meeting_content     # 합성됨
    assert len(traj) == 3                              # 3턴 만에 마무리


async def test_eval_auto_stops_when_ready_even_if_model_asks():
    """모델이 계속 ask 여도 3턴째 만점이면 auto-stop done."""
    full = _scores(goal=1, features=1, data=1, users=1, constraints=1, usage=1)
    turn, traj = await _simulate([_ask(_scores(goal=0.4)), _ask(_scores(goal=0.8)), _ask(full)])
    assert turn.phase == "done"           # auto-stop
    assert len(traj) == 3


async def test_eval_brownfield_grounding_is_more_conservative():
    """동일 스크립트라도 그래프 미완성(완성도 낮음)이면 더 늦게/덜 마무리.

    3턴째 만점 done: greenfield(1.0) → done, 미완성(0.0) → effective 0.6 <0.8 → ask 유지.
    """
    full = _scores(goal=1, features=1, data=1, users=1, constraints=1, usage=1)
    script = [_ask(_scores(goal=0.5)), _ask(_scores(goal=1)), _done(full)]

    green, _ = await _simulate(script, graph_readiness=1.0)
    assert green.phase == "done" and green.readiness == 1.0

    brown, _ = await _simulate(script, graph_readiness=0.0)
    assert brown.phase == "ask"           # grounding 감쇠로 조기 done 차단
    assert brown.readiness == 0.6         # 1.0 * (0.6 + 0.4*0.0)


async def test_eval_readiness_monotonic_with_more_info():
    """정보가 쌓일수록 준비도(grounding 후)가 단조 증가(같거나 큼)."""
    turns = [
        _ask(_scores(goal=0.3)),
        _ask(_scores(goal=0.6, features=0.4)),
        _ask(_scores(goal=0.8, features=0.7, data=0.5)),
    ]
    _, traj = await _simulate(turns)
    readinesses = [r for _, r in traj]
    assert readinesses == sorted(readinesses)      # 단조 증가
    assert readinesses[0] < readinesses[-1]
