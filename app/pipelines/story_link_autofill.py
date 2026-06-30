"""
storyLinkAutofill 파이프라인 — PRD 연결(스토리 추적)이 끊긴 SPACK 노드를 AI 매칭으로 보완.

[배경 — 2026-06-12]
"AI로 빈 명세 채우기"(api_spec_autofill)는 error_cases/auth 만 채워서, 완성도 모달의
두 섹션이 그대로 남았다:
  - "지금 이것부터 채우면 확 올라가요": API/Entity ↔ Story 추적 미비
    (scorer tier3 의 api_story_mapped_ratio / entity_story_mapped_ratio)
  - "SPACK PRD 연결 상세": API·Policy 미연결 칩
    (API 는 lineage_confidence 가 저장 자체가 안 됐고, Policy 는 연결 모델이 없었음)

이 파이프라인은 미연결 API/Entity/Policy 를 PRD Story 목록과 LLM 1회 배치 매칭한 뒤,
노드 속성(related_story_id / lineage_confidence) + 엣지(IMPLEMENTS / DERIVED_FROM)를
부분 저장한다. autofill 잡(autofill_api_specs_job)의 후속 스테이지로 실행된다.

[환각 차단 — 가장 중요]
LLM 이 반환한 story_id 는 실제 Story 노드 id 집합(whitelist)에 있을 때만 적용한다.
근거 없는 항목은 null 로 두라고 지시하고, null/미지 id 는 그대로 미연결로 남긴다 —
틀린 연결은 빈 연결보다 나쁘다.

[정직한 마킹]
AI 매칭 결과의 confidence 는 'inferred'(파생 추론) — 설계 생성이 직접 낸 'direct' 와
구분된다. 노드에 link_source='ai_autofill' 도 부착해 감사 가능.

[부분 실패 격리]
LLM 호출 실패/빈 결과 → 연결 0건으로 강등 (error/auth 결과는 이미 저장됨 — 잡은 성공).
한 노드의 저장 실패도 그 노드만 saved=False.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.pipelines.base import PipelineContext, generate_json_with_retry

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 한 배치에 LLM 에 보낼 최대 항목 수 — 토큰/출력 길이 상한. 초과분은 다음 실행에서.
_MAX_ITEMS_PER_BATCH = 60
# 매칭은 단일 호출(배치)이라 단건 autofill(35s)보다 여유. 비정상 폭주만 컷.
_LINK_LLM_TIMEOUT = 60.0
_LINK_LLM_MAX_RETRIES = 1

_LINK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    # 근거 없으면 빈 문자열 "" — parse_links 가 미연결로 버린다.
                    "story_id": {"type": "string"},
                },
                "required": ["item_id", "story_id"],
            },
        },
    },
    "required": ["links"],
}

# kind → (Neo4j 라벨, 사람 설명) — update_node_story_link 의 라벨 whitelist 와 일치.
_KIND_TO_LABEL = {"api": "API", "entity": "Entity", "policy": "Policy"}


@dataclass(frozen=True)
class LinkItem:
    """매칭 대상 노드 (SPACK API/Entity/Policy 의 부분집합)."""

    id: str
    kind: str  # "api" | "entity" | "policy"
    name: str = ""
    description: str = ""


@dataclass
class LinkFill:
    item_id: str
    kind: str
    story_id: str
    saved: bool = False


def collect_link_targets(spack: Any) -> List[LinkItem]:
    """SPACK 그래프(dict 또는 .apis/.entities/.policies 객체)에서 미연결 노드만 추출.

    기준은 scorer/fix_targets 와 동일:
      - API: related_story_id 비어 있음
      - Entity: lineage.related_stories 비어 있음 (DERIVED_FROM 엣지 없음)
      - Policy: related_story_id 비어 있음 (연결 모델 신설 — 이전엔 항상 미연결)
    """
    def _get(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    out: List[LinkItem] = []
    for a in _get(spack, "apis") or []:
        if not a.get("id") or a.get("related_story_id"):
            continue
        desc = " ".join(
            s for s in (
                str(a.get("method") or "").upper(),
                str(a.get("endpoint") or ""),
                str(a.get("description") or ""),
            ) if s
        )
        out.append(LinkItem(id=str(a["id"]), kind="api", name=str(a.get("name") or ""), description=desc))
    for e in _get(spack, "entities") or []:
        if not e.get("id") or (e.get("lineage") or {}).get("related_stories"):
            continue
        out.append(LinkItem(
            id=str(e["id"]), kind="entity",
            name=str(e.get("name") or ""), description=str(e.get("description") or ""),
        ))
    for p in _get(spack, "policies") or []:
        if not p.get("id") or p.get("related_story_id"):
            continue
        out.append(LinkItem(
            id=str(p["id"]), kind="policy",
            name=str(p.get("category") or ""), description=str(p.get("description") or ""),
        ))
    return out[:_MAX_ITEMS_PER_BATCH]


def _render_stories(stories: List[Dict[str, Any]]) -> str:
    lines = []
    for s in stories:
        sid = str(s.get("id") or "").strip()
        if not sid:
            continue
        summary = str(s.get("summary") or "").strip().replace("\n", " ")[:200]
        lines.append(f"- {sid}: {summary}" if summary else f"- {sid}")
    return "\n".join(lines) or "(없음)"


def _render_items(items: List[LinkItem]) -> str:
    lines = []
    for it in items:
        desc = (it.description or "").replace("\n", " ")[:200]
        lines.append(f"- [{it.kind}] {it.id}: {it.name}" + (f" — {desc}" if desc else ""))
    return "\n".join(lines)


def _build_prompt(stories: List[Dict[str, Any]], items: List[LinkItem]) -> str:
    # single-pass 렌더 (placeholder 주입 방지) — api_spec_autofill 과 동일 경로.
    from app.core.prompt_render import render_template

    template = (PROMPT_DIR / "story_link_autofill.md").read_text(encoding="utf-8")
    return render_template(
        template, stories=_render_stories(stories), items=_render_items(items),
    )


def parse_links(
    parsed: Any, items: List[LinkItem], story_ids: set,
) -> List[LinkFill]:
    """LLM 출력 → 검증된 LinkFill 목록.

    whitelist 검증: item_id 는 대상 집합에, story_id 는 실제 Story id 집합에 있어야
    적용. null/미지 id/중복(첫 번째만)은 버린다 — 환각 연결 차단.
    """
    by_id = {it.id: it for it in items}
    seen: set = set()
    out: List[LinkFill] = []
    links = (parsed or {}).get("links") if isinstance(parsed, dict) else None
    for link in links or []:
        if not isinstance(link, dict):
            continue
        item_id = str(link.get("item_id") or "").strip()
        raw_story = link.get("story_id")
        story_id = str(raw_story or "").strip()
        if not item_id or item_id in seen or item_id not in by_id:
            continue
        if not story_id or story_id.lower() == "null" or story_id not in story_ids:
            continue
        seen.add(item_id)
        out.append(LinkFill(item_id=item_id, kind=by_id[item_id].kind, story_id=story_id))
    return out


async def run_story_link_autofill(
    ctx: PipelineContext,
    project_name: str,
    spack: Any,
    *,
    team_id: str = "",
    fallback_model: Optional[str] = None,
) -> Dict[str, Any]:
    """미연결 노드 수집 → LLM 배치 매칭 1회 → whitelist 검증 → 부분 저장.

    반환 meta: {linkTargets, linkedCount, linkSavedCount} — autofill 잡 meta 에 병합.
    어떤 실패도 예외로 올리지 않는다 (이미 저장된 error/auth 결과 보호).
    """
    # 지연 import — 모듈 로드 시점 neo4j 의존 회피 (api_spec_autofill 과 동일 정책).
    from app.service import query_repository

    meta = {"linkTargets": 0, "linkedCount": 0, "linkSavedCount": 0}
    try:
        items = collect_link_targets(spack)
        meta["linkTargets"] = len(items)
        if not items:
            return meta

        nodes = await query_repository.list_prd_nodes(project_name, team_id)
        stories = [n for n in nodes if (n.get("label") or "") == "Story"]
        story_ids = {str(s.get("id")) for s in stories if s.get("id")}
        if not story_ids:
            logger.info("story_link_autofill: Story 노드 없음 — 연결 생략 (project=%s)", project_name)
            return meta

        prompt = _build_prompt(stories, items)
        try:
            parsed, _ = await generate_json_with_retry(
                ctx.gemini,
                prompt,
                temperature=0.1,
                response_schema=_LINK_SCHEMA,
                timeout=_LINK_LLM_TIMEOUT,
                max_retries=_LINK_LLM_MAX_RETRIES,
            )
        except Exception as primary_err:  # noqa: BLE001 — 폴백 1회 후 강등
            if not fallback_model:
                logger.warning("story_link_autofill: LLM 실패, 폴백 없음 — 연결 생략 (%s)", primary_err)
                return meta
            logger.warning("story_link_autofill: primary LLM 실패 — 폴백(%s) 재시도", fallback_model)
            try:
                parsed, _ = await generate_json_with_retry(
                    ctx.gemini,
                    prompt,
                    temperature=0.1,
                    response_schema=_LINK_SCHEMA,
                    model=fallback_model,
                    timeout=_LINK_LLM_TIMEOUT,
                    max_retries=_LINK_LLM_MAX_RETRIES,
                )
            except Exception:  # noqa: BLE001
                logger.warning("story_link_autofill: 폴백 모델도 실패 — 연결 생략")
                return meta

        fills = parse_links(parsed, items, story_ids)
        meta["linkedCount"] = len(fills)

        for f in fills:
            label = _KIND_TO_LABEL.get(f.kind)
            if not label:
                continue
            try:
                f.saved = await query_repository.update_node_story_link(
                    project_name, label, f.item_id, f.story_id, team_id=team_id,
                )
            except Exception:  # noqa: BLE001 — 한 노드 저장 실패 격리
                logger.exception("story_link_autofill: 저장 실패 — %s %s", label, f.item_id)
                f.saved = False
        meta["linkSavedCount"] = sum(1 for f in fills if f.saved)
        logger.info(
            "story_link_autofill done: project=%s meta=%s", project_name, json.dumps(meta),
        )
    except Exception:  # noqa: BLE001 — 연결 보완 실패가 잡 전체를 깨지 않게
        logger.exception("story_link_autofill: 예기치 못한 실패 — 연결 생략 (project=%s)", project_name)
    return meta
