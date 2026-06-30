"""interview 파이프라인 — 질문(ask) + 회의록 합성(synthesize) 분리 흐름 검증.

흐름(현재):
- ask 턴: 질문 프롬프트 1회 호출 → PHASE: ask + MESSAGE/SUGGESTIONS/COVERAGE
- done 턴: 질문 프롬프트가 PHASE: done → 별도 합성 프롬프트(구독 등급 모델) 1회 더
  호출해 meeting_content 생성 (질문 프롬프트엔 회의록 템플릿 없음 = 슬림)
- 턴 상한 도달 시 강제 done → 합성
- 합성 실패/빈 결과 → 기존 초안 보존 폴백
- 보완: 질문·합성 프롬프트 모두에 기존 초안 주입
- 스트리밍: token … → finalizing → done(meeting_content 포함)
"""
from __future__ import annotations

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.interview import (
    InterviewMessage,
    build_graph_summary,
    run_interview_turn,
    run_interview_turn_stream,
)
from app.pipelines.interview.interview import (
    _fallback_meeting_content,
    _parse_scores,
    _parse_turn,
    compute_readiness,
    weakest_dimension,
)
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio


def _ctx(gemini) -> PipelineContext:
    return PipelineContext(
        gemini=gemini, neo4j=FakeNeo4j(responses=[]), idempotency_key="iv-test"
    )


_FULL_SCORES = "goal=1.0|features=1.0|data=1.0|users=1.0|constraints=1.0|usage=1.0"  # readiness=1.0


def _ask(message, suggestions="", coverage="", scores=""):
    lines = [
        "PHASE: ask",
        f"MESSAGE: {message}",
        f"SUGGESTIONS: {suggestions}",
        f"COVERAGE: {coverage}",
    ]
    if scores:
        lines.append(f"SCORES: {scores}")
    return "\n".join(lines)


def _done(message="정리해 드릴게요!", coverage="", scores=_FULL_SCORES):
    return "\n".join([
        "PHASE: done",
        f"MESSAGE: {message}",
        "SUGGESTIONS:",
        f"COVERAGE: {coverage}",
        f"SCORES: {scores}",
    ])


def _users(n):
    """done 게이트의 최소 턴(_MIN_USER_TURNS=3) 충족용 — n개의 사용자 발화."""
    return [InterviewMessage(role="user", content=f"답변 {i}") for i in range(n)]


async def test_ask_phase_returns_next_question_and_suggestions():
    resp = _ask("누가 이 서비스를 주로 사용하나요?", suggestions="일반 사용자|관리자도 따로", coverage="정의")
    gemini = FakeGemini(responses=[resp])
    turn = await run_interview_turn(
        _ctx(gemini),
        [InterviewMessage(role="user", content="할 일 관리 앱 만들고 싶어요")],
    )
    assert turn.phase == "ask"
    assert "사용" in turn.assistant_message
    assert turn.suggestions == ["일반 사용자", "관리자도 따로"]
    assert turn.coverage == ["정의"]
    assert turn.meeting_content == ""
    assert len(gemini.calls) == 1  # ask 턴은 합성 호출 없음


async def test_done_triggers_separate_synthesis_call():
    """done 판정 → 합성 프롬프트로 한 번 더 호출해 meeting_content 채움."""
    synth_md = "# 프로젝트 개요\n할 일 관리 앱\n\n# 주요 기능\n1. 작업 생성"
    gemini = FakeGemini(responses=[_done("정리할게요!"), synth_md])
    turn = await run_interview_turn(
        _ctx(gemini),
        _users(3),  # 최소 턴 충족 (done 게이트)
    )
    assert turn.phase == "done"
    assert "프로젝트 개요" in turn.meeting_content
    assert "작업 생성" in turn.meeting_content
    assert len(gemini.calls) == 2  # 질문 결정 + 합성


async def test_synthesis_prompt_receives_history_and_existing():
    """합성 호출이 대화 + 기존 초안을 받는다 (보존·병합 근거)."""
    gemini = FakeGemini(responses=[_done(), "# 회의록\n내용"])
    await run_interview_turn(
        _ctx(gemini),
        _users(2) + [InterviewMessage(role="user", content="중고책 거래")],  # 3턴
        existing_content="# 프로젝트 개요\n동네 책방 앱",
    )
    synth_prompt = gemini.calls[1]["prompt"]
    assert "동네 책방 앱" in synth_prompt      # 기존 초안 주입
    assert "중고책 거래" in synth_prompt        # 대화 주입
    # [회의록 self-check] 합성 단일 패스 안에 출력 직전 자기점검 지시가 실린다
    # (추가 LLM 호출 없이 핵심 5개·명시 거부·초안 보존을 스스로 검수 → 누락 보강).
    assert "자기점검" in synth_prompt


async def test_fallback_used_when_synthesis_empty():
    """합성이 빈 결과면 기존 초안 보존 폴백."""
    existing = "# 프로젝트 개요\n예약 시스템\n# 주요 기능\n1. 시간 예약"
    gemini = FakeGemini(responses=[_done(), ""])  # 합성 빈 응답
    turn = await run_interview_turn(
        _ctx(gemini),
        _users(2) + [InterviewMessage(role="user", content="미용실 예약")],  # 3턴
        existing_content=existing,
    )
    assert turn.phase == "done"
    assert "예약 시스템" in turn.meeting_content   # 기존 초안 보존
    assert "미용실 예약" in turn.meeting_content    # 대화 보강분


async def test_force_finalize_after_turn_cap():
    """턴 상한 초과 + 모델이 계속 ask → 강제 done + 합성."""
    synth_md = "# 프로젝트 개요\n뭔가"
    history = [InterviewMessage(role="user", content=f"답변 {i}") for i in range(13)]
    gemini = FakeGemini(responses=[_ask("하나만 더요"), synth_md])
    turn = await run_interview_turn(_ctx(gemini), history)
    assert turn.phase == "done"
    assert "프로젝트 개요" in turn.meeting_content


async def test_existing_content_injected_into_question_prompt():
    """보완 — 질문 프롬프트에도 기존 초안 주입(이미 담긴 건 재질문 안 하도록)."""
    gemini = FakeGemini(responses=[_ask("결제는 어떻게 하나요?")])
    await run_interview_turn(
        _ctx(gemini), [], existing_content="# 프로젝트 개요\n중고거래 앱",
    )
    prompt = gemini.calls[0]["prompt"]
    assert "중고거래 앱" in prompt
    assert "기존 초안" in prompt


async def test_empty_existing_content_is_cold_start():
    gemini = FakeGemini(responses=[_ask("무엇을 만드세요?")])
    await run_interview_turn(_ctx(gemini), [], existing_content="")
    assert "없음" in gemini.calls[0]["prompt"]


async def test_question_prompt_has_score_self_check():
    # SCORES 매기기 전 '추측 vs 실제 답변' 자기점검 지시가 질문 프롬프트에 실린다
    # (같은 호출 안에서 자가점수 부풀림 억제 → 조기 종료 정확도↑, 추가 호출 0).
    gemini = FakeGemini(responses=[_ask("무엇을 만드세요?")])
    await run_interview_turn(_ctx(gemini), [])
    assert "자기점검" in gemini.calls[0]["prompt"]


def test_fallback_preserves_existing_draft_unit():
    """폴백 함수: 기존 초안을 통째로 보존하고 대화 보강분을 덧붙인다."""
    existing = "# 프로젝트 개요\n예약 시스템"
    history = [InterviewMessage(role="user", content="노쇼 방지")]
    out = _fallback_meeting_content(history, existing)
    assert existing in out
    assert "노쇼 방지" in out


async def test_stream_emits_token_finalizing_done_for_done_turn():
    """스트리밍 done 턴: token … → finalizing → done(meeting_content 포함)."""
    gemini = FakeGemini(responses=[_done("정리할게요!"), "# 회의록\n할 일 앱"])
    events = []
    async for evt_type, data in run_interview_turn_stream(
        _ctx(gemini), _users(2) + [InterviewMessage(role="user", content="할 일 앱")],  # 3턴
    ):
        events.append((evt_type, data))

    types = [t for t, _ in events]
    assert "finalizing" in types
    assert types[-1] == "done"
    done_turn = events[-1][1]
    assert done_turn.phase == "done"
    assert "회의록" in done_turn.meeting_content


async def test_stream_ask_turn_has_no_finalizing():
    """ask 턴은 finalizing 없이 바로 done(=턴 종료) 이벤트."""
    gemini = FakeGemini(responses=[_ask("어떤 앱?", suggestions="쇼핑몰|SNS")])
    events = []
    async for evt_type, data in run_interview_turn_stream(_ctx(gemini), []):
        events.append((evt_type, data))
    types = [t for t, _ in events]
    assert "finalizing" not in types
    assert types[-1] == "done"
    assert events[-1][1].phase == "ask"
    assert events[-1][1].suggestions == ["쇼핑몰", "SNS"]


# ─── T1: 준비도 점수 (정량 done 게이트의 토대) ────────────────────────────

def test_parse_scores_known_dims_clamped():
    s = _parse_scores("goal=0.8|features=0.4|users=1.5|junk=0.9|data=-0.2|bad=x")
    assert s == {"goal": 0.8, "features": 0.4, "users": 1.0, "data": 0.0}  # 미지/비수치 제외, 0~1 클램프


def test_parse_scores_empty():
    assert _parse_scores("") == {}
    assert _parse_scores("no-equals-here") == {}


def test_compute_readiness_weighted_sum():
    # 전 차원 1.0 → 가중치 합 = 1.0
    full = {k: 1.0 for k in ("goal", "features", "data", "users", "constraints", "usage")}
    assert compute_readiness(full) == 1.0
    assert compute_readiness({}) == 0.0
    # goal(0.25)만 1.0 → 0.25
    assert compute_readiness({"goal": 1.0}) == 0.25
    # goal=0.8(0.20) + features=0.5(0.125) = 0.325
    assert compute_readiness({"goal": 0.8, "features": 0.5}) == 0.325


def test_weakest_dimension_targets_lowest():
    assert weakest_dimension({}) == "goal"  # 점수 없으면 가중 최상위(goal=0.25, features 동률이나 첫 매칭)
    # data 가 최저 → data 반환
    s = {"goal": 0.9, "features": 0.8, "data": 0.1, "users": 0.7, "constraints": 0.5, "usage": 0.6}
    assert weakest_dimension(s) == "data"
    # 누락 차원(=0)이 가장 약함: constraints/usage 누락 → 가중 큰 constraints(0.10) 우선
    assert weakest_dimension({"goal": 0.9, "features": 0.9, "data": 0.9, "users": 0.9}) == "constraints"


def test_parse_turn_extracts_scores_and_readiness():
    text = (
        "PHASE: ask\n"
        "MESSAGE: 누가 쓰나요?\n"
        "SUGGESTIONS: 일반 사용자|관리자\n"
        "COVERAGE: 정의|사용자\n"
        "SCORES: goal=1.0|features=0.0|data=0.0|users=1.0|constraints=0.0|usage=0.0\n"
    )
    turn = _parse_turn(text)
    assert turn.scores == {"goal": 1.0, "features": 0.0, "data": 0.0, "users": 1.0, "constraints": 0.0, "usage": 0.0}
    assert turn.readiness == 0.4  # goal 0.25 + users 0.15


def test_parse_turn_without_scores_defaults_zero():
    turn = _parse_turn("PHASE: ask\nMESSAGE: 무엇을 만들고 싶으세요?\n")
    assert turn.scores == {}
    assert turn.readiness == 0.0


# ─── T2: 준비도 게이트 (done 을 점수로 결정) ──────────────────────────────

async def test_gate_blocks_premature_done_low_readiness():
    """모델이 done 줘도 준비도 부족이면 ask 로 되돌리고 최약 차원 질문(조기종료 방지)."""
    low = "goal=1.0|features=0.0|data=0.0|users=0.0|constraints=0.0|usage=0.0"  # readiness 0.25
    gemini = FakeGemini(responses=[_done("정리할게요!", scores=low)])
    turn = await run_interview_turn(_ctx(gemini), _users(3))
    assert turn.phase == "ask"                 # done 거부
    assert turn.meeting_content == ""          # 합성 안 함
    assert len(gemini.calls) == 1              # 합성 호출 없음
    assert turn.assistant_message             # 최약 차원 질문으로 교체됨


async def test_gate_blocks_done_before_min_turns():
    """준비도 충분해도 최소 턴(3) 전에는 done 불가."""
    gemini = FakeGemini(responses=[_done("정리할게요!")])  # readiness 1.0
    turn = await run_interview_turn(_ctx(gemini), _users(1))  # 1턴뿐
    assert turn.phase == "ask"
    assert len(gemini.calls) == 1


async def test_gate_auto_stops_when_ready():
    """모델이 ask 여도 준비도 충분 + 최소턴이면 auto-stop done + 합성."""
    gemini = FakeGemini(responses=[_ask("하나만 더요?", scores=_FULL_SCORES), "# 회의록\n앱"])
    turn = await run_interview_turn(_ctx(gemini), _users(3))
    assert turn.phase == "done"                # auto-stop
    assert "회의록" in turn.meeting_content     # 합성됨
    assert len(gemini.calls) == 2


async def test_gate_keeps_asking_when_not_ready():
    """준비도 미달 + 모델 ask → 그대로 ask (정상 진행)."""
    mid = "goal=1.0|features=1.0|data=0.0|users=0.0|constraints=0.0|usage=0.0"  # 0.5
    gemini = FakeGemini(responses=[_ask("어떤 데이터를 다루나요?", scores=mid)])
    turn = await run_interview_turn(_ctx(gemini), _users(3))
    assert turn.phase == "ask"
    assert turn.readiness == 0.5


# ─── T3: 최약 차원 타기팅 (next_focus) ────────────────────────────────────

async def test_next_focus_set_on_ask_turn():
    """ask 턴이면 next_focus = 가장 약한 차원."""
    mid = "goal=1.0|features=1.0|data=0.0|users=0.5|constraints=0.0|usage=1.0"
    gemini = FakeGemini(responses=[_ask("질문?", scores=mid)])
    turn = await run_interview_turn(_ctx(gemini), _users(3))
    assert turn.phase == "ask"
    # data/constraints 동률(0.0) → 가중 큰 data(0.20 > constraints 0.10) 우선.
    assert turn.next_focus == "data"


async def test_next_focus_none_on_done():
    """done 턴엔 next_focus 없음."""
    gemini = FakeGemini(responses=[_done(), "# 회의록\n앱"])
    turn = await run_interview_turn(_ctx(gemini), _users(3))
    assert turn.phase == "done"
    assert turn.next_focus is None


async def test_next_focus_on_downgraded_done():
    """모델 done 이 준비도 미달로 ask 강등될 때도 next_focus 설정."""
    low = "goal=1.0|features=0.0|data=0.0|users=0.0|constraints=0.0|usage=0.0"
    gemini = FakeGemini(responses=[_done("정리할게요", scores=low)])
    turn = await run_interview_turn(_ctx(gemini), _users(3))
    assert turn.phase == "ask"
    assert turn.next_focus is not None  # 약한 차원이 지정됨


# ─── T4: 그래프 갭 → 보강 질문 (Phase 2) ──────────────────────────────────

def _imp():
    from app.pipelines.interview.interview import graph_gaps_to_questions
    return graph_gaps_to_questions


def test_gaps_maps_user_intent_codes_to_questions():
    g = _imp()
    out = g([
        {"code": "API_MISSING_STORY_REF"},
        {"code": "AGGREGATE_INVARIANTS_MISSING"},
    ])
    assert len(out) == 2
    assert any("사용자 시나리오" in q for q in out)
    assert any("규칙" in q for q in out)


def test_gaps_ignores_internal_autofix_codes():
    g = _imp()
    # ID 재배정·tech stack 정규화·pascal 등은 사용자 질문 대상 아님 → 빈 목록
    out = g([
        {"code": "ARCH_SVC_ID_REASSIGNED"},
        {"code": "ARCH_TECH_STACK_NORMALIZED"},
        {"code": "ENTITY_NAME_NOT_PASCAL_CASE"},
        {"code": "ARCH_API_UNMAPPED"},  # 기술적 배치 — 비전공자 질문 아님
    ])
    assert out == []


def test_gaps_dedup_and_priority_order():
    g = _imp()
    # 같은 코드 다수 → 1개. 우선순위순(API_MISSING_STORY_REF 가 INVARIANTS 보다 앞).
    out = g([
        {"code": "AGGREGATE_INVARIANTS_MISSING"},
        {"code": "API_MISSING_STORY_REF"},
        {"code": "API_MISSING_STORY_REF"},
    ])
    assert len(out) == 2
    assert "사용자 시나리오" in out[0]  # story_ref 가 먼저


def test_gaps_cap_and_accepts_objects():
    g = _imp()
    from types import SimpleNamespace
    viols = [SimpleNamespace(code=c) for c in (
        "API_MISSING_STORY_REF", "DDD_MISSING_SPACK_ENTITY", "AGGREGATE_INVARIANTS_MISSING",
        "ENTITY_ATTRIBUTES_MISSING", "API_NOT_FOUND_CASE_MISSING", "API_AUTH_ERROR_CASE_MISSING",
    )]
    out = g(viols, cap=3)            # Violation 객체(duck-typed) 수용 + 캡
    assert len(out) == 3


def test_gaps_empty():
    g = _imp()
    assert g([]) == []
    assert g(None) == []


# ─── T5: 경량 그래프 갭 추출 + 인터뷰 주입 ────────────────────────────────

def _gnode(nid, label, **props):
    from types import SimpleNamespace
    return SimpleNamespace(id=nid, label=label, properties=props)


def _gedge(s, t, ty):
    from types import SimpleNamespace
    return SimpleNamespace(source_id=s, target_id=t, type=ty)


def _pg(nodes, edges):
    from types import SimpleNamespace
    return SimpleNamespace(nodes=nodes, edges=edges)


def test_extract_gap_codes_edge_based():
    from app.pipelines.interview.interview import extract_graph_gap_codes
    nodes = [_gnode("api1", "API"), _gnode("api2", "API"), _gnode("evt1", "DomainEvent")]
    edges = [_gedge("api1", "s1", "IMPLEMENTS")]  # api2 미구현, evt1 트리거 없음
    gaps = extract_graph_gap_codes(_pg(nodes, edges))
    codes = {g["code"] for g in gaps}
    assert "API_MISSING_STORY_REF" in codes and "DDD_EVENT_MISSING_STORY_REF" in codes
    # api1 은 IMPLEMENTS 있음 → 갭 아님 (api2 만)
    assert {g["item_id"] for g in gaps if g["code"] == "API_MISSING_STORY_REF"} == {"api2"}


def test_extract_gap_codes_property_conservative():
    from app.pipelines.interview.interview import extract_graph_gap_codes
    nodes = [
        _gnode("agg1", "Aggregate", invariants="[]"),         # 명시적 빈 → 갭
        _gnode("agg2", "Aggregate", invariants='["재고>=0"]'),  # 있음 → 갭 아님
        _gnode("agg3", "Aggregate"),                            # 누락(키 없음) → 보수적, 갭 아님
        _gnode("e1", "Entity", attributes="[]"),                # 갭
        _gnode("de1", "DomainEntity", attributes=[]),           # 갭(빈 list)
    ]
    codes = [(g["code"], g["item_id"]) for g in extract_graph_gap_codes(_pg(nodes, []))]
    assert ("AGGREGATE_INVARIANTS_MISSING", "agg1") in codes
    assert ("ENTITY_ATTRIBUTES_MISSING", "e1") in codes
    assert ("DOMAIN_ENTITY_ATTRIBUTES_MISSING", "de1") in codes
    items = {item for _, item in codes}
    assert "agg2" not in items and "agg3" not in items  # 있음/누락은 갭 아님


async def test_graph_gap_questions_integration(monkeypatch):
    import app.service.query_repository as _qr
    from app.pipelines.interview.interview import graph_gap_questions
    async def fake_graph(name, team_id=""):
        return _pg([_gnode("api2", "API")], [])  # IMPLEMENTS 없음
    monkeypatch.setattr(_qr, "get_project_graph", fake_graph)
    qs = await graph_gap_questions("proj")
    assert any("사용자 시나리오" in q for q in qs)


async def test_graph_gap_questions_failure_returns_empty(monkeypatch):
    import app.service.query_repository as _qr
    from app.pipelines.interview.interview import graph_gap_questions
    async def boom(name, team_id=""):
        raise RuntimeError("neo down")
    monkeypatch.setattr(_qr, "get_project_graph", boom)
    assert await graph_gap_questions("proj") == []  # 실패는 보강 생략(인터뷰 안 막음)


async def test_gap_questions_injected_into_prompt():
    gemini = FakeGemini(responses=[_ask("질문?")])
    await run_interview_turn(_ctx(gemini), [], gap_questions=["설계에 빠진 기능 X를 확인하세요"])
    assert "설계에 빠진 기능 X" in gemini.calls[0]["prompt"]  # {{GAPS}} 주입 확인


# ─── T6: 설계 그래프 완성도 (verification 신호) ───────────────────────────

def test_graph_readiness_fraction():
    from app.pipelines.interview.interview import graph_readiness
    # API 2개 중 1개만 IMPLEMENTS → 완성도 0.5
    nodes = [_gnode("api1", "API"), _gnode("api2", "API")]
    edges = [_gedge("api1", "s1", "IMPLEMENTS")]
    assert graph_readiness(_pg(nodes, edges)) == 0.5
    # 갭 없음 → 1.0
    assert graph_readiness(_pg([_gnode("api1", "API")], [_gedge("api1", "s1", "IMPLEMENTS")])) == 1.0
    # 점수 대상 노드 없음(라벨 없음/Story 등) → 1.0 (흠잡을 것 없음)
    assert graph_readiness(_pg([_gnode("s1", "Story")], [])) == 1.0


def test_graph_readiness_order_independent():
    from app.pipelines.interview.interview import graph_readiness
    n1 = [_gnode("a1", "API"), _gnode("a2", "API"), _gnode("agg1", "Aggregate", invariants="[]")]
    n2 = list(reversed(n1))
    assert graph_readiness(_pg(n1, [])) == graph_readiness(_pg(n2, []))  # 비율 → 순서 무관


async def test_build_graph_summary_appends_completeness_when_gaps(monkeypatch):
    import app.pipelines.lint_pipeline as _lint
    import app.service.query_repository as _qr
    async def fake_fetch(ctx, name):
        return {"ddd": {}, "spack": {}, "architecture": {}}
    async def fake_graph(name, team_id=""):
        return _pg([_gnode("api1", "API"), _gnode("api2", "API")], [_gedge("api1", "s1", "IMPLEMENTS")])
    monkeypatch.setattr(_lint, "_fetch_specs", fake_fetch)
    monkeypatch.setattr(_qr, "get_project_graph", fake_graph)
    out = await build_graph_summary(_ctx(FakeGemini(responses=["x"])), "proj")
    assert "설계 완성도: 50%" in out  # api2 미연결 → 완성도 신호 주입


# ─── T7: 빌드 검증(lint) 환류 ─────────────────────────────────────────────

def test_lint_failures_to_feedback_collects_unapplied():
    from app.pipelines.interview.interview import lint_failures_to_feedback
    from types import SimpleNamespace as NS
    lr = NS(cases=[
        NS(rules=[
            NS(rule="api:POST /tickets", description="티켓 생성 API", applied=False),
            NS(rule="entity:Ticket", description="Ticket 엔티티", applied=True),   # 적용됨 → 제외
        ]),
        NS(rules=[
            NS(rule="api:GET /x", description="조회 API", applied=None),           # 미상 → 제외(보수적)
            NS(rule="policy:auth", description="인증 정책", applied=False),
        ]),
    ])
    out = lint_failures_to_feedback(lr)
    assert "티켓 생성 API" in out and "인증 정책" in out
    assert "Ticket 엔티티" not in out and "조회 API" not in out


def test_lint_failures_to_feedback_dict_dedup_cap():
    from app.pipelines.interview.interview import lint_failures_to_feedback
    # dict 입력 + 중복 제거
    lr = {"cases": [{"rules": [
        {"description": "A", "applied": False},
        {"description": "A", "applied": False},
        {"description": "B", "applied": False},
    ]}]}
    out = lint_failures_to_feedback(lr, cap=1)
    assert out == ["A"]   # 중복 제거 + 캡


def test_lint_failures_empty():
    from app.pipelines.interview.interview import lint_failures_to_feedback
    assert lint_failures_to_feedback(None) == []
    assert lint_failures_to_feedback({"cases": []}) == []


async def test_build_graph_summary_appends_lint_feedback(monkeypatch):
    import app.pipelines.lint_pipeline as _lint
    import app.service.query_repository as _qr
    import app.service.lint_repository as _lr
    from types import SimpleNamespace as NS
    async def fake_fetch(ctx, name):
        return {"ddd": {}, "spack": {}, "architecture": {}}
    async def fake_graph(name, team_id=""):
        return _pg([], [])
    async def fake_last_lint(name, team_id=""):
        return NS(cases=[NS(rules=[NS(rule="api:POST /x", description="결제 API", applied=False)])])
    monkeypatch.setattr(_lint, "_fetch_specs", fake_fetch)
    monkeypatch.setattr(_qr, "get_project_graph", fake_graph)
    monkeypatch.setattr(_lr, "get_last_lint_for_project", fake_last_lint)
    out = await build_graph_summary(_ctx(FakeGemini(responses=["x"])), "proj")
    assert "이전 빌드 검증" in out and "결제 API" in out


# ─── T8: 게이트 객관 보정 (그래프 완성도로 자가점수 감쇠) ──────────────────

async def test_gate_grounding_damps_when_graph_incomplete():
    """자가 readiness 1.0 이라도 그래프 완성도 0.0 → effective 0.6 < 0.8 → done 거부."""
    gemini = FakeGemini(responses=[_done("정리할게요", scores=_FULL_SCORES)])
    turn = await run_interview_turn(_ctx(gemini), _users(3), graph_readiness=0.0)
    assert turn.phase == "ask"        # grounding 으로 down-convert (조기 done 차단)
    assert turn.readiness == 0.6      # 1.0 * (0.6 + 0.4*0.0)
    assert len(gemini.calls) == 1     # 합성 안 함


async def test_gate_grounding_noop_for_greenfield():
    """그래프 완성도 1.0(greenfield 기본) → 감쇠 0 → 기존과 동일하게 done 수락."""
    gemini = FakeGemini(responses=[_done("정리할게요", scores=_FULL_SCORES), "# 회의록\n앱"])
    turn = await run_interview_turn(_ctx(gemini), _users(3), graph_readiness=1.0)
    assert turn.phase == "done"
    assert turn.readiness == 1.0


async def test_graph_interview_context_returns_gaps_and_readiness(monkeypatch):
    import app.service.query_repository as _qr
    from app.pipelines.interview.interview import graph_interview_context
    async def fake_graph(name, team_id=""):
        return _pg([_gnode("api1", "API"), _gnode("api2", "API")], [_gedge("api1", "s1", "IMPLEMENTS")])
    monkeypatch.setattr(_qr, "get_project_graph", fake_graph)
    gaps, gr = await graph_interview_context("proj")
    assert any("사용자 시나리오" in q for q in gaps)  # api2 미연결 → 갭 질문
    assert gr == 0.5                                   # API 2개 중 1개 갭 → 완성도 0.5


async def test_graph_interview_context_failure_defaults(monkeypatch):
    import app.service.query_repository as _qr
    from app.pipelines.interview.interview import graph_interview_context
    async def boom(name, team_id=""):
        raise RuntimeError("neo down")
    monkeypatch.setattr(_qr, "get_project_graph", boom)
    assert await graph_interview_context("proj") == ([], 1.0)  # 실패 → 감쇠 없음(안전)


# ─── T9: soft-cap 정체 완화 ───────────────────────────────────────────────

async def test_soft_cap_relaxes_threshold_after_7_turns():
    """7턴 이상 + 부분 준비도(0.6~0.8) → soft 임계(0.6)로 마무리."""
    # 0.65 = effective(greenfield). 7턴(<min 아님, <soft 아님)에서 done 허용.
    partial = "goal=1.0|features=1.0|data=0.5|users=0.0|constraints=0.0|usage=0.0"  # readiness 0.6
    # 6턴까지는 0.6<0.8 → ask, 7턴째 done 모델 → soft 임계 0.6 충족 → done
    hist = _users(7)
    gemini = FakeGemini(responses=[_done("정리할게요", scores=partial), "# 회의록\n앱"])
    turn = await run_interview_turn(_ctx(gemini), hist)
    assert turn.phase == "done"           # soft-cap 완화로 마무리
    assert turn.readiness == 0.6


async def test_below_soft_cap_still_requires_full_threshold():
    """soft-cap 전(예: 5턴)엔 여전히 0.8 요구 — 0.6 이면 계속 질문."""
    partial = "goal=1.0|features=1.0|data=0.5|users=0.0|constraints=0.0|usage=0.0"  # 0.6
    gemini = FakeGemini(responses=[_done("정리할게요", scores=partial)])
    turn = await run_interview_turn(_ctx(gemini), _users(5))
    assert turn.phase == "ask"            # 5<7 → 임계 0.8 → 0.6 미달


async def test_down_convert_clears_stray_meeting_content():
    """모델이 done+MEETING_CONTENT 흘려도 게이트가 ask 로 되돌리면 회의록 누출 없음."""
    resp = (
        "PHASE: done\nMESSAGE: 끝!\nSUGGESTIONS:\nCOVERAGE:\n"
        "SCORES: goal=0.3|features=0.0|data=0.0|users=0.0|constraints=0.0|usage=0.0\n"
        "MEETING_CONTENT: 모델이 멋대로 쓴 회의록"
    )
    gemini = FakeGemini(responses=[resp])
    turn = await run_interview_turn(_ctx(gemini), _users(3))
    assert turn.phase == "ask"
    assert turn.meeting_content == ""   # 누출 방지


# ─── [2026-06-12] 보강(supplement) 모드 — 프로젝트 현황 + 의제 ──────────────
# 실사고: 진행 중 프로젝트에서 "부족한 부분 채워줘" → "무슨 앱 만드세요?" 반복.
# 브리프/의제 주입 + 게이트 완화(min 1턴, 감쇠 비활성)를 검증한다.

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.pipelines.interview.interview import _interview_prompt
from app.pipelines.interview import build_interview_project_context


async def test_supplement_allows_done_after_one_turn():
    """보강 모드 — 핵심 5개는 브리프로 충족이라 1턴 + 준비도 충분이면 done.
    (greenfield 의 최소 3턴 강제는 test_gate_blocks_done_before_min_turns 가 가드.)"""
    gemini = FakeGemini(responses=[_done("정리할게요!"), "# 회의록\n보강 내용"])
    turn = await run_interview_turn(
        _ctx(gemini), _users(1),
        project_brief="프로젝트명: p", supplement=True,
    )
    assert turn.phase == "done"
    assert "보강" in turn.meeting_content


async def test_supplement_skips_graph_damping():
    """보강 모드 — 그래프 미완성 감쇠 비활성.
    미완성이라서 보강하러 온 사람에게 미완성을 이유로 인터뷰를 더 끌지 않는다."""
    gemini = FakeGemini(responses=[_done("정리!"), "# 회의록"])
    turn = await run_interview_turn(
        _ctx(gemini), _users(1),
        graph_readiness=0.0,           # greenfield 였다면 1.0×0.6=0.6 → done 거부
        project_brief="b", supplement=True,
    )
    assert turn.phase == "done"
    assert turn.readiness == 1.0       # 감쇠 미적용


async def test_prompt_includes_project_brief():
    """브리프가 {{PROJECT}} 슬롯으로 프롬프트에 실린다 / 비면 신규 안내 렌더."""
    p = _interview_prompt([], "", None, "프로젝트명: pX / PRD 충실도: 98%")
    assert "프로젝트명: pX" in p
    assert "보강 모드" in p            # 템플릿의 (C) 섹션 헤더
    p2 = _interview_prompt([], "", None, "")
    assert "(없음 — 새 기획입니다" in p2


async def test_build_project_context_merges_agenda_lint_gaps(monkeypatch):
    """FE agenda(최우선) > 평가 fix_targets > PRD lint > 그래프 갭 순 병합 + 브리프."""
    import app.pipelines.interview.interview as iv
    from app.service import query_repository as qr

    monkeypatch.setattr(
        iv, "graph_interview_context", AsyncMock(return_value=(["그래프갭 질문"], 0.7)),
    )
    # 짧은 PRD — lint 가 결정적으로 이슈(PRD_TOO_SHORT 등)를 낸다 (실물 lint 사용).
    fake_prd = SimpleNamespace(prd_content="## Master PRD\n### 1. Overview\n짧은 비전")
    monkeypatch.setattr(qr, "get_master_prd", AsyncMock(return_value=fake_prd))
    monkeypatch.setattr(qr, "get_all_meeting_content", AsyncMock(return_value=""))
    monkeypatch.setattr(iv, "_fetch_eval_context", AsyncMock(return_value=(None, [])))

    pc = await build_interview_project_context("p_scoped", display_name="p", agenda=["FE 의제"])

    assert pc.supplement is True
    assert "프로젝트명: p" in pc.brief and "PRD 충실도" in pc.brief
    assert pc.gap_questions[0] == "FE 의제"            # FE 가 최우선
    assert "그래프갭 질문" in pc.gap_questions          # 그래프 갭 병합
    assert len(pc.gap_questions) > 2                    # lint 이슈도 포함됨
    assert pc.graph_readiness == 0.7


async def test_build_project_context_greenfield(monkeypatch):
    """프로젝트/PRD/회의록 없으면 greenfield — 기존 인터뷰와 동일(브리프 없음)."""
    import app.pipelines.interview.interview as iv
    from app.service import query_repository as qr

    monkeypatch.setattr(iv, "graph_interview_context", AsyncMock(return_value=([], 1.0)))
    monkeypatch.setattr(qr, "get_master_prd", AsyncMock(return_value=None))
    monkeypatch.setattr(qr, "get_all_meeting_content", AsyncMock(return_value=""))
    monkeypatch.setattr(iv, "_fetch_eval_context", AsyncMock(return_value=(None, [])))

    pc = await build_interview_project_context("p_scoped", display_name="p")
    assert pc.supplement is False
    assert pc.brief == ""
    assert pc.gap_questions == []


# ─── [Phase 1 — 2026-06-12] 설계 평가·회의록 컨텍스트 연결 ─────────────────
# 실사고: "디자인 점수 낮대, 봐 봐" → 인터뷰가 그 점수·미연결 목록을 못 봐
# 동문서답. FE 완성도 모달과 같은 데이터(fix_targets)와 회의록 발췌를 주입한다.


def test_fix_targets_to_agenda_names_and_cap():
    """fix_targets → 항목 이름 포함 의제 문구. label 없음/0건은 제외, cap 적용."""
    from app.pipelines.interview.interview import fix_targets_to_agenda

    fts = [
        {
            "label": "API 에러 응답 명시", "tier": 2,
            "missing": [{"id": "API-01", "name": "작업 생성"}, {"id": "API-02", "name": "작업 조회"}],
            "missing_total": 17,
            "fix": "PRD Epic & Story 탭에서 에러 응답을 추가하세요",
        },
        {"label": "", "missing": [{"id": "X", "name": "x"}], "missing_total": 1},   # label 없음 → skip
        {"label": "정책 연결", "missing": [], "missing_total": 0},                    # 0건 → skip
    ]
    out = fix_targets_to_agenda(fts)
    assert len(out) == 1
    assert "API 에러 응답 명시" in out[0]
    assert "17건" in out[0]
    assert "작업 생성" in out[0] and "작업 조회" in out[0]
    assert "외" in out[0]                       # 17 > 표시 2개 → '외' 표기
    assert "PRD Epic & Story" in out[0]

    many = [{"label": f"L{i}", "missing": [{"id": f"I{i}", "name": f"n{i}"}], "missing_total": 1} for i in range(9)]
    assert len(fix_targets_to_agenda(many)) == 5    # cap


async def test_build_project_context_includes_eval_and_meetings(monkeypatch):
    """브리프에 설계 평가 %·회의록 발췌, 의제에 이름 포함 fix_target 이 실린다."""
    import app.pipelines.interview.interview as iv
    from app.service import query_repository as qr

    monkeypatch.setattr(iv, "graph_interview_context", AsyncMock(return_value=([], 0.5)))
    fake_prd = SimpleNamespace(prd_content="## Master PRD\n### 1. Overview\n에이전트 관리 시스템")
    monkeypatch.setattr(qr, "get_master_prd", AsyncMock(return_value=fake_prd))
    monkeypatch.setattr(
        qr, "get_all_meeting_content",
        AsyncMock(return_value="V1 내용...\n\nV28: 결제 정책과 에이전트 권한 논의"),
    )
    ft = {
        "label": "API 에러 응답 명시",
        "missing": [{"id": "API-01", "name": "작업 생성"}],
        "missing_total": 17, "fix": "", "tier": 2,
    }
    monkeypatch.setattr(iv, "_fetch_eval_context", AsyncMock(return_value=(54, [ft])))

    pc = await build_interview_project_context("p_scoped", display_name="p")

    assert "기획서 완성도(설계 평가): 54%" in pc.brief
    assert "결제 정책과 에이전트 권한 논의" in pc.brief      # 회의록 꼬리(최신) 발췌
    assert any("API 에러 응답 명시" in q and "17건" in q for q in pc.gap_questions)


async def test_build_project_context_meetings_only_triggers_supplement(monkeypatch):
    """PRD 가 아직 없어도 회의록이 있으면 브리프 생성 — 보강 모드 발동."""
    import app.pipelines.interview.interview as iv
    from app.service import query_repository as qr

    monkeypatch.setattr(iv, "graph_interview_context", AsyncMock(return_value=([], 1.0)))
    monkeypatch.setattr(qr, "get_master_prd", AsyncMock(return_value=None))
    monkeypatch.setattr(qr, "get_all_meeting_content", AsyncMock(return_value="회의록: 펫시터 매칭 앱"))
    monkeypatch.setattr(iv, "_fetch_eval_context", AsyncMock(return_value=(None, [])))

    pc = await build_interview_project_context("p_scoped", display_name="p")
    assert pc.supplement is True
    assert "펫시터 매칭 앱" in pc.brief
    assert "기획서 완성도" not in pc.brief       # 설계 없음 → 완성도 언급 생략


async def test_fetch_eval_context_failure_returns_empty(monkeypatch):
    """설계 평가 조회 실패 → (None, []) — 인터뷰를 막지 않는다."""
    from app.pipelines.interview.interview import _fetch_eval_context
    from app.service import query_repository as qr

    monkeypatch.setattr(qr, "get_spack_graph", AsyncMock(side_effect=RuntimeError("down")))
    pct, fts = await _fetch_eval_context("p_scoped")
    assert pct is None and fts == []


# ─── [B-1 — 2026-06-12] 읽기 코파일럿 도구 (ACTION 프로토콜) ────────────────
# 보강 모드에서 모델이 ACTION 한 줄로 원자료(prd/meetings/eval/design)를 조회.
# 텍스트 프로토콜(native function calling 미지원 클라이언트)·턴당 2회 캡·
# greenfield 비활성·스트림 tool 이벤트/토큰 무누출을 가드한다.

from app.pipelines.interview.interview import (  # noqa: E402
    _has_message,
    execute_interview_tool,
    parse_action,
)


def test_parse_action_whitelist():
    """ACTION 줄에서 도구명 추출 — whitelist 외/형식 오류는 None."""
    assert parse_action("ACTION: meetings") == "meetings"
    assert parse_action("  ACTION: PRD  ") == "prd"          # 대소문자/공백 관용
    assert parse_action("ACTION: rm -rf /") is None           # 미등록 도구
    assert parse_action("ACTION:") is None
    assert parse_action("PHASE: ask\nMESSAGE: 질문") is None
    assert parse_action("") is None


def test_has_message_guard():
    """MESSAGE 가 실제 내용과 함께 있으면 True — ACTION 보다 답변 우선의 근거."""
    assert _has_message("PHASE: ask\nMESSAGE: 안녕하세요\nSCORES: goal=1.0")
    assert not _has_message("MESSAGE:")                       # 빈 내용
    assert not _has_message("ACTION: prd")


async def test_execute_tool_meetings_and_unknown(monkeypatch):
    """meetings 도구는 회의록 꼬리를 반환, 미등록 도구/빈 프로젝트는 조회 불가."""
    from app.service import query_repository as qr

    monkeypatch.setattr(qr, "get_all_meeting_content", AsyncMock(return_value="옛 내용\n최신 결제 논의"))
    ctx = _ctx(FakeGemini(responses=[]))
    out = await execute_interview_tool(ctx, "meetings", "p_scoped")
    assert "최신 결제 논의" in out
    assert await execute_interview_tool(ctx, "없는도구", "p_scoped") == "(조회 불가)"
    assert await execute_interview_tool(ctx, "meetings", "") == "(조회 불가)"


async def test_execute_tool_failure_degrades(monkeypatch):
    """도구 내부 예외 → 안내 문구로 강등 (턴을 깨지 않음)."""
    from app.service import query_repository as qr

    monkeypatch.setattr(qr, "get_master_prd", AsyncMock(side_effect=RuntimeError("down")))
    out = await execute_interview_tool(_ctx(FakeGemini(responses=[])), "prd", "p_scoped")
    assert "조회 실패" in out


async def test_turn_tool_loop_executes_and_answers(monkeypatch):
    """보강 모드: ACTION → 도구 실행 → 결과 주입 재호출 → 데이터 근거 답변."""
    from app.service import query_repository as qr

    monkeypatch.setattr(qr, "get_all_meeting_content", AsyncMock(return_value="V28: 결제 정책 논의"))
    gemini = FakeGemini(responses=[
        "ACTION: meetings",
        "PHASE: ask\nMESSAGE: 회의록의 결제 정책 기준으로 여쭤볼게요 — 환불 규정은요?\nSUGGESTIONS: 7일 내 환불\nCOVERAGE: 데이터\nSCORES: goal=0.8|features=0.5|data=0.5|users=0.8|constraints=0.3|usage=0.8",
    ])
    turn = await run_interview_turn(
        _ctx(gemini), _users(1),
        project_brief="프로젝트명: p", supplement=True, project_scoped="p_scoped",
    )
    assert turn.phase == "ask"
    assert "환불 규정" in turn.assistant_message
    assert len(gemini.calls) == 2
    # 2번째 호출 프롬프트에 도구 결과가 주입됐는지
    assert "[meetings]" in gemini.calls[1]["prompt"]
    assert "결제 정책 논의" in gemini.calls[1]["prompt"]


async def test_turn_tool_loop_capped(monkeypatch):
    """ACTION 만 반복해도 _MAX_TOOL_CALLS(2)회 후 강제 종료 — 폴백 질문으로 강등."""
    from app.service import query_repository as qr

    monkeypatch.setattr(qr, "get_all_meeting_content", AsyncMock(return_value="m"))
    gemini = FakeGemini(responses=["ACTION: meetings", "ACTION: meetings", "ACTION: meetings"])
    turn = await run_interview_turn(
        _ctx(gemini), _users(1),
        project_brief="b", supplement=True, project_scoped="p_scoped",
    )
    assert len(gemini.calls) == 3            # 도구 2회 + 최종 1회
    assert turn.phase == "ask"               # MESSAGE 없음 → 폴백 질문 (안전 강등)
    assert turn.assistant_message            # 비어 있지 않음


async def test_greenfield_never_runs_tools(monkeypatch):
    """(A) 신규 모드: ACTION 출력해도 도구 미실행 — 1회 호출 후 폴백."""
    from app.service import query_repository as qr

    spy = AsyncMock(return_value="m")
    monkeypatch.setattr(qr, "get_all_meeting_content", spy)
    gemini = FakeGemini(responses=["ACTION: meetings"])
    turn = await run_interview_turn(_ctx(gemini), _users(1))   # supplement=False
    assert len(gemini.calls) == 1
    spy.assert_not_called()
    assert turn.phase == "ask"


async def test_stream_tool_event_no_token_leak(monkeypatch):
    """스트림: ACTION 턴은 토큰 무누출 + ("tool", 이름) 이벤트 → 최종 답 스트림."""
    from app.service import query_repository as qr

    monkeypatch.setattr(qr, "get_all_meeting_content", AsyncMock(return_value="회의록"))
    gemini = FakeGemini(responses=[
        "ACTION: meetings",
        "PHASE: ask\nMESSAGE: 데이터 봤어요!\nSUGGESTIONS: \nCOVERAGE: \nSCORES: goal=0.5",
    ])
    events = []
    async for ev in run_interview_turn_stream(
        _ctx(gemini), _users(1),
        project_brief="b", supplement=True, project_scoped="p_scoped",
    ):
        events.append(ev)

    types = [t for t, _ in events]
    tool_idx = types.index("tool")
    first_token_idx = types.index("token")
    assert events[tool_idx] == ("tool", "meetings")
    assert tool_idx < first_token_idx                      # 도구가 토큰보다 먼저
    streamed = "".join(d for t, d in events if t == "token")
    assert streamed == "데이터 봤어요!"                      # ACTION 턴 누출 없음
    assert events[-1][0] == "done"


async def test_build_project_context_agenda_only_without_project():
    """프로젝트 이름이 없어도 FE agenda 는 의제로 살아남는다 (게이트 완화 포함)."""
    pc = await build_interview_project_context("", agenda=["  인증 방식은?  ", ""])
    assert pc.gap_questions == ["인증 방식은?"]
    assert pc.supplement is True       # 의제 기반 — 게이트 완화 적용
    assert pc.brief == ""              # 단 (C) 모드 발동은 브리프 기준 (프롬프트는 신규)
