"""PRD autofix — lint finding 들을 AI가 기존 맥락으로 자동 보완 (하이브리드).

[배경]
PRD lint(prd_lint/linter.py)는 문제를 찾아 "직접 이렇게 고쳐라"는 hint 를 줄 뿐,
수정 부담을 사용자에게 떠넘겼다. 이 파이프라인은 그 finding 들을 입력으로 받아:

  1) PRD 본문 + CPS + Screens 참조 등 **이미 있는 맥락**으로 최대한 자동 보완.
  2) 근거가 없어 안전하게 채울 수 없는 항목은 지어내지 않고 `needs_input` 으로 반환
     → FE 가 그 항목만 AI 인터뷰로 수집 (interview 파이프라인 재사용).

LLM 1회 호출. 결과는 preview 만 — 저장은 FE 가 사용자 승인 후 PATCH /api/v2/prd 로.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.prompt_render import render_template
from app.pipelines.base import PipelineContext, generate_json_with_retry
from app.pipelines.prd_lint import lint_prd
from app.service import query_repository as q

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


_AUTOFIX_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "improved_prd": {"type": "string"},
        "needs_input": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["topic", "question"],
            },
        },
    },
    "required": ["improved_prd"],
}

# [2026-05] 보완은 "필요한 곳만 최소 변경" 이 목표 — 결정적으로 동작하도록 0.
# (이전 0.2 에서 멀쩡한 Overview/NFR 까지 재작성하는 over-reach 가 관찰됨.)
_TEMPERATURE = 0.0
# CPS 맥락은 보조 근거 — 프롬프트 폭주 방지를 위해 앞부분만.
_MAX_CPS_CHARS = 12_000
# 원본 회의록 — 빠진 핵심을 채울 1차 근거. 토큰 폭주 방지를 위해 앞부분만.
_MAX_MEETING_CHARS = 24_000
# [자기정제] lint 점수가 '오르는 한' 남은 finding 만 재투입하는 최대 총 패스 수.
# 비용(회의록 24k+CPS 12k 큰 프롬프트)을 고려해 보수적으로 2(원본 1 + 재정제 1).
# 점수 정체/하락·변화 없음·잔여 이슈 없음이면 즉시 중단(어휘계약↔lint 정규식 정합
# 확인됨 — 무한 미수렴 없음). 첫 패스는 기존 동작 보존, 추가 패스만 점수 게이팅.
_AUTOFIX_MAX_PASSES = 2


@dataclass
class PrdAutofixResult:
    project_name: str
    current_markdown: str
    improved_markdown: str
    before_score: float
    after_score: float
    before_issues: List[Dict[str, Any]] = field(default_factory=list)
    after_issues: List[Dict[str, Any]] = field(default_factory=list)
    needs_input: List[Dict[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.improved_markdown.strip() != self.current_markdown.strip()


def _issues_to_dicts(report) -> List[Dict[str, Any]]:
    return [
        {
            "code": i.code,
            "severity": i.severity,
            "message": i.message,
            "hint": i.hint,
            "detail": dict(i.detail),
        }
        for i in report.issues
    ]


def _format_issues_for_prompt(report) -> str:
    import json

    return json.dumps(
        [
            {"code": i.code, "severity": i.severity, "message": i.message, "hint": i.hint}
            for i in report.issues
        ],
        ensure_ascii=False,
        indent=2,
    )


def _sanitize_needs_input(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        topic = str(it.get("topic") or "").strip()
        question = str(it.get("question") or "").strip()
        if topic and question:
            out.append({"topic": topic[:120], "question": question[:400]})
    return out


async def _autofix_once(
    ctx: PipelineContext, md: str, report, *, meeting_md: str, cps_md: str
) -> tuple[str, List[Dict[str, str]]]:
    """단일 보완 패스 — 현재 md + report.issues 로 LLM 1회. (improved, needs_input) 반환.

    improved 가 빈 문자열이면 LLM 이 형식을 못 지킨 것(상위에서 원본 보존·중단).
    """
    prompt = render_template(
        _load_prompt("prd_autofix.md"),
        prd_markdown=md,
        meeting_markdown=meeting_md or "(없음)",
        cps_markdown=cps_md or "(없음)",
        issues_json=_format_issues_for_prompt(report),
    )
    parsed, _ = await generate_json_with_retry(
        ctx.gemini, prompt,
        temperature=_TEMPERATURE,
        response_schema=_AUTOFIX_SCHEMA,
    )
    improved = ""
    needs_input: List[Dict[str, str]] = []
    if isinstance(parsed, dict):
        improved = str(parsed.get("improved_prd") or "").strip()
        needs_input = _sanitize_needs_input(parsed.get("needs_input"))
    return improved, needs_input


async def run_prd_autofix(
    ctx: PipelineContext,
    project_name: str,
    *,
    current_markdown: Optional[str] = None,
) -> Optional[PrdAutofixResult]:
    """PRD lint finding 들을 AI 로 자동 보완.

    Args:
        current_markdown: FE 가 화면에 띄운 현재 PRD. 없으면 master PRD 를 조회.

    Returns:
        PrdAutofixResult — preview(저장 안 함). 대상 PRD 가 없으면 None (404 매핑).
    """
    md = current_markdown
    if md is None:
        master = await q.get_master_prd(project_name)
        md = master.prd_content if master else None
    if not md or not md.strip():
        return None

    before = lint_prd(md)
    before_issues = _issues_to_dicts(before)

    # 보완할 게 없으면(이미 깨끗) LLM 호출 없이 그대로 반환 — 토큰 절약.
    if not before.issues:
        return PrdAutofixResult(
            project_name=project_name,
            current_markdown=md,
            improved_markdown=md,
            before_score=before.score,
            after_score=before.score,
            before_issues=before_issues,
            after_issues=before_issues,
            needs_input=[],
        )

    # 맥락은 한 번만 조회해 재정제 패스에서 재사용 (CPS·회의록 모두 best-effort).
    cps_md = ""
    try:
        cps_master = await q.get_master_cps(project_name)
        if cps_master and cps_master.content:
            cps_md = cps_master.content[:_MAX_CPS_CHARS]
    except Exception as e:  # noqa: BLE001 — CPS 맥락은 best-effort
        logger.warning("prd_autofix: CPS 조회 실패 (project=%s): %s", project_name, e)

    # 원본 회의록 (1차 근거) — '회의록엔 있는데 PRD 에 빠진 핵심'을 채우는 근거.
    meeting_md = ""
    try:
        meeting_md = (await q.get_all_meeting_content(project_name) or "")[:_MAX_MEETING_CHARS]
    except Exception as e:  # noqa: BLE001 — 회의록 맥락은 best-effort
        logger.warning("prd_autofix: 회의록 조회 실패 (project=%s): %s", project_name, e)

    # [score-gated 자기정제] 첫 패스는 기존 동작(LLM 1회, 결과를 미리보기로 채택).
    # 이후 lint 점수가 '오르는 한' 남은 finding 만 재투입해 마저 정리한다. 추가 패스는
    # lint 점수 단조증가 + 실제 변경일 때만 채택, 정체/하락·변화없음·이슈없음이면 즉시
    # 중단(직전 결과 유지) → 회귀 없이 잔여 이슈만 추가 제거. (어휘계약↔lint 정규식 정합
    # 확인됨 — 무한 미수렴 없음.)
    cur_md, cur_report = md, before
    needs_input: List[Dict[str, str]] = []
    for _pass in range(_AUTOFIX_MAX_PASSES):
        improved, ni = await _autofix_once(
            ctx, cur_md, cur_report, meeting_md=meeting_md, cps_md=cps_md
        )
        if not improved:  # LLM 형식 실패 → 원본/직전 보존, 중단
            if _pass == 0:
                logger.warning("prd_autofix: LLM 이 improved_prd 를 못 냄 (project=%s)", project_name)
            break
        after = lint_prd(improved)
        changed = improved.strip() != cur_md.strip()
        if _pass == 0:
            cur_md, cur_report, needs_input = improved, after, ni  # 첫 패스: 기존 동작 보존
        elif after.score > cur_report.score and changed:
            cur_md, cur_report, needs_input = improved, after, ni  # 재정제: 단조증가+변화시만
        else:
            break  # 정체/하락/무변화 → 직전 결과 유지, 중단
        # 더 돌릴 이유 없으면 중단: 변화없음 / 이슈없음 / 첫 패스가 개선 없음(회귀·정체)
        if not changed or not cur_report.issues or cur_report.score <= before.score:
            break

    logger.info(
        "prd_autofix done: project=%s before=%.2f after=%.2f needs_input=%d changed=%s",
        project_name, before.score, cur_report.score,
        len(needs_input), cur_md.strip() != md.strip(),
    )

    return PrdAutofixResult(
        project_name=project_name,
        current_markdown=md,
        improved_markdown=cur_md,
        before_score=before.score,
        after_score=cur_report.score,
        before_issues=before_issues,
        after_issues=_issues_to_dicts(cur_report),
        needs_input=needs_input,
    )
