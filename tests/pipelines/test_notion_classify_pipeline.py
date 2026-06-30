"""
notion_classify_pipeline 단위 테스트 — LLM mock 으로 분류 결과 검증.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.pipelines.base import PipelineContext, Neo4jClientProxy
from app.pipelines.notion_classify_pipeline import (
    NotionClassifyInput,
    run_notion_classify,
)

pytestmark = pytest.mark.asyncio


@dataclass
class _R:
    text: str


class _FakeGemini:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []
        self.last_temperature: float | None = None

    async def generate(self, prompt: str, *, temperature: float = 0.2, **kw):
        self.calls.append(prompt)
        self.last_temperature = temperature
        return _R(text=self.text)


def _ctx(g) -> PipelineContext:
    return PipelineContext(gemini=g, neo4j=Neo4jClientProxy(), idempotency_key="t")


def _payload(**kw) -> NotionClassifyInput:
    defaults = {
        "page_title": "테스트 페이지",
        "markdown": "본문 내용 약간",
    }
    defaults.update(kw)
    return NotionClassifyInput(**defaults)


# ─── tier 매핑 ─────────────────────────────────────────────


class TestTierMapping:
    """각 type 이 올바른 tier 로 매핑되는지."""

    async def test_meeting_log_is_accept(self):
        g = _FakeGemini(text=json.dumps({
            "type": "meeting_log", "confidence": 0.92,
            "reason": "PM/개발자/디자이너 등 발화자 식별 명확",
        }))
        r = await run_notion_classify(_ctx(g), _payload())
        assert r.type == "meeting_log"
        assert r.tier == "ACCEPT"
        assert r.confidence == 0.92

    async def test_retrospective_is_warn(self):
        g = _FakeGemini(text=json.dumps({
            "type": "retrospective", "confidence": 0.81,
            "reason": "KPT 구조 + 1인칭 회고체",
        }))
        r = await run_notion_classify(_ctx(g), _payload())
        assert r.tier == "WARN"

    async def test_spec_doc_is_warn(self):
        g = _FakeGemini(text=json.dumps({
            "type": "spec_doc", "confidence": 0.88,
            "reason": "API 명세 구조",
        }))
        r = await run_notion_classify(_ctx(g), _payload())
        assert r.tier == "WARN"

    async def test_task_request_is_block(self):
        g = _FakeGemini(text=json.dumps({
            "type": "task_request", "confidence": 0.95,
            "reason": "엑셀 보고서 작성 요청",
        }))
        r = await run_notion_classify(_ctx(g), _payload())
        assert r.tier == "BLOCK"

    async def test_general_doc_is_block(self):
        g = _FakeGemini(text=json.dumps({
            "type": "general_doc", "confidence": 0.7,
            "reason": "회사 정책 안내문",
        }))
        r = await run_notion_classify(_ctx(g), _payload())
        assert r.tier == "BLOCK"

    async def test_unknown_is_block(self):
        g = _FakeGemini(text=json.dumps({
            "type": "unknown", "confidence": 0.4,
            "reason": "내용 부족",
        }))
        r = await run_notion_classify(_ctx(g), _payload())
        assert r.tier == "BLOCK"


# ─── 비정상 응답 fallback ──────────────────────────────────


class TestFallback:
    async def test_invalid_type_falls_back_to_unknown_block(self):
        # LLM 이 정의 외 type 반환
        g = _FakeGemini(text=json.dumps({
            "type": "presentation",  # 우리 카테고리 아님
            "confidence": 0.8, "reason": "프레젠테이션 슬라이드",
        }))
        r = await run_notion_classify(_ctx(g), _payload())
        assert r.type == "unknown"
        assert r.tier == "BLOCK"

    async def test_empty_response_falls_back(self):
        g = _FakeGemini(text="")
        r = await run_notion_classify(_ctx(g), _payload())
        # generate_json_with_retry 가 빈 dict 반환 → 'unknown' fallback
        assert r.type == "unknown"
        assert r.tier == "BLOCK"

    async def test_missing_confidence_clamped_to_zero(self):
        g = _FakeGemini(text=json.dumps({
            "type": "meeting_log", "reason": "no conf field",
        }))
        r = await run_notion_classify(_ctx(g), _payload())
        assert r.confidence == 0.0

    async def test_confidence_clamped_to_unit_range(self):
        # confidence > 1.0 클램프
        g = _FakeGemini(text=json.dumps({
            "type": "meeting_log", "confidence": 2.5, "reason": "x",
        }))
        r = await run_notion_classify(_ctx(g), _payload())
        assert r.confidence == 1.0

    async def test_negative_confidence_clamped(self):
        g = _FakeGemini(text=json.dumps({
            "type": "meeting_log", "confidence": -0.5, "reason": "x",
        }))
        r = await run_notion_classify(_ctx(g), _payload())
        assert r.confidence == 0.0


# ─── 입력 길이 제한 ─────────────────────────────────────────


class TestInputTruncation:
    async def test_long_markdown_truncated_in_prompt(self):
        long_md = "a" * 50_000   # _MAX_CLASSIFY_CHARS=8_000 초과
        g = _FakeGemini(text=json.dumps({
            "type": "meeting_log", "confidence": 0.9, "reason": "x",
        }))
        await run_notion_classify(_ctx(g), _payload(markdown=long_md))
        prompt = g.calls[0]
        # 프롬프트 자체 + 자른 markdown 더해도 한도 안에
        assert len(prompt) < 12_000


# ─── temperature ───────────────────────────────────────────


class TestDeterminism:
    async def test_temperature_zero_for_classification(self):
        g = _FakeGemini(text=json.dumps({
            "type": "meeting_log", "confidence": 0.9, "reason": "x",
        }))
        await run_notion_classify(_ctx(g), _payload())
        # 분류는 결정성 중요 — 0.0
        assert g.last_temperature == 0.0
