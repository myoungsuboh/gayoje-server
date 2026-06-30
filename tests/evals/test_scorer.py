"""
evals.scorer 단위 테스트.

채점 함수는 LLM 호출 없이 그래프 dict 만 입력 받음 → 결정적 검증 가능.
다양한 충실도 시나리오 (빈 / 중간 / 완전) 에서 점수가 monotonic 하게
올라가는지 + 분모 0 케이스가 N/A (만점) 처리되는지 검증.
"""
from __future__ import annotations

from evals.scorer import (
    EvalReport,
    TierScore,
    render_report_text,
    score_spack,
)


# ─── 픽스처 ──────────────────────────────────────────────────────────────


def _empty_spack():
    return {"apis": [], "entities": [], "policies": []}


def _minimal_spack():
    """API/Entity/Policy 있지만 디테일 부족 (plant 패키지 수준)."""
    return {
        "apis": [
            {
                "id": "API-01", "name": "list",
                "method": "GET", "endpoint": "/api/v1/plants/{plantId}/growth",
                "description": "조회",
                # 디테일 모두 비어있음 — Tier 2 0점에 가까움
                "path_params": [], "query_params": [],
                "request_body": {"fields": []},
                "response_body": {"status": 0, "fields": []},
                "error_cases": [],
                "auth": {"required": True, "required_roles": [], "ownership_check": "", "description": ""},
                "related_story_id": "",  # Story 매핑 없음
            },
        ],
        "entities": [
            {
                "id": "ENT-01", "name": "Plant",
                "attributes": [],  # 비어있음
                "lineage": {"confidence": "none", "related_stories": []},
            },
        ],
        "policies": [{"id": "POL-01", "category": "Security", "description": "x"}],
    }


def _fully_specified_spack():
    """A-1/A-2/A-3 모두 충실히 채워진 그래프 — Tier 2/3 만점 근처."""
    return {
        "apis": [
            {
                "id": "API-01", "name": "기록 생성",
                "method": "POST",
                "endpoint": "/api/v1/plants/{plantId}/growth",
                "description": "식물 생장 데이터 기록",
                "related_story_id": "Story-03.1",
                "path_params": [
                    {"name": "plantId", "type": "uuid", "required": True,
                     "constraint": "", "description": "식물 식별자"}
                ],
                "query_params": [],
                "request_body": {
                    "content_type": "application/json",
                    "fields": [
                        {"name": "height", "type": "double", "required": True,
                         "constraint": ">0", "description": "cm 단위"},
                        {"name": "leafCount", "type": "integer", "required": True,
                         "constraint": ">=0", "description": "잎 개수"},
                    ],
                    "example": '{"height": 12.5, "leafCount": 8}',
                },
                "response_body": {
                    "status": 201, "content_type": "application/json",
                    "fields": [{"name": "id", "type": "uuid", "required": True,
                               "constraint": "", "description": ""}],
                    "example": "",
                },
                "error_cases": [
                    {"status": 401, "code": "AUTH_REQUIRED", "condition": "",
                     "message": "인증 필요", "lineage_quote": ""},
                    {"status": 404, "code": "PLANT_NOT_FOUND", "condition": "",
                     "message": "", "lineage_quote": ""},
                    {"status": 422, "code": "VALIDATION_ERROR", "condition": "",
                     "message": "", "lineage_quote": ""},
                ],
                "auth": {
                    "required": True, "required_roles": ["owner", "admin"],
                    "ownership_check": "Plant.ownerId == requester.userId",
                    "description": "본인 식물만",
                },
            },
        ],
        "entities": [
            {
                "id": "ENT-01", "name": "Plant",
                "attributes": [
                    {"name": "id", "type": "uuid", "required": True,
                     "constraint": "", "description": ""},
                    {"name": "ownerId", "type": "uuid", "required": True,
                     "constraint": "", "description": ""},
                ],
                "lineage": {
                    "confidence": "direct",
                    "related_stories": [
                        {"story_id": "Story-01.1", "quote": "식물 정보 등록"}
                    ],
                },
            },
        ],
        "policies": [{"id": "POL-01", "category": "Security", "description": "JWT"}],
    }


# ─── 기본 동작 ───────────────────────────────────────────────────────────


def test_score_returns_eval_report():
    report = score_spack(_minimal_spack())
    assert isinstance(report, EvalReport)
    assert isinstance(report.tier1, TierScore)
    assert 0.0 <= report.overall <= 1.0


def test_empty_spack_tier1_is_zero():
    """API/Entity/Policy 모두 비면 Tier 1 (구조) 0점."""
    report = score_spack(_empty_spack())
    assert report.tier1.score == 0.0


def test_empty_spack_tier2_is_neutral():
    """디테일 분모가 0 (entities/apis 부재) → N/A → 만점 (1.0).
    빈 그래프 자체는 Tier 1 에서 잡고, Tier 2 가 추가 penalty 주면 이중감점."""
    report = score_spack(_empty_spack())
    assert report.tier2.score == 1.0
    # notes 에 N/A 사유 기록
    assert len(report.tier2.notes) > 0


def test_minimal_spack_tier2_low():
    """디테일 모두 비어있는 minimal → Tier 2 매우 낮음."""
    report = score_spack(_minimal_spack())
    # entity attributes 0, API 디테일 모두 비어있음.
    assert report.tier2.score < 0.5


def test_fully_specified_spack_tier2_high():
    """완전 명세 → Tier 2 매우 높음."""
    report = score_spack(_fully_specified_spack())
    assert report.tier2.score > 0.8


def test_score_monotonic_minimal_to_full():
    """minimal → full 로 갈수록 overall 단조 증가."""
    r_min = score_spack(_minimal_spack())
    r_full = score_spack(_fully_specified_spack())
    assert r_full.overall > r_min.overall
    assert r_full.tier2.score > r_min.tier2.score
    assert r_full.tier3.score > r_min.tier3.score


# ─── Tier 별 세부 ───────────────────────────────────────────────────────


def test_tier3_api_story_mapping():
    """API 의 related_story_id 가 있으면 매핑률 향상."""
    spack = {
        "apis": [
            {"id": "API-01", "method": "GET", "endpoint": "/x", "related_story_id": "Story-01.1"},
            {"id": "API-02", "method": "GET", "endpoint": "/y", "related_story_id": ""},
        ],
        "entities": [], "policies": [],
    }
    report = score_spack(spack)
    assert report.tier3.sub_metrics["api_story_mapped_ratio"] == 0.5


def test_tier2_attribute_typed_ratio_unknown_drops_score():
    """type=unknown 이 많으면 attribute_typed_ratio 낮음."""
    spack = {
        "apis": [],
        "entities": [
            {"id": "ENT-01", "name": "X", "attributes": [
                {"name": "a", "type": "uuid"},
                {"name": "b", "type": "unknown"},  # legacy 마이그레이션
                {"name": "c", "type": "unknown"},
            ]},
        ],
        "policies": [],
    }
    report = score_spack(spack)
    assert report.tier2.sub_metrics["attribute_typed_ratio"] == 1 / 3


def test_tier4_error_zeros_score_when_validator_finds_error():
    """validation_report 에 error 1건만 있어도 error_score 0 → 전체 감점."""
    report = score_spack(
        _fully_specified_spack(),
        validation_report={"total_errors": 1, "total_warnings": 0, "total_infos": 0},
    )
    assert report.tier4.sub_metrics["error_score"] == 0.0
    # error_score 0 + warning_score 1.0 → 0.6*0 + 0.4*1 = 0.4
    assert abs(report.tier4.score - 0.4) < 1e-6


def test_tier4_warnings_decay_score():
    """WARNING N건당 0.05 감점, 0 까지."""
    report_clean = score_spack(
        _fully_specified_spack(),
        validation_report={"total_errors": 0, "total_warnings": 0, "total_infos": 0},
    )
    report_5w = score_spack(
        _fully_specified_spack(),
        validation_report={"total_errors": 0, "total_warnings": 5, "total_infos": 0},
    )
    # 5 * 0.05 = 0.25 감점 → warning_score 0.75
    assert abs(report_5w.tier4.sub_metrics["warning_score"] - 0.75) < 1e-6
    assert report_5w.tier4.score < report_clean.tier4.score


def test_tier4_warning_floor_at_zero():
    """엄청 많은 WARNING 도 0 미만으로 안 떨어짐."""
    report = score_spack(
        _fully_specified_spack(),
        validation_report={"total_errors": 0, "total_warnings": 100, "total_infos": 0},
    )
    assert report.tier4.sub_metrics["warning_score"] == 0.0


def test_tier4_missing_validation_yields_full_score():
    """validation_report 부재 → 만점 (penalty 안 줌)."""
    report = score_spack(_fully_specified_spack())
    assert report.tier4.score == 1.0
    assert any("validation_report 부재" in n for n in report.tier4.notes)


# ─── DDD/Architecture 통합 ──────────────────────────────────────────────


def test_ddd_present_adds_tier1_metrics():
    report = score_spack(
        _minimal_spack(),
        ddd={"contexts": [{"id": "CTX-01"}], "aggregates": [{"id": "AGG-01"}]},
    )
    assert "ddd_contexts_present" in report.tier1.sub_metrics
    assert "ddd_aggregates_present" in report.tier1.sub_metrics


def test_tier3_aggregate_lineage_mapped():
    ddd = {
        "contexts": [{"id": "CTX-01"}],
        "aggregates": [
            {"id": "AGG-01", "lineage": {"related_stories": [{"story_id": "Story-01.1"}]}},
            {"id": "AGG-02", "lineage": {"related_stories": []}},
        ],
    }
    report = score_spack(_minimal_spack(), ddd=ddd)
    assert report.tier3.sub_metrics["aggregate_lineage_mapped_ratio"] == 0.5


# ─── 안정성 ──────────────────────────────────────────────────────────────


def test_score_does_not_raise_on_garbage_input():
    """비정상 입력에 예외 안 던짐."""
    report = score_spack({"apis": None, "entities": None, "policies": None})
    assert 0.0 <= report.overall <= 1.0


def test_render_report_text_outputs_lines():
    """텍스트 렌더링 — CLI 출력용. 키워드 포함만 가볍게 검증."""
    report = score_spack(_fully_specified_spack())
    text = render_report_text(report)
    assert "Overall" in text
    assert "Tier 1" in text
    assert "Tier 4" in text


def test_path_params_consistent_ratio():
    """endpoint 의 {p} 가 path_params 에 모두 선언되면 만점."""
    spack = {
        "apis": [
            {
                "id": "API-01", "method": "GET",
                "endpoint": "/api/v1/plants/{plantId}/leaves/{leafId}",
                "path_params": [
                    {"name": "plantId", "type": "uuid"},
                    {"name": "leafId", "type": "uuid"},
                ],
            },
            {
                "id": "API-02", "method": "GET",
                "endpoint": "/api/v1/plants/{plantId}",
                "path_params": [],  # 누락
            },
        ],
        "entities": [], "policies": [],
    }
    report = score_spack(spack)
    # 2개 중 1개만 일치 → 0.5
    assert report.tier2.sub_metrics["api_path_params_consistent_ratio"] == 0.5


# ─── [E — 2026-05-25] Tier 2 가 DDD/Arch detail 까지 채점 ────────────────


def _ddd_full():
    """DDD detail 모두 채워진 fixture."""
    return {
        "contexts": [{"id": "CTX-01"}],
        "aggregates": [{
            "id": "AGG-01", "name": "Plant",
            "invariants": ["leafCount >= 0", "min < max"],
        }],
        "domain_entities": [{
            "id": "DENT-01", "name": "PlantGrowthData",
            "aggregate_id": "AGG-01",
            "attributes": [{"name": "height", "type": "double"}],
        }],
        "domain_events": [{
            "id": "EVT-01", "name": "Recorded",
            "payload_fields": [{"name": "id", "type": "uuid"}],
        }],
    }


def _ddd_empty_detail():
    """DDD 구조는 있지만 detail 모두 비어있음."""
    return {
        "contexts": [{"id": "CTX-01"}],
        "aggregates": [{"id": "AGG-01", "name": "Plant", "invariants": []}],
        "domain_entities": [{"id": "DENT-01", "name": "X", "attributes": []}],
        "domain_events": [{"id": "EVT-01", "name": "Ev", "payload_fields": []}],
    }


def _arch_full():
    return {
        "services": [{
            "id": "SVC-01", "name": "Backend", "type": "Backend API",
            "deployment": {"port": 8080, "replicas": 2, "env_vars": ["A"]},
        }],
        "connections": [
            {"source_id": "A", "target_id": "B", "auth": "bearer"},
            {"source_id": "A", "target_id": "C", "auth": "mTLS"},
        ],
    }


def _arch_no_detail():
    return {
        "services": [{
            "id": "SVC-01", "name": "Backend", "type": "Backend API",
            "deployment": {"port": 0},
        }],
        "connections": [
            {"source_id": "A", "target_id": "B", "auth": "none"},
        ],
    }


def test_tier2_ddd_invariants_ratio():
    """DDD detail 충실 → invariants/attributes/payload 비율 1.0."""
    report = score_spack(_minimal_spack(), ddd=_ddd_full())
    sub = report.tier2.sub_metrics
    assert sub.get("aggregate_invariants_ratio") == 1.0
    assert sub.get("domain_entity_attributes_ratio") == 1.0
    assert sub.get("domain_event_payload_ratio") == 1.0


def test_tier2_ddd_empty_detail_zero_ratios():
    """DDD detail 모두 비어있으면 비율 0."""
    report = score_spack(_minimal_spack(), ddd=_ddd_empty_detail())
    sub = report.tier2.sub_metrics
    assert sub.get("aggregate_invariants_ratio") == 0.0
    assert sub.get("domain_entity_attributes_ratio") == 0.0
    assert sub.get("domain_event_payload_ratio") == 0.0


def test_tier2_ddd_absent_omits_metrics():
    """ddd 인자 부재 → DDD metric 자체 미산정 (notes 도 없음 — SPACK only)."""
    report = score_spack(_minimal_spack())
    sub = report.tier2.sub_metrics
    assert "aggregate_invariants_ratio" not in sub
    assert "domain_entity_attributes_ratio" not in sub


def test_tier2_arch_deployment_ratio():
    report = score_spack(_minimal_spack(), arch=_arch_full())
    sub = report.tier2.sub_metrics
    assert sub.get("service_deployment_ratio") == 1.0
    assert sub.get("connection_auth_ratio") == 1.0


def test_tier2_arch_no_detail_zero_ratios():
    report = score_spack(_minimal_spack(), arch=_arch_no_detail())
    sub = report.tier2.sub_metrics
    assert sub.get("service_deployment_ratio") == 0.0
    assert sub.get("connection_auth_ratio") == 0.0


def test_tier2_arch_frontend_excluded_from_deployment_ratio():
    """Frontend 는 port=0 가능 → 분모에서 제외 (Backend/Worker 만)."""
    arch = {
        "services": [
            {"id": "SVC-01", "type": "Frontend", "deployment": {"port": 0}},
            {"id": "SVC-02", "type": "Backend API", "deployment": {"port": 8080}},
        ],
        "connections": [],
    }
    report = score_spack(_minimal_spack(), arch=arch)
    # Backend 1개 중 1개 deployed → 1.0
    assert report.tier2.sub_metrics["service_deployment_ratio"] == 1.0


def test_full_phase_a_with_ddd_arch_high_overall():
    """SPACK + DDD + Arch 모두 충실 채움 → overall 90%+."""
    report = score_spack(
        _fully_specified_spack(),
        ddd=_ddd_full(),
        arch=_arch_full(),
    )
    assert report.overall > 0.90
    assert report.tier2.score > 0.95


def test_empty_spack_overall_is_zero_not_misleading_90pct():
    """[2026-05-25 fix] 빈 그래프 = overall 0% (이전엔 다른 Tier 의 N/A
    만점 합산 → 90% 잘못 표시되던 버그)."""
    report = score_spack(_empty_spack())
    assert report.tier1.score == 0.0
    # 빈 데이터 = 충실도 0 — 사용자 오해 방지
    assert report.overall == 0.0


def test_minimal_structure_full_detail_overall_not_misleading():
    """Tier 1 점수가 0.5 미만이면 overall 도 비례 감소."""
    # API 1개만 있고 entities/policies 빈 그래프 (Tier 1 ≈ 0.33)
    partial = {
        "apis": [{"id": "API-01", "method": "GET", "endpoint": "/x"}],
        "entities": [], "policies": [],
    }
    report = score_spack(partial)
    # Tier 1 ≈ 1/3, overall 도 동일 스케일 (0.5 미만 가드)
    assert report.tier1.score < 0.5
    assert report.overall < 0.5


# ─── [AI 초안 보완 — 2026-05-29] 자가참조 방지 가중치 ────────────────────────
#
# autofill 이 채운 error_case/auth 는 source="ai_draft" 로 마킹.
# [2026-06-10 정책 변경] 미검토 초안 0.5 인정(자가참조 방지)은 검토 UI 부재 +
# 일괄 '검토 완료' 고무도장으로 형해화 → 폐지. 채워졌으면 reviewed 무관 1.0.
# (메타는 보존 — 'AI 초안' 뱃지 등 투명성 용도. 빈 항목 0.0 은 기존 동작 불변.)


def _spack_one_api(error_cases, auth):
    """error_cases/auth 만 다른, API 1개짜리 SPACK fixture."""
    return {
        "apis": [
            {
                "id": "API-01", "name": "x", "method": "GET", "endpoint": "/x",
                "error_cases": error_cases,
                "auth": auth,
            },
        ],
        "entities": [], "policies": [],
    }


def test_error_cases_manual_full_credit():
    """메타 없는 수동 error_case → 1.0 (기존 동작)."""
    spack = _spack_one_api(
        [{"status": 404, "code": "NOT_FOUND"}],
        {"description": "로그인 필요"},
    )
    report = score_spack(spack)
    assert report.tier2.sub_metrics["api_error_cases_ratio"] == 1.0
    assert report.tier2.sub_metrics["api_auth_specified_ratio"] == 1.0


def test_error_cases_unreviewed_ai_draft_full_credit():
    """[2026-06-10] 미검토 AI 초안도 채워진 명세 — 1.0 (0.5 정책 폐지)."""
    spack = _spack_one_api(
        [{"status": 404, "code": "NOT_FOUND", "source": "ai_draft", "reviewed": False}],
        {"description": "로그인 필요", "source": "ai_draft", "reviewed": False},
    )
    report = score_spack(spack)
    assert report.tier2.sub_metrics["api_error_cases_ratio"] == 1.0
    assert report.tier2.sub_metrics["api_auth_specified_ratio"] == 1.0


def test_error_cases_reviewed_ai_draft_full_credit():
    """검토 완료 AI 초안 (reviewed=True) → 1.0."""
    spack = _spack_one_api(
        [{"status": 404, "code": "NOT_FOUND", "source": "ai_draft", "reviewed": True}],
        {"description": "로그인 필요", "source": "ai_draft", "reviewed": True},
    )
    report = score_spack(spack)
    assert report.tier2.sub_metrics["api_error_cases_ratio"] == 1.0
    assert report.tier2.sub_metrics["api_auth_specified_ratio"] == 1.0


def test_error_cases_empty_zero_credit():
    """error_cases 비면 0.0 (기존 동작 불변)."""
    spack = _spack_one_api([], {})
    report = score_spack(spack)
    assert report.tier2.sub_metrics["api_error_cases_ratio"] == 0.0


def test_error_cases_mixed_draft_and_manual_full_credit():
    """한 API 안에 초안 + 수동 항목 혼재 → 1.0 (모두 채워진 명세)."""
    spack = _spack_one_api(
        [
            {"status": 404, "source": "ai_draft", "reviewed": False},
            {"status": 500, "code": "MANUAL"},
        ],
        {"description": "로그인 필요"},
    )
    report = score_spack(spack)
    assert report.tier2.sub_metrics["api_error_cases_ratio"] == 1.0


def test_auth_draft_full_credit_multi_api():
    """[2026-06-10] 두 API: 수동·미검토 초안 모두 1.0 → 평균 1.0 (0.5 정책 폐지)."""
    spack = {
        "apis": [
            {"id": "API-01", "method": "GET", "endpoint": "/a",
             "error_cases": [{"status": 404}],
             "auth": {"description": "수동"}},
            {"id": "API-02", "method": "GET", "endpoint": "/b",
             "error_cases": [{"status": 404, "source": "ai_draft", "reviewed": False}],
             "auth": {"description": "초안", "source": "ai_draft", "reviewed": False}},
        ],
        "entities": [], "policies": [],
    }
    report = score_spack(spack)
    assert report.tier2.sub_metrics["api_error_cases_ratio"] == 1.0
    assert report.tier2.sub_metrics["api_auth_specified_ratio"] == 1.0


def test_unreviewed_draft_scores_equal_to_reviewed():
    """[2026-06-10] reviewed 여부가 점수에 영향 없음 — 메타는 투명성 용도일 뿐."""
    draft = _spack_one_api(
        [{"status": 404, "source": "ai_draft", "reviewed": False}],
        {"description": "초안", "source": "ai_draft", "reviewed": False},
    )
    reviewed = _spack_one_api(
        [{"status": 404, "source": "ai_draft", "reviewed": True}],
        {"description": "검토됨", "source": "ai_draft", "reviewed": True},
    )
    r_draft = score_spack(draft)
    r_reviewed = score_spack(reviewed)
    assert r_draft.tier2.score == r_reviewed.tier2.score
