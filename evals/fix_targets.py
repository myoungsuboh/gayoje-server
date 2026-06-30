"""
[2026-05-28] PRD 완성도 — 구체적 보강 대상 추출기 (fix targets).

scorer.py 는 비율(예: api_error_cases_ratio=0.0)만 산출한다. 그러나 사용자는
"어느 API 가, 무엇이 빠졌는지" 를 모르면 손을 못 댄다 ("정확히 어딜 고쳐야하는지
진짜 떠먹여줘야 안다니깐?").

이 모듈은 scorer 와 동일한 판정 기준을 쓰되, **실패한 개별 항목의 id/name 까지**
수집해 사용자가 콕 집어 고칠 수 있는 actionable list 를 만든다.

각 fix target:
  {
    "metric_key": "api_error_cases_ratio",
    "label": "API 에러 응답 명시",
    "tier": 2,
    "missing": [{"id": "API-01", "name": "작업 생성"}, ...],   # 빠진 항목들
    "total": 24,                                              # 분모
    "fix": "PRD Epic & Story 탭에서 ... 추가",                  # 사용자 액션
    "prd_section": "epic",                                   # FE 점프 대상 탭
  }

LLM 호출 없음 — 순수 dict 분석. eval_score_routes 에서 호출.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


_BODY_METHODS = {"POST", "PUT", "PATCH"}
_PATH_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 한 metric 당 사용자에게 보여줄 최대 항목 수 (너무 길면 압도됨).
_MAX_ITEMS_PER_TARGET = 8

# scorer.py 와 동일 — overall = Σ tierᵢ.score × weightᵢ.
_TIER_WEIGHTS = {1: 0.10, 2: 0.40, 3: 0.25, 4: 0.25}


def delta_pct_for(now: float, n_tier_metrics: int, tier_weight: float) -> int:
    """이 metric 을 now→1.0 까지 채웠을 때 overall 상승분(%, 근사). 최소 1.

    tier.score 는 그 tier 의 sub_metrics 평균이므로, 한 metric 을 now→1.0 으로
    올리면 tier.score 는 (1.0-now)/N 만큼 오르고, overall 은 그 값 ×tier_weight.
    "약 +X%" 로만 노출하므로 정수 반올림 + 하한 1.
    """
    if n_tier_metrics <= 0:
        return 1
    delta = (1.0 - now) / n_tier_metrics * tier_weight * 100.0
    return max(1, round(delta))


def _item_ref(node: Dict[str, Any]) -> Dict[str, str]:
    """노드에서 사용자 표시용 (id, name) 추출."""
    return {
        "id": str(node.get("id") or node.get("identity") or "?"),
        "name": str(
            node.get("name")
            or node.get("title")
            or node.get("summary")
            or node.get("id")
            or "이름 없음"
        ),
    }


def _target(
    metric_key: str,
    label: str,
    tier: int,
    missing: List[Dict[str, str]],
    total: int,
    fix: str,
    prd_section: str = "",
) -> Optional[Dict[str, Any]]:
    """fix target dict 생성. 빠진 항목 0 이면 None (보강 불필요)."""
    if not missing:
        return None
    return {
        "metric_key": metric_key,
        "label": label,
        "tier": tier,
        "missing": missing[:_MAX_ITEMS_PER_TARGET],
        "missing_total": len(missing),
        "total": total,
        "fix": fix,
        "prd_section": prd_section,
    }


def collect_fix_targets(
    spack: Dict[str, Any],
    ddd: Optional[Dict[str, Any]] = None,
    arch: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """SPACK/DDD/Arch 그래프에서 구체적 보강 대상 list 반환.

    scorer 의 ratio 가 1.0 미만인 항목에 대해, 실패한 개별 노드의 id/name 을 수집.
    심각도 순 (빠진 비율 높은 것 우선) 정렬.
    """
    apis = spack.get("apis") or []
    entities = spack.get("entities") or []
    targets: List[Dict[str, Any]] = []

    # ── Tier 2: Entity 필드 명시 ──
    ent_missing_attrs = [_item_ref(e) for e in entities if not e.get("attributes")]
    targets.append(_target(
        "entity_attributes_present_ratio", "데이터 필드 명시", 2,
        ent_missing_attrs, len(entities),
        "PRD Epic & Story 탭에서 해당 데이터를 다루는 Story 본문에 "
        "'- 입력: name(문자열), email(이메일)' 같이 필드 목록을 적으세요.",
        "epic",
    ))

    # ── Tier 2: API 입력(request body) — POST/PUT/PATCH 만 ──
    post_apis = [a for a in apis if str(a.get("method") or "").upper() in _BODY_METHODS]
    post_missing_req = [
        _item_ref(a) for a in post_apis
        if not (a.get("request_body") or {}).get("fields")
    ]
    targets.append(_target(
        "api_request_body_ratio", "API 입력 명세", 2,
        post_missing_req, len(post_apis),
        "PRD Epic & Story 탭에서 이 API 에 해당하는 Story 본문에 "
        "'- 입력: { 필드명: 타입 }' 형식으로 받는 데이터를 적으세요.",
        "epic",
    ))

    # ── Tier 2: API 출력(response body) ──
    missing_res = [
        _item_ref(a) for a in apis
        if not (a.get("response_body") or {}).get("fields")
    ]
    targets.append(_target(
        "api_response_body_ratio", "API 출력 명세", 2,
        missing_res, len(apis),
        "PRD Epic & Story 탭에서 이 API 의 Story 본문에 "
        "'- 출력: { 필드명: 타입 }' 형식으로 응답 내용을 적으세요.",
        "epic",
    ))

    # ── Tier 2: API 에러 응답 ──
    missing_errors = [_item_ref(a) for a in apis if not a.get("error_cases")]
    targets.append(_target(
        "api_error_cases_ratio", "API 에러 응답 명시", 2,
        missing_errors, len(apis),
        "PRD Epic & Story 탭에서 이 API 의 Story 본문에 "
        "'권한 없으면 401', '데이터 없으면 404' 같이 실패 케이스를 적으세요.",
        "epic",
    ))

    # ── Tier 2: API 인증 방식 ──
    missing_auth = []
    for a in apis:
        auth = a.get("auth") or {}
        if not (auth.get("description") or auth.get("required_roles")):
            missing_auth.append(_item_ref(a))
    targets.append(_target(
        "api_auth_specified_ratio", "API 인증 방식 명시", 2,
        missing_auth, len(apis),
        "PRD NFR 탭에 인증 방식을 적거나(예: 'OAuth 로그인', '관리자만'), "
        "각 Story 에 '로그인 필요' 여부를 명시하세요.",
        "nfr",
    ))

    # ── Tier 2: 경로 변수 정합성 ──
    inconsistent_path = []
    for a in apis:
        endpoint = str(a.get("endpoint") or "")
        endpoint_names = set(_PATH_RE.findall(endpoint))
        if not endpoint_names:
            continue
        declared = {f.get("name") for f in (a.get("path_params") or [])}
        if not (endpoint_names <= declared):
            inconsistent_path.append(_item_ref(a))
    targets.append(_target(
        "api_path_params_consistent_ratio", "API 경로 변수 일관성", 2,
        inconsistent_path, len(apis),
        "이 API 의 경로에 있는 {id} 같은 변수가 path_params 에 빠졌습니다. "
        "PRD 의 해당 API 경로 변수를 명확히 하고 재실행하세요.",
        "epic",
    ))

    # ── Tier 2: 화면 ↔ 기능(Story) 연결 ──
    screens = spack.get("screens") or []
    screens_no_story = [_item_ref(s) for s in screens if not s.get("related_story_id")]
    targets.append(_target(
        "screen_story_mapped_ratio", "화면 ↔ 기능(Story) 연결", 2,
        screens_no_story, len(screens),
        "PRD Screens 탭에서 이 화면 섹션에 '`[Story 1.1]` 기능명' 형태로 "
        "어느 기능을 위한 화면인지 참조를 추가하세요.",
        "screen",
    ))

    # ── Tier 3: API ↔ Story 추적 ──
    apis_no_story = [_item_ref(a) for a in apis if not a.get("related_story_id")]
    targets.append(_target(
        "api_story_mapped_ratio", "API ↔ 기능(Story) 추적", 3,
        apis_no_story, len(apis),
        "이 API 가 어느 PRD Story 에서 왔는지 연결이 끊겼습니다. "
        "PRD 의 Story id (Story 1.1 형식) 가 일관적인지 확인 후 재실행하세요.",
        "epic",
    ))

    # ── Tier 3: Entity ↔ Story 추적 ──
    ent_no_story = []
    for e in entities:
        if not (e.get("lineage") or {}).get("related_stories"):
            ent_no_story.append(_item_ref(e))
    targets.append(_target(
        "entity_story_mapped_ratio", "데이터 ↔ 기능(Story) 추적", 3,
        ent_no_story, len(entities),
        "이 데이터 항목이 어느 Story 에서 도출됐는지 출처가 없습니다. "
        "PRD Story 본문에 해당 데이터를 다루는 내용을 명확히 적으세요.",
        "epic",
    ))

    # ── DDD: Aggregate 무결성 규칙 ──
    if ddd is not None:
        aggregates = ddd.get("aggregates") or []
        agg_no_inv = [_item_ref(a) for a in aggregates if not a.get("invariants")]
        targets.append(_target(
            "aggregate_invariants_ratio", "모델 무결성 규칙", 2,
            agg_no_inv, len(aggregates),
            "이 도메인 모델의 '항상 지켜야 하는 규칙' 이 없습니다. "
            "PRD 에 '주문은 결제 후 수량 변경 불가' 같은 제약을 적으세요.",
            "epic",
        ))

    # None (보강 불필요) 제거 + 심각도(빠진 비율) 순 정렬.
    targets = [t for t in targets if t]

    def _severity(t: Dict[str, Any]) -> float:
        total = t.get("total") or 0
        if not total:
            return 0.0
        return t["missing_total"] / total

    targets.sort(key=_severity, reverse=True)
    return targets
