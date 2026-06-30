"""
createMD 파이프라인 — Spack/DDD/Architecture 그래프를 바이브코딩용 MD 3종으로 변환.

[스테이지 매핑]
- ExecuteQuery Get Spack / DDD / Architecture (3 parallel) → `query_repository`
  (PR9 에서 이미 구현. 그대로 재활용)
- Create Spack/DDD/Architecture MD AI Agent (3 parallel) → `_call_*_agent`
  3 LLM 호출은 서로 독립적이라 `asyncio.gather` 로 병렬 실행 → 응답 시간 ~1/3
- Edit Fields / Merge / Code in JS → `CreateMdResult` dataclass 로 합침
- Respond → 반환

[병렬화 결정]
3개 Agent 호출은 서로 독립적이므로 `asyncio.gather` 로 명시적 병렬 실행.
토큰은 같지만 wall time 1/3.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from app.pipelines.base import PipelineContext, strip_code_blocks
from app.pipelines.design_pipeline.ddd_filter import filter_ddd_for_codegen
from app.service.query_repository import (
    ArchitectureGraph,
    DddGraph,
    SpackGraph,
    get_architecture_graph,
    get_ddd_graph,
    get_spack_graph,
)
from app.service.skill_repository import get_all_skills

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


@dataclass(frozen=True)
class CreateMdInput:
    project_name: str


@dataclass
class CreateMdResult:
    project_name: str
    spack_md: str = ""
    ddd_md: str = ""
    arch_md: str = ""
    orchestrator_md: str = ""
    # [2026-06 '루프의 시대'] 그래프 기반 결정적 전수 대조 체크리스트 (LLM 미사용).
    checklist_md: str = ""
    diagnostic: Dict[str, Any] = field(default_factory=dict)


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


async def _call_spack_md(ctx: PipelineContext, spack: SpackGraph) -> str:
    prompt = _render(
        _load_prompt("create_md_spack.md"),
        spack_json=json.dumps(spack.model_dump(), ensure_ascii=False, indent=2),
    )
    result = await ctx.gemini.generate(prompt, temperature=0.3)
    return strip_code_blocks(result.text)


async def _call_ddd_md(ctx: PipelineContext, ddd: DddGraph) -> str:
    # [2026-05-27] "전시 vs 코드-입력" 분리 — 바이브 코딩 패키지(ddd_md)는 AI 에이전트가
    # 실제 코드 짤 때 보는 명세라, confidence=none DDD(PRD 근거 없음)를 제외해 오염 차단.
    # 화면(getDDD)은 원본 그대로. inferred 는 남기고 프롬프트가 '추정-검증필요' 처리.
    filtered = filter_ddd_for_codegen(ddd.model_dump())
    prompt = _render(
        _load_prompt("create_md_ddd.md"),
        ddd_json=json.dumps(filtered, ensure_ascii=False, indent=2),
    )
    result = await ctx.gemini.generate(prompt, temperature=0.3)
    return strip_code_blocks(result.text)


async def _call_architecture_md(
    ctx: PipelineContext, arch: ArchitectureGraph, spack: SpackGraph
) -> str:
    # [2026-06 Gemini 평가 #2] API↔Service 매핑(HANDLED_BY)은 spack 그래프에 있는데
    # arch 입력에 실리지 않아 architecture.md 의 "API ↔ Service Mapping" 이 항상 0
    # (⚠️ 임의 배치 위험 경고만 출력). 프롬프트가 기대하는 api_service_mapping 키로 합성.
    api_label_by_id = {
        _node_prop(n, "id"): " ".join(
            p for p in (
                f"{_node_prop(n, 'method').upper()} {_node_prop(n, 'endpoint', 'path')}".strip(),
                f"— {_node_prop(n, 'name')}" if _node_prop(n, "name") else "",
            ) if p
        )
        for n in (spack.apis or [])
    }
    arch_payload = arch.model_dump()
    arch_payload["api_service_mapping"] = [
        {
            "api_id": r.source_id,
            # or-폴백: 키가 있어도 라벨이 빈 문자열(메서드/경로/이름 전부 미정)이면 id 로
            "api": api_label_by_id.get(r.source_id) or r.source_id,
            "service_id": r.target_id,
            "service_name": r.target_name or "",
            "reason": getattr(r, "reason", None) or "",
        }
        for r in (spack.api_service_rels or [])
        if getattr(r, "source_id", None)
    ]
    prompt = _render(
        _load_prompt("create_md_architecture.md"),
        arch_json=json.dumps(arch_payload, ensure_ascii=False, indent=2),
    )
    result = await ctx.gemini.generate(prompt, temperature=0.3)
    return strip_code_blocks(result.text)


# ─── [2026-06] IMPLEMENTATION-CHECKLIST — 그래프 기반 결정적 생성 ─────────────
#
# [배경 — '프롬프트의 시대는 가고 루프의 시대']
# LLM 이 만드는 MD/오케스트레이터는 입력 절단(_ORCHESTRATOR_SECTION_CAP)·요약 과정에서
# 항목이 빠질 수 있고, 에이전트가 "전부 구현했는지" 를 대조할 권위 있는 목록이 없었다.
# 이 체크리스트는 설계 그래프에서 **기계적으로** 생성하므로 누락이 구조적으로 불가능하며,
# zip 의 마지막 Phase(전수 대조 루프)가 이 파일을 기준으로 "모든 항목 [x] 까지 반복" 한다.


def _node_prop(node: Dict[str, Any], *keys: str) -> str:
    """노드 dict 에서 키를 평탄 → properties 중첩 순으로 안전 추출.

    Neo4j 노드는 조회 경로에 따라 평탄 dict 또는 {properties: {...}} 로 올 수 있다
    (FE utils/nodeUtils.getNodeProp 와 동일 발상).
    """
    props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
    for k in keys:
        v = node.get(k)
        if v is None:
            v = props.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _node_json_list(node: Dict[str, Any], key: str) -> list:
    """노드의 list 속성 안전 추출 — Neo4j 저장 시 JSON string 직렬화도 복원.

    [2026-06-11 오탐 픽스] attributes 등은 Neo4j 에 JSON string 으로 저장될 수 있는데
    (스키마 주석), 기존 len 판정이 str 을 0 으로 취급 → 속성이 있는데 ⚠️속성미정 오탐.
    """
    props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
    v = node.get(key)
    if v is None:
        v = props.get(key)
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return []
    return v if isinstance(v, list) else []


def _node_list_len(node: Dict[str, Any], key: str) -> int:
    return len(_node_json_list(node, key))


def _body_field_count(node: Dict[str, Any], key: str) -> int:
    """request_body/response_body 의 fields 수 — 스펙 갭(미정) 판정용.

    Neo4j 저장 시 JSON string 으로 직렬화될 수 있어(스키마 주석 참고) 복원 처리.
    """
    props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
    v = node.get(key)
    if v is None:
        v = props.get(key)
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return 0
    if isinstance(v, dict):
        f = v.get("fields")
        return len(f) if isinstance(f, list) else 0
    return 0


def build_implementation_checklist(
    project_name: str,
    spack: SpackGraph,
    ddd: DddGraph,
    arch: ArchitectureGraph,
) -> tuple[str, int, int]:
    """설계 그래프 → 구현 전수 대조 체크리스트 markdown. (md, 항목수, 스펙갭수) 반환.

    그래프가 완전히 비어 있으면 ("", 0, 0) — 소비자(FE)는 파일을 생략한다.

    [2026-06 스펙 갭 마커] 체크리스트는 '커버리지'를 보증하지만, 설계 자체에 스키마가
    비어 있으면(API request/response 미정 등) 에이전트가 추측으로 채운다 — 패키지
    품질의 진짜 천장. 갭 항목에 ⚠️ 마커를 박아 "추측 말고 STOP 에서 사용자에게
    확인"을 항목 단위로 강제한다.
    """
    sections: list[tuple[str, list[str]]] = []
    spec_gaps = 0

    # [2026-06-11 Gemini 평가 반영] API↔서비스 매핑(HANDLED_BY)은 그래프에 이미 존재
    # (대시보드 카드가 표시 중)하는데 패키지에 안 실려 "어느 서버에 만들지?" 추측을
    # 유발했다 — MSA 에서 가장 치명적인 갭. 항목마다 담당 서비스를 명시한다.
    svc_by_api = {
        r.source_id: (r.target_name or r.target_id)
        for r in (spack.api_service_rels or [])
        if getattr(r, "source_id", None)
    }
    multi_service = len(arch.services or []) > 1

    api_lines = []
    for n in spack.apis or []:
        api_id = _node_prop(n, "id")
        method = _node_prop(n, "method").upper()
        endpoint = _node_prop(n, "endpoint", "path")
        name = _node_prop(n, "name", "title", "id")
        label = f"`{method} {endpoint}`" if (method or endpoint) else f"`{name}`"
        svc = svc_by_api.get(api_id, "")
        svc_str = f" [→ {svc}]" if svc else ""
        gaps = []
        # 서비스가 2개 이상(MSA)인데 담당 서비스 미지정 — 에이전트가 엉뚱한 서버에 구현할 위험.
        if multi_service and not svc:
            gaps.append("서비스미지정")
        if method in ("POST", "PUT", "PATCH") and _body_field_count(n, "request_body") == 0:
            gaps.append("요청스펙미정")
        if _body_field_count(n, "response_body") == 0:
            gaps.append("응답스펙미정")
        spec_gaps += len(gaps)
        gap_str = (" ⚠️" + "·".join(gaps)) if gaps else ""
        api_lines.append(f"- [ ] {label} — {name}{svc_str}{gap_str}  ←구현위치: ")
    sections.append(("APIs", api_lines))

    # [2026-06] 엔티티 중복 정리 — 추출 그래프에 같은 개념이 'Foo'+'FooEntity' 짝
    # (도메인명 vs 영속명)으로 들어오거나 동일 이름이 2회 실리는 경우가 있다(실사용:
    # TopUsersChartEntity 2회). 완전성을 깨지 않도록: (1) 이름·속성수가 '정확히' 같은
    # 중복만 1개로 합치고(정보량 0, 안전), (2) 'FooEntity'+'Foo' 짝은 드롭하지 않고
    # ⚠️중복가능 으로 표시해 사용자/에이전트가 통합 여부를 판단하게 한다.
    entity_norms = {
        _node_prop(n, "name", "title", "id").lower()
        for n in (spack.entities or [])
        if _node_prop(n, "name", "title", "id")
    }
    entity_lines = []
    seen_entities: set[tuple[str, int]] = set()
    for n in spack.entities or []:
        name = _node_prop(n, "name", "title", "id")
        norm = name.lower()
        attr_count = _node_list_len(n, "attributes")
        key = (norm, attr_count)
        if key in seen_entities:
            continue  # 이름·속성수 동일 = 정확 중복 → 1개로 합침
        seen_entities.add(key)
        if attr_count:
            suffix = f" (속성 {attr_count}개)"
        else:
            suffix = " ⚠️속성미정"
            spec_gaps += 1
        dup_note = ""
        if norm.endswith("entity") and norm[:-6] and norm[:-6] in entity_norms:
            dup_note = f" ⚠️중복가능(`{name[:-6]}` 와 동일 개념이면 통합)"
        entity_lines.append(f"- [ ] Entity `{name}`{suffix}{dup_note}  ←구현위치: ")
    sections.append(("Entities", entity_lines))

    policy_lines = []
    for n in spack.policies or []:
        # 정책 노드는 name/title 이 없어 id(POL-01 등, 합성값)로 라벨된다.
        pid = _node_prop(n, "name", "title", "id")
        # [2026-06] 규칙 본문(description)이 있으면 라벨에 노출 — 없으면 'POL-01' 만
        # 보여 에이전트가 무슨 규칙인지 모른 채 추측한다(실사용: 19개 중 9개가 본문 없는
        # 분류-only). 본문 없으면 ⚠️ 로 표시해 STOP 에서 PRD 근거 확인을 강제한다.
        desc = _node_prop(n, "description", "rule", "statement", "content", "detail")
        if desc:
            body = " ".join(desc.split())  # 개행/연속공백 정리 — md 리스트 항목 깨짐 방지
            policy_lines.append(f"- [ ] Policy `{pid}` — {body}  ←구현위치: ")
        else:
            cat = _node_prop(n, "category")
            cat_str = f" (분류: {cat})" if cat else ""
            spec_gaps += 1
            policy_lines.append(f"- [ ] Policy `{pid}`{cat_str} ⚠️정책내용미정  ←구현위치: ")
    sections.append(("Policies (비즈니스 규칙)", policy_lines))

    screen_lines = []
    for n in spack.screens or []:
        name = _node_prop(n, "name", "title", "id")
        path = _node_prop(n, "path", "route")
        suffix = f" (`{path}`)" if path else ""
        # 화면↔API 연결 — FE 가 어떤 API 를 조합해 그릴지 (Gemini 평가 #3 갭)
        calls = _node_json_list(n, "calls_apis")
        calls_str = f" (→ API: {', '.join(str(c) for c in calls)})" if calls else ""
        screen_lines.append(f"- [ ] Screen `{name}`{suffix}{calls_str}  ←구현위치: ")
    sections.append(("Screens (화면)", screen_lines))

    # [2026-06-14 오탐 픽스] Aggregate 는 schema 상 `invariants`(도메인 규칙)만 보유하고
    # `attributes` 는 구조적으로 존재하지 않는다 — 정의(schemas.py: aggregates→invariants),
    # 쓰기(cypher.py: agg.invariants 만 SET), 읽기/디코드(decode_aggregates_detail 는
    # invariants 만 복원) 전 경로에서 attributes 가 없다. 그런데 이전 코드는 aggregate 의
    # `attributes` 를 세어 0 이면 ⚠️속성미정 을 박았다 → 설계 품질과 무관하게 **모든**
    # Aggregate 가 100% 오탐으로 '속성미정' → 에이전트가 Phase 2(데이터 모델링)에서
    # "이 테이블 컬럼 뭐 넣죠?" 질문 지옥에 빠졌다(외부 평가 5.5/10 의 핵심 원인).
    # 실제 데이터 모델(속성)은 SPACK Entity(위 Entities 섹션)와 DDD DomainEntity(아래
    # 신설 섹션)에 있다. Aggregate 는 '정합성 경계'로서 invariants 를 정보로만 표기한다
    # (불변식 부재는 흔하고 정상 — STOP 을 강제할 갭이 아니다).
    # Aggregate → 소유 서비스 역매핑. owned_aggregate_names 는 읽기 후 ArchService 의
    # 평탄 list 속성(cypher.py: svc.owned_aggregate_names), owned_aggregates 는 lint
    # 경로 복원형 — 둘 다 수용. 값은 Aggregate name(string) 들이다.
    owner_by_agg: dict[str, str] = {}
    for s in arch.services or []:
        svc_name = _node_prop(s, "name", "id")
        owned = _node_json_list(s, "owned_aggregate_names") or _node_json_list(s, "owned_aggregates")
        for ag in owned:
            if isinstance(ag, str) and ag.strip():
                owner_by_agg.setdefault(ag.strip(), svc_name)

    agg_lines = []
    for n in ddd.aggregates or []:
        name = _node_prop(n, "name", "title", "id")
        inv_count = _node_list_len(n, "invariants")
        info = f" (불변식 {inv_count}개)" if inv_count else ""
        # [2026-06-14 오너십 갭] MSA(서비스 2개 이상)인데 이 Aggregate 를 소유하는 서비스가
        # 명시되지 않으면 에이전트가 '어느 서버에 이 데이터/로직을 둘지' 추측한다 — MSA 의
        # 가장 비싼 실수. API 의 ⚠️서비스미지정 과 대칭으로 STOP 을 강제한다. 단일 서비스면
        # 갈 곳이 자명하므로 갭 아님.
        owner = owner_by_agg.get(name)
        if owner:
            owner_str = f" [→ {owner}]"
        elif multi_service:
            owner_str = " ⚠️오너십미정"
            spec_gaps += 1
        else:
            owner_str = ""
        agg_lines.append(f"- [ ] Aggregate `{name}`{info}{owner_str}  ←구현위치: ")
    sections.append(("Aggregates (정합성 경계)", agg_lines))

    # [2026-06-14] DomainEntity = DDD 데이터 모델에서 실제 attributes(컬럼)를 가진 노드.
    # 속성 미정이면 DB/JPA 설계를 추측하게 되므로 ⚠️속성미정 으로 STOP 을 강제한다.
    # (원래 Aggregate 에 걸려던 '속성 미정 → 질문 강제' 의도를 구조적으로 올바른
    # 노드로 옮긴 것 — Gemini 평가 #2 의 본래 취지는 여기서 충족된다.)
    domain_entity_lines = []
    for n in ddd.domain_entities or []:
        name = _node_prop(n, "name", "title", "id")
        attr_count = _node_list_len(n, "attributes")
        if attr_count:
            suffix = f" (속성 {attr_count}개)"
        else:
            suffix = " ⚠️속성미정"
            spec_gaps += 1
        domain_entity_lines.append(f"- [ ] Domain Entity `{name}`{suffix}  ←구현위치: ")
    sections.append(("Domain Entities (데이터 모델)", domain_entity_lines))

    event_lines = []
    for n in ddd.domain_events or []:
        name = _node_prop(n, "name", "title", "id")
        event_lines.append(f"- [ ] Domain Event `{name}`  ←구현위치: ")
    sections.append(("Domain Events", event_lines))

    svc_lines = []
    for n in arch.services or []:
        name = _node_prop(n, "name", "title", "id")
        tech = _node_prop(n, "tech_stack")
        suffix = f" ({tech})" if tech else ""
        svc_lines.append(f"- [ ] Service `{name}`{suffix}  ←구현위치: ")
    for n in arch.databases or []:
        name = _node_prop(n, "name", "title", "id")
        tech = _node_prop(n, "tech_stack")
        suffix = f" ({tech})" if tech else ""
        svc_lines.append(f"- [ ] Database `{name}`{suffix}  ←구현위치: ")
    sections.append(("Services / Databases", svc_lines))

    total = sum(len(lines) for _, lines in sections)
    if total == 0:
        return "", 0, 0

    out = [
        f"# ✅ IMPLEMENTATION-CHECKLIST — {project_name}",
        "",
        "> 이 목록은 설계 그래프에서 **기계적으로 생성**되었습니다 — 명세 요약 과정의 누락이 없습니다.",
        "> ",
        "> **사용법 (AI 에이전트 필독):** 구현이 끝났다고 판단되면 이 파일의 각 항목을 실제 코드와",
        "> 대조해 `- [x]` 로 바꾸고, 항목 끝 `←구현위치:` 뒤에 **실제 파일 경로**를 적으세요.",
        "> 경로를 적은 파일은 실제로 **열어서 확인**하세요 — 열 수 없는 경로는 거짓 체크입니다.",
        "> 경로를 적을 수 없는 항목은 **미구현**입니다 — 구현한 뒤 다시 대조하세요.",
        f"> **모든 항목({total}개)이 [x] 가 될 때까지 이 루프를 반복**한 뒤에만 완료를 보고하세요.",
    ]
    if spec_gaps:
        out += [
            "> ",
            f"> ⚠️ **스펙 갭 {spec_gaps}건**: ⚠️ 표시 항목은 설계에 스키마(요청/응답/속성)·담당 서비스·정책 내용이 비어 있습니다.",
            "> **추측으로 채우지 마세요** — 해당 Phase 의 STOP 보고에서 사용자에게 확인을 받은 뒤 구현하세요.",
        ]
    out.append("")
    for title, lines in sections:
        if not lines:
            continue
        out.append(f"## {title} ({len(lines)})")
        out.extend(lines)
        out.append("")
    return "\n".join(out), total, spec_gaps


def _normalize_category(raw: str) -> str:
    # FE designExport.normalizeCategory 와 바이트 단위로 동일해야 함(zip 폴더명 ↔ orchestrator 참조경로 일치).
    # 공백류를 [\s\u0085\u001c-\u001f\uFEFF] 로 명시 — JS \s 와 Python \s 의 유니코드 공백 정의차
    # (NEL U+0085, BOM U+FEFF, 정보구분자 U+001C-1F)를 양쪽 동일 흡수. trim/strip 대신 앞뒤 '-' 제거.
    s = raw or ""
    s = re.sub(r"[\\/]+", "-", s)
    s = re.sub(r'[<>:*?"|]', "-", s)
    s = re.sub(r"[\s\u0085\u001c-\u001f\uFEFF]+", "-", s)
    s = re.sub(r"^-+|-+$", "", s)
    return s or "etc"


def get_category_from_skill(s) -> str:
    # FE designExport.getCategoryFromSkill 와 동일 규칙(zip 경로 ↔ orchestrator 참조 일치).
    # 1순위: cat: 마커(동적 폴더 카테고리 보존) → 정규화. 2순위(레거시): KNOWN_CATEGORIES.
    known_categories = {'frontEnd', 'backEnd', 'db', 'mobile', 'design', 'security', 'devops', 'testing', 'ai', 'core'}
    tags = s.tags or []
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("cat:"):
            return _normalize_category(tag[4:])
    for tag in tags:
        if tag in known_categories:
            return tag
    return 'etc'


def get_skill_path(s) -> str:
    category = get_category_from_skill(s)
    tags = s.tags or []
    # [#6] FE designExport.skillFileBase() 와 동일 규칙. 같은 카테고리에 같은 첫 태그를
    # 가진 스킬이 여러 개면 파일명이 겹쳐 zip 에서 silent overwrite 되던 문제를 막기 위해
    # id 를 suffix 로 붙여 유니크화한다. 이 경로가 zip 내 실제 파일 경로와 일치해야
    # 오케스트레이터가 지시하는 skills/ 참조가 깨지지 않는다.
    #   태그 있으면 `{tags[0]}-{id}`, 없으면 `{id}`. id = 선두 'SKL-' 제거 + 소문자.
    id_part = re.sub(r'^SKL-', '', s.id or '', flags=re.IGNORECASE).lower()
    non_cat = [t for t in tags if not (isinstance(t, str) and t.startswith("cat:"))]   # cat: 마커 제외한 첫 태그
    base_name = f"{non_cat[0]}-{id_part}" if non_cat else id_part
    # 경로 구분자('/'·'\')와 Windows 무효 문자('<>:*?"|')·공백을 '-' 로 치환.
    # FE skillFileBase 와 동일 규칙 — 두 함수가 다르면 orchestrator 경로와 zip 경로가 어긋난다.
    base_name = re.sub(r'[\\/]+', '-', base_name)
    base_name = re.sub(r'[<>:*?"|]', '-', base_name)
    base_name = base_name.replace(' ', '-')
    return f"skills/{category}/{base_name}.md"


# [2026-06-03] orchestrator 입력 섹션 상한.
# (2026-06 병렬화 이후) 입력이 MD 전문 → 그래프 digest 로 바뀌어 보통 수 KB 지만,
# 초대형 그래프(API 수백 개)에서 프롬프트가 폭주하지 않도록 보호용 상한은 유지한다.
_ORCHESTRATOR_SECTION_CAP = 24_000  # digest 1종당 최대 char


def _cap_for_orchestrator(md: str, cap: int = _ORCHESTRATOR_SECTION_CAP) -> str:
    """orchestrator 프롬프트에 넣기 전 MD 섹션을 상한까지만 자른다.

    잘릴 때는 LLM 이 "뒤가 잘렸다" 를 인지하도록 명시 마커를 남긴다 (정상 규모면 그대로).
    """
    if not md or len(md) <= cap:
        return md
    return md[:cap] + "\n\n<!-- (이하 생략: orchestrator 입력 크기 제한으로 일부 잘림) -->\n"


def _build_orchestrator_digests(
    spack: SpackGraph, ddd: DddGraph, arch: ArchitectureGraph
) -> tuple[str, str, str]:
    """orchestrator 입력용 결정적 digest — MD 산문 출력을 기다리지 않기 위한 그래프 요약.

    [2026-06 병렬화] orchestrator 의 태스크 분해에 필요한 건 '무엇이 있는지(목록)'이지
    MD 산문이 아니다. 그래프에서 직접 만들면 (1) Stage 2(3 MD LLM)와 **병렬** 실행 가능
    → 직렬로 더해지던 orchestrator 시간(~1-2분) 전액 절약, (2) 입력 ~72KB → 수 KB
    (비용↓ + 24K cap 절단으로 인한 불완전 플랜 문제 소멸).
    """
    # API→담당 서비스 매핑 포함 — 오케스트레이터가 서비스별로 태스크를 정확히 배치하게.
    svc_by_api = {
        r.source_id: (r.target_name or r.target_id)
        for r in (spack.api_service_rels or [])
        if getattr(r, "source_id", None)
    }
    spack_d = {
        "apis": [
            {
                "method": _node_prop(n, "method").upper(),
                "endpoint": _node_prop(n, "endpoint", "path"),
                "name": _node_prop(n, "name", "title", "id"),
                "service": svc_by_api.get(_node_prop(n, "id"), ""),
            }
            for n in (spack.apis or [])
        ],
        "entities": [
            {
                "name": _node_prop(n, "name", "title", "id"),
                "attribute_count": _node_list_len(n, "attributes"),
            }
            for n in (spack.entities or [])
        ],
        "policies": [_node_prop(n, "name", "title", "id") for n in (spack.policies or [])],
        "screens": [
            {
                "name": _node_prop(n, "name", "title", "id"),
                "path": _node_prop(n, "path", "route"),
            }
            for n in (spack.screens or [])
        ],
    }
    ddd_d = {
        "contexts": [_node_prop(n, "name", "title", "id") for n in (ddd.contexts or [])],
        "aggregates": [_node_prop(n, "name", "title", "id") for n in (ddd.aggregates or [])],
        "domain_events": [_node_prop(n, "name", "title", "id") for n in (ddd.domain_events or [])],
    }

    def _rel(c) -> Dict[str, Any]:
        d = c.model_dump() if hasattr(c, "model_dump") else (c if isinstance(c, dict) else {})
        return {
            "source": d.get("source_id", ""),
            "target": d.get("target_id", ""),
            "protocol": d.get("protocol") or d.get("type") or "",
        }

    arch_d = {
        "services": [
            {
                "name": _node_prop(n, "name", "title", "id"),
                "tech_stack": _node_prop(n, "tech_stack"),
            }
            for n in (arch.services or [])
        ],
        "databases": [
            {
                "name": _node_prop(n, "name", "title", "id"),
                "tech_stack": _node_prop(n, "tech_stack"),
            }
            for n in (arch.databases or [])
        ],
        "connections": [_rel(c) for c in (arch.connections or [])],
    }

    def _dump(d: Dict[str, Any]) -> str:
        return json.dumps(d, ensure_ascii=False, indent=1)

    return _dump(spack_d), _dump(ddd_d), _dump(arch_d)


async def _call_orchestrator_md(
    ctx: PipelineContext,
    spack_digest: str,
    ddd_digest: str,
    arch_digest: str,
    available_skills_str: str,
) -> str:
    # digest 는 보통 수 KB — 초대형 그래프(API 수백 개) 보호용으로 cap 은 유지.
    prompt = _render(
        _load_prompt("create_md_orchestrator.md"),
        spack_digest=_cap_for_orchestrator(spack_digest),
        ddd_digest=_cap_for_orchestrator(ddd_digest),
        arch_digest=_cap_for_orchestrator(arch_digest),
        available_skills=available_skills_str,
    )
    result = await ctx.gemini.generate(prompt, temperature=0.2)
    return strip_code_blocks(result.text)


async def _safe_get_all_skills(project_name: str):
    """[2026-05-29] skills 조회를 Stage 2 gather 에 합치기 위한 격리 wrapper.

    skills 조회 실패가 MD 생성 전체를 막지 않도록 빈 리스트로 흡수 (db resilience).
    """
    try:
        return await get_all_skills(project_name)
    except Exception as e:
        logger.warning("Failed to fetch skills for project %s: %s", project_name, e)
        return []


async def run_create_md_pipeline(
    ctx: PipelineContext, payload: CreateMdInput
) -> CreateMdResult:
    """
    fetch 3 graphs → 3 LLM 병렬 → 정리 반환.

    한 그래프가 비어있어도 LLM 은 호출 (빈 그래프 데이터 받으면
    LLM 이 "데이터 없음" 메시지 반환).
    """
    if not payload.project_name or not payload.project_name.strip():
        raise ValueError("project_name 은 비어 있을 수 없습니다.")

    logger.info(
        "create_md pipeline start: project=%s key=%s",
        payload.project_name,
        ctx.idempotency_key,
    )

    # [2026-06 진행 신호] FE 진행 표시가 elapsed 추정만으로 동작해 "98% 에서 멈춘 듯"
    # 보이던 문제 — design 파이프라인과 동일한 emit_stage 로 실제 구간을 보고한다.
    #   md:collecting                  그래프/체크리스트/skills 수집 중
    #   md:docs:<n>/4[:<완료목록>]     병렬 LLM 4건 중 n 건 완료 (목록은 콤마 구분)
    #   md:assembling                  결과 조립(완료 직전)
    # stage_callback 미배선(테스트/sync 호출)이면 emit_stage 가 no-op — 동작 불변.
    await ctx.emit_stage("md:collecting")

    # Stage 1: fetch 3 graphs (Neo4j 직렬 — driver 가 같은 connection 공유)
    spack = await get_spack_graph(payload.project_name)
    ddd = await get_ddd_graph(payload.project_name)
    arch = await get_architecture_graph(payload.project_name)

    # [2026-06] 전수 대조 체크리스트 — 그래프에서 결정적 생성 (LLM 0회, 실패 격리).
    checklist_md = ""
    checklist_count = 0
    checklist_spec_gaps = 0
    try:
        checklist_md, checklist_count, checklist_spec_gaps = build_implementation_checklist(
            payload.project_name, spack, ddd, arch
        )
    except Exception as e:  # noqa: BLE001 — 부가 산출물 실패가 MD 생성을 막지 않게
        logger.warning("implementation checklist build failed: %s", e)

    # skills 는 orchestrator 의 입력(Target Skill 매핑)이라 LLM gather 전에 먼저 조회.
    # Neo4j read-only 단건이라 수십 ms — 직렬 비용 무시 가능.
    skills_list = await _safe_get_all_skills(payload.project_name)
    skills_lines = []
    for s in skills_list:
        path = get_skill_path(s)
        # cat: 마커는 내부 카테고리 분류용 — orchestrator 문서엔 노출 안 함(FE skillToMd 와 동일).
        visible_tags = [t for t in (s.tags or []) if not (isinstance(t, str) and t.startswith("cat:"))]
        skills_lines.append(
            f"- {path} (Name: {s.name}, Scope: {s.scope or 'N/A'}, Tags: {', '.join(visible_tags)})"
        )
    skills_str = (
        "\n".join(skills_lines)
        if skills_lines
        else "(No custom skills defined. Use default/general guidelines.)"
    )
    spack_digest, ddd_digest, arch_digest = _build_orchestrator_digests(spack, ddd, arch)

    # 구간별 소요(ms) — "왜 느려?" 에 즉답하기 위한 관측성. diagnostic 으로 노출.
    timings_ms: Dict[str, int] = {}

    async def _timed(name: str, coro):
        t0 = time.monotonic()
        try:
            return await coro
        finally:
            timings_ms[name] = int((time.monotonic() - t0) * 1000)

    # Stage 2: 4 LLM 을 전부 병렬 — spack/ddd/arch MD + orchestrator.
    # [2026-06 병렬화] 기존엔 orchestrator 가 3 MD '출력'을 입력으로 받느라 Stage 3 에서
    # 직렬로 +1~2분 — 패키지 생성이 "95% 부근에서 오래 걸리는" 주범이었다. 입력을 그래프
    # digest(결정적, 수 KB)로 바꿔 의존을 제거 → 총시간 = max(spack, ddd, arch, orch).
    # orchestrator 실패는 기존 정책대로 격리(빈 문자열 degrade, 3종 MD 보존, job 은 성공).
    async def _safe_orchestrator() -> tuple[str, bool, str]:
        try:
            md = await _call_orchestrator_md(
                ctx, spack_digest, ddd_digest, arch_digest, skills_str
            )
            return md, False, ""
        except Exception as e:  # noqa: BLE001 — LLM 타임아웃/quota 등 모든 실패를 degrade 로 흡수
            logger.warning(
                "create_md orchestrator failed — degrading gracefully "
                "(spack/ddd/arch preserved): project=%s err=%s",
                payload.project_name,
                e,
            )
            return "", True, str(e)[:200]

    # [2026-06 진행 신호] 병렬 4건이 하나씩 끝날 때마다 누적 완료 목록을 emit —
    # FE 가 문서별 체크(✓)를 실시간으로 채울 수 있다. 각 emit 은 누적 전체를
    # 담으므로(마지막 쓰기 우선인 Redis stage 키 특성) 폴링이 중간 값을 놓쳐도 안전.
    _docs_done: list[str] = []
    _docs_lock = asyncio.Lock()

    async def _staged(doc_name: str, coro):
        try:
            return await coro
        finally:
            async with _docs_lock:
                _docs_done.append(doc_name)
                await ctx.emit_stage(
                    f"md:docs:{len(_docs_done)}/4:{','.join(_docs_done)}"
                )

    await ctx.emit_stage("md:docs:0/4")
    spack_md, ddd_md, arch_md, orch_result = await asyncio.gather(
        _staged("spack", _timed("spack_md", _call_spack_md(ctx, spack))),
        _staged("ddd", _timed("ddd_md", _call_ddd_md(ctx, ddd))),
        _staged("architecture", _timed("arch_md", _call_architecture_md(ctx, arch, spack))),
        _staged("orchestrator", _timed("orchestrator_md", _safe_orchestrator())),
    )
    orchestrator_md, orchestrator_failed, orchestrator_error = orch_result

    # 결과 조립 직전 — FE 의 '패키지 조립' 단계 (짧지만 실재하는 마지막 구간).
    await ctx.emit_stage("md:assembling")

    return CreateMdResult(
        project_name=payload.project_name,
        spack_md=spack_md,
        ddd_md=ddd_md,
        arch_md=arch_md,
        orchestrator_md=orchestrator_md,
        checklist_md=checklist_md,
        diagnostic={
            "timings_ms": timings_ms,
            "checklist_item_count": checklist_count,
            "checklist_spec_gap_count": checklist_spec_gaps,
            "spack_size": len(spack_md),
            "ddd_size": len(ddd_md),
            "arch_size": len(arch_md),
            "orchestrator_size": len(orchestrator_md),
            "orchestrator_failed": orchestrator_failed,
            "orchestrator_error": orchestrator_error,
            "spack_node_count": (
                len(spack.apis) + len(spack.entities) + len(spack.policies)
            ),
            "ddd_node_count": (
                len(ddd.contexts)
                + len(ddd.aggregates)
                + len(ddd.domain_entities)
                + len(ddd.domain_events)
            ),
            "arch_node_count": len(arch.services) + len(arch.databases),
        },
    )
