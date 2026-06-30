from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.pipelines.base import (
    PipelineContext,
    generate_json_with_retry,
    strip_code_blocks,
    strip_template_placeholders,
)
from app.pipelines.cps_pipeline.schemas import CPS_AGENT_SCHEMA, CPS_IMPACT_SCHEMA, _TEMPERATURE
from app.pipelines.cps_pipeline.sections import _SECTION_HEADER_RE
from app.pipelines.cps_pipeline.types import CpsInput

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# [2026-05-26 perf A] Impact analyzer 는 단순 JSON 분류 (master + latest →
# affected_sections / removed_ids). pro 모델 불필요 — flash-lite 로 충분.
# 운영 측정: pro ~6-8s / flash-lite ~1-2s. 정확도 회귀 없음 (단순 분류).
#
# [모델 선택] LiteLLM 프록시에 등록된 모델만 사용 가능 (litellm/config.yaml):
#   - gemini-2.5-flash (PRO 등급 — 기본)
#   - gemini-2.5-flash-lite (저비용 — impact 다운그레이드 + Lite 오버플로우)
# [2026-06] Google 이 gemini-2.0-flash-lite 를 폐기(404) → 2.5-flash-lite 로 이전 (litellm 등록 완료).
IMPACT_ANALYZER_MODEL = "gemini-2.5-flash-lite"

_GET_ALL_CPS_QUERY = """\
// 1. 마스터 CPS 1건
OPTIONAL MATCH (m:CPS_Document {project: $project, type: 'Master', is_latest: true})
WITH m ORDER BY m.updated_at DESC LIMIT 1

// 2. 최신 Delta CPS 1건
OPTIONAL MATCH (l:CPS_Document {project: $project, is_latest: true})
WHERE l.type IS NULL OR l.type <> 'Master'
WITH m, l ORDER BY l.id DESC LIMIT 1

// 3. 마스터 하위 Problem/Solution
OPTIONAL MATCH (m)<-[:EXTRACTED_FROM]-(mp:Problem)
OPTIONAL MATCH (mp)<-[:SOLVES]-(mr:Solution)
WITH m, l, collect(DISTINCT CASE WHEN mp IS NOT NULL
    THEN {id: mp.id, summary: mp.summary, resolved_by: mr.summary}
    ELSE NULL END) AS master_probs

// 4. 최신 하위 Problem/Solution
OPTIONAL MATCH (l)<-[:EXTRACTED_FROM]-(lp:Problem)
OPTIONAL MATCH (lp)<-[:SOLVES]-(lr:Solution)
WITH m, l, master_probs, collect(DISTINCT CASE WHEN lp IS NOT NULL
    THEN {id: lp.id, summary: lp.summary, resolved_by: lr.summary}
    ELSE NULL END) AS latest_probs

// 5. [2026-05] 진단 — 프로젝트의 CPS_Document 총 개수. is_first_run 안전망용.
OPTIONAL MATCH (any_cps:CPS_Document {project: $project})
WITH m, l, master_probs, latest_probs, count(any_cps) AS cps_total

RETURN
    m.id AS master_id,
    m.full_markdown AS master_content,
    master_probs,
    l.id AS latest_id,
    l.full_markdown AS latest_content,
    latest_probs,
    coalesce(m.project, l.project, $project) AS project_name,
    cps_total
"""


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


_LENIENT_SUFFIX = (
    "\n\n# ⚠️ LENIENT MODE — 적극적 추측 요청 (2026-05-25 신규)\n"
    "위 strict 모드에서 Problem/Solution 0개 도출됨. 이 회의록이 \"테스트 결과 보고\",\n"
    "\"진행 상황 공유\", \"두루뭉실한 브레인스토밍\", \"짧은 의사결정\" 등 spec 명시가 약한\n"
    "형식일 가능성. 그러나 사용자 입장에서 reject 는 좌절이므로 적극 추측해 주세요:\n\n"
    "- 회의에서 언급된 \"개선해야 할 것 / 검토 필요 / 추가 작업 / 우려 사항\" 을 모두\n"
    "  Problem 또는 Requirement 로 추출. \"확정\" 안 된 항목도 OK.\n"
    "- 회의에서 결정한 사항 / 합의된 방향 / 적용할 기술 / 채택한 형식 등을 Solution\n"
    "  또는 Requirement 로 추출.\n"
    "- 추출된 모든 노드의 properties 에 `\"confidence\": \"inferred\"` 표시 — 사용자가\n"
    "  나중에 검토 가능. 확실하면 `\"direct\"`.\n"
    "- 그래도 1개도 추출 안 되면 (예: \"안녕하세요\" 같은 빈 인사) 빈 nodes 반환 OK —\n"
    "  pipeline 이 skip 처리.\n"
)


def _count_specs(obj: Dict[str, Any]) -> int:
    nodes = (obj or {}).get("nodes") or []
    spec_labels = {"Problem", "Solution", "Requirement"}
    return sum(1 for n in nodes if (n or {}).get("label") in spec_labels)


def _make_skip_stub(payload: CpsInput, reason: str) -> Dict[str, Any]:
    """[2026-05-25] AI 가 strict/lenient 모두에서 spec 0개일 때 placeholder.

    BATCH 처리 멈춤 차단 — \"이 미팅은 spec 변동 없음\" 으로 통과.
    Neo4j 에 CPS_Document 노드만 저장 (Problem/Solution 0개) — 후속 머지에 영향 X.

    [2026-05-25 hotfix] full_markdown 누락 버그 fix.
    이전: properties 에 full_markdown 없음 → fetch_master_and_latest 의 latest_content
    가 빈 string → merge 단계 fallback 으로 payload.meeting_content (raw 미팅 로그)
    사용 → master 에 raw text 저장 → FE 의 4 섹션 split 실패.
    이후: placeholder 4 섹션 markdown 을 full_markdown 으로 채워 FE 정상 표시.
    """
    placeholder_md = (
        f"## 📄 CPS 명세서: {payload.project_name} ({payload.version})\n\n"
        f"### 1. Context (배경 및 상황)\n"
        f"- 이 미팅은 \"테스트 결과 / 보고 / 완료 선언\" 형식으로 판정되어 "
        f"AI 가 신규 Problem/Solution 도출하지 않음.\n"
        f"- 사유: {reason}\n\n"
        f"### 2. Problem (핵심 문제)\n"
        f"- (해당 미팅에 명시된 신규 문제 없음)\n\n"
        f"### 3. Solution (최종 해결책 및 기획 방향)\n"
        f"- (해당 미팅에 명시된 신규 해결책 없음)\n\n"
        f"### 4. Pending & Action Items\n"
        f"- 후속 미팅에서 누적 예정 (현재 미팅은 spec 변동 없음).\n"
    )
    return {
        "_extraction_mode": "skip",
        "_extraction_warning": reason,
        "nodes": [{
            "id": f"doc_cps_{payload.project_name}_{payload.normalized_version()}",
            "label": "CPS_Document",
            "properties": {
                "project": payload.project_name,
                "version": payload.version,
                "is_latest": True,
                "summary": "(이 미팅에서 신규 spec 도출 안 됨 — skip)",
                "full_markdown": placeholder_md,
                "skipped": True,
                "skip_reason": reason,
            },
        }],
        "relationships": [],
    }


async def call_cps_agent(ctx: PipelineContext, payload: CpsInput) -> Dict[str, Any]:
    """
    Stage: `CPS Agent` — 3-tier fallback (2026-05-25).

    1. Strict (기존 prompt): 명시적 Problem/Solution 추출.
    2. Lenient (재호출 + 적극 추측 suffix): 두루뭉실/보고 미팅도 추출.
       → 추출된 노드는 properties.confidence='inferred' 마크.
    3. Skip stub: 그래도 0개면 placeholder — BATCH 멈춤 차단.

    응답에 `_extraction_mode` (strict/lenient/skip) + `_extraction_warning` 포함.
    FE 가 사용자에게 모드/경고 표시 (특히 inferred 노드는 검토 권유).

    Returns:
      Parsed JSON object: { "_harness_metadata", "_extraction_mode",
                            "_extraction_warning"?, "nodes", "relationships" }
    """
    base_prompt = _render(
        _load_prompt("cps_extract.md"),
        project_name=payload.project_name,
        version=payload.version,
        version_normalized=payload.normalized_version(),
        meeting_content=payload.meeting_content,
        previous_cps_id=payload.previous_cps_id or "null",
    )

    # ── Tier 1: Strict ──
    obj, _ = await generate_json_with_retry(
        ctx.gemini, base_prompt,
        temperature=_TEMPERATURE,
        response_schema=CPS_AGENT_SCHEMA,
    )
    if not obj or "nodes" not in obj:
        raise ValueError(
            f"CPS Agent returned unparseable JSON (idempotency_key={ctx.idempotency_key})"
        )
    if _count_specs(obj) > 0:
        obj["_extraction_mode"] = "strict"
        return obj

    # ── Tier 2: Lenient (1회 재호출) ──
    # 비용: LLM 호출 1회 추가 (Strict 0개 케이스에만). 보고/두루뭉실 미팅 구제.
    lenient_obj, _ = await generate_json_with_retry(
        ctx.gemini, base_prompt + _LENIENT_SUFFIX,
        temperature=_TEMPERATURE,
        response_schema=CPS_AGENT_SCHEMA,
    )
    if lenient_obj and _count_specs(lenient_obj) > 0:
        lenient_obj["_extraction_mode"] = "lenient"
        lenient_obj["_extraction_warning"] = (
            "회의록이 두루뭉실하거나 \"보고 형식\" 이라 AI 가 적극 추측으로 추출. "
            "각 노드의 confidence=\"inferred\" — 검토 후 보강 권장."
        )
        return lenient_obj

    # ── Tier 3: Skip stub ──
    content_size = len((payload.meeting_content or "").encode("utf-8"))
    reason = (
        f"AI 가 strict/lenient 모두에서 신규 spec 0개 추출 "
        f"(입력 {content_size:,} bytes). 이 미팅은 \"spec 변동 없음\" 으로 처리. "
        f"필요하면 미팅 내용 보강 후 재처리."
    )
    return _make_skip_stub(payload, reason)


async def fetch_master_and_latest(ctx: PipelineContext, project_name: str) -> Dict[str, Any]:
    """Stage: `Get All CPS2`."""
    records = await ctx.neo4j.run_cypher(_GET_ALL_CPS_QUERY, {"project": project_name})
    if not records:
        return {
            "master_id": None,
            "master_content": "",
            "master_probs": [],
            "latest_id": None,
            "latest_content": "",
            "latest_probs": [],
            "project_name": project_name,
            "cps_total": 0,
        }
    row = records[0]
    return {
        "master_id": row.get("master_id"),
        "master_content": row.get("master_content") or "",
        "master_probs": [p for p in (row.get("master_probs") or []) if p is not None],
        "latest_id": row.get("latest_id"),
        "latest_content": row.get("latest_content") or "",
        "latest_probs": [p for p in (row.get("latest_probs") or []) if p is not None],
        "project_name": row.get("project_name") or project_name,
        # [2026-05] 진단 — orphan master 가드용.
        "cps_total": int(row.get("cps_total") or 0),
    }


async def call_impact_analyzer(
    ctx: PipelineContext,
    master_probs: List[Dict[str, Any]],
    latest_content: str,
) -> Dict[str, Any]:
    """Stage: `CPS Impact Analyzer1`.

    [2026-05-26 perf A] flash-lite override — 단순 JSON 분류 stage 라
    pro 모델 불필요. 지연/비용 절감 (대략 -2~5초 / 호출).
    """
    prompt = _render(
        _load_prompt("cps_impact.md"),
        master_probs_json=json.dumps(master_probs, ensure_ascii=False),
        latest_content=latest_content,
    )
    parsed, _ = await generate_json_with_retry(
        ctx.gemini, prompt,
        temperature=_TEMPERATURE,
        response_schema=CPS_IMPACT_SCHEMA,
        model=IMPACT_ANALYZER_MODEL,
    )
    return {
        "affected_sections": list(parsed.get("affected_sections") or []),
        "removed_prb_ids": list(parsed.get("removed_prb_ids") or []),
        "removed_res_ids": list(parsed.get("removed_res_ids") or []),
        "analysis": parsed.get("analysis", ""),
    }


async def call_merge_agent(ctx: PipelineContext, filter_data: Dict[str, Any]) -> str:
    """Stage: `Merge CPS Agent2`."""
    impact = filter_data.get("impact") or {}
    prompt = _render(
        _load_prompt("cps_merge.md"),
        affected_sections_content=filter_data.get("affected_sections_content", ""),
        latest_content=filter_data.get("latest_content", ""),
        removed_prb_ids=json.dumps(impact.get("removed_prb_ids") or [], ensure_ascii=False),
        removed_res_ids=json.dumps(impact.get("removed_res_ids") or [], ensure_ascii=False),
    )
    result = await ctx.gemini.generate(prompt, temperature=_TEMPERATURE)
    return strip_template_placeholders(strip_code_blocks(result.text))


def reassemble_master(filter_data: Dict[str, Any], agent_output: str) -> Dict[str, Any]:
    """Stage: `CPS Reassembler1`."""
    is_first_run = bool(filter_data.get("is_first_run"))
    section_order: List[str] = filter_data.get("section_order") or []
    section_map: Dict[str, str] = filter_data.get("full_section_map") or {}
    affected_keys: List[str] = filter_data.get("affected_section_keys") or []

    if is_first_run or not section_order:
        return {
            "merged_content": agent_output,
            "_diagnostic": {
                "mode": "FIRST_RUN_PASSTHROUGH" if is_first_run else "PARSE_FAIL_PASSTHROUGH"
            },
        }

    agent_section_map: Dict[str, str] = {}
    agent_current_key: Optional[str] = None
    agent_current_lines: List[str] = []
    for line in agent_output.split("\n"):
        m = _SECTION_HEADER_RE.match(line)
        if m:
            if agent_current_key is not None and agent_current_lines:
                agent_section_map[agent_current_key] = "\n".join(agent_current_lines)
            agent_current_key = m.group(1).strip()
            agent_current_lines = [line]
        elif agent_current_key is not None:
            agent_current_lines.append(line)
    if agent_current_key is not None and agent_current_lines:
        agent_section_map[agent_current_key] = "\n".join(agent_current_lines)

    final_parts: List[str] = []
    used_agent_keys: set[str] = set()
    replaced = 0
    preserved = 0

    if section_map.get("__header__"):
        final_parts.append(section_map["__header__"].strip())

    for key in section_order:
        if key == "__header__":
            continue
        if key in affected_keys:
            agent_key = next(
                (
                    ak
                    for ak in agent_section_map.keys()
                    if ak not in used_agent_keys
                    and (
                        ak.lower().find(key.lower()) >= 0
                        or key.lower().find(ak.lower()) >= 0
                    )
                ),
                None,
            )
            if agent_key:
                final_parts.append(agent_section_map[agent_key].strip())
                used_agent_keys.add(agent_key)
                replaced += 1
            else:
                final_parts.append(section_map[key].strip())
                preserved += 1
        else:
            final_parts.append(section_map[key].strip())
            preserved += 1

    return {
        "merged_content": "\n\n".join(final_parts),
        "_diagnostic": {
            "mode": "INCREMENTAL_REASSEMBLED",
            "total_sections": len(section_order),
            "replaced_count": replaced,
            "preserved_count": preserved,
            "affected_section_keys": affected_keys,
        },
    }
