"""
skill_recommend_pipeline 단위 + e2e fakes.

recommendSkillsByAI 동작 검증:
- 비어있는 catalog → ValueError
- LLM 출력에서 catalog 에 없는 id 는 reject
- 중복 id 는 한 번만
- confidence clamp [0, 1]
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.skill_recommend_pipeline import (
    CatalogEntry,
    RecommendInput,
    _format_arch_context,
    _parse_and_validate,
    run_skill_recommend_pipeline,
)
from tests.conftest import FakeGemini, FakeNeo4j


# ─── _parse_and_validate (순수 동기 함수) ─────────────────────


def test_parse_filters_unknown_ids():
    # [2026-05] _parse_and_validate 가 string 대신 parsed dict 받음 — call_skill_picker
    # 에서 generate_json_with_retry 가 이미 JSON 파싱 + fence 처리 수행.
    parsed = {
        "recommended": [
            {"id": "SKL-01", "reason": "ok", "confidence": 0.9},
            {"id": "SKL-FAKE", "reason": "made up", "confidence": 1.0},
        ]
    }
    result = _parse_and_validate(parsed, {"SKL-01", "SKL-02"})
    assert len(result.recommended) == 1
    assert result.recommended[0].id == "SKL-01"
    assert result.meta["rawCount"] == 2
    assert result.meta["validCount"] == 1


def test_parse_deduplicates():
    parsed = {
        "recommended": [
            {"id": "SKL-01", "reason": "first", "confidence": 0.95},
            {"id": "SKL-01", "reason": "duplicate", "confidence": 0.95},
        ]
    }
    result = _parse_and_validate(parsed, {"SKL-01"})
    assert len(result.recommended) == 1
    assert result.recommended[0].reason == "first"


def test_parse_clamps_confidence():
    parsed = {
        "recommended": [
            {"id": "A", "confidence": 1.5},
            {"id": "B", "confidence": -0.5},
            {"id": "C", "confidence": "bad"},
        ]
    }
    result = _parse_and_validate(parsed, {"A", "B", "C"})
    confs = {r.id: r.confidence for r in result.recommended}
    assert confs["A"] == 1.0
    # [2026-06 추천 신뢰 정책] clamp 후 0.0 은 0.90 미만 → 추천에서 제외
    assert "B" not in confs
    # [2026-06-13 구멍 보강] non-numeric → None → 확신 미표명으로 제외 (이전엔 통과)
    assert "C" not in confs


def test_parse_drops_missing_confidence():
    """[2026-06-13] confidence 누락(None)도 제외 — 0.90 게이트 우회 차단."""
    parsed = {
        "recommended": [
            {"id": "A", "reason": "근거", "confidence": 0.95},
            {"id": "B", "reason": "근거지만 확신 미표명"},   # confidence 누락
        ]
    }
    result = _parse_and_validate(parsed, {"A", "B"})
    assert [r.id for r in result.recommended] == ["A"]
    assert result.meta["lowConfidenceDropped"] == 1


def test_parse_drops_low_confidence():
    """[추천 신뢰 정책] 0.90 미만 확신 추천은 제외 — '70~80점대 추천' 사용자 불신 방지."""
    parsed = {
        "recommended": [
            {"id": "A", "confidence": 0.7},
            {"id": "B", "confidence": 0.89},
            {"id": "C", "confidence": 0.90},
            {"id": "D", "confidence": 0.95},
        ]
    }
    result = _parse_and_validate(parsed, {"A", "B", "C", "D"})
    assert [r.id for r in result.recommended] == ["C", "D"]
    assert result.meta["lowConfidenceDropped"] == 2
    assert result.meta["validCount"] == 2


def test_prompt_enforces_min_confidence():
    """프롬프트 정적 가드 — 0.90 미만 포함 금지 + 근거 인용 지시가 빠지면 실패."""
    from pathlib import Path
    body = (
        Path(__file__).resolve().parents[2] / "app" / "prompts" / "skill_recommend.md"
    ).read_text(encoding="utf-8")
    assert "0.90~1.0" in body
    assert "0.90 미만이면 그 스킬은 추천 목록에 아예 포함하지 마세요" in body
    assert "구체적 문구·항목을 반드시 인용" in body


def test_parse_returns_empty_on_empty_dict():
    """generate_json_with_retry 가 두 시도 모두 실패 시 빈 dict 반환 → 빈 결과."""
    result = _parse_and_validate({}, {"X"})
    assert result.recommended == []
    # recommended 키 누락 → meta.error 설정
    assert "error" in result.meta or result.meta["rawCount"] == 0


def test_parse_handles_recommended_not_a_list():
    """recommended 가 dict/null 같은 잘못된 타입이면 빈 결과 + error."""
    result = _parse_and_validate({"recommended": "wrong type"}, {"X"})
    assert result.recommended == []
    assert "error" in result.meta or result.meta["rawCount"] == 0


# ─── End-to-end pipeline (async, per-function asyncio mark) ───


def _responder():
    def respond(prompt: str) -> str:
        assert "시스템 아키텍처 표준화 전문가" in prompt
        # CPS / PRD / arch / catalog 가 프롬프트에 포함됐는지 확인
        assert "smoke-cps" in prompt
        assert "smoke-prd" in prompt
        assert "Spring Boot" in prompt  # [A1] ArchService tech_stack 포함 확인
        return json.dumps(
            {
                "recommended": [
                    {
                        "id": "SKL-01",
                        "reason": "인증 정책",
                        "confidence": 0.92,
                    },
                    {
                        "id": "SKL-FAKE",
                        "reason": "should be filtered",
                        "confidence": 0.5,
                    },
                ]
            }
        )

    return respond


# ─── _format_arch_context (순수 동기 함수) ─────────────────────


def test_format_arch_context_full():
    """[A1] 서비스·엔티티·API 모두 있으면 세 섹션 모두 포맷."""
    text = _format_arch_context(
        services=[{"name": "UserSvc", "tech_stack": "Spring Boot"}],
        entities=["User", "Order"],
        apis=["GET /api/users", "POST /api/orders"],
    )
    assert "Spring Boot" in text
    assert "UserSvc" in text
    assert "User, Order" in text
    assert "GET /api/users" in text


def test_format_arch_context_empty():
    """모두 비어 있으면 빈 문자열 반환."""
    assert _format_arch_context([], [], []) == ""


def test_format_arch_context_partial():
    """서비스만 있는 경우 엔티티·API 섹션 미노출."""
    text = _format_arch_context(
        services=[{"name": "API-GW", "tech_stack": "Node.js"}],
        entities=[],
        apis=[],
    )
    assert "Node.js" in text
    assert "도메인 엔티티" not in text
    assert "API 엔드포인트" not in text


@pytest.mark.asyncio
async def test_recommend_full_flow():
    payload = RecommendInput(
        project_name="smoketest",
        skill_catalog=[
            CatalogEntry(id="SKL-01", name="Auth", description="인증 표준"),
            CatalogEntry(id="SKL-02", name="DB", description="DB 표준"),
        ],
    )
    gemini = FakeGemini(_responder())
    # [A1] Neo4j 응답 2개: 1) CPS+PRD  2) ArchService+Entity+API
    neo = FakeNeo4j(
        responses=[
            [{"cps_content": "smoke-cps content here", "prd_content": "smoke-prd content here"}],
            [{"services": [{"name": "UserSvc", "tech_stack": "Spring Boot"}], "entities": ["User"], "apis": ["GET /api/users"]}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="t1")

    result = await run_skill_recommend_pipeline(ctx, payload)
    # SKL-FAKE 는 catalog 에 없으므로 reject
    assert [r.id for r in result.recommended] == ["SKL-01"]
    assert result.recommended[0].confidence == 0.92
    # [A1] Neo4j 가 2번 호출됨: CPS/PRD + ArchContext
    assert len(neo.executed) == 2
    assert "MATCH (cps:CPS_Document" in neo.executed[0]["cypher"]
    assert "ArchService" in neo.executed[1]["cypher"]


@pytest.mark.asyncio
async def test_recommend_raises_on_empty_catalog():
    payload = RecommendInput(
        project_name="smoketest",
        skill_catalog=[],
    )
    gemini = FakeGemini(lambda p: "should not be called")
    neo = FakeNeo4j()
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="t2")

    with pytest.raises(ValueError, match="skillCatalog"):
        await run_skill_recommend_pipeline(ctx, payload)
    assert neo.executed == []
    assert gemini.calls == []


@pytest.mark.asyncio
async def test_recommend_handles_empty_cps_prd():
    """[2026-06-13 빈 상태 가드] CPS/PRD 가 둘 다 비어있으면 LLM 을 호출하지 않고
    즉시 빈 결과 + reason='no_source_docs' — 토큰 낭비·근거 없는 환각 차단."""
    payload = RecommendInput(
        project_name="empty_project",
        skill_catalog=[CatalogEntry(id="SKL-01", name="X")],
    )

    def respond(prompt: str) -> str:
        raise AssertionError("CPS/PRD 가 비었으면 LLM 을 호출하면 안 된다")

    gemini = FakeGemini(respond)
    # [A1] 응답 2개 필요
    neo = FakeNeo4j(
        responses=[
            [{"cps_content": None, "prd_content": None}],
            [],  # arch context 없음
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="t3")

    result = await run_skill_recommend_pipeline(ctx, payload)
    assert result.recommended == []
    assert result.meta["validCount"] == 0
    assert result.meta["reason"] == "no_source_docs"
    # LLM 미호출 검증 — CPS/PRD fetch 1회만, gemini 0회.
    assert gemini.calls == []


@pytest.mark.asyncio
async def test_recommend_runs_llm_when_only_prd_present():
    """CPS 는 비어도 PRD 가 있으면 정상 추천 — short-circuit 은 '둘 다 빈' 경우만."""
    payload = RecommendInput(
        project_name="prd_only",
        skill_catalog=[CatalogEntry(id="SKL-01", name="Auth", description="인증")],
    )

    def respond(prompt: str) -> str:
        return json.dumps({"recommended": [{"id": "SKL-01", "reason": "필요", "confidence": 0.95}]})

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(
        responses=[[{"cps_content": None, "prd_content": "실제 PRD 내용"}]]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="t4")

    result = await run_skill_recommend_pipeline(ctx, payload)
    assert [r.id for r in result.recommended] == ["SKL-01"]
    assert len(gemini.calls) == 1   # PRD 있으면 LLM 호출됨
