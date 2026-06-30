"""
design_pipeline 의 health top-level 노출 회귀 가드.

[배경]
이전: design_validator 결과가 diagnostic.design_health 안에 nested → FE 가 놓치기
    쉬워 cross-stage 위반 (Spack/DDD 이름 불일치 등) 을 사용자가 모름.
이후: DesignResult.health 신규 top-level 필드 (has_errors/has_warnings 등).

[가드]
- DesignResult.health 필드 존재 + 키 매핑 (has_errors, has_warnings, total_*,
  top_violation_codes)
- v2 DesignResponse 모델에 health 필드 노출
- gateway_compat _h_create_design 응답 dict 에 health 포함
"""
from __future__ import annotations

from dataclasses import fields

import pytest

from app.pipelines.design_pipeline import DesignResult


def test_design_result_has_health_field():
    """[회귀] DesignResult dataclass 에 health 필드 존재."""
    field_names = {f.name for f in fields(DesignResult)}
    assert "health" in field_names, (
        "DesignResult.health 누락 — FE 가 cross-stage 위반 못 알아챔"
    )


def test_design_result_health_default_empty_dict():
    """기본값은 빈 dict — 미설정 시 graceful."""
    r = DesignResult(project_name="p", master_prd_id="m1")
    assert r.health == {}


def test_design_result_health_accepts_summary():
    """summarize_reports 결과를 health 로 받음."""
    r = DesignResult(
        project_name="p", master_prd_id="m1",
        health={
            "total_errors": 2,
            "total_warnings": 5,
            "has_errors": True,
            "has_warnings": True,
            "top_violation_codes": ["SPACK_ENTITY_NAME_MISMATCH", "DDD_DANGLING_AGG"],
        },
    )
    assert r.health["has_errors"] is True
    assert r.health["total_errors"] == 2
    assert len(r.health["top_violation_codes"]) == 2


def test_design_response_model_has_health():
    """v2 DesignResponse Pydantic 모델에 health 필드 노출."""
    from app.api.v2_routes import DesignResponse
    assert "health" in DesignResponse.model_fields, (
        "DesignResponse.health 누락 — API 응답 schema 에서 사라짐"
    )


def test_gateway_compat_create_design_response_includes_health():
    """gateway_compat _h_create_design 의 응답 dict 가 health 키 포함 — 코드 inspection."""
    # 직접 호출하려면 LLM/Neo4j 의존 — 코드 grep 으로 회귀 가드 충분.
    import inspect
    from app.api.gateway_compat_routes import _h_create_design
    src = inspect.getsource(_h_create_design)
    assert '"health"' in src or "'health'" in src, (
        "_h_create_design 응답에 health 키 누락 — FE 가 못 받음"
    )
