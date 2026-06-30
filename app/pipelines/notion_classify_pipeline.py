"""
Notion 페이지 분류 파이프라인 — 3-Tier 시스템의 첫 단계.

[목적]
정형화 LLM 을 호출하기 *전*에 페이지가 회의록인지 판단. 회의록 아닌 경우를
명확히 거부해서 CPS/PRD 단계의 환각/품질 저하를 사전 차단.

[Tier]
- ACCEPT: meeting_log → 정형화 + 등록 정상 진행
- WARN:   retrospective / spec_doc → 사용자에게 경고 후 진행 가능
- BLOCK:  task_request / general_doc / unknown → 라우트가 400 으로 차단

[설계]
- 1회 LLM call. JSON 출력 (type / confidence / reason).
- 입력 markdown 너무 길면 앞부분 8KB 만 사용 (분류 신호는 헤더/도입부에 집중됨).
- temperature=0 — 같은 입력에 같은 분류 결과 보장 (정형화의 0.2 와 별개 정책).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.pipelines.base import (
    PipelineContext,
    generate_json_with_retry,
)

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 분류는 결정성이 매우 중요 — 같은 페이지를 두 번 변환 시도해도 같은 판정.
_TEMPERATURE = 0.0

# 분류는 페이지의 첫 부분만 봐도 충분 (회의록인지 명세서인지 헤더/도입부에서 판별).
# 8KB ≈ 2k 토큰 — 충분히 작아 cost 부담 무.
_MAX_CLASSIFY_CHARS = 8_000

ContentType = Literal[
    "meeting_log",
    "retrospective",
    "spec_doc",
    "task_request",
    "general_doc",
    "unknown",
]

Tier = Literal["ACCEPT", "WARN", "BLOCK"]


# 카테고리 → Tier 매핑. LLM 출력에 의존하지 않고 BE 측에서 결정 (보안/정책).
_TIER_MAP: dict[str, Tier] = {
    "meeting_log": "ACCEPT",
    "retrospective": "WARN",
    "spec_doc": "WARN",
    "task_request": "BLOCK",
    "general_doc": "BLOCK",
    "unknown": "BLOCK",
}

_ALL_TYPES = set(_TIER_MAP.keys())


@dataclass(frozen=True)
class NotionClassifyInput:
    page_title: str
    markdown: str


@dataclass(frozen=True)
class NotionClassifyResult:
    type: ContentType
    confidence: float
    reason: str
    tier: Tier


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


async def run_notion_classify(
    ctx: PipelineContext,
    payload: NotionClassifyInput,
) -> NotionClassifyResult:
    """
    Notion 페이지 분류. 항상 결과 반환 (LLM 오류 시 'unknown' fallback).

    Args:
        ctx: PipelineContext — gemini 만 사용.
        payload: 페이지 제목 + markdown (앞부분만 사용).

    Returns:
        NotionClassifyResult — type/confidence/reason + 계산된 tier.

    [실패 정책]
    LLM JSON 파싱 실패 / 응답 비정상 → type='unknown', confidence=0.0 fallback.
    BLOCK Tier 라 자연스럽게 라우트가 거부 → 사용자는 다시 시도 안내.
    """
    md = payload.markdown or ""
    if len(md) > _MAX_CLASSIFY_CHARS:
        md = md[:_MAX_CLASSIFY_CHARS]

    prompt = _render(
        _load_prompt("notion_classify.md"),
        page_title=payload.page_title or "(제목 없음)",
        markdown=md,
    )

    parsed, _ = await generate_json_with_retry(
        ctx.gemini, prompt, temperature=_TEMPERATURE,
    )

    raw_type = str(parsed.get("type", "")).strip().lower()
    confidence_raw = parsed.get("confidence", 0.0)
    reason = str(parsed.get("reason", "")).strip() or "(근거 미제공)"

    # 정상화
    if raw_type not in _ALL_TYPES:
        logger.warning(
            "notion_classify: 비정상 type=%r — unknown 으로 fallback (reason=%r)",
            raw_type, reason[:80],
        )
        raw_type = "unknown"
        reason = "분류 LLM 응답이 비정상 — 안전을 위해 거부됩니다."

    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    # clamp
    if confidence < 0.0:
        confidence = 0.0
    elif confidence > 1.0:
        confidence = 1.0

    return NotionClassifyResult(
        type=raw_type,  # type: ignore[arg-type]
        confidence=confidence,
        reason=reason,
        tier=_TIER_MAP[raw_type],
    )
