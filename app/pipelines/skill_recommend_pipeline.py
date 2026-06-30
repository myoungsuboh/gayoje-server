"""
recommendSkillsByAI 파이프라인 — 프로젝트의 CPS/PRD 를 기반으로 스킬 카탈로그
중 적용해야 할 스킬을 LLM 이 선별.

[스테이지 매핑]
- Prepare Input        → `_validate_input`
- Fetch CPS / Fetch PRD / Arch Context → `_fetch_master_documents` (Neo4j 직접)
- Build LLM Context    → `_build_context`
- Skill Picker AI Agent → `call_skill_picker` (prompts/skill_recommend.md)
- Parse & Validate     → `_parse_and_validate`

[데이터 접근]
Neo4j 에서 master CPS/PRD + ArchService/Entity/API 컨텍스트를 직접 읽어
추가 webhook 홉 없이 단축한다.

[A1 — 2026-06-13] 아키텍처 컨텍스트 주입:
CPS·PRD 만으론 프로젝트의 tech_stack·도메인 모델·API 설계를 LLM 이 파악 못해
스택 중립 스킬(Security·Testing 등)만 추천되던 문제를 개선.
ArchService.tech_stack + Entity.name + API.endpoint 를 추가 컨텍스트로 주입.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.pipelines.base import (
    PipelineContext,
    extract_json_object,
    generate_json_with_retry,
    strip_code_blocks,
)

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# LLM 입력 token 한도 보호용 (12000 으로 슬라이스)
_MAX_CPS_PRD_CHARS = 12000

# [2026-06 추천 신뢰 정책] 어중간한(0.9 미만) 추천이 추천 전체의 신뢰를 깨뜨린다는
# 사용자 피드백 — 프롬프트가 0.90 미만 포함을 금지하지만, LLM 이 어겨도 여기서
# 걸러낸다.
# [2026-06-13 구멍 보강] 이전엔 confidence 미반환(None)을 '점수 미상'으로 통과시켜,
# LLM 이 confidence 를 빼고 응답하면 0.90 게이트를 그대로 우회했다(신뢰 정책 무력화).
# 이제 confidence 누락도 '확신을 표명 못 한 추천' = 제외. 단, 누락이 잦으면 추천이
# 전부 사라지므로 schema 에서 confidence 를 required 로 강제(1차) + 여기서 drop(2차).
_MIN_CONFIDENCE = 0.90


# ─── Structured Output Schema (2026-05 결정성 강화) ─────────────────
_SKILL_RECOMMEND_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "recommended": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                # [2026-06-13] confidence 도 required — 누락 시 0.90 게이트 우회 차단.
                "required": ["id", "confidence"],
            },
        },
    },
    "required": ["recommended"],
}


# ─── Domain types ───────────────────────────────────────────────


@dataclass(frozen=True)
class CatalogEntry:
    """프론트가 보내는 카탈로그 한 항목."""

    id: str
    name: str
    description: str = ""
    category: str = ""

    def to_prompt_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
        }


@dataclass(frozen=True)
class RecommendInput:
    project_name: str
    skill_catalog: List[CatalogEntry]
    allowed_categories: List[str] = field(default_factory=list)


@dataclass
class RecommendedSkill:
    id: str
    reason: str = ""
    confidence: Optional[float] = None


@dataclass
class RecommendResult:
    recommended: List[RecommendedSkill] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


# ─── Stage 1: validate ─────────────────────────────────────────


def _validate_input(payload: RecommendInput) -> None:
    if not payload.skill_catalog:
        raise ValueError("skillCatalog 가 비어 있습니다.")
    if not payload.project_name:
        raise ValueError("projectName 이 비어 있습니다.")


# ─── Stage 2: fetch CPS + PRD master content ───────────────────


_FETCH_CPS_PRD_CYPHER = """\
// 마스터 CPS + 마스터 PRD 의 full_markdown 만.
OPTIONAL MATCH (cps:CPS_Document {project: $project, type: 'Master', is_latest: true})
OPTIONAL MATCH (prd:PRD_Document {project: $project, type: 'Master', is_latest: true})
RETURN
    cps.full_markdown AS cps_content,
    prd.full_markdown AS prd_content
"""

# [A1] ArchService tech_stack + Entity 이름 + API 엔드포인트 — 추천 컨텍스트 강화.
# CPS/PRD 만으론 "어떤 기술스택을 쓰는지" LLM 이 모름 → 스택 특화 스킬 추천 불가.
_FETCH_ARCH_CONTEXT_CYPHER = """\
// 서비스 tech_stack (ArchService), 도메인 엔티티명, API 엔드포인트 샘플
OPTIONAL MATCH (arch:ArchService {project: $project})
WITH collect({name: arch.name, tech_stack: arch.tech_stack}) AS services
OPTIONAL MATCH (ent:Entity {project: $project})
WITH services, collect(ent.name)[0..15] AS entities
OPTIONAL MATCH (api:API {project: $project})
WITH services, entities,
     collect(coalesce(api.method, '') + ' ' + coalesce(api.endpoint, ''))[0..20] AS apis
RETURN services, entities, apis
"""

_MAX_ARCH_CONTEXT_CHARS = 2000


def _format_arch_context(
    services: List[Dict[str, Any]],
    entities: List[str],
    apis: List[str],
) -> str:
    """ArchService·Entity·API 컨텍스트를 프롬프트용 텍스트로 포맷."""
    parts: List[str] = []
    if services:
        lines = [
            f"- {s.get('name', '?')} (tech_stack: {s.get('tech_stack') or '미상'})"
            for s in services
            if isinstance(s, dict) and s.get("name")
        ]
        if lines:
            parts.append("기술 스택 (ArchService):\n" + "\n".join(lines))
    if entities:
        valid = [e for e in entities if e and str(e).strip()]
        if valid:
            parts.append("주요 도메인 엔티티: " + ", ".join(str(e) for e in valid))
    if apis:
        valid = [a for a in apis if a and str(a).strip()]
        if valid:
            parts.append("API 엔드포인트 샘플:\n" + "\n".join(f"- {a}" for a in valid))
    return "\n\n".join(parts)


async def _fetch_master_documents(
    ctx: PipelineContext, project_name: str
) -> Dict[str, str]:
    # [멀티테넌시] ctx.team_id 로 스코프 (개인=이름 그대로).
    from app.core.project_scope import scoped_project
    project_name = scoped_project(project_name, ctx.team_id)

    # 1) CPS + PRD
    records = await ctx.neo4j.run_cypher(
        _FETCH_CPS_PRD_CYPHER, {"project": project_name}
    )
    row = records[0] if records else {}

    # 2) ArchService + Entity + API 컨텍스트 [A1]
    arch_records = await ctx.neo4j.run_cypher(
        _FETCH_ARCH_CONTEXT_CYPHER, {"project": project_name}
    )
    arch_row = arch_records[0] if arch_records else {}
    arch_text = _format_arch_context(
        arch_row.get("services") or [],
        arch_row.get("entities") or [],
        arch_row.get("apis") or [],
    )[:_MAX_ARCH_CONTEXT_CHARS]

    return {
        "cps": (row.get("cps_content") or "")[:_MAX_CPS_PRD_CHARS],
        "prd": (row.get("prd_content") or "")[:_MAX_CPS_PRD_CHARS],
        "arch": arch_text,
    }


# ─── Stage 3: build LLM context + call ─────────────────────────


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


def _build_context(
    payload: RecommendInput,
    cps_text: str,
    prd_text: str,
    arch_context_text: str = "",
) -> Dict[str, Any]:
    catalog_json = json.dumps(
        [c.to_prompt_dict() for c in payload.skill_catalog],
        ensure_ascii=False,
        indent=2,
    )
    return {
        "project_name": payload.project_name,
        "catalog_ids": {c.id for c in payload.skill_catalog},
        "cps_text": cps_text,
        "prd_text": prd_text,
        "arch_context_text": arch_context_text,
        "catalog_json": catalog_json,
    }


async def call_skill_picker(
    ctx: PipelineContext, context: Dict[str, Any]
) -> Dict[str, Any]:
    """
    [2026-05] generate_json_with_retry 적용 — 첫 시도 unparseable 이면 strict
    prefix + low temperature 로 1회 재시도. 두 시도 모두 실패면 빈 dict 반환.
    호출자(_parse_and_validate)는 dict 받아 'recommended' 키 검증.
    """
    prompt = _render(
        _load_prompt("skill_recommend.md"),
        project_name=context["project_name"],
        cps_text=context["cps_text"],
        prd_text=context["prd_text"],
        arch_context_text=context.get("arch_context_text", ""),
        catalog_json=context["catalog_json"],
    )
    # [2026-05] structured output 강제 — id/reason/confidence 형식 안정화.
    parsed, _ = await generate_json_with_retry(
        ctx.gemini, prompt,
        temperature=0.1,
        response_schema=_SKILL_RECOMMEND_SCHEMA,
    )
    return parsed


# ─── Stage 4: parse & validate ─────────────────────────────────


def _parse_and_validate(
    parsed: Dict[str, Any], catalog_ids: set[str]
) -> RecommendResult:
    """
    Stage: 'Parse & Validate':
      - 입력은 이미 parsed dict (call_skill_picker 가 generate_json_with_retry 로
        반환 — 빈 dict 면 retry 도 실패한 케이스)
      - 'recommended' 배열 누락 시 빈 결과 + error
      - 카탈로그에 있는 id 만 통과
      - 중복 제거 + confidence clamp [0, 1]
    """
    raw = parsed.get("recommended") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        return RecommendResult(
            recommended=[],
            meta={
                "totalCatalogSize": len(catalog_ids),
                "rawCount": 0,
                "validCount": 0,
                "error": "recommended 배열 누락",
            },
        )

    seen: set[str] = set()
    valid: List[RecommendedSkill] = []
    low_confidence_dropped = 0
    for r in raw:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if not isinstance(rid, str) or rid not in catalog_ids or rid in seen:
            continue
        seen.add(rid)
        conf = r.get("confidence")
        if isinstance(conf, (int, float)) and not isinstance(conf, bool):
            conf_clamped: Optional[float] = max(0.0, min(1.0, float(conf)))
        else:
            conf_clamped = None
        # [추천 신뢰 정책] 프롬프트 금지의 2차 방어. 0.90 미만은 물론, confidence
        # 누락(None)도 제외 — '확신을 표명 못 한 추천'은 가장 못 믿을 추천이므로.
        # (schema required 로 누락은 드물지만, 어겨도 여기서 막는다.)
        if conf_clamped is None or conf_clamped < _MIN_CONFIDENCE:
            low_confidence_dropped += 1
            continue
        valid.append(
            RecommendedSkill(
                id=rid,
                reason=r.get("reason") if isinstance(r.get("reason"), str) else "",
                confidence=conf_clamped,
            )
        )

    return RecommendResult(
        recommended=valid,
        meta={
            "totalCatalogSize": len(catalog_ids),
            "rawCount": len(raw),
            "validCount": len(valid),
            "lowConfidenceDropped": low_confidence_dropped,
        },
    )


# ─── End-to-end orchestrator ────────────────────────────────────


async def run_skill_recommend_pipeline(
    ctx: PipelineContext, payload: RecommendInput
) -> RecommendResult:
    """
    validate → fetch CPS/PRD → build context → call LLM → parse.
    """
    logger.info(
        "skill recommend start: project=%s catalog_size=%d key=%s",
        payload.project_name,
        len(payload.skill_catalog),
        ctx.idempotency_key,
    )
    _validate_input(payload)
    docs = await _fetch_master_documents(ctx, payload.project_name)

    # [2026-06-13 빈 상태 가드] CPS·PRD 가 둘 다 비어 있으면(회의록/기획 미작성 프로젝트에서
    # 추천 버튼을 누른 경우) LLM 을 호출하지 않고 즉시 빈 결과. 두 가지를 막는다:
    #   1) 토큰 낭비 — 근거 문서가 0 인데 LLM 을 돌리는 건 무의미한 비용.
    #   2) 환각 — 빈 PRD + 풍부한 카탈로그면 LLM 이 '흔히 쓰는' 스킬을 높은 confidence 로
    #      추천해 0.90 게이트마저 통과시킬 수 있다(근거 없는 추천). 원천 차단.
    # FE 는 meta.reason == 'no_source_docs' 를 보고 "먼저 회의록/PRD 를 작성하세요" 안내.
    if not (docs["cps"].strip() or docs["prd"].strip()):
        logger.info("skill recommend short-circuit: CPS/PRD 모두 비어있음 — LLM skip (project=%s)",
                    payload.project_name)
        return RecommendResult(
            recommended=[],
            meta={
                "totalCatalogSize": len(payload.skill_catalog),
                "rawCount": 0,
                "validCount": 0,
                "reason": "no_source_docs",
            },
        )

    context = _build_context(payload, docs["cps"], docs["prd"], docs.get("arch", ""))
    parsed = await call_skill_picker(ctx, context)
    return _parse_and_validate(parsed, context["catalog_ids"])
