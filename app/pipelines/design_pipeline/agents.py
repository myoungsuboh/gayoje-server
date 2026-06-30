from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from app.pipelines.base import PipelineContext, generate_json_with_retry
from .schemas import SPACK_AGENT_SCHEMA, DDD_AGENT_SCHEMA, ARCHITECTURE_AGENT_SCHEMA, _TEMPERATURE

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


# ─── Stage 3: Spack Agent + Save ───────────────────────────────────────────────────────────────


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


async def call_spack_agent(ctx: PipelineContext, spack_input: str) -> Dict[str, Any]:
    """Stage: `Spack Agent`."""
    prompt = _render(_load_prompt("design_spack.md"), spack_input=spack_input)
    # [2026-05] structured output + strict retry. normalize_spack 이 검증 보강.
    parsed, _ = await generate_json_with_retry(
        ctx.gemini, prompt,
        temperature=_TEMPERATURE,
        response_schema=SPACK_AGENT_SCHEMA,
    )
    return parsed


async def call_ddd_agent(
    ctx: PipelineContext, ddd_input: str, spack_output_json: str
) -> Dict[str, Any]:
    """Stage: `DDD Agent`. (의존: Spack Agent 결과)"""
    prompt = _render(
        _load_prompt("design_ddd.md"),
        ddd_input=ddd_input,
        spack_output=spack_output_json,
    )
    parsed, _ = await generate_json_with_retry(
        ctx.gemini, prompt,
        temperature=_TEMPERATURE,
        response_schema=DDD_AGENT_SCHEMA,
    )
    return parsed


async def call_architecture_agent(
    ctx: PipelineContext,
    arch_input: str,
    spack_output_json: str,
    ddd_output_json: str,
) -> Dict[str, Any]:
    """Stage: `Architecture Agent`. (의존: Spack + DDD)"""
    prompt = _render(
        _load_prompt("design_architecture.md"),
        arch_input=arch_input,
        spack_output=spack_output_json,
        ddd_output=ddd_output_json,
    )
    parsed, _ = await generate_json_with_retry(
        ctx.gemini, prompt,
        temperature=_TEMPERATURE,
        response_schema=ARCHITECTURE_AGENT_SCHEMA,
    )
    return parsed
