"""
improveSkill 파이프라인 — 사용자가 대충 적은 코딩 규칙(Skill) 초안을
AI 가 구체적·실행 가능한 규칙으로 다듬는다 (단건, 동기).

[설계 메모]
- trigger_fill 이 trigger_condition 한 줄만 생성하는 데 비해, improve 는
  이름·지시사항·적용조건·범위를 통째로 개선한다. "개똥처럼 적어도 보정"의 핵심.
- 단건 호출 — 사용자가 편집 중인 규칙 1개를 'AI 로 다듬기' 버튼으로 즉시 개선
  (배치 아님). FE 가 before/after 를 보여주고 사용자가 검토 후 적용한다.
- **사용자 의도 보존이 최우선** (프롬프트 규칙 1). LLM 결과가 비거나 깨지면
  원본을 그대로 반환해 'AI 가 사용자 입력을 망치는' 일을 막는다 (graceful fallback).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from app.pipelines.base import (
    PipelineContext,
    generate_json_with_retry,
    strip_template_placeholders,
)

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# LLM 입력 token 한도 보호 — instructions 합산 슬라이스.
_MAX_INSTRUCTIONS_CHARS = 4000


# ─── Structured Output Schema (결정성 강화) ─────────────────────────
_IMPROVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "improved_name": {"type": "string"},
        "improved_scope": {"type": "string"},
        "improved_trigger_condition": {"type": "string"},
        "improved_instructions": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
    },
    "required": ["improved_name", "improved_instructions"],
}


# ─── Domain types ───────────────────────────────────────────────


@dataclass(frozen=True)
class SkillImproveInput:
    """편집 중인 규칙 1개의 초안 (엉성하거나 일부 비어 있을 수 있음)."""

    name: str
    scope: str = ""
    trigger_condition: str = ""
    instructions: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)


@dataclass
class SkillImproveResult:
    name: str
    scope: str
    trigger_condition: str
    instructions: List[str]
    explanation: str
    improved: bool  # True = LLM 개선 적용, False = fallback(원본 유지)
    meta: Dict[str, Any] = field(default_factory=dict)


# ─── helpers ────────────────────────────────────────────────────


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # single-pass 렌더로 통일 (placeholder 주입 방지). trigger_fill 과 동일 패턴.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


def _build_prompt(template: str, skill: SkillImproveInput) -> str:
    instructions_text = "\n".join(f"- {i}" for i in (skill.instructions or []))
    instructions_text = instructions_text[:_MAX_INSTRUCTIONS_CHARS]
    return _render(
        template,
        name=skill.name or "(이름 없음)",
        scope=skill.scope or "(범위 미지정)",
        trigger_condition=skill.trigger_condition or "(미지정)",
        # cat: 내부 마커는 LLM 프롬프트에서 제외(create_md_pipeline visible_tags 와 동일 규약).
        tags=", ".join(t for t in (skill.tags or []) if not (isinstance(t, str) and t.startswith("cat:"))) or "(태그 없음)",
        instructions=instructions_text or "(세부 규칙 없음)",
    )


def _clean_str(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return strip_template_placeholders(raw).strip()


def _clean_list(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = strip_template_placeholders(item).strip()
        if cleaned:
            out.append(cleaned)
    return out


def _fallback(skill: SkillImproveInput, reason: str) -> SkillImproveResult:
    """LLM 실패 시 원본을 그대로 반환 — AI 가 사용자 입력을 망치지 않게."""
    logger.warning("skill_improve fallback (%s)", reason)
    return SkillImproveResult(
        name=skill.name,
        scope=skill.scope,
        trigger_condition=skill.trigger_condition,
        instructions=list(skill.instructions or []),
        explanation="",
        improved=False,
        meta={"fallback": reason},
    )


# ─── End-to-end orchestrator ────────────────────────────────────


async def run_skill_improve_pipeline(
    ctx: PipelineContext, payload: SkillImproveInput
) -> SkillImproveResult:
    """단건 규칙 초안 → LLM 개선. 실패 시 원본 보존(fallback)."""
    template = _load_prompt("skill_improve.md")
    prompt = _build_prompt(template, payload)

    parsed, _ = await generate_json_with_retry(
        ctx.gemini,
        prompt,
        temperature=0.3,
        response_schema=_IMPROVE_SCHEMA,
    )
    if not isinstance(parsed, dict):
        return _fallback(payload, "unparseable")

    instructions = _clean_list(parsed.get("improved_instructions"))
    # 핵심 산출물(지시사항)이 비면 개선 실패로 간주 — 원본 유지.
    if not instructions:
        return _fallback(payload, "empty_instructions")

    return SkillImproveResult(
        name=_clean_str(parsed.get("improved_name")) or payload.name,
        scope=_clean_str(parsed.get("improved_scope")) or payload.scope,
        trigger_condition=(
            _clean_str(parsed.get("improved_trigger_condition")) or payload.trigger_condition
        ),
        instructions=instructions,
        explanation=_clean_str(parsed.get("explanation")),
        improved=True,
        meta={
            "originalInstructionCount": len(payload.instructions or []),
            "improvedInstructionCount": len(instructions),
        },
    )
