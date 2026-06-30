"""
API payload (request_body / response_body / path_params / query_params) 정규화.

[A-2 — 2026-05-25] API contract 객체화
이전 schema: API 노드에 method/endpoint/description 만 → 본문 schema 가
SPACK 그래프에 보존되지 않음. AI 에이전트가 임의 request/response 결정.

이후: 4개 필드가 PRD contract 보존:
- path_params:  endpoint 의 {param} 부분.       항상 list (없으면 []).
- query_params: GET 의 query string.            항상 list.
- request_body: POST/PUT/PATCH 의 body.        {content_type, fields[], example}.
- response_body: 성공 응답 본문.                 {status, content_type, fields[], example}.
                 에러 응답은 A-3 의 error_cases 에서.

설계 원칙 (A-1 의 attributes 와 동일):
1. **객체 형태가 canonical** — LLM 출력, normalize 결과, MD 입력 모두 객체.
2. **Neo4j 저장은 JSON string** — primitive 제약 우회. cypher.py 가 직렬화.
3. **read 헬퍼가 모든 입력 형태 흡수** — string / None / 객체 모두 정상 객체로.
4. **backward compat 우선** — 기존 API 노드 (4 필드 미존재) 도 read 시 빈 객체로
   복원돼 깨지지 않음. has_legacy_unknown_types 와 비슷한 시그널: payload
   미정의를 MD 가 ⚠️ 로 노출.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.pipelines.design_validator.attributes import normalize_entity_attributes

logger = logging.getLogger(__name__)


_PATH_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DEFAULT_CONTENT_TYPE = "application/json"
_METHODS_WITH_BODY = {"POST", "PUT", "PATCH"}


def _normalize_field_list(raw: Any) -> List[Dict[str, Any]]:
    """request_body.fields / path_params / query_params 의 list 정규화.

    Entity attributes 와 동일 형태이므로 normalize_entity_attributes 재사용.
    """
    return normalize_entity_attributes(raw)


def _normalize_example(raw: Any) -> str:
    """example 은 어떤 형태든 string 으로 보관 (LLM 출력 안정).

    - dict / list 면 json.dumps
    - string 이면 그대로
    - None / 빈 값이면 ""
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def normalize_request_body(raw: Any) -> Dict[str, Any]:
    """request_body 객체 정규화. 항상 4개 키 포함 (호출자 분기 제거).

    어떤 입력이든:
      None / 빈 dict   → {"content_type": "", "fields": [], "example": ""}
      JSON string      → parse 후 정규화
      객체            → 키 추출 후 형태 통일
    """
    if raw is None:
        return {"content_type": "", "fields": [], "example": ""}
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {"content_type": "", "fields": [], "example": ""}
        try:
            raw = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return {"content_type": "", "fields": [], "example": ""}
    if not isinstance(raw, dict):
        return {"content_type": "", "fields": [], "example": ""}
    return {
        "content_type": str(raw.get("content_type") or "").strip(),
        "fields": _normalize_field_list(raw.get("fields")),
        "example": _normalize_example(raw.get("example")),
    }


def normalize_response_body(raw: Any) -> Dict[str, Any]:
    """response_body 객체 정규화. status 가 0 이면 미명시."""
    if raw is None:
        return {"status": 0, "content_type": "", "fields": [], "example": ""}
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {"status": 0, "content_type": "", "fields": [], "example": ""}
        try:
            raw = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return {"status": 0, "content_type": "", "fields": [], "example": ""}
    if not isinstance(raw, dict):
        return {"status": 0, "content_type": "", "fields": [], "example": ""}
    # status 정규화 — int 못 변환되면 0.
    raw_status = raw.get("status")
    try:
        status = int(raw_status) if raw_status is not None else 0
    except (TypeError, ValueError):
        status = 0
    return {
        "status": status,
        "content_type": str(raw.get("content_type") or "").strip(),
        "fields": _normalize_field_list(raw.get("fields")),
        "example": _normalize_example(raw.get("example")),
    }


def extract_path_param_names(endpoint: str) -> List[str]:
    """endpoint 의 {param} 추출. 검증 (LLM 누락 vs endpoint 불일치) 에 활용."""
    if not isinstance(endpoint, str):
        return []
    return _PATH_PARAM_RE.findall(endpoint)


def normalize_api_payload(api: Dict[str, Any]) -> Dict[str, Any]:
    """API 객체 in-place 정규화. 원본 mutate (cypher 단계가 따로 복사하므로 안전).

    6개 payload 필드를 모두 객체 형태로 보장. 누락 필드는 빈 형태로 채움.
    [A-3] error_cases + auth 추가.
    """
    if not isinstance(api, dict):
        return api
    api["path_params"] = _normalize_field_list(api.get("path_params"))
    api["query_params"] = _normalize_field_list(api.get("query_params"))
    api["request_body"] = normalize_request_body(api.get("request_body"))
    api["response_body"] = normalize_response_body(api.get("response_body"))
    api["error_cases"] = normalize_error_cases(api.get("error_cases"))
    api["auth"] = normalize_auth(api.get("auth"))
    return api


def serialize_api_payload_for_neo4j(
    api: Dict[str, Any],
) -> Dict[str, str]:
    """API node 의 6개 payload 필드를 Neo4j 저장용 JSON string 으로.

    cypher.py 가 build 단계에서 호출. 원본 api dict 는 그대로 두고
    저장용 dict (6개 string) 반환.
    """
    return {
        "path_params": json.dumps(
            _normalize_field_list(api.get("path_params")), ensure_ascii=False
        ),
        "query_params": json.dumps(
            _normalize_field_list(api.get("query_params")), ensure_ascii=False
        ),
        "request_body": json.dumps(
            normalize_request_body(api.get("request_body")), ensure_ascii=False
        ),
        "response_body": json.dumps(
            normalize_response_body(api.get("response_body")), ensure_ascii=False
        ),
        "error_cases": json.dumps(
            normalize_error_cases(api.get("error_cases")), ensure_ascii=False
        ),
        "auth": json.dumps(
            normalize_auth(api.get("auth")), ensure_ascii=False
        ),
    }


def decode_apis_payload(apis: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Neo4j fetch 결과의 API list 의 6개 payload 필드를 객체로 복원.

    legacy API 노드 (필드 미존재) 도 빈 형태로 정규화돼 깨지지 않음.
    원본 mutate 회피 — 새 list.
    """
    out: List[Dict[str, Any]] = []
    for a in apis or []:
        if not isinstance(a, dict):
            continue
        copy = dict(a)
        copy["path_params"] = _normalize_field_list(a.get("path_params"))
        copy["query_params"] = _normalize_field_list(a.get("query_params"))
        copy["request_body"] = normalize_request_body(a.get("request_body"))
        copy["response_body"] = normalize_response_body(a.get("response_body"))
        copy["error_cases"] = normalize_error_cases(a.get("error_cases"))
        copy["auth"] = normalize_auth(a.get("auth"))
        out.append(copy)
    return out


def is_body_expected(method: Any) -> bool:
    """이 HTTP method 가 request body 를 가져야 하는지."""
    if not isinstance(method, str):
        return False
    return method.strip().upper() in _METHODS_WITH_BODY


# ─── [A-3 — 2026-05-25] error_cases + auth ─────────────────────────────


_VALID_STATUS_RANGE = range(400, 600)  # 4xx + 5xx


def _coerce_int(value: Any) -> int:
    """status 를 안전히 int 로. 변환 불가면 0."""
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


# [AI 초안 보완 — 2026-05-29] error_case / auth 에 AI 초안 출처 메타데이터를
# 부착할 수 있다. autofill 파이프라인이 채운 항목은 source="ai_draft",
# reviewed=False 로 마킹되며 scorer 가 미검토 초안을 절반(0.5)만 점수 인정한다.
# 정규화 시 이 메타는 **보존** (존재할 때만 출력) — 수동 입력(메타 없음)의
# 기존 동작과 round-trip 결과는 변하지 않는다.
def _carry_draft_meta(raw: Dict[str, Any], out: Dict[str, Any]) -> Dict[str, Any]:
    """raw 에 source/reviewed 메타가 있으면 out 에 보존. 없으면 out 그대로."""
    source = raw.get("source")
    if isinstance(source, str) and source.strip():
        out["source"] = source.strip()
    if "reviewed" in raw:
        out["reviewed"] = bool(raw.get("reviewed"))
    return out


def _coerce_error_case(raw: Any) -> Optional[Dict[str, Any]]:
    """단일 error_case 정규화. status 가 0 또는 범위 외면 drop."""
    if not isinstance(raw, dict):
        return None
    status = _coerce_int(raw.get("status"))
    if status not in _VALID_STATUS_RANGE:
        return None
    out = {
        "status": status,
        "code": str(raw.get("code") or "").strip(),
        "condition": str(raw.get("condition") or "").strip(),
        "message": str(raw.get("message") or "").strip(),
        "lineage_quote": str(raw.get("lineage_quote") or "").strip(),
    }
    return _carry_draft_meta(raw, out)


def normalize_error_cases(raw: Any) -> List[Dict[str, Any]]:
    """error_cases list 정규화. 4가지 입력 형태 흡수 (None/list/JSON string/dict)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            raw = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(raw, dict):
        # 단일 객체로 잘못 들어오면 list 승격.
        raw = [raw]
    if not isinstance(raw, list):
        return []

    # status 별로 첫 항목만 유지 (LLM 중복 출력 흡수).
    seen_status: set = set()
    out: List[Dict[str, Any]] = []
    for item in raw:
        case = _coerce_error_case(item)
        if case is None:
            continue
        if case["status"] in seen_status:
            continue
        seen_status.add(case["status"])
        out.append(case)
    # 안정 정렬 — status 오름차순. 결정성 보장.
    out.sort(key=lambda c: c["status"])
    return out


def normalize_auth(raw: Any) -> Dict[str, Any]:
    """auth 객체 정규화. None 이면 보수적 default (required=True, roles=[])."""
    if raw is None:
        return {
            "required": True,
            "required_roles": [],
            "ownership_check": "",
            "description": "",
        }
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {
                "required": True,
                "required_roles": [],
                "ownership_check": "",
                "description": "",
            }
        try:
            raw = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return {
                "required": True,
                "required_roles": [],
                "ownership_check": "",
                "description": "",
            }
    if not isinstance(raw, dict):
        return {
            "required": True,
            "required_roles": [],
            "ownership_check": "",
            "description": "",
        }
    roles_raw = raw.get("required_roles") or []
    if not isinstance(roles_raw, list):
        roles_raw = []
    # role 은 string 만 유지 + 중복 제거 (순서 안정).
    seen: set = set()
    roles: List[str] = []
    for r in roles_raw:
        if isinstance(r, str) and r.strip() and r not in seen:
            seen.add(r)
            roles.append(r.strip())
    out = {
        "required": bool(raw.get("required", True)),
        "required_roles": roles,
        "ownership_check": str(raw.get("ownership_check") or "").strip(),
        "description": str(raw.get("description") or "").strip(),
    }
    # [AI 초안 보완 — 2026-05-29] source/reviewed 메타 보존 (존재할 때만).
    return _carry_draft_meta(raw, out)
