from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.pipelines.base import extract_json_object
from app.pipelines.lint_evidence import (
    Evidence,
    FileSample,
    collect_api_evidence,
    collect_class_evidence,
    collect_context_evidence,
    collect_event_evidence,
    collect_rule_evidence,
    collect_screen_evidence,
    collect_tech_stack_evidence,
)
from app.service.lint_repository import LintCase, LintCaseRule, LintEvidence, LintResult

logger = logging.getLogger(__name__)


def _evidence_to_dict_list(evs: List[Evidence]) -> List[LintEvidence]:
    return [
        LintEvidence(file=e.file, line=e.line, snippet=e.snippet, kind=e.kind)
        for e in evs
    ]


def _rule_id_for_api(api: Dict[str, Any]) -> str:
    method = (api.get("method") or "").upper() or "?"
    endpoint = api.get("endpoint") or api.get("name") or "?"
    return f"api:{method} {endpoint}"


def _rule_id_for_named(prefix: str, item: Dict[str, Any]) -> str:
    name = item.get("name") or item.get("id") or "?"
    return f"{prefix}:{name}"


def _normalize_instructions(raw: Any) -> List[str]:
    """Skill.instructions 를 cleaned List[str] 로. 비문자/공백 항목 제거."""
    if isinstance(raw, list):
        return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _build_cases(
    specs: Dict[str, Any],
    samples: List[FileSample],
) -> Tuple[List[LintCase], List[Dict[str, Any]]]:
    cases: List[LintCase] = [
        LintCase(title="SPACK 준수율", convergence=0, rules=[]),
        LintCase(title="DDD 준수율", convergence=0, rules=[]),
        LintCase(title="Architecture 준수율", convergence=0, rules=[]),
        LintCase(title="Rule Generator 준수율", convergence=0, rules=[]),
        # [2026-06] 기획 항목 (PRD Story + 설계 Screen) — 이전엔 FE 토큰 매칭 +
        # "체크리스트를 AI 도구에 붙여넣어 확인" 으로 사용자에게 떠넘기던 영역을
        # lint 가 자동 검증. 한국어 항목은 residual LLM 이 의미 매칭.
        LintCase(title="기획 항목 구현율", convergence=0, rules=[]),
    ]
    residual: List[Dict[str, Any]] = []

    def _append_rule(
        category_idx: int,
        rule_id: str,
        description: str,
        evidence_list: List[Evidence],
        hint: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        applied = len(evidence_list) > 0
        cases[category_idx].rules.append(LintCaseRule(
            rule=rule_id,
            description=description,
            applied=applied,
            evidence=_evidence_to_dict_list(evidence_list),
            detection_method="deterministic" if applied else "fallback",
        ))
        if not applied:
            item: Dict[str, Any] = {
                "category_idx": category_idx,
                "rule_idx": len(cases[category_idx].rules) - 1,
                "rule": rule_id,
                "description": description,
                "hint": hint,
            }
            if extra:
                item.update(extra)
            residual.append(item)

    # ─ SPACK ─
    spack = specs.get("spack") or {}
    for api in spack.get("apis") or []:
        evs = collect_api_evidence(api, samples)
        rid = _rule_id_for_api(api)
        desc = (api.get("description") or api.get("name") or "").strip() or rid
        _append_rule(
            0, rid, desc, evs,
            "API endpoint pattern match — FastAPI/Express/Spring/Vue/Django/React Router",
        )

    for entity in spack.get("entities") or []:
        name = entity.get("name") or ""
        evs = collect_class_evidence(name, samples, kind_label="entity_class")
        rid = _rule_id_for_named("entity", entity)
        desc = (entity.get("description") or name).strip() or rid
        _append_rule(0, rid, desc, evs, f"class/interface/type declaration '{name}'")

    for pol in spack.get("policies") or []:
        rid = _rule_id_for_named("policy", pol)
        desc = (pol.get("description") or pol.get("name") or "").strip() or rid
        # [2026-06 위양성 차단] 이전엔 4글자+ 토큰 substring 매칭 — 'audit' 가
        # 주석/변수명 어디에 있든 applied 처리돼 카테고리 중 가장 부정확했다.
        # 정책은 본질이 의미('감사 로그를 남긴다')라 토큰 등장은 증거가 아님 →
        # 결정적 패스를 폐기하고 전부 LLM residual (file:line 인용 강제)로 일원화.
        _append_rule(
            0, rid, desc, [],
            f"policy '{pol.get('name')}' (category={pol.get('category')}) — "
            "정책의 의미가 코드에 구현됐는지 (토큰 등장은 근거 아님)",
        )

    if not cases[0].rules:
        cases[0].rules.append(LintCaseRule(
            rule="spack:empty",
            description="SPACK 명세가 없습니다. createDesign 먼저 실행하세요.",
            applied=False, detection_method="fallback",
        ))

    # ─ DDD ─
    ddd = specs.get("ddd") or {}
    for ctx_item in ddd.get("contexts") or []:
        name = ctx_item.get("name") or ""
        evs = collect_context_evidence(name, samples)
        rid = _rule_id_for_named("context", ctx_item)
        desc = (ctx_item.get("description") or name).strip() or rid
        _append_rule(1, rid, desc, evs, f"bounded context '{name}' — 디렉토리 매칭")

    for agg in ddd.get("aggregates") or []:
        name = agg.get("name") or ""
        evs = collect_class_evidence(name, samples, kind_label="aggregate_class")
        rid = _rule_id_for_named("aggregate", agg)
        desc = (agg.get("description") or name).strip() or rid
        _append_rule(1, rid, desc, evs, f"aggregate root class '{name}'")

    for dent in ddd.get("domain_entities") or []:
        name = dent.get("name") or ""
        evs = collect_class_evidence(name, samples, kind_label="domain_entity_class")
        rid = _rule_id_for_named("dent", dent)
        desc = (dent.get("description") or name).strip() or rid
        _append_rule(1, rid, desc, evs, f"domain entity class '{name}'")

    for evt in ddd.get("domain_events") or []:
        name = evt.get("name") or ""
        evs = collect_event_evidence(name, samples)
        rid = _rule_id_for_named("event", evt)
        desc = (evt.get("description") or name).strip() or rid
        _append_rule(1, rid, desc, evs, f"event class '{name}' 또는 publish 호출")

    if not cases[1].rules:
        cases[1].rules.append(LintCaseRule(
            rule="ddd:empty",
            description="DDD 명세가 없습니다. createDesign 먼저 실행하세요.",
            applied=False, detection_method="fallback",
        ))

    # ─ Architecture ─
    arch = specs.get("architecture") or {}
    for svc in arch.get("services") or []:
        evs = collect_tech_stack_evidence(svc.get("tech_stack"), samples)
        rid = _rule_id_for_named("service", svc)
        desc = (svc.get("description") or svc.get("name") or "").strip() or rid
        _append_rule(
            2, rid, desc, evs,
            f"service '{svc.get('name')}' tech_stack={svc.get('tech_stack')} — manifest 매칭",
        )

    for db in arch.get("databases") or []:
        evs = collect_tech_stack_evidence(db.get("tech_stack"), samples)
        rid = _rule_id_for_named("database", db)
        desc = (db.get("description") or db.get("name") or "").strip() or rid
        _append_rule(
            2, rid, desc, evs,
            f"database '{db.get('name')}' tech_stack={db.get('tech_stack')} — manifest/config 매칭",
        )

    if not cases[2].rules:
        cases[2].rules.append(LintCaseRule(
            rule="arch:empty",
            description="Architecture 명세가 없습니다. createDesign 먼저 실행하세요.",
            applied=False, detection_method="fallback",
        ))

    # ─ Rules (Skill) ─
    # 다른 카테고리는 name/desc 만으로 token 매칭이 충분하지만, Rule Generator 의
    # instructions (List[str]) 는 규칙의 세부 본문이라 token 매칭으로 못 잡는다.
    # residual extra 로 함께 넘겨 LLM 이 본문 의미와 코드를 대조하도록 한다.
    for r in specs.get("rules") or []:
        evs = collect_rule_evidence(r, samples)
        rid = _rule_id_for_named("rule", r)
        desc = (r.get("description") or r.get("name") or "").strip() or rid
        instructions = _normalize_instructions(r.get("instructions"))
        _append_rule(
            3, rid, desc, evs,
            f"rule '{r.get('name')}' — instructions 본문 의미와 코드 대조 (token 매칭 불충분)",
            extra={"instructions": instructions} if instructions else None,
        )

    if not cases[3].rules:
        cases[3].rules.append(LintCaseRule(
            rule="rule:empty",
            description="Rule (Skill) 명세가 없습니다. 먼저 등록하세요.",
            applied=False, detection_method="fallback",
        ))

    # ─ 기획 (PRD Story + 설계 Screen) ─
    plan = specs.get("plan") or {}
    for sc in plan.get("screens") or []:
        evs = collect_screen_evidence(sc, samples)
        rid = _rule_id_for_named("screen", sc)
        desc = (sc.get("description") or sc.get("name") or "").strip() or rid
        path_part = f" route '{sc.get('path')}'" if sc.get("path") else ""
        _append_rule(
            4, rid, desc, evs,
            f"화면 '{sc.get('name')}' —{path_part} 정의 또는 화면 컴포넌트 구현",
            extra={"screen_path": sc.get("path") or ""},
        )

    for st in plan.get("stories") or []:
        name = (st.get("name") or "").strip()
        rid = f"story:{st.get('id') or name or '?'}"
        desc = name or (st.get("description") or "").strip() or rid
        # Story 제목은 대개 한국어 — 결정적 grep 불가. evidence 빈 채로 추가해
        # 전부 residual LLM 으로 (제목·설명의 의미 ↔ 코드 대조, file:line 인용 강제).
        st_desc = (st.get("description") or "").strip()
        _append_rule(
            4, rid, desc, [],
            "스토리 기능이 코드에 구현돼 있는지 — 제목·설명의 의미와 코드 대조",
            extra={"story_description": st_desc} if st_desc else None,
        )

    if not cases[4].rules:
        cases[4].rules.append(LintCaseRule(
            rule="plan:empty",
            description="기획 항목(Story/Screen)이 없습니다. 회의록 정리에서 PRD 를 먼저 생성하세요.",
            applied=False, detection_method="fallback",
        ))

    return cases, residual


def _compute_score(cases: List[LintCase]) -> int:
    # 카테고리 균등 가중 — 4개(legacy 결과)든 5개(기획 포함)든 동일 의미.
    if not cases:
        return 0
    weights = [1.0 / len(cases)] * len(cases)
    total_score = 0.0
    for c, w in zip(cases, weights):
        total = len(c.rules)
        if total == 0:
            conv = 0
        else:
            applied = sum(1 for r in c.rules if r.applied)
            conv = round((applied / total) * 100)
        c.convergence = conv
        total_score += conv * w
    return int(round(total_score))


def _empty_result(scanned_files: int, error: str) -> LintResult:
    return LintResult(
        score=0,
        scanned_files=scanned_files,
        rules_checked=0,
        violations=0,
        cases=[],
        error=error,
    )


def _normalize_result(llm_output: str, scanned_files: int) -> LintResult:
    """[Legacy] 회귀 호환만 — Phase A/B hybrid 로 대체됨."""
    parsed = extract_json_object(llm_output)
    if not parsed:
        return LintResult(
            score=0,
            scanned_files=scanned_files,
            rules_checked=0,
            violations=0,
            cases=[
                LintCase(title="SPACK 준수율", convergence=0),
                LintCase(title="DDD 준수율", convergence=0),
                LintCase(title="Architecture 준수율", convergence=0),
                LintCase(title="Rule Generator 준수율", convergence=0),
            ],
        )

    raw_cases = parsed.get("cases") or []
    cases: List[LintCase] = []
    for c in raw_cases:
        if not isinstance(c, dict):
            continue
        try:
            conv = int(c.get("convergence") or 0)
        except (TypeError, ValueError):
            conv = 0
        conv = max(0, min(100, conv))
        rules: List[LintCaseRule] = []
        for r in c.get("rules") or []:
            if not isinstance(r, dict):
                continue
            rules.append(
                LintCaseRule(
                    rule=str(r.get("rule") or ""),
                    description=str(r.get("description") or ""),
                    applied=bool(r.get("applied")),
                    detection_method="llm",
                )
            )
        cases.append(
            LintCase(title=str(c.get("title") or ""), convergence=conv, rules=rules)
        )

    total_rules = sum(len(c.rules) for c in cases)
    total_violations = sum(1 for c in cases for r in c.rules if not r.applied)
    try:
        score = int(parsed.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    try:
        rc = int(parsed.get("rulesChecked") or total_rules)
    except (TypeError, ValueError):
        rc = total_rules
    try:
        v = int(parsed.get("violations") or total_violations)
    except (TypeError, ValueError):
        v = total_violations

    return LintResult(
        score=score,
        scanned_files=scanned_files,
        rules_checked=rc,
        violations=v,
        cases=cases,
    )
