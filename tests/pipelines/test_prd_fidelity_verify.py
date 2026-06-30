"""verify_fidelity_llm — PRD 정밀 대조(2단계 LLM) 단위 테스트."""
from __future__ import annotations

import json

import pytest

from app.pipelines.prd_fidelity_verify import verify_fidelity_llm
from tests.conftest import FakeGemini

pytestmark = pytest.mark.asyncio


class _Ctx:
    def __init__(self, g):
        self.gemini = g


def _responder(payload):
    body = json.dumps(payload, ensure_ascii=False)

    def respond(prompt: str) -> str:
        return body

    return respond


async def test_returns_missing_and_hallucination():
    gemini = FakeGemini(_responder({
        "coverage_pct": 70,
        "summary": "대체로 충실하나 결제 한도가 빠짐",
        "missing": [
            {"point": "결제 한도 100만원", "evidence": "회의록: 1회 결제 100만원 제한",
             "section": "nfr", "severity": "high"},
        ],
        "hallucination": [
            {"point": "OAuth 로그인은 회의록에 없음", "severity": "medium"},
        ],
    }))
    r = await verify_fidelity_llm(_Ctx(gemini), "회의록 본문", "PRD 본문")
    assert r["coverage_pct"] == 70
    assert r["summary"].startswith("대체로")
    assert len(r["missing"]) == 1
    assert r["missing"][0]["point"] == "결제 한도 100만원"
    assert r["missing"][0]["section"] == "nfr"
    assert r["missing"][0]["severity"] == "high"
    assert len(r["hallucination"]) == 1
    # LLM 1회 호출.
    assert len(gemini.calls) == 1


async def test_sanitizes_bad_values():
    gemini = FakeGemini(_responder({
        "coverage_pct": 200,  # 범위 초과 → 100 으로 clamp
        "missing": [
            {"point": "정상", "severity": "critical", "section": "weird"},  # 잘못된 값 보정
            {"point": "", "severity": "low"},  # point 빈 값 → 제외
        ],
        "hallucination": [],
    }))
    r = await verify_fidelity_llm(_Ctx(gemini), "m", "p")
    assert r["coverage_pct"] == 100               # clamp
    assert len(r["missing"]) == 1                 # 빈 point 제외
    assert r["missing"][0]["severity"] == "medium"  # critical → medium
    assert r["missing"][0]["section"] == ""         # weird → ""


async def test_caps_items_at_12():
    many_m = [{"point": f"m{i}", "severity": "low"} for i in range(20)]
    many_h = [{"point": f"h{i}", "severity": "low"} for i in range(20)]
    gemini = FakeGemini(_responder({"coverage_pct": 50, "missing": many_m, "hallucination": many_h}))
    r = await verify_fidelity_llm(_Ctx(gemini), "m", "p")
    assert len(r["missing"]) == 12
    assert len(r["hallucination"]) == 12


async def test_empty_or_broken_output():
    # LLM 이 형식을 못 지켜 빈 dict → coverage 0, 빈 목록 (방어).
    gemini = FakeGemini(_responder({}))
    r = await verify_fidelity_llm(_Ctx(gemini), "m", "p")
    assert r["coverage_pct"] == 0
    assert r["missing"] == []
    assert r["hallucination"] == []
