"""run_prd_autofix — PRD lint finding AI 자동 보완 (하이브리드) 단위 테스트."""
from __future__ import annotations

import json

import pytest

from app.pipelines import prd_autofix_pipeline as mod
from app.pipelines.prd_autofix_pipeline import run_prd_autofix
from tests.conftest import FakeGemini

pytestmark = pytest.mark.asyncio


# Story 가 없어 PRD_NO_STORY(error) 가 잡히는 PRD (500바이트 이상).
_PRD_NO_STORY = """\
### 1. Product Overview
Product Vision: 사용자가 회의록을 PRD 로 변환한다. Success Metrics: 전환율.
Role A: 기획자, Role B: 개발자. 제품 개요 설명이 충분히 길게 들어갑니다.

### 2. Epic & User Story Map
(아직 작성되지 않음)

### 3. Screen Architecture
[Screen: 대시보드] 포함된 기능: [Story 1.1] 작업 요청 및 실행 (from Epic 1)

### 4. NFR
응답시간 500ms 이하, 가용성 99.9%, OAuth 로그인. 401 권한 없음 처리.
추가 설명을 덧붙여 최소 길이를 넉넉히 넘기도록 합니다. 충분히 길게.
"""


def _responder(improved: str, needs_input=None):
    payload = {"improved_prd": improved, "needs_input": needs_input or []}
    body = json.dumps(payload, ensure_ascii=False)

    def respond(prompt: str) -> str:
        return body

    return respond


@pytest.fixture(autouse=True)
def _no_cps(monkeypatch):
    # CPS·회의록 조회는 best-effort — 테스트에서 Neo4j 접근 회피.
    async def _none(_project):
        return None
    async def _empty_meeting(_project):
        return ""
    monkeypatch.setattr(mod.q, "get_master_cps", _none)
    monkeypatch.setattr(mod.q, "get_all_meeting_content", _empty_meeting)


async def test_autofix_fills_stories_and_passes_needs_input():
    improved = _PRD_NO_STORY.replace(
        "(아직 작성되지 않음)",
        "**[Story 1.1] 사용자가 작업을 요청한다**\n- 입력: 작업명\n- 출력: 작업 ID\n- 제약: 작업명 필수",
    )
    gemini = FakeGemini(_responder(
        improved,
        needs_input=[{"topic": "인증 방식", "question": "로그인은 어떤 방식인가요?"}],
    ))

    class _Ctx:
        def __init__(self, g): self.gemini = g

    result = await run_prd_autofix(_Ctx(gemini), "p", current_markdown=_PRD_NO_STORY)

    assert result is not None
    assert result.changed is True
    assert "[Story 1.1]" in result.improved_markdown
    # 보완 후 점수가 떨어지지 않아야 한다 (Story 추가로 보통 개선).
    assert result.after_score >= result.before_score
    # 근거 없는 항목은 지어내지 않고 인터뷰 질문으로 넘어온다.
    assert result.needs_input == [{"topic": "인증 방식", "question": "로그인은 어떤 방식인가요?"}]
    # LLM 1회 호출.
    assert len(gemini.calls) == 1


async def test_autofix_empty_llm_output_keeps_original():
    gemini = FakeGemini(_responder(""))  # improved_prd 비어있음

    class _Ctx:
        def __init__(self, g): self.gemini = g

    result = await run_prd_autofix(_Ctx(gemini), "p", current_markdown=_PRD_NO_STORY)

    assert result is not None
    # 원본 보존 — 데이터 손실 없음.
    assert result.improved_markdown == _PRD_NO_STORY
    assert result.changed is False


_P1 = _PRD_NO_STORY.replace(
    "(아직 작성되지 않음)",
    "**[Story 1.1] 사용자가 작업을 요청한다**\n- 입력: 작업명\n- 출력: 작업 ID",
)  # Story 추가했지만 검증(제약) 없음 → 점수↑(0.96), STORY_NO_VALIDATION 잔존
_P2 = _PRD_NO_STORY.replace(
    "(아직 작성되지 않음)",
    "**[Story 1.1] 사용자가 작업을 요청한다**\n- 입력: 작업명\n- 출력: 작업 ID\n- 제약: 작업명 필수, 길이 4~20자",
)  # 검증까지 채움 → 1.0


def _ctx_with(responses):
    class _Ctx:
        def __init__(self, g): self.gemini = g
    return _Ctx(FakeGemini(responses=responses))


async def test_autofix_refines_remaining_findings():
    # score-gated 재정제: 1패스가 일부만 고치면(점수↑·이슈 잔존) 2패스로 마저 정리.
    ctx = _ctx_with([
        json.dumps({"improved_prd": _P1, "needs_input": []}, ensure_ascii=False),
        json.dumps({"improved_prd": _P2, "needs_input": []}, ensure_ascii=False),
    ])
    result = await run_prd_autofix(ctx, "p", current_markdown=_PRD_NO_STORY)
    assert len(ctx.gemini.calls) == 2                # 재정제 1회 추가됨
    assert "제약:" in result.improved_markdown        # 2패스가 마저 채운 검증 규칙
    assert result.after_score == 1.0                 # 잔여 finding 제거
    assert result.after_score > result.before_score


async def test_autofix_refine_rejects_non_improving_pass():
    # 2패스가 lint 점수를 못 올리면 채택 안 하고 1패스 결과 유지(회귀 차단).
    p2_nogain = _PRD_NO_STORY.replace(
        "(아직 작성되지 않음)",
        "**[Story 1.1] 사용자가 작업을 요청한다**\n- 입력: 작업명\n- 출력: 작업 ID 그리고 부가 설명",
    )  # 여전히 검증 없음 → 점수 그대로(~0.96), score gain 없음
    ctx = _ctx_with([
        json.dumps({"improved_prd": _P1, "needs_input": []}, ensure_ascii=False),
        json.dumps({"improved_prd": p2_nogain, "needs_input": []}, ensure_ascii=False),
    ])
    result = await run_prd_autofix(ctx, "p", current_markdown=_PRD_NO_STORY)
    assert len(ctx.gemini.calls) == 2                # 2패스 시도는 함
    assert "부가 설명" not in result.improved_markdown  # 점수 못 올려 거부
    assert result.improved_markdown == _P1.strip()    # 1패스 결과 유지


async def test_autofix_no_target_returns_none(monkeypatch):
    async def _no_prd(_project):
        return None
    monkeypatch.setattr(mod.q, "get_master_prd", _no_prd)
    gemini = FakeGemini(_responder("x"))

    class _Ctx:
        def __init__(self, g): self.gemini = g

    result = await run_prd_autofix(_Ctx(gemini), "p", current_markdown=None)
    assert result is None
    # 대상 없음 — LLM 호출 0회.
    assert len(gemini.calls) == 0
