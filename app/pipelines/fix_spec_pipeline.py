"""
generateFixSpec — Lint 실패 항목 + 전체 명세를 LLM 에 주고 한국어 마크다운
수정 지시서 생성. 다른 AI 어시스턴트(Claude/Gemini/Cursor) 가 별도 컨텍스트
없이 작업할 수 있는 self-contained 한 문서.

[스테이지 매핑]
- Parse Lint Failures → `_parse_failures`
- Has Failures (IF)   → `totalFailed == 0` 시 100% 메시지 즉시 반환
- Get Full Spec       → `_fetch_full_spec`
- Build Fix Context   → `_build_context`
- Fix Spec AI Agent   → `call_fix_spec_agent` (prompts/fix_spec.md)
- Format Fix Spec     → `_format`

[의미 매핑]
`Get Full Spec` 단계는 `:Skill` 노드를 'rules' 로 매핑 (lint_pipeline 과 동일).
Skill.instructions (List[str]) 본문도 함께 가져와 fix_spec LLM 이 규칙의 실제
요구사항까지 보고 수정 지시서를 만들 수 있게 한다.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.pipelines.base import PipelineContext, strip_code_blocks
from app.pipelines.design_validator.api_payload import decode_apis_payload
from app.pipelines.design_validator.arch_detail import decode_services_detail
from app.pipelines.design_validator.attributes import decode_entities_attributes
from app.pipelines.design_validator.ddd_detail import (
    decode_aggregates_detail,
    decode_domain_entities_detail,
    decode_domain_events_detail,
)

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

_GITHUB_OWNER_REPO_RE = re.compile(
    r"github\.com[/:]([^/]+)/([^/?#]+)", re.IGNORECASE
)


@dataclass(frozen=True)
class FixSpecInput:
    project_name: str
    github_url: str
    lint_result: Dict[str, Any]  # 클라이언트가 전달한 lint 결과 원본


@dataclass
class FixSpecResult:
    success: bool
    markdown: Optional[str] = None
    filename: Optional[str] = None
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


# ─── Stage 1: parse failures ───────────────────────────────────


def _parse_failures(payload: FixSpecInput) -> Dict[str, Any]:
    clean_url = (
        (payload.github_url or "")
        .strip()
        .rstrip("/")
    )
    clean_url = re.sub(r"\.git$", "", clean_url, flags=re.IGNORECASE)

    owner, repo = "", ""
    m = _GITHUB_OWNER_REPO_RE.search(clean_url)
    if m:
        owner, repo = m.group(1), re.sub(
            r"\.git$", "", m.group(2), flags=re.IGNORECASE
        )

    cases = payload.lint_result.get("cases") or []
    failed_by_category: List[Dict[str, Any]] = []
    for c in cases:
        if not isinstance(c, dict):
            continue
        failed = [
            {"rule": r.get("rule", ""), "description": r.get("description", "")}
            for r in (c.get("rules") or [])
            if isinstance(r, dict) and not r.get("applied")
        ]
        if failed:
            failed_by_category.append(
                {
                    "category": c.get("title", ""),
                    "convergence": c.get("convergence", 0),
                    "failedRules": failed,
                }
            )

    total_failed = sum(len(c["failedRules"]) for c in failed_by_category)

    return {
        "projectName": payload.project_name,
        "githubUrl": clean_url,
        "owner": owner,
        "repo": repo,
        "score": int(payload.lint_result.get("score") or 0),
        "totalFailed": total_failed,
        "failedByCategory": failed_by_category,
        "hasFailures": total_failed > 0,
    }


# ─── Stage 2: fetch full spec ──────────────────────────────────


_GET_FULL_SPEC_CYPHER = """\
MATCH (n)
WHERE n.project = $project
  AND (n:API OR n:Entity OR n:Policy
       OR n:BoundedContext OR n:Aggregate OR n:DomainEntity OR n:DomainEvent
       OR n:ArchService OR n:ArchDatabase OR n:Skill
       OR n:Story OR n:Screen)
RETURN
  [x IN collect(DISTINCT n) WHERE x:API |
    {id: x.id, name: x.name, description: x.description, endpoint: x.endpoint, method: x.method,
     path_params: x.path_params, query_params: x.query_params,
     request_body: x.request_body, response_body: x.response_body,
     error_cases: x.error_cases, auth: x.auth}] AS apis,
  [x IN collect(DISTINCT n) WHERE x:Entity |
    {id: x.id, name: x.name, attributes: x.attributes}] AS entities,
  [x IN collect(DISTINCT n) WHERE x:Policy |
    {id: x.id, name: x.name, description: x.description}] AS policies,
  [x IN collect(DISTINCT n) WHERE x:BoundedContext |
    {id: x.id, name: x.name, description: x.description}] AS contexts,
  [x IN collect(DISTINCT n) WHERE x:Aggregate |
    {id: x.id, name: x.name, description: x.description,
     invariants: x.invariants}] AS aggregates,
  [x IN collect(DISTINCT n) WHERE x:DomainEntity |
    {id: x.id, name: x.name, description: x.description,
     attributes: x.attributes}] AS domain_entities,
  [x IN collect(DISTINCT n) WHERE x:DomainEvent |
    {id: x.id, name: x.name, description: x.description,
     payload_fields: x.payload_fields}] AS domain_events,
  [x IN collect(DISTINCT n) WHERE x:ArchService |
    {id: x.id, name: x.name, tech_stack: x.tech_stack, description: x.description,
     deployment: x.deployment, external_dependencies: x.external_dependencies}] AS services,
  [x IN collect(DISTINCT n) WHERE x:ArchDatabase |
    {id: x.id, name: x.name, tech_stack: x.tech_stack}] AS databases,
  [x IN collect(DISTINCT n) WHERE x:Skill |
    {id: x.id, name: x.name, description: x.scope, category: x.priority, severity: x.priority,
     instructions: x.instructions, tags: [t IN COALESCE(x.tags, []) WHERE NOT t STARTS WITH 'cat:']}] AS rules,
  // [2026-06 기획 카테고리] lint 5번째 케이스의 실패 항목(story:/screen:)에 대해
  // 지시서가 제목뿐 아니라 본문/route 까지 보고 구체적 가이드를 쓰게 한다.
  [x IN collect(DISTINCT n) WHERE x:Story |
    {id: x.id, name: x.name, description: x.description}] AS stories,
  [x IN collect(DISTINCT n) WHERE x:Screen |
    {id: x.id, name: x.name, path: x.path, description: x.description}] AS screens
"""


async def _fetch_full_spec(
    ctx: PipelineContext, project: str
) -> Dict[str, Any]:
    records = await ctx.neo4j.run_cypher(_GET_FULL_SPEC_CYPHER, {"project": project})
    row = records[0] if records else {}
    return {
        # [A-2 — 2026-05-25] API payload 4개 필드 객체 복원.
        "apis": decode_apis_payload(row.get("apis") or []),
        # [A-1 — 2026-05-25] Neo4j JSON string → 객체 list 복원.
        "entities": decode_entities_attributes(row.get("entities") or []),
        "policies": row.get("policies") or [],
        "contexts": row.get("contexts") or [],
        # [D-1 — 2026-05-25] DDD detail 객체 복원.
        "aggregates": decode_aggregates_detail(row.get("aggregates") or []),
        "domain_entities": decode_domain_entities_detail(
            row.get("domain_entities") or []
        ),
        "domain_events": decode_domain_events_detail(
            row.get("domain_events") or []
        ),
        # [D-2 — 2026-05-25] Service detail 객체 복원.
        "services": decode_services_detail(row.get("services") or []),
        "databases": row.get("databases") or [],
        # Skill.instructions/tags 까지 통째로 — fix_spec LLM 이 규칙의 실제
        # 요구사항을 보고 구체적인 수정 가이드를 만들 수 있도록 한다.
        "rules": row.get("rules") or [],
        # [2026-06] 기획 항목 — lint 5번째 카테고리(story:/screen:) 실패분의 맥락.
        "stories": row.get("stories") or [],
        "screens": row.get("screens") or [],
    }


# ─── Stage 3: LLM ──────────────────────────────────────────────


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


async def call_fix_spec_agent(
    ctx: PipelineContext,
    *,
    project_name: str,
    github_url: str,
    current_score: int,
    total_failed: int,
    failed_by_category: List[Dict[str, Any]],
    spec: Dict[str, Any],
) -> str:
    prompt = _render(
        _load_prompt("fix_spec.md"),
        project_name=project_name,
        github_url=github_url,
        current_score=str(current_score),
        total_failed=str(total_failed),
        failed_by_category_json=json.dumps(failed_by_category, ensure_ascii=False, indent=2),
        spec_json=json.dumps(spec, ensure_ascii=False, indent=2),
    )
    result = await ctx.gemini.generate(prompt, temperature=0.3)
    return strip_code_blocks(result.text)


# ─── Stage 4: format ───────────────────────────────────────────


def _format(
    markdown: str,
    *,
    project_name: str,
    github_url: str,
    score: int,
    total_failed: int,
) -> FixSpecResult:
    ts = int(time.time())
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", project_name) or "project"
    filename = f"{safe_name}-fix-spec-{ts}.md"
    return FixSpecResult(
        success=True,
        markdown=markdown,
        filename=filename,
        message="수정 명세서가 생성되었습니다.",
        metadata={
            "currentScore": score,
            "totalFailed": total_failed,
            "githubUrl": github_url,
        },
    )


# ─── End-to-end orchestrator ────────────────────────────────────


async def run_fix_spec_pipeline(
    ctx: PipelineContext, payload: FixSpecInput
) -> FixSpecResult:
    """parse failures → (early return if 100%) → fetch spec → LLM → format."""
    logger.info(
        "fix_spec pipeline start: project=%s key=%s",
        payload.project_name,
        ctx.idempotency_key,
    )
    failures = _parse_failures(payload)

    if not failures["hasFailures"]:
        return FixSpecResult(
            success=True,
            markdown=None,
            filename=None,
            message="이미 100% 달성. 추가 작업이 필요하지 않습니다.",
            metadata={
                "currentScore": 100,
                "totalFailed": 0,
                "githubUrl": failures["githubUrl"],
            },
        )

    spec = await _fetch_full_spec(ctx, failures["projectName"])
    markdown = await call_fix_spec_agent(
        ctx,
        project_name=failures["projectName"],
        github_url=failures["githubUrl"],
        current_score=failures["score"],
        total_failed=failures["totalFailed"],
        failed_by_category=failures["failedByCategory"],
        spec=spec,
    )
    return _format(
        markdown,
        project_name=failures["projectName"],
        github_url=failures["githubUrl"],
        score=failures["score"],
        total_failed=failures["totalFailed"],
    )
