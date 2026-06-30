from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.prompt_render import render_template
from app.pipelines.base import PipelineContext, generate_json_with_retry
from app.pipelines.lint_evidence import FileSample
from app.pipelines.lint_pipeline.types import _LINT_RESIDUAL_SCHEMA, _RESIDUAL_LLM_BUDGET
from app.service.lint_repository import LintCase, LintCaseRule, LintEvidence

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


# [2026-05 보안] 이전엔 순차 str.replace 기반 _render 였으나, Lint 는 임의의 외부
# GitHub 레포 본문(samples_json)을 프롬프트에 넣으므로 placeholder 주입에 취약했다.
# single-pass 렌더(app.core.prompt_render)로 통일 — 치환된 값은 재스캔되지 않는다.
_render = render_template


def _shrink_samples_for_llm(
    samples: List[FileSample], budget: int = _RESIDUAL_LLM_BUDGET
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    used = 0
    for s in samples:
        if used >= budget:
            break
        remaining = budget - used
        if remaining < 200:
            break
        body = s.content if len(s.content) <= remaining else s.content[:remaining]
        out.append({"path": s.path, "size": s.size, "content": body})
        used += len(body)
    return out


def _build_item_payload(it: Dict[str, Any]) -> Dict[str, Any]:
    """residual 항목 한 건을 LLM 프롬프트용 dict 로 변환.

    Rules (Skill) 카테고리에서 evaluator 가 넣어준 ``instructions`` 가 있으면
    함께 전달해 LLM 이 본문 의미를 코드와 대조할 수 있게 한다.
    [2026-06] 기획 카테고리의 ``screen_path`` (route) / ``story_description``
    (스토리 본문) 도 같은 방식 — 한국어 항목의 의미 매칭에 필요한 추가 맥락.
    """
    payload: Dict[str, Any] = {
        "category_idx": it["category_idx"],
        "rule_idx": it["rule_idx"],
        "rule": it["rule"],
        "description": it["description"],
        "hint": it.get("hint", ""),
    }
    for key in ("instructions", "screen_path", "story_description"):
        if it.get(key):
            payload[key] = it[key]
    return payload


async def _residual_llm_pass(
    ctx: PipelineContext,
    residual_items: List[Dict[str, Any]],
    samples: List[FileSample],
) -> List[Dict[str, Any]]:
    if not residual_items:
        return []

    samples_for_llm = _shrink_samples_for_llm(samples)
    items_payload = [_build_item_payload(it) for it in residual_items]

    try:
        prompt = _render(
            _load_prompt("lint_residual.md"),
            items_json=json.dumps(items_payload, ensure_ascii=False, indent=2),
            samples_json=json.dumps(samples_for_llm, ensure_ascii=False, indent=2),
        )
    except FileNotFoundError:
        logger.warning("lint_residual.md not found — skipping residual LLM pass")
        return []

    try:
        parsed, _ = await generate_json_with_retry(
            ctx.gemini, prompt,
            temperature=0.1,
            response_schema=_LINT_RESIDUAL_SCHEMA,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("residual LLM call failed: %s", e)
        return []

    raw = parsed.get("verdicts") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        return []

    out: List[Dict[str, Any]] = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        try:
            ci = int(v.get("category_idx"))
            ri = int(v.get("rule_idx"))
        except (TypeError, ValueError):
            continue
        applied = bool(v.get("applied"))
        reason = str(v.get("reason") or "")
        ev_file = v.get("evidence_file")
        ev_line_raw = v.get("evidence_line")
        try:
            ev_line = int(ev_line_raw) if ev_line_raw is not None else 0
        except (TypeError, ValueError):
            ev_line = 0
        out.append({
            "category_idx": ci,
            "rule_idx": ri,
            "applied": applied,
            "reason": reason[:300],
            "evidence_file": str(ev_file) if isinstance(ev_file, str) else "",
            "evidence_line": ev_line,
        })
    return out


def _apply_residual_verdicts(
    cases: List[LintCase],
    verdicts: List[Dict[str, Any]],
    samples: List[FileSample],
) -> None:
    sample_by_path = {s.path: s for s in samples}
    for v in verdicts:
        ci = v["category_idx"]
        ri = v["rule_idx"]
        if ci < 0 or ci >= len(cases):
            continue
        rules = cases[ci].rules
        if ri < 0 or ri >= len(rules):
            continue
        rule = rules[ri]
        ev_file = v.get("evidence_file") or ""
        ev_line = v.get("evidence_line") or 0

        if v["applied"]:
            if ev_file and ev_file in sample_by_path:
                content = sample_by_path[ev_file].content
                lines = content.splitlines()
                snippet = ""
                if 1 <= ev_line <= len(lines):
                    snippet = lines[ev_line - 1].strip()[:200]
                if snippet:
                    rule.applied = True
                    rule.detection_method = "llm"
                    rule.evidence = [
                        LintEvidence(
                            file=ev_file,
                            line=ev_line,
                            snippet=snippet,
                            kind="llm_quoted",
                        )
                    ]
                    continue
            rule.applied = False
            rule.detection_method = "llm"
            rule.evidence = []
        else:
            rule.applied = False
            rule.detection_method = "llm"
            rule.evidence = []
