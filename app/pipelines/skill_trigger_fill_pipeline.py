"""
fillSkillTriggers 파이프라인 — trigger_condition 이 빈 Skill 들에 대해 LLM 으로
적용 조건(trigger_condition)을 자동 생성.

[스테이지 매핑]
- Prepare Input         → `_split_targets` (빈 trigger 만 골라냄, 채워진 건 보존)
- Trigger Fill AI Agent → `call_trigger_filler` (prompts/skill_trigger_fill.md)
  대상 skill 마다 1 LLM 호출 — 서로 독립적이라 `asyncio.gather` 로 병렬 실행.
  (create_md_pipeline 의 3-way gather 와 동일 패턴, 동적 N개로 확장)
- Merge & Respond       → 원본 순서 유지하며 생성된 trigger 병합

[병렬화 결정]
각 skill 의 trigger 생성은 서로 독립 → `asyncio.gather` 로 명시적 병렬.
토큰 합계는 동일하지만 wall time 은 직렬 대비 ~1/N.

[건너뜀 정책]
trigger_condition 이 이미 비어있지 않으면 LLM 을 호출하지 않고 그대로 통과.
LLM 비용·토큰을 아끼고 사용자가 손으로 적은 trigger 를 덮어쓰지 않기 위함.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.pipelines.base import (
    PipelineContext,
    generate_json_with_retry,
    strip_template_placeholders,
)

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# LLM 입력 token 한도 보호용 — instructions 합산 슬라이스.
_MAX_INSTRUCTIONS_CHARS = 4000

# [2026-05-29] LLM 동시 호출 상한 — skill 이 많을 때 gather 가 전부 한 번에 던져
# Gemini rate limit 을 유발하지 않도록 제한 (api_spec_autofill 과 동일 방어).
_MAX_LLM_CONCURRENCY = 5


# ─── Structured Output Schema (결정성 강화) ─────────────────────────
_TRIGGER_FILL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "trigger_condition": {"type": "string"},
    },
    "required": ["trigger_condition"],
}


# ─── Domain types ───────────────────────────────────────────────


@dataclass(frozen=True)
class SkillTriggerInput:
    """trigger 생성 대상 한 항목 (저장 스키마 SkillInput 의 부분집합)."""

    id: str
    name: str
    scope: str = ""
    trigger_condition: str = ""
    instructions: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class TriggerFillInput:
    skills: List[SkillTriggerInput]


@dataclass
class FilledTrigger:
    id: str
    trigger_condition: str
    generated: bool  # True = LLM 으로 새로 생성, False = 기존 값 유지(건너뜀)


@dataclass
class TriggerFillResult:
    skills: List[FilledTrigger] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


# ─── Stage 1: split targets ────────────────────────────────────


def _has_trigger(s: SkillTriggerInput) -> bool:
    """trigger_condition 이 의미있는 값으로 채워졌는지 (공백만 있으면 빈 것으로 간주)."""
    return bool(s.trigger_condition and s.trigger_condition.strip())


def _split_targets(
    skills: List[SkillTriggerInput],
) -> tuple[List[SkillTriggerInput], List[SkillTriggerInput]]:
    """(생성 대상=빈 trigger, 건너뜀=이미 채워짐) 으로 분리. 원본 순서는 호출자가 유지."""
    targets = [s for s in skills if not _has_trigger(s)]
    skipped = [s for s in skills if _has_trigger(s)]
    return targets, skipped


# ─── Stage 2: LLM call (per skill) ─────────────────────────────


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


def _build_prompt(template: str, skill: SkillTriggerInput) -> str:
    instructions_text = "\n".join(f"- {i}" for i in (skill.instructions or []))
    instructions_text = instructions_text[:_MAX_INSTRUCTIONS_CHARS]
    return _render(
        template,
        name=skill.name or "(이름 없음)",
        scope=skill.scope or "(범위 미지정)",
        # cat: 내부 마커는 LLM 프롬프트에서 제외(create_md_pipeline visible_tags 와 동일 규약).
        tags=", ".join(t for t in (skill.tags or []) if not (isinstance(t, str) and t.startswith("cat:"))) or "(태그 없음)",
        instructions=instructions_text or "(세부 규칙 없음)",
    )


async def call_trigger_filler(
    ctx: PipelineContext, template: str, skill: SkillTriggerInput
) -> FilledTrigger:
    """
    단일 skill 에 대해 trigger_condition 생성.

    generate_json_with_retry 로 첫 시도 unparseable 이면 strict 재시도.
    두 시도 모두 실패(빈 dict)하거나 빈 문자열이면 trigger 를 비운 채(generated=False)
    반환 — 한 skill 의 LLM 실패가 전체 배치를 깨지 않도록 한다.
    """
    prompt = _build_prompt(template, skill)
    parsed, _ = await generate_json_with_retry(
        ctx.gemini,
        prompt,
        temperature=0.2,
        response_schema=_TRIGGER_FILL_SCHEMA,
    )
    raw = parsed.get("trigger_condition") if isinstance(parsed, dict) else None
    trigger = strip_template_placeholders(raw.strip()) if isinstance(raw, str) else ""
    if not trigger:
        logger.warning(
            "trigger_fill: skill=%s LLM 결과 비어있음 — 건너뜀 처리", skill.id
        )
        return FilledTrigger(id=skill.id, trigger_condition="", generated=False)
    return FilledTrigger(id=skill.id, trigger_condition=trigger, generated=True)


# ─── End-to-end orchestrator ────────────────────────────────────


async def run_trigger_fill_pipeline(
    ctx: PipelineContext, payload: TriggerFillInput
) -> TriggerFillResult:
    """
    split → (빈 trigger 만) N개 LLM 병렬 → 건너뛴 것과 병합, 원본 순서 유지.
    """
    skills = payload.skills or []
    logger.info(
        "trigger_fill start: total=%d key=%s",
        len(skills),
        ctx.idempotency_key,
    )

    targets, _skipped = _split_targets(skills)

    # 생성 대상이 없으면 LLM 호출 0 — 전부 기존 값 그대로 통과.
    generated_map: Dict[str, FilledTrigger] = {}
    if targets:
        template = _load_prompt("skill_trigger_fill.md")
        # 각 대상 skill 1 LLM 호출 — 독립적이라 병렬. 단 동시성을 제한해
        # Gemini rate limit 회피 (skill 이 많을 때 한꺼번에 던지면 GeminiError).
        sem = asyncio.Semaphore(_MAX_LLM_CONCURRENCY)

        async def _run_one(s: SkillTriggerInput) -> FilledTrigger:
            async with sem:
                return await call_trigger_filler(ctx, template, s)

        filled = await asyncio.gather(*(_run_one(s) for s in targets))
        generated_map = {f.id: f for f in filled}

    # 원본 순서 유지하며 병합 — 대상은 생성 결과, 나머지는 기존 trigger 보존.
    out: List[FilledTrigger] = []
    for s in skills:
        gen = generated_map.get(s.id)
        if gen is not None:
            out.append(gen)
        else:
            out.append(
                FilledTrigger(
                    id=s.id,
                    trigger_condition=s.trigger_condition or "",
                    generated=False,
                )
            )

    generated_count = sum(1 for f in out if f.generated)
    return TriggerFillResult(
        skills=out,
        meta={
            "total": len(skills),
            "targetCount": len(targets),
            "skippedCount": len(skills) - len(targets),
            "generatedCount": generated_count,
        },
    )
