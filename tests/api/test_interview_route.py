"""/api/v2/interview/turn 라우트 회귀 테스트.

검증:
- 200 + 턴 응답 shape (phase/assistant_message/suggestions/coverage/meeting_content)
- 토큰 쿼터 가드 호출
- history 과대 시 413
- ownership 검사 없음 (cold start — 프로젝트 생성 전에도 동작)
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import interview_routes as ir
from app.pipelines.interview import InterviewProjectContext, InterviewTurn
from app.service.user_repository import UserPublic


def _make_app() -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[ir.get_current_user] = lambda: UserPublic(
        id="u-1", email="t@b.com", name="tester",
        subscription_type="free", is_admin=False,
    )
    app.include_router(ir.router)
    # slowapi limiter — 미니 앱에도 state 연결
    from app.core.limiter import limiter
    app.state.limiter = limiter
    return app


@pytest.fixture(autouse=True)
def _stub_quota(monkeypatch):
    monkeypatch.setattr(
        ir.quota, "assert_tokens_within_limit", AsyncMock(return_value=None)
    )
    # [T5+T8 → 2026-06-12] 턴 테스트 hermetic 유지 — 프로젝트 컨텍스트/ownership 기본
    # 무력화(개별 테스트가 덮음). 라우트는 이제 build_interview_project_context 를 쓴다.
    monkeypatch.setattr(
        ir, "build_interview_project_context",
        AsyncMock(return_value=InterviewProjectContext()),
    )
    monkeypatch.setattr(ir.ownership_repository, "assert_access", AsyncMock(return_value=None))


def test_turn_returns_question(monkeypatch):
    monkeypatch.setattr(
        ir, "run_interview_turn",
        AsyncMock(return_value=InterviewTurn(
            phase="ask",
            assistant_message="누가 쓰나요?",
            suggestions=["일반 사용자"],
            coverage=["정의"],
        )),
    )
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/turn", json={
        "project_name": "todo",
        "history": [{"role": "user", "content": "할 일 앱"}],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["phase"] == "ask"
    assert body["assistant_message"] == "누가 쓰나요?"
    assert body["suggestions"] == ["일반 사용자"]
    assert body["meeting_content"] == ""


def test_turn_exposes_readiness_and_scores(monkeypatch):
    """[T2] 응답에 readiness/scores 노출 — FE 진행바용."""
    monkeypatch.setattr(
        ir, "run_interview_turn",
        AsyncMock(return_value=InterviewTurn(
            phase="ask",
            assistant_message="데이터는 무엇을 다루나요?",
            scores={"goal": 1.0, "features": 0.5},
            readiness=0.375,
        )),
    )
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/turn", json={"history": [{"role": "user", "content": "앱"}]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["readiness"] == 0.375
    assert body["scores"] == {"goal": 1.0, "features": 0.5}


def test_turn_forwards_existing_content(monkeypatch):
    """보완 인터뷰 — 라우트가 existing_content 를 파이프라인에 전달한다."""
    spy = AsyncMock(return_value=InterviewTurn(phase="ask", assistant_message="?"))
    monkeypatch.setattr(ir, "run_interview_turn", spy)
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/turn", json={
        "history": [],
        "existing_content": "# 프로젝트 개요\n중고거래 앱",
    })
    assert resp.status_code == 200
    # run_interview_turn(ctx, history, existing_content) — 3번째 인자로 전달
    args = spy.await_args.args
    assert args[2] == "# 프로젝트 개요\n중고거래 앱"


def test_turn_done_returns_meeting_content(monkeypatch):
    monkeypatch.setattr(
        ir, "run_interview_turn",
        AsyncMock(return_value=InterviewTurn(
            phase="done",
            assistant_message="정리했어요!",
            meeting_content="# 프로젝트 개요\n할 일 앱",
        )),
    )
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/turn", json={"history": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["phase"] == "done"
    assert "프로젝트 개요" in body["meeting_content"]


def test_turn_checks_token_quota(monkeypatch):
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(ir.quota, "assert_tokens_within_limit", spy)
    monkeypatch.setattr(
        ir, "run_interview_turn",
        AsyncMock(return_value=InterviewTurn(phase="ask", assistant_message="?")),
    )
    client = TestClient(_make_app())
    client.post("/api/v2/interview/turn", json={"history": []})
    spy.assert_awaited_once()


def test_turn_rejects_oversize_history(monkeypatch):
    monkeypatch.setattr(
        ir, "run_interview_turn",
        AsyncMock(return_value=InterviewTurn(phase="ask", assistant_message="?")),
    )
    client = TestClient(_make_app())
    history = [{"role": "user", "content": "x"} for _ in range(50)]
    resp = client.post("/api/v2/interview/turn", json={"history": history})
    assert resp.status_code == 413


# ─── 스트리밍 라우트 ────────────────────────────────────────────────

import json as _json


async def _fake_stream(ctx, history, existing_content="", gap_questions=None, graph_readiness=1.0, **_kw):
    yield ("token", "안녕")
    yield ("token", "하세요")
    yield ("finalizing", None)
    from app.pipelines.interview import InterviewProjectContext, InterviewTurn
    yield ("done", InterviewTurn(phase="done", assistant_message="정리했어요", suggestions=[], coverage=[], meeting_content="# 회의록"))


def test_stream_route_yields_sse_events(monkeypatch):
    monkeypatch.setattr(ir, "run_interview_turn_stream", _fake_stream)
    client = TestClient(_make_app())
    resp = client.post(
        "/api/v2/interview/turn/stream",
        json={"history": []},
        headers={"Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    text = resp.text
    assert '"type": "token"' in text or '"type":"token"' in text
    assert '"type": "finalizing"' in text or '"type":"finalizing"' in text
    assert '"type": "done"' in text or '"type":"done"' in text


def test_stream_route_rejects_oversize_history(monkeypatch):
    monkeypatch.setattr(ir, "run_interview_turn_stream", _fake_stream)
    client = TestClient(_make_app())
    history = [{"role": "user", "content": "x"} for _ in range(50)]
    resp = client.post("/api/v2/interview/turn/stream", json={"history": history})
    assert resp.status_code == 413


# ─── build_plan 라우트 ────────────────────────────────────────────────

from app.pipelines.interview import BuildPlan


def test_build_plan_returns_plan(monkeypatch):
    monkeypatch.setattr(
        ir, "synthesize_build_plan",
        AsyncMock(return_value=BuildPlan(
            recommended_stack="Next.js+Supabase",
            scope_now=["할 일 추가"],
            milestones=["데이터 모델"],
            acceptance_criteria=["추가하면 목록에 보인다"],
            start_prompt="만들어줘",
        )),
    )
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/build_plan", json={"meeting_content": "# 개요\n할 일 앱"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["recommended_stack"] == "Next.js+Supabase"
    assert body["scope_now"] == ["할 일 추가"]
    assert body["acceptance_criteria"] == ["추가하면 목록에 보인다"]
    assert body["start_prompt"] == "만들어줘"


def test_build_plan_exposes_quality_score(monkeypatch):
    """응답에 객관 품질점수(0~1)+항목 분해가 실린다 — 자기정제 A/B 측정 토대."""
    monkeypatch.setattr(
        ir, "synthesize_build_plan",
        AsyncMock(return_value=BuildPlan(
            recommended_stack="Next.js+Supabase",
            scope_now=["할 일 추가"],
            milestones=["데이터 모델", "목록 화면", "추가 폼"],
            acceptance_criteria=["추가하면 목록에 보인다"],
            risks=["일정 지연"],
            start_prompt="아래 기획대로 흔한 웹 스택으로 작은 단위부터 순서대로 만들어줘.",
        )),
    )
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/build_plan", json={"meeting_content": "# 개요\n할 일 앱"})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["quality_score"], (int, float))
    assert 0.0 < body["quality_score"] <= 1.0  # 실질 플랜이라 0 초과
    # 항목 분해 키가 가중치 정의와 일치
    assert set(body["quality_breakdown"]) == {
        "stack", "scope", "milestones", "acceptance", "risks", "start_prompt",
    }


def test_build_plan_fallback_scores_low(monkeypatch):
    """빈 폴백(start_prompt만)은 품질점수가 낮게 나온다 — 회귀 잠금 신호."""
    monkeypatch.setattr(
        ir, "synthesize_build_plan",
        AsyncMock(return_value=BuildPlan(start_prompt="x")),  # 짧아 start_prompt도 0
    )
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/build_plan", json={"meeting_content": "m"})
    assert resp.status_code == 200
    assert resp.json()["quality_score"] == 0.0


def test_build_plan_forwards_meeting_content(monkeypatch):
    spy = AsyncMock(return_value=BuildPlan(start_prompt="x"))
    monkeypatch.setattr(ir, "synthesize_build_plan", spy)
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/build_plan", json={"meeting_content": "# 개요\n중고거래"})
    assert resp.status_code == 200
    # synthesize_build_plan(ctx, meeting_content) — 2번째 인자로 전달
    args = spy.await_args.args
    assert args[1] == "# 개요\n중고거래"


def test_build_plan_requires_content(monkeypatch):
    monkeypatch.setattr(ir, "synthesize_build_plan", AsyncMock(return_value=BuildPlan()))
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/build_plan", json={"meeting_content": ""})
    assert resp.status_code == 422  # min_length=1 위반


def test_build_plan_with_project_checks_ownership_and_injects_graph(monkeypatch):
    """project_name 주면 소유권 확인 후 그래프 요약을 synthesize 에 전달."""
    monkeypatch.setattr(ir.ownership_repository, "assert_access", AsyncMock(return_value=None))
    monkeypatch.setattr(ir, "build_graph_summary", AsyncMock(return_value="- Aggregate: Order"))
    syn = AsyncMock(return_value=BuildPlan(start_prompt="x"))
    monkeypatch.setattr(ir, "synthesize_build_plan", syn)
    client = TestClient(_make_app())
    resp = client.post(
        "/api/v2/interview/build_plan",
        json={"meeting_content": "m", "project_name": "proj"},
    )
    assert resp.status_code == 200
    assert syn.await_args.kwargs.get("graph_summary") == "- Aggregate: Order"


def test_build_plan_denied_project_skips_graph(monkeypatch):
    """소유권 없는 project_name → IDOR 차단: 에러 대신 그래프만 생략, plan 은 성공."""
    from fastapi import HTTPException
    monkeypatch.setattr(
        ir.ownership_repository, "assert_access",
        AsyncMock(side_effect=HTTPException(status_code=403)),
    )
    syn = AsyncMock(return_value=BuildPlan(start_prompt="x"))
    monkeypatch.setattr(ir, "synthesize_build_plan", syn)
    client = TestClient(_make_app())
    resp = client.post(
        "/api/v2/interview/build_plan",
        json={"meeting_content": "m", "project_name": "남의프로젝트"},
    )
    assert resp.status_code == 200  # build_plan 자체는 성공
    assert syn.await_args.kwargs.get("graph_summary") == ""  # 권한 없으니 그래프 없음


def test_build_plan_cache_hit_skips_synthesis(monkeypatch):
    """입력 해시가 저장된 것과 같으면 LLM 합성 없이 저장된 플랜 재사용."""
    monkeypatch.setattr(ir.ownership_repository, "assert_access", AsyncMock(return_value=None))
    monkeypatch.setattr(ir, "build_graph_summary", AsyncMock(return_value=""))
    monkeypatch.setattr(ir, "build_plan_input_hash", lambda *a, **k: "H")
    monkeypatch.setattr(ir, "get_build_plan", AsyncMock(return_value=(BuildPlan(start_prompt="cached!"), "H")))
    syn = AsyncMock(return_value=BuildPlan(start_prompt="fresh"))
    monkeypatch.setattr(ir, "synthesize_build_plan", syn)
    monkeypatch.setattr(ir, "save_build_plan", AsyncMock(return_value=None))
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/build_plan", json={"meeting_content": "m", "project_name": "p"})
    assert resp.status_code == 200
    assert resp.json()["start_prompt"] == "cached!"  # 저장된 것 재사용
    syn.assert_not_awaited()  # LLM 합성 호출 안 함 (지연·비용 절감)


def test_build_plan_cache_miss_synthesizes_and_saves(monkeypatch):
    """저장된 해시와 다르면(입력 변경) 새로 생성하고 저장."""
    monkeypatch.setattr(ir.ownership_repository, "assert_access", AsyncMock(return_value=None))
    monkeypatch.setattr(ir, "build_graph_summary", AsyncMock(return_value=""))
    monkeypatch.setattr(ir, "build_plan_input_hash", lambda *a, **k: "NEW")
    monkeypatch.setattr(ir, "get_build_plan", AsyncMock(return_value=(BuildPlan(start_prompt="old"), "OLD")))
    # 실질 합성 결과 (recommended_stack 있음) → 저장 대상.
    syn = AsyncMock(return_value=BuildPlan(start_prompt="fresh", recommended_stack="Next.js"))
    monkeypatch.setattr(ir, "synthesize_build_plan", syn)
    save = AsyncMock(return_value=None)
    monkeypatch.setattr(ir, "save_build_plan", save)
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/build_plan", json={"meeting_content": "m", "project_name": "p"})
    assert resp.status_code == 200
    assert resp.json()["start_prompt"] == "fresh"  # 새로 생성
    syn.assert_awaited_once()
    save.assert_awaited_once()  # 새 플랜 저장됨


def test_build_plan_cache_miss_fallback_not_saved(monkeypatch):
    """합성이 폴백(빈 껍데기)을 내면 저장하지 않는다 — 일시 장애의 캐시 박제 방지."""
    monkeypatch.setattr(ir.ownership_repository, "assert_access", AsyncMock(return_value=None))
    monkeypatch.setattr(ir, "build_graph_summary", AsyncMock(return_value=""))
    monkeypatch.setattr(ir, "build_plan_input_hash", lambda *a, **k: "NEW")
    monkeypatch.setattr(ir, "get_build_plan", AsyncMock(return_value=(None, "")))
    # 폴백 모양: start_prompt 만 채워짐 (recommended_stack/scope/milestones 전부 빔)
    syn = AsyncMock(return_value=BuildPlan(start_prompt="아래 기획대로 만들어줘 ..."))
    monkeypatch.setattr(ir, "synthesize_build_plan", syn)
    save = AsyncMock(return_value=None)
    monkeypatch.setattr(ir, "save_build_plan", save)
    client = TestClient(_make_app())
    resp = client.post("/api/v2/interview/build_plan", json={"meeting_content": "m", "project_name": "p"})
    assert resp.status_code == 200
    assert resp.json()["start_prompt"].startswith("아래 기획대로")  # 폴백 응답은 정상 반환
    syn.assert_awaited_once()
    save.assert_not_awaited()  # 폴백은 캐시에 저장하지 않음


def test_turn_forwards_project_context(monkeypatch):
    """[T5+T8 → 2026-06-12] 소유 프로젝트면 브리프+통합 의제+grounding 을 턴에 전달."""
    monkeypatch.setattr(ir.ownership_repository, "assert_access", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ir, "build_interview_project_context",
        AsyncMock(return_value=InterviewProjectContext(
            brief="프로젝트명: p / PRD 충실도: 98%",
            gap_questions=["빠진 기능 X 확인"],
            graph_readiness=0.5,
            supplement=True,
        )),
    )
    spy = AsyncMock(return_value=InterviewTurn(phase="ask", assistant_message="?"))
    monkeypatch.setattr(ir, "run_interview_turn", spy)
    client = TestClient(_make_app())
    resp = client.post(
        "/api/v2/interview/turn",
        json={"project_name": "p", "history": [], "agenda": ["인증 방식은?"]},
    )
    assert resp.status_code == 200
    # run_interview_turn(ctx, history, existing, gaps, readiness, *, project_brief, supplement)
    assert spy.await_args.args[3] == ["빠진 기능 X 확인"]      # 통합 의제
    assert spy.await_args.args[4] == 0.5                       # grounding
    assert "PRD 충실도" in spy.await_args.kwargs["project_brief"]
    assert spy.await_args.kwargs["supplement"] is True
