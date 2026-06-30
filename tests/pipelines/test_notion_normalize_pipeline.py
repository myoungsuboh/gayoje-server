"""
notion_normalize_pipeline 단위 테스트 — LLM mock 으로 변환 흐름 검증.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from app.pipelines.base import PipelineContext, Neo4jClientProxy
from app.pipelines.notion_normalize_pipeline import (
    NotionNormalizeInput,
    run_notion_normalize,
)

pytestmark = pytest.mark.asyncio


# ─── Helpers ────────────────────────────────────────────────────


@dataclass
class _FakeResult:
    text: str


class _FakeGemini:
    """generate() 반환 text 를 명시적으로 주입. 호출 prompt 기록."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []
        self.last_temperature: Optional[float] = None

    async def generate(self, prompt: str, *, temperature: float = 0.2):
        self.calls.append(prompt)
        self.last_temperature = temperature
        return _FakeResult(text=self.text)


def _ctx(gemini) -> PipelineContext:
    return PipelineContext(
        gemini=gemini, neo4j=Neo4jClientProxy(), idempotency_key="test-1",
    )


def _input(**overrides) -> NotionNormalizeInput:
    defaults = {
        "project_name": "harness",
        "version": "v1.1",
        "page_title": "킥오프 미팅",
        "page_url": "https://notion.so/page-1",
        "last_edited": "2026-05-18T10:00:00.000Z",
        "original_markdown": "# 킥오프\n\n참석자: PM, 개발팀장\n\n주요 안건: 일정 협의...",
    }
    defaults.update(overrides)
    return NotionNormalizeInput(**defaults)


# ─── 정상 흐름 ──────────────────────────────────────────────────


class TestHappyPath:
    async def test_returns_normalized_markdown(self):
        gemini_output = (
            "### [미팅 로그 v1.1] - 킥오프\n"
            "* **일시:** 2026-05-18\n"
            "* **참석자:** PM, 개발팀장\n"
            "* **PM:** \"일정 협의가 시급합니다.\"\n"
            "* **[진척도: — - 킥오프 안건 정리]**\n"
            "---\n"
        )
        gemini = _FakeGemini(text=gemini_output)
        result = await run_notion_normalize(_ctx(gemini), _input())
        assert "### [미팅 로그 v1.1]" in result.normalized_markdown
        assert "킥오프" in result.normalized_markdown
        assert result.char_count > 0
        assert result.truncated is False

    async def test_prompt_includes_input_metadata(self):
        gemini = _FakeGemini(
            text="### [미팅 로그 v1.1] - 테스트\n* **일시:** 2026-05-18\n---\n"
        )
        await run_notion_normalize(_ctx(gemini), _input(
            project_name="myproj", page_title="페이지 제목",
            page_url="https://notion.so/p", last_edited="2026-05-18T11:00:00Z",
        ))
        prompt = gemini.calls[0]
        # 입력 메타데이터가 프롬프트에 모두 들어갔는지
        assert "myproj" in prompt
        assert "페이지 제목" in prompt
        assert "https://notion.so/p" in prompt
        assert "2026-05-18T11:00:00Z" in prompt
        assert "v1.1" in prompt

    async def test_low_temperature_for_determinism(self):
        gemini = _FakeGemini(
            text="### [미팅 로그 v1.0] - x\n* **일시:** 2026-05-18\n---\n"
        )
        await run_notion_normalize(_ctx(gemini), _input())
        # CPS 와 같은 0.2 — 결정성 확보용
        assert gemini.last_temperature == 0.2

    async def test_strips_fence_if_llm_adds(self):
        # LLM 이 펜스로 감싸도 안전하게 제거.
        gemini = _FakeGemini(text=(
            "```markdown\n"
            "### [미팅 로그 v1.1] - 회의\n"
            "* **일시:** 2026-05-18\n"
            "---\n"
            "```\n"
        ))
        result = await run_notion_normalize(_ctx(gemini), _input())
        assert "```" not in result.normalized_markdown
        assert "### [미팅 로그 v1.1]" in result.normalized_markdown


# ─── 입력 길이 제한 ─────────────────────────────────────────────


class TestTruncation:
    async def test_long_input_truncated_and_flag_set(self):
        long_md = "a" * 200_000   # _MAX_INPUT_CHARS=100_000 초과
        gemini = _FakeGemini(
            text="### [미팅 로그 v1.0] - x\n* **일시:** 2026-05-18\n---\n"
        )
        result = await run_notion_normalize(_ctx(gemini), _input(
            original_markdown=long_md
        ))
        assert result.truncated is True
        # 프롬프트에 들어간 markdown 길이가 한도 안으로 잘렸는지
        prompt = gemini.calls[0]
        # 한도 + 프롬프트 헤더/푸터 분량 정도까지 허용
        assert len(prompt) < 105_000


# ─── 에러 ───────────────────────────────────────────────────────


class TestErrors:
    async def test_empty_llm_response_raises(self):
        gemini = _FakeGemini(text="")
        with pytest.raises(ValueError, match="비어있"):
            await run_notion_normalize(_ctx(gemini), _input())

    async def test_missing_header_raises(self):
        # LLM 이 자유 형식으로 답한 경우 — 표준 헤더 없으면 거부.
        gemini = _FakeGemini(text="회의록입니다.\n어쩌고 저쩌고...\n")
        with pytest.raises(ValueError, match="표준 포맷"):
            await run_notion_normalize(_ctx(gemini), _input())

    async def test_whitespace_only_response_raises(self):
        gemini = _FakeGemini(text="   \n\n   \n")
        with pytest.raises(ValueError):
            await run_notion_normalize(_ctx(gemini), _input())
