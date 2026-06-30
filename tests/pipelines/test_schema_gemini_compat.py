"""
[A-1/A-2/A-3 schema 호환성 정적 검증]

Gemini structured output (responseSchema) 의 받는 JSON Schema subset 은
표준 JSON Schema 보다 제한적:
  - 지원: type, properties, items, required, enum, format, description,
          nullable, minimum, maximum 등 기본
  - 미지원: $ref, allOf, oneOf, anyOf, not, additionalProperties, default,
          const, patternProperties

우리 확장 schema 가 이 subset 안에 머무는지 미리 잡아 LLM 호출 시점에
schema 오류로 깨지는 일을 차단.

또한 schema 자체가 JSON Schema 표준에 부합하는지 jsonschema 라이브러리로
meta-validation.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

import pytest

from app.pipelines.design_pipeline.schemas import (
    ARCHITECTURE_AGENT_SCHEMA,
    DDD_AGENT_SCHEMA,
    SPACK_AGENT_SCHEMA,
)


# Gemini responseSchema 에서 명시적으로 미지원/제한된 키.
# https://ai.google.dev/api/generate-content#schema 의 OpenAPI 3.0 subset 기준.
_GEMINI_UNSUPPORTED_KEYS = {
    "$ref", "$schema", "$id",
    "allOf", "oneOf", "anyOf", "not",
    "additionalProperties",   # boolean True/False 모두 보통 미지원
    "patternProperties",
    "default", "const",
    "if", "then", "else",
    "dependencies", "dependentSchemas",
}


def _walk_schema(schema: Dict[str, Any], path: str = "$") -> List[str]:
    """schema 전체를 walk 하며 미지원 키 발견 시 경로 list 반환."""
    violations: List[str] = []
    if not isinstance(schema, dict):
        return violations

    for key in schema.keys():
        if key in _GEMINI_UNSUPPORTED_KEYS:
            violations.append(f"{path}.{key}")

    if "properties" in schema and isinstance(schema["properties"], dict):
        for prop_name, prop_schema in schema["properties"].items():
            violations.extend(_walk_schema(prop_schema, f"{path}.properties.{prop_name}"))

    if "items" in schema and isinstance(schema["items"], dict):
        violations.extend(_walk_schema(schema["items"], f"{path}.items"))

    return violations


def _max_nesting_depth(schema: Dict[str, Any], current: int = 0) -> int:
    """schema 의 최대 nesting 깊이 측정. Gemini 가 너무 깊은 nesting 처리 불안."""
    if not isinstance(schema, dict):
        return current
    deepest = current
    if "properties" in schema and isinstance(schema["properties"], dict):
        for prop_schema in schema["properties"].values():
            deepest = max(deepest, _max_nesting_depth(prop_schema, current + 1))
    if "items" in schema and isinstance(schema["items"], dict):
        deepest = max(deepest, _max_nesting_depth(schema["items"], current + 1))
    return deepest


# ─── 정적 호환성 검증 ──────────────────────────────────────────────────


def test_spack_schema_uses_only_gemini_supported_keys():
    """SPACK_AGENT_SCHEMA 가 Gemini 미지원 키 (allOf/oneOf/$ref 등) 안 씀."""
    violations = _walk_schema(SPACK_AGENT_SCHEMA)
    assert violations == [], (
        f"SPACK schema 가 Gemini responseSchema 미지원 키 사용:\n  "
        + "\n  ".join(violations)
    )


def test_ddd_schema_uses_only_gemini_supported_keys():
    violations = _walk_schema(DDD_AGENT_SCHEMA)
    assert violations == [], (
        f"DDD schema 미지원 키:\n  " + "\n  ".join(violations)
    )


def test_architecture_schema_uses_only_gemini_supported_keys():
    violations = _walk_schema(ARCHITECTURE_AGENT_SCHEMA)
    assert violations == [], (
        f"Architecture schema 미지원 키:\n  " + "\n  ".join(violations)
    )


# ─── 깊이 제한 ─────────────────────────────────────────────────────────


# Gemini 의 비공식 한계 — 깊이 5~6 까지 안정적. 그 이상은 출력 누락 보고됨.
# 우리 schema 가 deeply nested 라 안전 마진 확인.
_DEPTH_SAFE_LIMIT = 7


def test_spack_schema_depth_within_safe_limit():
    depth = _max_nesting_depth(SPACK_AGENT_SCHEMA)
    assert depth <= _DEPTH_SAFE_LIMIT, (
        f"SPACK schema 깊이 {depth} > 안전 한계 {_DEPTH_SAFE_LIMIT}. "
        "Gemini structured output 이 출력 누락할 위험."
    )


def test_ddd_schema_depth_within_safe_limit():
    depth = _max_nesting_depth(DDD_AGENT_SCHEMA)
    assert depth <= _DEPTH_SAFE_LIMIT


def test_architecture_schema_depth_within_safe_limit():
    depth = _max_nesting_depth(ARCHITECTURE_AGENT_SCHEMA)
    assert depth <= _DEPTH_SAFE_LIMIT


# ─── JSON Schema 표준 부합성 ───────────────────────────────────────────


def test_spack_schema_valid_jsonschema_meta():
    """schema 자체가 valid JSON Schema — Draft 7 기준."""
    from jsonschema import Draft7Validator
    Draft7Validator.check_schema(SPACK_AGENT_SCHEMA)


def test_ddd_schema_valid_jsonschema_meta():
    from jsonschema import Draft7Validator
    Draft7Validator.check_schema(DDD_AGENT_SCHEMA)


def test_architecture_schema_valid_jsonschema_meta():
    from jsonschema import Draft7Validator
    Draft7Validator.check_schema(ARCHITECTURE_AGENT_SCHEMA)


# ─── 의도된 LLM 출력이 schema 를 통과하는지 ──────────────────────────


def test_fully_specified_spack_output_validates_against_schema():
    """A-3 까지 충실히 채운 SPACK 출력이 새 schema 를 통과."""
    from jsonschema import validate

    sample = {
        "apis": [{
            "id": "API-01",
            "name": "기록 생성",
            "method": "POST",
            "endpoint": "/api/v1/plants/{plantId}/growth",
            "description": "생장 기록",
            "related_story_id": "Story-03.1",
            "path_params": [
                {"name": "plantId", "type": "uuid", "required": True,
                 "constraint": "", "description": ""}
            ],
            "query_params": [],
            "request_body": {
                "content_type": "application/json",
                "fields": [
                    {"name": "height", "type": "double", "required": True,
                     "constraint": ">0", "description": "cm"}
                ],
                "example": "",
            },
            "response_body": {
                "status": 201,
                "content_type": "application/json",
                "fields": [{"name": "id", "type": "uuid", "required": True,
                            "constraint": "", "description": ""}],
                "example": "",
            },
            "error_cases": [
                {"status": 401, "code": "AUTH", "condition": "",
                 "message": "", "lineage_quote": ""},
            ],
            "auth": {
                "required": True,
                "required_roles": ["owner"],
                "ownership_check": "Plant.ownerId == requester.userId",
                "description": "",
            },
        }],
        "entities": [{
            "id": "ENT-01",
            "name": "Plant",
            "attributes": [
                {"name": "id", "type": "uuid", "required": True,
                 "constraint": "", "description": ""},
            ],
            "description": "식물",
            "lineage": {
                "confidence": "direct",
                "related_stories": [{"story_id": "Story-01.1", "quote": "식물 등록"}],
            },
        }],
        "policies": [{
            "id": "POL-01",
            "category": "Security",
            "description": "JWT",
            "related_entity": "Plant",
        }],
    }
    validate(instance=sample, schema=SPACK_AGENT_SCHEMA)


def test_legacy_string_attributes_fails_new_schema():
    """legacy string list attributes 는 새 schema 통과 안 함 (객체 강제)."""
    from jsonschema import ValidationError, validate

    legacy_sample = {
        "apis": [],
        "entities": [{
            "id": "ENT-01",
            "name": "Plant",
            "attributes": ["id", "name"],  # legacy string list
            "description": "",
            "lineage": {"confidence": "none", "related_stories": []},
        }],
        "policies": [],
    }
    with pytest.raises(ValidationError):
        validate(instance=legacy_sample, schema=SPACK_AGENT_SCHEMA)


def test_minimal_legal_spack_output_validates():
    """모든 새 필드 비어있는 minimal 출력도 schema 통과 (필수만 채움)."""
    from jsonschema import validate

    minimal = {
        "apis": [{
            "id": "API-01", "method": "GET", "endpoint": "/x",
            "description": "", "name": "", "related_story_id": "",
            "path_params": [], "query_params": [],
            "request_body": {"content_type": "", "fields": [], "example": ""},
            "response_body": {"status": 0, "content_type": "", "fields": [], "example": ""},
            "error_cases": [],
            "auth": {"required": True, "required_roles": [], "ownership_check": "", "description": ""},
        }],
        "entities": [],
        "policies": [],
    }
    validate(instance=minimal, schema=SPACK_AGENT_SCHEMA)
