from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from .types import Violation, ValidationReport, SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO
from .lineage import normalize_lineage
from .attributes import has_legacy_unknown_types, normalize_entity_attributes
from .api_payload import (
    extract_path_param_names,
    is_body_expected,
    normalize_api_payload,
)


# Entity name 형식: PascalCase 단일 합성어 (영문자 시작, 영숫자 only).
_PASCAL_CASE_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")

# Policy category 화이트리스트 (프롬프트 명시).
_POLICY_CATEGORIES: Set[str] = {"Audit", "Compliance", "EdgeCase", "Performance", "Security"}

# tech_stack 정규화 — 별칭 → 표준 명칭.
_TECH_STACK_CANONICAL: Dict[str, str] = {
    "vue": "Vue.js",
    "vuejs": "Vue.js",
    "vue.js": "Vue.js",
    "spring": "Spring Boot",
    "springboot": "Spring Boot",
    "spring boot": "Spring Boot",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "redis": "Redis",
    "react": "React",
    "next": "Next.js",
    "nextjs": "Next.js",
    "next.js": "Next.js",
    "node": "Node.js",
    "nodejs": "Node.js",
    "node.js": "Node.js",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "kafka": "Kafka",
}

# Service type 정렬 우선순위 (프롬프트 명시).
_SERVICE_TYPE_ORDER: Dict[str, int] = {
    "Frontend": 0,
    "Backend API": 1,
    "Background Worker": 2,
    "Gateway": 3,
}

# HTTP method 정렬 우선순위 (프롬프트 명시).
_HTTP_METHOD_ORDER: Dict[str, int] = {
    "GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4,
}


def _normalize_tech_stack(s: Any) -> Optional[str]:
    if not isinstance(s, str):
        return None
    key = re.sub(r"[\s\-_.]", "", s).lower()
    if key in _TECH_STACK_CANONICAL:
        return _TECH_STACK_CANONICAL[key]
    # 매핑에 없으면 원본 그대로 (LLM 이 알 수 없는 신규 스택을 쓸 수도 있음)
    return s.strip()


# ─── Spack ──────────────────────────────────────────────────────


def _sort_apis(apis: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """프롬프트 명시 정렬: related_story_id α → HTTP method 순서 → endpoint α."""
    def key(a: Dict[str, Any]) -> Tuple[str, int, str]:
        return (
            str(a.get("related_story_id") or "~"),  # 누락은 맨 뒤로
            _HTTP_METHOD_ORDER.get(str(a.get("method") or "").upper(), 99),
            str(a.get("endpoint") or ""),
        )
    return sorted(apis, key=key)


def _sort_entities(entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """프롬프트 명시 정렬: name α."""
    return sorted(entities, key=lambda e: str(e.get("name") or ""))


def _sort_policies(policies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """프롬프트 명시 정렬: category α → description α."""
    return sorted(
        policies,
        key=lambda p: (
            str(p.get("category") or "~"),
            str(p.get("description") or ""),
        ),
    )


def normalize_spack(
    spack: Dict[str, Any],
    *,
    valid_story_ids: Optional[Set[str]] = None,
) -> Tuple[Dict[str, Any], ValidationReport]:
    """
    Spack 출력 정규화 + 검증.

    [valid_story_ids — 2026-05]
    호출자가 PRD 의 실제 Story IDs set (정규화 형태 'Story-XX.Y') 을 전달하면
    Entity lineage 의 related_stories 가 그 set 안에 있는 것만 유지.
    fake / hallucinated story_id 자동 drop. None 이면 형식 검증만.

    정규화:
      1. apis / entities / policies 를 표준 순서로 정렬
      2. ID 재부여 — API-01, ENT-01, POL-01 시퀀스 (정렬 결과 = ID 순서)
      3. tech_stack / category 표준 명칭으로 (정책 화이트리스트는 위반은 그대로 두고 report)

    검증:
      - Entity name PascalCase
      - Policy category 화이트리스트
      - API related_story_id 누락 여부

    Returns: (normalized_spack, report)
    """
    report = ValidationReport(stage="spack")

    apis = list(spack.get("apis") or [])
    entities = list(spack.get("entities") or [])
    policies = list(spack.get("policies") or [])

    # ─ 정렬 ─
    apis = _sort_apis(apis)
    entities = _sort_entities(entities)
    policies = _sort_policies(policies)

    # ─ ID 재부여 (auto-fix) ─
    old_to_new_api_id: Dict[str, str] = {}
    for idx, a in enumerate(apis, start=1):
        new_id = f"API-{idx:02d}"
        old_id = str(a.get("id") or "")
        if old_id and old_id != new_id:
            report.add_fixed(Violation(
                code="API_ID_REASSIGNED", severity=SEVERITY_INFO, stage="spack",
                message=f"API id {old_id} → {new_id} (정렬 순서 기반 재부여)",
                item_id=new_id, detail={"old": old_id, "new": new_id},
            ))
            old_to_new_api_id[old_id] = new_id
        a["id"] = new_id

    for idx, e in enumerate(entities, start=1):
        new_id = f"ENT-{idx:02d}"
        old_id = str(e.get("id") or "")
        if old_id and old_id != new_id:
            report.add_fixed(Violation(
                code="ENTITY_ID_REASSIGNED", severity=SEVERITY_INFO, stage="spack",
                message=f"Entity id {old_id} → {new_id}",
                item_id=new_id, detail={"old": old_id, "new": new_id},
            ))
        e["id"] = new_id

    # [2026-05-28] Policy stub drop —
    # LLM 이 category/description 둘 다 비운 stub policy 를 만드는 케이스가 빈번.
    # schemas.py 가 required 를 강제하지 않아 정규화까지 통과 → save_spack 가 id 만
    # 저장 → FE 가 "내용 없음" 6개 카드만 노출 (사용자 무가치 노이즈).
    # category 또는 description 중 하나라도 의미있게 채워진 것만 보존.
    stub_dropped: List[str] = []
    meaningful_policies: List[Dict[str, Any]] = []
    for p in policies:
        if not isinstance(p, dict):
            continue
        cat = (p.get("category") or "").strip() if isinstance(p.get("category"), str) else ""
        desc = (p.get("description") or "").strip() if isinstance(p.get("description"), str) else ""
        if not cat and not desc:
            stub_dropped.append(str(p.get("id") or "?"))
            continue
        meaningful_policies.append(p)
    if stub_dropped:
        report.add(Violation(
            code="POLICY_STUB_DROPPED", severity=SEVERITY_WARNING, stage="spack",
            message=(
                f"Policy {len(stub_dropped)}개가 category·description 모두 비어서 drop "
                f"(LLM 환각 의심): {', '.join(stub_dropped[:6])}"
            ),
            detail={"dropped_ids": stub_dropped, "count": len(stub_dropped)},
        ))
    policies = meaningful_policies

    for idx, p in enumerate(policies, start=1):
        new_id = f"POL-{idx:02d}"
        old_id = str(p.get("id") or "")
        if old_id and old_id != new_id:
            report.add_fixed(Violation(
                code="POLICY_ID_REASSIGNED", severity=SEVERITY_INFO, stage="spack",
                message=f"Policy id {old_id} → {new_id}",
                item_id=new_id, detail={"old": old_id, "new": new_id},
            ))
        p["id"] = new_id

    # ─ 검증: API related_story_id 누락 ─
    for a in apis:
        if not a.get("related_story_id"):
            report.add(Violation(
                code="API_MISSING_STORY_REF", severity=SEVERITY_WARNING, stage="spack",
                message=f"API '{a.get('name')}' 가 related_story_id 누락 (Story 추적성 손실)",
                item_id=a.get("id"),
            ))

    # [A-2 — 2026-05-25] API payload (request/response body, path/query params)
    # 정규화 + 검증. 누락은 코드 생성 단계의 결정적 위험원.
    for a in apis:
        normalize_api_payload(a)
        method = str(a.get("method") or "").upper()
        endpoint = str(a.get("endpoint") or "")
        item_id = a.get("id")
        name = a.get("name")

        # [2026-06] method/endpoint/description 비어있음 — 코드 생성의 핵심 계약 누락.
        # 스키마 required 로 1차 강제하지만, structured output strict 미설정이라
        # 빈 문자열로 빠져나올 수 있어 여기서 안전망으로 표면화(드롭은 안 함).
        if not method:
            report.add(Violation(
                code="API_METHOD_MISSING", severity=SEVERITY_WARNING, stage="spack",
                message=f"API '{name}' 의 method(HTTP 동사) 가 비어있음.",
                item_id=item_id,
            ))
        if not endpoint:
            report.add(Violation(
                code="API_ENDPOINT_MISSING", severity=SEVERITY_WARNING, stage="spack",
                message=f"API '{name}' 의 endpoint(경로) 가 비어있음.",
                item_id=item_id,
            ))
        if not str(a.get("description") or "").strip():
            report.add(Violation(
                code="API_DESCRIPTION_MISSING", severity=SEVERITY_INFO, stage="spack",
                message=f"API '{name}' 의 description 이 비어있음.",
                item_id=item_id,
            ))

        # 1) request_body 검증 — POST/PUT/PATCH 인데 fields 비어 있으면 WARNING.
        if is_body_expected(method) and not a["request_body"]["fields"]:
            report.add(Violation(
                code="API_REQUEST_BODY_MISSING", severity=SEVERITY_WARNING, stage="spack",
                message=(
                    f"API '{name}' ({method}) 의 request_body.fields 가 비어있음 — "
                    "에이전트가 임의 schema 정의 위험."
                ),
                item_id=item_id,
            ))

        # 2) response_body 검증 — 모든 API 에 권장.
        if not a["response_body"]["fields"]:
            report.add(Violation(
                code="API_RESPONSE_BODY_MISSING", severity=SEVERITY_INFO, stage="spack",
                message=(
                    f"API '{name}' 의 response_body.fields 가 비어있음 — "
                    "응답 형태가 PRD 와 어긋날 위험."
                ),
                item_id=item_id,
            ))

        # 3) path_params 검증 — endpoint 의 {param} 과 path_params 의 name 일치.
        endpoint_names = set(extract_path_param_names(endpoint))
        declared_names = {f["name"] for f in a["path_params"]}
        missing = endpoint_names - declared_names
        extra = declared_names - endpoint_names
        if missing:
            report.add(Violation(
                code="API_PATH_PARAM_UNDECLARED", severity=SEVERITY_WARNING, stage="spack",
                message=(
                    f"API '{name}' endpoint 의 {{{','.join(sorted(missing))}}} 가 "
                    "path_params 에 선언되지 않음 — 에이전트가 타입 추측."
                ),
                item_id=item_id,
                detail={"missing": sorted(missing)},
            ))
        if extra:
            report.add(Violation(
                code="API_PATH_PARAM_ORPHAN", severity=SEVERITY_WARNING, stage="spack",
                message=(
                    f"API '{name}' path_params 에 선언된 {sorted(extra)} 가 "
                    "endpoint '{endpoint}' 에 없음 — 정합성 깨짐."
                ),
                item_id=item_id,
                detail={"extra": sorted(extra)},
            ))

        # [A-3 — 2026-05-25] error_cases / auth 검증.
        # normalize_api_payload 가 이미 두 필드를 정상 객체로 채워둠.
        # 의미적 누락 (있어야 할 status 가 없음) 만 여기서 점검.
        error_statuses = {c["status"] for c in a.get("error_cases") or []}

        # 4) error_cases 전체 부재 → INFO (적어도 5xx 는 명시되어야 함).
        if not error_statuses:
            report.add(Violation(
                code="API_ERROR_CASES_MISSING", severity=SEVERITY_INFO, stage="spack",
                message=(
                    f"API '{name}' 의 error_cases 가 비어있음 — "
                    "에이전트가 임의 status code / 메시지 결정 위험."
                ),
                item_id=item_id,
            ))

        # 5) POST/PUT/PATCH 인데 422 (validation error) 없음 → WARNING.
        if is_body_expected(method) and 422 not in error_statuses:
            report.add(Violation(
                code="API_VALIDATION_ERROR_CASE_MISSING",
                severity=SEVERITY_INFO, stage="spack",
                message=(
                    f"API '{name}' ({method}) 의 error_cases 에 422 가 없음 — "
                    "입력 검증 실패 응답 미정의."
                ),
                item_id=item_id,
            ))

        # 6) path_params 가 있는데 404 없음 → INFO (resource not found 누락).
        if endpoint_names and 404 not in error_statuses:
            report.add(Violation(
                code="API_NOT_FOUND_CASE_MISSING",
                severity=SEVERITY_INFO, stage="spack",
                message=(
                    f"API '{name}' 의 error_cases 에 404 가 없음 — "
                    "리소스 없음 응답 미정의 (path param 존재)."
                ),
                item_id=item_id,
            ))

        # 7) auth.required=True 인데 401 없음 → INFO.
        auth = a.get("auth") or {}
        if auth.get("required") and 401 not in error_statuses:
            report.add(Violation(
                code="API_AUTH_ERROR_CASE_MISSING",
                severity=SEVERITY_INFO, stage="spack",
                message=(
                    f"API '{name}' 는 인증 필수인데 error_cases 에 401 이 없음."
                ),
                item_id=item_id,
            ))

    # ─ 검증: Entity name PascalCase ─
    for e in entities:
        name = str(e.get("name") or "")
        if not name:
            report.add(Violation(
                code="ENTITY_NAME_MISSING", severity=SEVERITY_ERROR, stage="spack",
                message="Entity name 누락", item_id=e.get("id"),
            ))
        elif not _PASCAL_CASE_RE.match(name):
            report.add(Violation(
                code="ENTITY_NAME_NOT_PASCAL_CASE", severity=SEVERITY_WARNING, stage="spack",
                message=f"Entity name '{name}' 가 PascalCase 단일 합성어 형식 위반",
                item_id=e.get("id"), detail={"name": name},
            ))

    # [B3 — 2026-05 lineage] Entity lineage 정규화
    # LLM 이 만든 lineage 객체를 결정적 형태로 변환 + 검증. lineage 누락 / 형식 오류 /
    # 의미 불일치 (confidence='direct' 인데 stories=[] 등) 모두 normalize_lineage 가 흡수.
    # [A — 2026-05] valid_story_ids 전달 시 PRD 부재 story_id drop (fake 차단).
    for e in entities:
        e["lineage"] = normalize_lineage(
            e.get("lineage"),
            node_id=str(e.get("id") or "ENT-?"),
            stage="spack", report=report,
            valid_story_ids=valid_story_ids,
        )

    # [A-1 — 2026-05-25] Entity attributes 객체화 + 누락/legacy 검증.
    # LLM 이 string list 로 줘도, legacy 데이터 read 도, 모두 객체 list 로 통합.
    # 누락 / unknown type 비율은 downstream MD 가 ⚠️ 로 노출하지만, 여기서
    # report 에도 기록 — health gate / lint 가 활용.
    for e in entities:
        # [2026-06] description 비어있음 — 엔티티 의도 미상. 스키마 required 안전망.
        if not str(e.get("description") or "").strip():
            report.add(Violation(
                code="ENTITY_DESCRIPTION_MISSING", severity=SEVERITY_INFO, stage="spack",
                message=f"Entity '{e.get('name')}' 의 description 이 비어있음.",
                item_id=e.get("id"),
            ))
        raw = e.get("attributes")
        normalized = normalize_entity_attributes(raw)
        e["attributes"] = normalized
        if not normalized:
            report.add(Violation(
                code="ENTITY_ATTRIBUTES_MISSING", severity=SEVERITY_WARNING, stage="spack",
                message=(
                    f"Entity '{e.get('name')}' 에 attributes 가 비어있음 — "
                    "AI 에이전트가 임의 schema 정의 위험."
                ),
                item_id=e.get("id"),
            ))
        elif has_legacy_unknown_types(normalized):
            # 전체가 unknown 이면 legacy migrate 케이스 — 변환 LLM 재실행 권장.
            unknown_count = sum(1 for a in normalized if a.get("type") == "unknown")
            report.add(Violation(
                code="ENTITY_ATTRIBUTES_LEGACY_UNKNOWN_TYPE",
                severity=SEVERITY_INFO, stage="spack",
                message=(
                    f"Entity '{e.get('name')}' 의 {unknown_count}/{len(normalized)}개 "
                    "attribute 가 type=unknown (legacy 형태 또는 LLM 누락). "
                    "변환 단계에서 PRD 의 타입 정보 보강 필요."
                ),
                item_id=e.get("id"),
                detail={"unknown_count": unknown_count, "total": len(normalized)},
            ))

    # ─ 검증: Policy category 화이트리스트 ─
    for p in policies:
        cat = p.get("category")
        if not cat:
            report.add(Violation(
                code="POLICY_CATEGORY_MISSING", severity=SEVERITY_ERROR, stage="spack",
                message="Policy category 누락", item_id=p.get("id"),
            ))
        elif cat not in _POLICY_CATEGORIES:
            report.add(Violation(
                code="POLICY_CATEGORY_UNKNOWN", severity=SEVERITY_WARNING, stage="spack",
                message=(
                    f"Policy category '{cat}' 가 화이트리스트 외부. "
                    f"허용: {sorted(_POLICY_CATEGORIES)}"
                ),
                item_id=p.get("id"), detail={"category": cat},
            ))

    # [#3 — 2026-05-25] Screens 정규화 + 검증.
    # id/name/path 필수. calls_apis 의 API id 가 실제 존재하는지 점검.
    api_id_set = {a.get("id") for a in apis}
    screens_raw = list(spack.get("screens") or [])
    screens: List[Dict[str, Any]] = []
    seen_paths: Set[str] = set()
    for idx, sc in enumerate(screens_raw, start=1):
        if not isinstance(sc, dict):
            continue
        name = str(sc.get("name") or "").strip()
        path = str(sc.get("path") or "").strip()
        if not name or not path:
            report.add(Violation(
                code="SCREEN_INVALID", severity=SEVERITY_WARNING, stage="spack",
                message=f"Screen #{idx}: name 또는 path 누락 → drop",
                item_id=sc.get("id"),
            ))
            continue
        # 경로 중복은 first-wins (결정성).
        if path in seen_paths:
            report.add(Violation(
                code="SCREEN_PATH_DUPLICATE", severity=SEVERITY_INFO, stage="spack",
                message=f"Screen path '{path}' 중복 — 첫 항목만 유지",
                item_id=sc.get("id"),
            ))
            continue
        seen_paths.add(path)
        # calls_apis 정규화 — API id 가 실제 존재하는지 검증.
        calls_raw = sc.get("calls_apis") or []
        if not isinstance(calls_raw, list):
            calls_raw = []
        calls_apis: List[str] = []
        for api_id in calls_raw:
            if not isinstance(api_id, str):
                continue
            if api_id_set and api_id not in api_id_set:
                report.add(Violation(
                    code="SCREEN_UNKNOWN_API",
                    severity=SEVERITY_WARNING, stage="spack",
                    message=f"Screen '{name}' 의 calls_apis 에 알 수 없는 API '{api_id}'",
                    item_id=sc.get("id"),
                    detail={"api_id": api_id},
                ))
                continue
            calls_apis.append(api_id)
        next_screens_raw = sc.get("next_screens") or []
        if not isinstance(next_screens_raw, list):
            next_screens_raw = []
        next_screens = [s for s in next_screens_raw if isinstance(s, str) and s.strip()]

        screens.append({
            "id": f"SCREEN-{idx:02d}",
            "name": name,
            "path": path,
            "description": str(sc.get("description") or "").strip(),
            "related_story_id": str(sc.get("related_story_id") or "").strip(),
            "calls_apis": calls_apis,
            "next_screens": next_screens,
        })

    # screens 가 비어있어도 valid — 백엔드 only 시스템은 화면 X.
    # 그러나 API 가 5개+ 인데 screens 0개면 INFO (FE 코드 생성 정보 부족).
    if apis and len(apis) >= 3 and not screens:
        report.add(Violation(
            code="NO_SCREENS_FOR_APIS", severity=SEVERITY_INFO, stage="spack",
            message=(
                f"{len(apis)}개 API 가 있지만 screens 가 비어있음 — "
                "FE 화면 코드 생성에 정보 부족. PRD 에 화면 흐름 명시 권장."
            ),
        ))

    normalized: Dict[str, Any] = {
        "apis": apis,
        "entities": entities,
        "policies": policies,
        "screens": screens,
        "_id_remap": old_to_new_api_id,  # 후속 stage 에서 reference 갱신용
    }
    return normalized, report
