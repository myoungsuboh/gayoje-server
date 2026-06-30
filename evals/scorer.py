"""
SPACK/DDD/Architecture 그래프 채점기.

[설계 — 2026-05-25 Phase C eval harness]
"AI 코딩 에이전트가 PRD 의 요구사항을 코드로 옮길 수 있는가" 를 정량 측정.
LLM 호출과 분리 — 그래프 dict 만 입력 받아 점수 산출 → 단위 테스트 가능.

4-tier 채점:
  Tier 1 (구조, 10%): 노드가 비어있지 않은지
  Tier 2 (디테일, 40%): A-1/A-2/A-3 로 보존되는 contract 채워짐
  Tier 3 (추적성, 25%): PRD ↔ 도출 항목 lineage
  Tier 4 (정합성, 25%): design_validator violations

가중치 합 = 100%. overall = weighted sum / 100.

채점 원칙:
- 모든 분모는 "해당 항목이 존재할 때만". 분모 0 이면 그 sub-metric 은 N/A
  (가중치에서 제외) — penalty 안 줌.
- 모든 점수 0.0 ~ 1.0. overall 도 동일.
- 실패 모드 (모든 grader 가 안전한 default 반환) — 예외 던지지 않음.

호출 예:
    from evals.scorer import score_spack
    report = score_spack(spack_dict, validation_report=val_dict)
    print(report.overall, report.tier2.score)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ─── 가중치 (전역) ───────────────────────────────────────────────────────
TIER1_WEIGHT = 0.10
TIER2_WEIGHT = 0.40
TIER3_WEIGHT = 0.25
TIER4_WEIGHT = 0.25
assert abs(TIER1_WEIGHT + TIER2_WEIGHT + TIER3_WEIGHT + TIER4_WEIGHT - 1.0) < 1e-6


@dataclass
class TierScore:
    score: float                                    # 0.0 ~ 1.0
    weight: float                                   # 전체 합 1.0
    sub_metrics: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)  # N/A 사유 등


@dataclass
class EvalReport:
    tier1: TierScore
    tier2: TierScore
    tier3: TierScore
    tier4: TierScore
    overall: float                                  # 0.0 ~ 1.0
    summary: Dict[str, Any] = field(default_factory=dict)


def _safe_ratio(numerator: int, denominator: int) -> Optional[float]:
    """분모 0 이면 None (N/A). penalty 안 줌."""
    if denominator <= 0:
        return None
    return numerator / denominator


def _average_present(values: List[Optional[float]]) -> Tuple[float, int]:
    """None 제외하고 평균. (avg, used_count) 반환. 모두 None 이면 (1.0, 0)
    — 분모 부재는 만점으로 (penalty 안 줌)."""
    present = [v for v in values if v is not None]
    if not present:
        return 1.0, 0
    return sum(present) / len(present), len(present)


# ─── Tier 1: 구조 ────────────────────────────────────────────────────────


def _score_tier1_structure(
    spack: Dict[str, Any],
    ddd: Optional[Dict[str, Any]] = None,
    arch: Optional[Dict[str, Any]] = None,
) -> TierScore:
    """노드가 비어있지 않은지. 가장 기본."""
    apis = spack.get("apis") or []
    entities = spack.get("entities") or []
    policies = spack.get("policies") or []

    metrics: Dict[str, float] = {
        "spack_apis_present": 1.0 if apis else 0.0,
        "spack_entities_present": 1.0 if entities else 0.0,
        "spack_policies_present": 1.0 if policies else 0.0,
    }

    if ddd is not None:
        contexts = ddd.get("contexts") or []
        aggregates = ddd.get("aggregates") or []
        metrics["ddd_contexts_present"] = 1.0 if contexts else 0.0
        metrics["ddd_aggregates_present"] = 1.0 if aggregates else 0.0

    if arch is not None:
        services = arch.get("services") or []
        databases = arch.get("databases") or []
        metrics["arch_services_present"] = 1.0 if services else 0.0
        metrics["arch_databases_present"] = 1.0 if databases else 0.0

    score = sum(metrics.values()) / len(metrics) if metrics else 1.0
    return TierScore(score=score, weight=TIER1_WEIGHT, sub_metrics=metrics)


# ─── Tier 2: 디테일 (A-1/A-2/A-3 보존성) ─────────────────────────────────


_BODY_METHODS = {"POST", "PUT", "PATCH"}

# [AI 초안 보완 — 2026-05-29] 자가참조 방지 가중치 → [2026-06-10 정책 폐지: 1.0]
# 원래 의도: autofill 초안(source="ai_draft", reviewed=False)은 절반(0.5)만 인정,
# 사람이 검토(reviewed=True)해야 1.0 — AI 가 채우고 AI 가 만점 받는 self-reference 방지.
#
# [왜 1.0 으로 바꿨나 — 사용자 검증 결과 정책이 형해화됨]
# 1) 단건 검토 UI 가 FE 에 존재하지 않음 — 항목을 '보고 확인'하는 경로 자체가 없음.
# 2) 유일한 동선이 '일괄 검토 완료' 버튼 = 내용을 안 본 채 0.5→1.0 고무도장.
#    방지하려던 자가참조를 바로 옆 원클릭이 무력화 — 사용자에겐 "거짓말 같은"
#    의미 불명의 두 번째 클릭만 남았다(완성도가 '채웠는데 안 오르는' 이탈 포인트).
# → 완성도의 질문은 "명세가 채워졌는가"이므로 초안도 채워진 명세로 인정(1.0).
#   신뢰 구분이 필요하면 점수 할인 대신 ai_draft 메타 기반 'AI 초안' 뱃지(투명성)가
#   맞는 자리 — source/reviewed 메타와 mark-reviewed 라우트는 호환 유지.
_AI_DRAFT_WEIGHT = 1.0


def _item_review_weight(item: Any) -> float:
    """단일 항목(error_case dict 또는 auth dict)의 점수 가중치.

    [2026-06-10] 정책 폐지로 사실상 항상 1.0 — 구조는 유지(메타 호환 + 정책 복원
    가능성). _AI_DRAFT_WEIGHT 상수만이 단일 스위치다.
    """
    if not isinstance(item, dict):
        return 1.0
    if item.get("source") != "ai_draft":
        return 1.0
    return 1.0 if item.get("reviewed") is True else _AI_DRAFT_WEIGHT


def _error_cases_credit(error_cases: Any) -> float:
    """API 의 error_cases 명시 점수(0.0~1.0).

    error_cases 가 비어있으면 0.0, 채워졌으면 각 항목의 review 가중치 중
    **최대값**을 인정. [2026-06-10] 0.5 정책 폐지로 채워진 항목은 출처 무관
    1.0 — max 구조는 정책 복원 가능성을 위해 유지.
    """
    cases = error_cases or []
    if not cases:
        return 0.0
    return max((_item_review_weight(c) for c in cases), default=0.0)


def _score_tier2_detail(
    spack: Dict[str, Any],
    ddd: Optional[Dict[str, Any]] = None,
    arch: Optional[Dict[str, Any]] = None,
) -> TierScore:
    """A + D 단계 contract 가 채워졌는지. 코드 생성 정확도의 핵심.

    - SPACK (A-1/A-2/A-3): Entity attributes / API payload / error_cases / auth
    - DDD (D-1): Aggregate invariants / DomainEntity attributes / Event payload
    - Architecture (D-2): Service deployment / Connection auth
    """
    apis = spack.get("apis") or []
    entities = spack.get("entities") or []

    sub: Dict[str, float] = {}
    notes: List[str] = []

    # ── A-1: Entity attributes 충실도 ──
    ent_with_attrs = [e for e in entities if e.get("attributes")]
    r = _safe_ratio(len(ent_with_attrs), len(entities))
    if r is not None:
        sub["entity_attributes_present_ratio"] = r
    else:
        notes.append("entities 0개 — entity_attributes_present 미산정")

    # 전체 attribute 중 type != "unknown" 비율
    total_attrs = 0
    typed_attrs = 0
    for e in entities:
        for a in e.get("attributes") or []:
            total_attrs += 1
            if a.get("type") and a.get("type") != "unknown":
                typed_attrs += 1
    r = _safe_ratio(typed_attrs, total_attrs)
    if r is not None:
        sub["attribute_typed_ratio"] = r
    else:
        notes.append("attributes 0개 — attribute_typed 미산정")

    # ── A-2: API request/response body / path/query params ──
    post_apis = [a for a in apis if str(a.get("method") or "").upper() in _BODY_METHODS]
    post_with_req = [
        a for a in post_apis
        if (a.get("request_body") or {}).get("fields")
    ]
    r = _safe_ratio(len(post_with_req), len(post_apis))
    if r is not None:
        sub["api_request_body_ratio"] = r
    else:
        notes.append("POST/PUT/PATCH 0개 — api_request_body 미산정")

    apis_with_res = [
        a for a in apis
        if (a.get("response_body") or {}).get("fields")
    ]
    r = _safe_ratio(len(apis_with_res), len(apis))
    if r is not None:
        sub["api_response_body_ratio"] = r

    # path_params 정합성 — endpoint 의 {p} 와 path_params 일치
    apis_with_path = 0
    apis_path_consistent = 0
    import re
    _PATH_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
    for a in apis:
        endpoint = str(a.get("endpoint") or "")
        endpoint_names = set(_PATH_RE.findall(endpoint))
        if not endpoint_names:
            continue
        apis_with_path += 1
        declared = {f.get("name") for f in (a.get("path_params") or [])}
        if endpoint_names <= declared:
            apis_path_consistent += 1
    r = _safe_ratio(apis_path_consistent, apis_with_path)
    if r is not None:
        sub["api_path_params_consistent_ratio"] = r

    # ── A-3: error_cases / auth ──
    # [AI 초안 보완 — 2026-05-29] 미검토 AI 초안(source=ai_draft, reviewed!=True)은
    # 0.5 만 인정 (자가참조 방지). 수동 입력/검토 완료는 1.0. 분모는 그대로 API 수.
    if apis:
        errors_credit = sum(_error_cases_credit(a.get("error_cases")) for a in apis)
        sub["api_error_cases_ratio"] = errors_credit / len(apis)

    # auth 명세율 — required True/False 가 명시 (default 가 아닌 의도적 선언)
    # default 가 required=True 라서 단순 required==True 로는 명시 판별 불가.
    # 대신 description 이 있거나 required_roles 가 비어있지 않으면 "의도적 명세".
    # 명시된 auth 가 미검토 AI 초안이면 0.5 만 인정.
    apis_auth_credit = 0.0
    for a in apis:
        auth = a.get("auth") or {}
        if auth.get("description") or auth.get("required_roles"):
            apis_auth_credit += _item_review_weight(auth)
    r = _safe_ratio(apis_auth_credit, len(apis))
    if r is not None:
        sub["api_auth_specified_ratio"] = r

    # POST/PUT/PATCH 의 422 명시율
    post_with_422 = 0
    for a in post_apis:
        statuses = {c.get("status") for c in (a.get("error_cases") or [])}
        if 422 in statuses:
            post_with_422 += 1
    r = _safe_ratio(post_with_422, len(post_apis))
    if r is not None:
        sub["api_validation_422_ratio"] = r

    # ── #3: Screen 명시율 (FE 코드 contract) ──
    # API 가 많은데 screens 가 적으면 FE 코드 정확도 저하.
    screens = spack.get("screens") or []
    # screens 가 비어있고 API 가 3+ 면 정보 부족 시그널 (0.0).
    # screens 가 있으면 calls_apis 매핑이 정상인지 + Story 매핑률 측정.
    if apis and len(apis) >= 3:
        if not screens:
            sub["screen_coverage_ratio"] = 0.0
            notes.append("API 3+ 인데 screens 0개 — FE 코드 정보 부족")
        else:
            # API ↔ Screen 양방향 — 각 API 가 적어도 1개 화면에서 호출되나
            api_ids = {a.get("id") for a in apis}
            api_called_set: set = set()
            for sc in screens:
                for api_id in sc.get("calls_apis") or []:
                    if api_id in api_ids:
                        api_called_set.add(api_id)
            r = _safe_ratio(len(api_called_set), len(apis))
            if r is not None:
                sub["screen_api_coverage_ratio"] = r
            # Story 매핑률
            screens_with_story = [s for s in screens if s.get("related_story_id")]
            r = _safe_ratio(len(screens_with_story), len(screens))
            if r is not None:
                sub["screen_story_mapped_ratio"] = r

    # ── D-1: DDD detail (Aggregate invariants / DomainEntity attributes / Event payload) ──
    if ddd is not None:
        aggregates = ddd.get("aggregates") or []
        agg_with_inv = [a for a in aggregates if a.get("invariants")]
        r = _safe_ratio(len(agg_with_inv), len(aggregates))
        if r is not None:
            sub["aggregate_invariants_ratio"] = r
        else:
            notes.append("aggregates 0개 — aggregate_invariants 미산정")

        domain_entities = ddd.get("domain_entities") or []
        de_with_attrs = [d for d in domain_entities if d.get("attributes")]
        r = _safe_ratio(len(de_with_attrs), len(domain_entities))
        if r is not None:
            sub["domain_entity_attributes_ratio"] = r

        events = ddd.get("domain_events") or ddd.get("events") or []
        ev_with_payload = [e for e in events if e.get("payload_fields")]
        r = _safe_ratio(len(ev_with_payload), len(events))
        if r is not None:
            sub["domain_event_payload_ratio"] = r

    # ── D-2: Architecture detail (deployment / connection auth) ──
    if arch is not None:
        services = arch.get("services") or []
        # Frontend 는 port=0 가능 — Backend/Worker 만 카운트.
        backend_services = [
            s for s in services
            if str(s.get("type") or "").lower() not in ("frontend", "static", "")
        ]
        # type 미명시 시 보수적으로 backend 로 간주 → 분모에 포함.
        if not backend_services:
            backend_services = services
        deployed = [
            s for s in backend_services
            if int((s.get("deployment") or {}).get("port") or 0) > 0
        ]
        r = _safe_ratio(len(deployed), len(backend_services))
        if r is not None:
            sub["service_deployment_ratio"] = r

        connections = arch.get("connections") or []
        # GraphRel pydantic 인 경우 .auth 가 없을 수도 — model_dump 시도 후 dict 추출.
        auth_specified = 0
        for c in connections:
            auth_val = None
            if isinstance(c, dict):
                auth_val = c.get("auth")
            else:
                # pydantic GraphRel — auth 필드 부재 가능. getattr 안전.
                auth_val = getattr(c, "auth", None)
            if auth_val and auth_val != "none":
                auth_specified += 1
        r = _safe_ratio(auth_specified, len(connections))
        if r is not None:
            sub["connection_auth_ratio"] = r

    score, _ = _average_present(list(sub.values()))
    return TierScore(score=score, weight=TIER2_WEIGHT, sub_metrics=sub, notes=notes)


# ─── Tier 3: 추적성 (PRD lineage) ────────────────────────────────────────


def _score_tier3_lineage(
    spack: Dict[str, Any],
    ddd: Optional[Dict[str, Any]] = None,
) -> TierScore:
    """PRD ↔ 도출 항목 매핑."""
    apis = spack.get("apis") or []
    entities = spack.get("entities") or []

    sub: Dict[str, float] = {}

    # API → Story 매핑률
    apis_with_story = [a for a in apis if a.get("related_story_id")]
    r = _safe_ratio(len(apis_with_story), len(apis))
    if r is not None:
        sub["api_story_mapped_ratio"] = r

    # Entity lineage confidence direct 비율
    direct = 0
    total_lineage = 0
    for e in entities:
        lineage = e.get("lineage") or {}
        conf = lineage.get("confidence")
        if conf:
            total_lineage += 1
            if conf == "direct":
                direct += 1
    r = _safe_ratio(direct, total_lineage)
    if r is not None:
        sub["entity_lineage_direct_ratio"] = r

    # Entity 의 related_stories 가 비어있지 않은 비율
    ent_with_stories = 0
    for e in entities:
        if (e.get("lineage") or {}).get("related_stories"):
            ent_with_stories += 1
    r = _safe_ratio(ent_with_stories, len(entities))
    if r is not None:
        sub["entity_story_mapped_ratio"] = r

    # DDD Aggregate lineage 매핑률
    if ddd is not None:
        aggregates = ddd.get("aggregates") or []
        agg_with_stories = 0
        for agg in aggregates:
            if (agg.get("lineage") or {}).get("related_stories"):
                agg_with_stories += 1
        r = _safe_ratio(agg_with_stories, len(aggregates))
        if r is not None:
            sub["aggregate_lineage_mapped_ratio"] = r

    score, _ = _average_present(list(sub.values()))
    return TierScore(score=score, weight=TIER3_WEIGHT, sub_metrics=sub)


# ─── Tier 4: 정합성 (validator violations) ───────────────────────────────


_ERROR_PENALTY_FULL = 1.0      # ERROR 1건만 있어도 0점
_WARNING_DECAY = 0.05          # WARNING 1건당 0.05 감점, 0.0 까지
_WARNING_FLOOR = 0.0


def _score_tier4_validation(validation_report: Optional[Dict[str, Any]]) -> TierScore:
    """design_validator violations. validation_report 가 없으면 N/A → 만점."""
    if not validation_report:
        return TierScore(score=1.0, weight=TIER4_WEIGHT,
                         sub_metrics={}, notes=["validation_report 부재 — 만점 처리"])

    errors = int(validation_report.get("total_errors", 0) or 0)
    warnings = int(validation_report.get("total_warnings", 0) or 0)
    infos = int(validation_report.get("total_infos", 0) or 0)

    # ERROR 가 하나라도 있으면 0점.
    if errors > 0:
        error_score = 0.0
    else:
        error_score = 1.0

    # WARNING 점수 — 1건당 _WARNING_DECAY 감점.
    warning_score = max(_WARNING_FLOOR, 1.0 - warnings * _WARNING_DECAY)

    # INFO 는 점수에 영향 없음 (정보성).

    # error 가중 0.6, warning 가중 0.4
    score = 0.6 * error_score + 0.4 * warning_score

    return TierScore(
        score=score,
        weight=TIER4_WEIGHT,
        sub_metrics={
            "error_score": error_score,
            "warning_score": warning_score,
            "errors": float(errors),
            "warnings": float(warnings),
            "infos": float(infos),
        },
    )


# ─── 메인 채점기 ─────────────────────────────────────────────────────────


def score_spack(
    spack: Dict[str, Any],
    *,
    ddd: Optional[Dict[str, Any]] = None,
    arch: Optional[Dict[str, Any]] = None,
    validation_report: Optional[Dict[str, Any]] = None,
) -> EvalReport:
    """그래프 dict 들 받아서 4-tier 점수 + overall.

    spack 만 필수. ddd/arch 는 있으면 더 정확. validation_report 는
    {"total_errors": N, "total_warnings": M, "total_infos": K} 형태.
    """
    t1 = _score_tier1_structure(spack, ddd, arch)
    t2 = _score_tier2_detail(spack, ddd, arch)
    t3 = _score_tier3_lineage(spack, ddd)
    t4 = _score_tier4_validation(validation_report)

    overall = (
        t1.score * t1.weight
        + t2.score * t2.weight
        + t3.score * t3.weight
        + t4.score * t4.weight
    )
    # [2026-05-25 fix] 빈 그래프 (Tier 1 = 0) 인데 다른 Tier 가 N/A 만점이라
    # overall 90% 표시되던 버그. 데이터 자체가 0 인데 충실도 90% 는 사용자
    # 오해의 직접 원인. Tier 1 가 절반 미만이면 overall 도 같은 비율로 강제.
    # (Tier 1 만점 = 모든 노드 존재. 0.5 이상은 부분 데이터로 다른 Tier 도
    # 실제 계산 가능 → 정상 weighting 유지.)
    if t1.score < 0.5:
        overall = t1.score * 0.9  # 데이터 부재 시 만점 근처에서 떨어뜨림

    return EvalReport(
        tier1=t1, tier2=t2, tier3=t3, tier4=t4,
        overall=overall,
        summary={
            "api_count": len(spack.get("apis") or []),
            "entity_count": len(spack.get("entities") or []),
            "policy_count": len(spack.get("policies") or []),
            "tier1": round(t1.score, 3),
            "tier2": round(t2.score, 3),
            "tier3": round(t3.score, 3),
            "tier4": round(t4.score, 3),
            "overall": round(overall, 3),
        },
    )


def render_report_text(report: EvalReport) -> str:
    """사람이 읽는 표 출력 — CLI 용."""
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append(f"Overall:  {report.overall*100:5.1f}%")
    lines.append("-" * 60)
    for label, tier in [
        ("Tier 1 (구조)",     report.tier1),
        ("Tier 2 (디테일)",   report.tier2),
        ("Tier 3 (추적성)",   report.tier3),
        ("Tier 4 (정합성)",   report.tier4),
    ]:
        lines.append(f"{label:20s} {tier.score*100:5.1f}%  (가중치 {tier.weight*100:.0f}%)")
        for key, value in sorted(tier.sub_metrics.items()):
            lines.append(f"  - {key:38s} {value*100:5.1f}%" if value <= 1.0
                         else f"  - {key:38s} {value:.0f}")
        for note in tier.notes:
            lines.append(f"  ⚠️  {note}")
    lines.append("=" * 60)
    return "\n".join(lines)
