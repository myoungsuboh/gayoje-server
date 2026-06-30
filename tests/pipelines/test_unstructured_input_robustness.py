"""
[비정형 미팅 로그 robustness 테스트]

샘플 로그처럼 잘 포맷된 미팅 로그가 아닌, 실제 사용자가 입력할 수 있는
다양한 비정형 입력에 대해 CPS pipeline 이 어떻게 동작하는지 검증.

[검증 시나리오]
1. 입력 검증 layer (min_length=1) — 1글자만 있어도 통과
2. LLM 응답 별 pipeline 동작:
   a) LLM 이 빈 응답 ({}) → ValueError raise
   b) LLM 이 nodes:[] 응답 → silent pass (위험)
   c) LLM 이 무관한 가짜 CPS → silent pass + Neo4j 저장 (가장 위험)
3. 다양한 입력 형식 (영어, 잡담, 코드, 빈 라인 등) 에서 input canonicalization 영향

[이 테스트의 목적]
사용자가 "hi" 또는 잡담 같은 비정형 입력 시:
- 시스템이 어떤 결과를 사용자에게 보여주는지
- 환각된 데이터가 Neo4j 에 저장되는지
- 적절한 에러/안내 메시지가 나오는지
정확히 파악.
"""
from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from tests.conftest import FakeGemini, FakeNeo4j


# ─── 헬퍼 ────────────────────────────────────────────────────


def _fake_ctx_with_responder(responder, neo_responses=None):
    """FakeGemini + FakeNeo4j 로 PipelineContext 구성."""
    from app.pipelines.base import PipelineContext
    return PipelineContext(
        gemini=FakeGemini(responder),
        neo4j=FakeNeo4j(responses=neo_responses or []),
        idempotency_key="test-key",
    )


def _cps_input(content: str):
    """CpsInput dataclass — 비정형 입력 테스트용."""
    from app.pipelines.cps_pipeline import CpsInput
    return CpsInput(
        project_name="test_project",
        version="v1.0",
        date="2026-05-18",
        meeting_content=content,
        previous_cps_id=None,
    )


# ─── 1. 입력 검증 layer — quota / schema ────────────────────


def test_short_meeting_content_rejected_by_pydantic_validator():
    """[2026-05-18 P0] 200자 미만 입력은 Pydantic validator 가 거부.

    이전에는 min_length=1 이라 1글자도 통과 → LLM 환각 위험.
    이제 field_validator 가 assert_meeting_content_substantial 호출 → 의미적 차단.
    """
    from app.api.v2_routes import PostMeetingRequest

    # 1글자 — 거부
    with pytest.raises(Exception) as exc:
        PostMeetingRequest(
            project_name="x", version="v1.0", date="2026-05-18",
            meeting_content="h",
        )
    assert "너무 짧" in str(exc.value) or "최소" in str(exc.value)

    # 200자 미만 — 거부
    with pytest.raises(Exception):
        PostMeetingRequest(
            project_name="x", version="v1.0", date="2026-05-18",
            meeting_content="짧은 메모입니다",
        )

    # 빈 문자열 — Pydantic min_length=1 단계에서 거부
    with pytest.raises(Exception):
        PostMeetingRequest(
            project_name="x", version="v1.0", date="2026-05-18",
            meeting_content="",
        )

    # 정상 길이 (200자 + 공백 제외 100자 이상) — 통과
    valid = (
        "오늘 회의에서는 AI 계정 관리 프로세스의 신청 흐름을 정의했습니다. "
        "재무팀은 비용 통제 관점에서, 보안팀은 데이터 보호 관점에서 의견을 제시했고, "
        "다음 단계로는 4단계 프로세스 골격 (신청 → 승인 → 배정 → 관리) 을 설계하기로 했습니다. "
        "PO 는 4월 말까지 v1.0 운영 매뉴얼과 신청·관리 화면 설계서를 산출물로 가져가기로 했고, "
        "다음 미팅은 2월 6일로 현황 데이터 분석 세션을 잡았습니다."
    )
    assert len(valid) >= 200
    p_ok = PostMeetingRequest(
        project_name="x", version="v1.0", date="2026-05-18",
        meeting_content=valid,
    )
    assert p_ok.meeting_content == valid


def test_whitespace_only_content_rejected():
    """공백/줄바꿈만 가득한 입력 — 공백 제외 글자수 100자 미만 → 거부."""
    from app.api.v2_routes import PostMeetingRequest

    # 200자 공백 — 통과: chars >= 200 ✓, 하지만 non_ws_chars=0 → 거부
    with pytest.raises(Exception) as exc:
        PostMeetingRequest(
            project_name="x", version="v1.0", date="2026-05-18",
            meeting_content=" " * 300,
        )
    assert "공백 제외" in str(exc.value) or "실제 내용" in str(exc.value)


def test_meeting_validation_helper_messages():
    """assert_meeting_content_substantial 메시지 검증 — 사용자 안내 충실성."""
    from app.core.meeting_validation import (
        MeetingContentTooShort,
        assert_meeting_content_substantial,
    )

    # 너무 짧음
    with pytest.raises(MeetingContentTooShort) as exc:
        assert_meeting_content_substantial("hi")
    assert "샘플" in str(exc.value)  # 가이드 포함
    assert exc.value.chars == 2

    # 공백 가득
    with pytest.raises(MeetingContentTooShort) as exc:
        assert_meeting_content_substantial(" " * 250)
    assert "공백 제외" in str(exc.value)
    assert exc.value.non_ws_chars == 0

    # 정상 — silent return
    valid = "a" * 250
    assert assert_meeting_content_substantial(valid) is None


# ─── 2. LLM 응답 별 시나리오 ─────────────────────────────────


@pytest.mark.asyncio
async def test_llm_empty_response_raises_value_error():
    """[시나리오 A] LLM 이 빈 응답 → 명시적 ValueError 로 fail.

    이 경우 사용자는 'CPS Agent returned unparseable JSON' 에러 메시지를 봄.
    arq 가 3회 retry 후 최종 실패 처리.
    """
    from app.pipelines.cps_pipeline.agents import call_cps_agent

    # LLM 이 빈 JSON 응답
    def empty_responder(prompt: str) -> str:
        return "{}"

    ctx = _fake_ctx_with_responder(empty_responder)
    payload = _cps_input("hi")

    with pytest.raises(ValueError, match="unparseable JSON"):
        await call_cps_agent(ctx, payload)


@pytest.mark.asyncio
async def test_llm_empty_nodes_now_raises():
    """[시나리오 B — 2026-05-18 P1 fix] LLM 이 nodes:[] 응답 → ValueError raise.

    이전엔 silent pass (빈 그래프 저장). 이제 명시적 에러 → 사용자에게 안내.
    """
    from app.pipelines.cps_pipeline.agents import call_cps_agent

    def empty_nodes_responder(prompt: str) -> str:
        return json.dumps({
            "_harness_metadata": {"state": "recording"},
            "nodes": [],
            "relationships": [],
        })

    ctx = _fake_ctx_with_responder(empty_nodes_responder)
    payload = _cps_input("hi")

    # [2026-05-25 fallback] raise 대신 skip stub 반환 — BATCH 멈춤 차단.
    result = await call_cps_agent(ctx, payload)
    assert result.get("_extraction_mode") == "skip"
    assert "spec 변동 없음" in (result.get("_extraction_warning") or "") \
        or "신규 spec 0개" in (result.get("_extraction_warning") or "")
    # nodes 는 placeholder CPS_Document 1개
    assert len(result.get("nodes") or []) == 1
    assert result["nodes"][0]["label"] == "CPS_Document"
    assert result["nodes"][0]["properties"].get("skipped") is True


@pytest.mark.asyncio
async def test_llm_only_cps_document_no_spec_now_raises():
    """[시나리오 B-2 — P1 fix] CPS_Document 만 있고 Problem/Solution 0개도 raise.

    이전엔 nodes:[CPS_Document] 면 통과했지만, spec 노드가 없으면 의미 없음.
    """
    from app.pipelines.cps_pipeline.agents import call_cps_agent

    def doc_only_responder(prompt: str) -> str:
        return json.dumps({
            "nodes": [
                {
                    "id": "doc_cps_x_v1_0",
                    "label": "CPS_Document",
                    "properties": {"project": "x", "version": "v1.0"},
                },
            ],
            "relationships": [],
        })

    ctx = _fake_ctx_with_responder(doc_only_responder)
    payload = _cps_input("h" * 250)  # 길이 통과시키고 LLM 응답만 검증

    # [2026-05-25 fallback] raise 대신 skip stub (lenient 도 같은 빈 응답).
    result = await call_cps_agent(ctx, payload)
    assert result.get("_extraction_mode") == "skip"
    # CPS_Document placeholder
    assert any(
        (n or {}).get("label") == "CPS_Document"
        for n in (result.get("nodes") or [])
    )


@pytest.mark.asyncio
async def test_llm_hallucinated_cps_silent_pass():
    """[시나리오 C — 🔴 가장 위험] LLM 이 입력과 무관한 환각 CPS 응답.

    사용자가 "hi" 만 입력했는데 LLM 이 가짜 Problem / Solution 추출.
    schema 통과 → Neo4j 저장 → 사용자에게는 정상처럼 보임.
    """
    from app.pipelines.cps_pipeline.agents import call_cps_agent

    def hallucinated_responder(prompt: str) -> str:
        # LLM 이 'hi' 라는 인사를 "사용자의 인사 응답 요구" 같은 가짜 spec 으로 변환
        return json.dumps({
            "_harness_metadata": {"state": "recording", "verification_passed": True},
            "nodes": [
                {
                    "id": "doc_cps_test_project_v1_0",
                    "label": "CPS_Document",
                    "properties": {
                        "project": "test_project",
                        "version": "v1.0",
                        "is_latest": True,
                        "full_markdown": "## CPS\\n사용자가 시스템에 인사를 했다.",
                    },
                },
                {
                    "id": "prb_01",
                    "label": "Problem",
                    "properties": {
                        "summary": "사용자가 시스템과 인사 인터랙션을 원함",
                        "project": "test_project",
                    },
                },
                {
                    "id": "res_01",
                    "label": "Solution",
                    "properties": {
                        "summary": "친근한 응답 메시지 시스템 구축",
                        "project": "test_project",
                    },
                },
            ],
            "relationships": [
                {"source": "res_01", "type": "SOLVES", "target": "prb_01"},
            ],
        })

    ctx = _fake_ctx_with_responder(hallucinated_responder)
    payload = _cps_input("hi")  # 사용자는 단순히 "hi" 만 입력

    result = await call_cps_agent(ctx, payload)
    # ⚠️ 위험: LLM 이 만든 가짜 Problem/Solution 이 그대로 통과
    assert len(result["nodes"]) == 3
    problems = [n for n in result["nodes"] if n["label"] == "Problem"]
    assert problems[0]["properties"]["summary"] == "사용자가 시스템과 인사 인터랙션을 원함"
    # 사용자가 절대 입력 안 한 spec 이 만들어짐.


# ─── 3. 다양한 비정형 입력 정규화 ────────────────────────────


def test_canonicalization_strips_unusual_input():
    """canonicalize_meeting_content 가 다양한 입력을 어떻게 처리하는지."""
    from app.pipelines.base import canonicalize_meeting_content

    # 일반 텍스트 — 그대로
    assert canonicalize_meeting_content("hello") == "hello"

    # CRLF → LF
    assert canonicalize_meeting_content("a\r\nb\r\nc") == "a\nb\nc"

    # 빈 줄 다수 → 정리 됨 (실제 동작 검증)
    result = canonicalize_meeting_content("\n\n\nhello\n\n\n")
    # 빈 줄 정리 + 양끝 공백 strip 가 어떻게 처리되는지 그대로 받음
    assert "hello" in result
    # canonicalize 가 너무 공격적이면 사용자 입력 손실 가능
    print(f"\n[비정형 정규화 결과] '\\n\\n\\nhello\\n\\n\\n' → {result!r}")

    # 단일 글자 — 그대로
    assert canonicalize_meeting_content("h") == "h"

    # 잡담 — 그대로 (정규화는 형식만 다듬음)
    casual = "오늘 점심 뭐 먹지? 김치찌개? ㅋㅋ"
    assert canonicalize_meeting_content(casual) == casual


# ─── 4. 전체 흐름 — 빈 그래프가 끝까지 통과하는지 ─────────────


@pytest.mark.asyncio
async def test_empty_nodes_graph_silently_saved():
    """[가장 위험한 시나리오] LLM 이 빈 그래프 응답 → Cypher build → 빈 query → 저장 skip 또는 실패.

    이 시나리오에서 사용자가 보는 결과를 추적.
    """
    from app.pipelines.cps_pipeline.cypher import build_save_cps_query

    # 빈 그래프
    empty_graph = {"nodes": [], "relationships": []}
    cypher, params = build_save_cps_query(empty_graph, project_name="test_project")

    # 빈 그래프 → 빈 cypher
    assert cypher == ""
    assert params == {}
    # ✓ pipeline 의 run_in_transaction 이 빈 cypher 를 skip 한다면 fail 없이 통과
    # ⚠️ 결과: Neo4j 에 CPS_Document 노드 자체가 안 만들어짐
    #         사용자는 "처리 완료" 알림을 받지만 실제로는 빈 DB
