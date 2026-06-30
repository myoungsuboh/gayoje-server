"""PRD 정확성 검증 — 2단계: LLM 정밀 대조.

[왜 2단계인가]
1단계(prd_fidelity.compare_fidelity)는 회의록 ↔ PRD 를 토큰 단위로 비교한다. 한글 형태소
분석 없이 정확 추출되는 신호(숫자·날짜·영문)만 보므로, 회의록의 잡담·날짜 나열·미팅로그 번호
까지 "누락"으로 세어 노이즈가 폭증했다(반영도가 비현실적으로 낮게 나옴).

2단계는 LLM 이 *제품적으로 중요한* 내용만 보고 누락/환각을 정밀 식별한다. 잡담·진행 메타는
무시하고, 기능 요구·정책·구체 수치·결정사항 위주로 판정한다. LLM 1회 호출 — 온디맨드(사용자
요청 시)로만 부른다.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from app.core.prompt_render import render_template
from app.pipelines.base import PipelineContext, generate_json_with_retry

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 프롬프트 폭주 방지 — 회의록·PRD 앞부분만 사용.
_MAX_MEETING = 40_000
_MAX_PRD = 30_000
# 핵심만 — 너무 많으면 사용자가 다시 압도된다(1단계의 실패 교훈).
_MAX_ITEMS = 12
_TEMPERATURE = 0.0

_SEVERITIES = {"high", "medium", "low"}
_SECTIONS = {"overview", "epic", "screen", "nfr"}

_VERIFY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "coverage_pct": {"type": "integer"},
        "summary": {"type": "string"},
        "missing": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string"},
                    "evidence": {"type": "string"},
                    "section": {"type": "string"},
                    "severity": {"type": "string"},
                },
                "required": ["point", "severity"],
            },
        },
        "hallucination": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string"},
                    "severity": {"type": "string"},
                },
                "required": ["point", "severity"],
            },
        },
    },
    "required": ["coverage_pct", "missing", "hallucination"],
}


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _clip_severity(v: Any) -> str:
    s = str(v or "").strip().lower()
    return s if s in _SEVERITIES else "medium"


def _sanitize_missing(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for it in raw[:_MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        point = str(it.get("point") or "").strip()
        if not point:
            continue
        section = str(it.get("section") or "").strip().lower()
        out.append(
            {
                "point": point[:300],
                "evidence": str(it.get("evidence") or "").strip()[:300],
                "section": section if section in _SECTIONS else "",
                "severity": _clip_severity(it.get("severity")),
            }
        )
    return out


def _sanitize_hall(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for it in raw[:_MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        point = str(it.get("point") or "").strip()
        if not point:
            continue
        out.append({"point": point[:300], "severity": _clip_severity(it.get("severity"))})
    return out


async def verify_fidelity_llm(
    ctx: PipelineContext, meeting_text: str, prd_text: str
) -> Dict[str, Any]:
    """원본 회의록 ↔ PRD 를 LLM 으로 정밀 대조해 핵심 누락/환각을 반환.

    Returns:
        { coverage_pct, summary, missing[{point,evidence,section,severity}],
          hallucination[{point,severity}] }
    """
    prompt = render_template(
        _load_prompt("prd_fidelity_verify.md"),
        meeting_text=(meeting_text or "")[:_MAX_MEETING],
        prd_text=(prd_text or "")[:_MAX_PRD],
    )
    parsed, _ = await generate_json_with_retry(
        ctx.gemini, prompt, temperature=_TEMPERATURE, response_schema=_VERIFY_SCHEMA,
    )
    if not isinstance(parsed, dict):
        parsed = {}

    try:
        pct = max(0, min(100, int(parsed.get("coverage_pct"))))
    except (TypeError, ValueError):
        pct = 0

    missing = _sanitize_missing(parsed.get("missing"))
    hall = _sanitize_hall(parsed.get("hallucination"))

    logger.info(
        "prd_fidelity_verify done: coverage=%d missing=%d hall=%d",
        pct, len(missing), len(hall),
    )
    return {
        "coverage_pct": pct,
        "summary": str(parsed.get("summary") or "").strip()[:500],
        "missing": missing,
        "hallucination": hall,
    }
