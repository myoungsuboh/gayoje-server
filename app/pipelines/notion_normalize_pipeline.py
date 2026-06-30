"""
Notion 페이지 markdown → Harness 표준 미팅 로그 포맷 변환 파이프라인.

[설계]
- 1회 LLM call. JSON 아닌 plain text 출력 (markdown).
- 입력: Notion 페이지 메타 + 원본 markdown.
- 출력: `### [미팅 로그 <version>] - <title> ... ---` 형식의 단일 미팅 로그 텍스트.
- 사용 모델: 호출자가 quota.get_model_for_subscription(subscription_type) 으로 결정.
  (라우트가 사용자 등급 알기 때문에 라우트 측 책임).

[왜 별도 파이프라인]
- CPS / PRD 처럼 그래프 저장 X → Neo4j 불필요
- 영속화 없음 — 결과는 FE 가 받아서 수정/등록 결정
- 짧은 LLM call (1회) — token tracking 만 wrap

[Neo4j 미사용]
PipelineContext 의 neo4j 필드는 사용 안 함. 파이프라인 함수가 ctx.gemini 만 호출.
호출자가 ctx 만들 때 neo4j 는 Neo4jClientProxy() 로 두지만 호출되지 않음.

[프롬프트 — app/prompts/notion_normalize.md]
표준 미팅 로그 포맷 가이드 + 회고록/명세서 fallback 지시 + 환각 차단 규칙.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.pipelines.base import (
    PipelineContext,
    canonicalize_meeting_content,
    strip_code_blocks,
)

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# LLM 호출 시 결정성을 위해 낮은 temperature. CPS 와 동일 정책 (_TEMPERATURE=0.2).
_TEMPERATURE = 0.2

# 입력 markdown 안전 한도. 너무 길면 LLM context 한도 초과 + 비용 폭발.
# 일반 노션 페이지 < 30000자. 100000자 cap (약 25k 토큰) — 그 이상은 truncate.
_MAX_INPUT_CHARS = 100_000


@dataclass(frozen=True)
class NotionNormalizeInput:
    """정형화 파이프라인 입력."""

    project_name: str
    version: str
    page_title: str
    page_url: str
    last_edited: str   # ISO8601 string, empty 가능
    original_markdown: str   # Notion → markdown 변환 결과 (포맷 전)


@dataclass(frozen=True)
class NotionNormalizeResult:
    """정형화 결과."""

    normalized_markdown: str
    truncated: bool         # 입력이 _MAX_INPUT_CHARS 넘어서 잘렸는지
    char_count: int         # normalized_markdown 길이


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


def _clean_output(raw: str) -> str:
    """
    LLM 출력 정리:
    - 앞뒤 ``` fence 제거 (혹시 LLM 이 펜스로 감싸도 안전)
    - 양 끝 공백 제거
    - 줄바꿈 정규화 (canonicalize 재사용)
    """
    if not raw:
        return ""
    out = strip_code_blocks(raw)
    out = canonicalize_meeting_content(out)
    return out


async def run_notion_normalize(
    ctx: PipelineContext,
    payload: NotionNormalizeInput,
) -> NotionNormalizeResult:
    """
    Notion 원본 markdown → 표준 미팅 로그 포맷 변환.

    Args:
        ctx: PipelineContext — gemini 만 사용 (neo4j 미사용).
              gemini 는 호출자가 등급별 모델로 만든 인스턴스 (TrackedGemini 권장).
        payload: 페이지 메타 + 원본 markdown.

    Returns:
        NotionNormalizeResult — 변환된 markdown + 메타.

    Raises:
        GeminiError: LLM quota/auth/transient 에러.
        ValueError: 출력이 비어있거나 형식 위반 (헤더 없음 등).
    """
    # 입력 markdown truncate — 비용 + context 한도 안전망.
    original = payload.original_markdown or ""
    truncated = False
    if len(original) > _MAX_INPUT_CHARS:
        original = original[:_MAX_INPUT_CHARS]
        truncated = True
        logger.warning(
            "notion_normalize: input truncated %d → %d chars (page='%s')",
            len(payload.original_markdown), _MAX_INPUT_CHARS, payload.page_title[:30],
        )

    prompt = _render(
        _load_prompt("notion_normalize.md"),
        project_name=payload.project_name,
        version=payload.version,
        page_title=payload.page_title,
        page_url=payload.page_url,
        last_edited=payload.last_edited,
        original_markdown=original,
    )

    result = await ctx.gemini.generate(prompt, temperature=_TEMPERATURE)
    text = (result.text or "") if hasattr(result, "text") else ""
    normalized = _clean_output(text)

    # 최소 sanity — 표준 헤더가 있는지. LLM 이 형식 어기면 ValueError.
    if not normalized:
        raise ValueError("notion_normalize: LLM 응답이 비어있습니다.")
    if "### [미팅 로그" not in normalized:
        # 헤더 누락 시 LLM 이 자유 형식으로 응답한 것 — 사용자에게 알려야 함.
        logger.warning(
            "notion_normalize: LLM 출력에 표준 헤더 없음 — preview head=%r",
            normalized[:120],
        )
        raise ValueError(
            "notion_normalize: 정형화 결과가 표준 포맷을 따르지 않습니다. 다시 시도해주세요."
        )

    return NotionNormalizeResult(
        normalized_markdown=normalized,
        truncated=truncated,
        char_count=len(normalized),
    )
