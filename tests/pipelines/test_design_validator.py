"""
design_validator 단위 테스트 — normalize + cross-stage validation.

핵심 회귀 시나리오:
1. Spack normalize: 정렬 + ID 재부여 (LLM 이 ID 흔들어도 결과 동일)
2. Spack 검증: PascalCase 위반, policy category 외부, related_story_id 누락
3. DDD cross-검증: Aggregate name = Spack Entity name 불일치 detect
4. DDD cross-검증: spack_entity_mapping 누락/중복/unknown id detect
5. Arch cross-검증: api_service_mapping 완전성, owned_aggregates 완전성, multi-owned
6. Arch: tech_stack 별칭 자동 교정 ('vue' → 'Vue.js')
7. Arch: Frontend 에 owned_aggregates 들어가면 경고
8. 멱등성: 같은 입력 두 번 normalize 시 결과 동일
"""
from __future__ import annotations

import pytest

from app.pipelines.design_validator import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    normalize_architecture,
    normalize_ddd,
    normalize_spack,
    summarize_reports,
)
from app.pipelines.design_validator.attributes import (
    has_legacy_unknown_types,
    normalize_entity_attributes,
    serialize_attributes_for_neo4j,
)


# ─── Spack normalize ────────────────────────────────────────────


def test_spack_normalize_sorts_and_reassigns_ids():
    """LLM 이 random 순서로 줘도 정렬 + ID 재부여로 결정적."""
    raw = {
        "apis": [
            {"id": "API-XYZ", "name": "B", "method": "POST", "endpoint": "/b", "related_story_id": "Story-02.1"},
            {"id": "API-99", "name": "A", "method": "GET", "endpoint": "/a", "related_story_id": "Story-01.1"},
        ],
        "entities": [
            {"id": "ENT-9", "name": "Ticket"},
            {"id": "ENT-1", "name": "Account"},
        ],
        "policies": [
            {"id": "POL-2", "category": "Security", "description": "Z"},
            {"id": "POL-1", "category": "Audit", "description": "A"},
        ],
    }
    norm, report = normalize_spack(raw)
    # apis: Story-01 GET 가 먼저
    assert [a["id"] for a in norm["apis"]] == ["API-01", "API-02"]
    assert norm["apis"][0]["endpoint"] == "/a"
    # entities α
    assert [e["id"] for e in norm["entities"]] == ["ENT-01", "ENT-02"]
    assert norm["entities"][0]["name"] == "Account"
    # policies: Audit 먼저
    assert [p["id"] for p in norm["policies"]] == ["POL-01", "POL-02"]
    assert norm["policies"][0]["category"] == "Audit"
    # auto-fixed 기록
    assert len(report.auto_fixed) >= 4  # ID 재부여 여러 건


def test_spack_normalize_is_idempotent():
    """같은 입력 두 번 normalize 시 결과 동일."""
    raw = {
        "apis": [{"name": "X", "method": "POST", "endpoint": "/x", "related_story_id": "Story-01.1"}],
        "entities": [{"name": "Foo"}, {"name": "Bar"}],
        "policies": [{"category": "Performance", "description": "p"}],
    }
    a, _ = normalize_spack({**raw, "apis": list(raw["apis"]), "entities": list(raw["entities"]), "policies": list(raw["policies"])})
    b, _ = normalize_spack({**raw, "apis": list(raw["apis"]), "entities": list(raw["entities"]), "policies": list(raw["policies"])})
    # 내부 key (_id_remap) 빼고 비교
    aa = {k: v for k, v in a.items() if not k.startswith("_")}
    bb = {k: v for k, v in b.items() if not k.startswith("_")}
    assert aa == bb


def test_spack_validation_pascal_case_violation():
    raw = {
        "entities": [
            {"name": "Encrypted_Credential"},
            {"name": "valid_PascalCase"},  # 소문자 시작
            {"name": "ToolApplication"},   # OK
        ],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    # 2건 위반 ('Encrypted_Credential', 'valid_PascalCase')
    assert codes.count("ENTITY_NAME_NOT_PASCAL_CASE") == 2


def test_spack_validation_policy_category_whitelist():
    raw = {
        "policies": [
            {"category": "Performance", "description": "ok"},
            {"category": "Reliability", "description": "외부 category"},  # 위반
        ],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "POLICY_CATEGORY_UNKNOWN" in codes


def test_spack_validation_policy_stub_dropped():
    """[2026-05-28] category·description 모두 비운 stub policy 는 drop.

    증상: LLM 이 id 만 채운 빈 policy 를 만들면 FE 가 '내용 없음' 6개 카드만 노출 →
    사용자 무가치 노이즈. 정규화 단계에서 의미있는 정책만 보존.
    """
    raw = {
        "policies": [
            {"id": "POL-A", "category": "Security", "description": "권한 없으면 403"},
            {"id": "POL-B"},  # stub (모두 비어있음) — drop 대상
            {"id": "POL-C", "category": "", "description": ""},  # 빈 string stub — drop 대상
            {"id": "POL-D", "category": "Performance", "description": "응답 500ms 이하"},
            {"id": "POL-E", "category": "Compliance"},  # category 만 있음 — 보존 (의미 있음)
        ],
    }
    norm, report = normalize_spack(raw)
    # 5개 중 2개 stub drop, 3개 보존
    assert len(norm["policies"]) == 3
    codes = [v.code for v in report.violations]
    assert "POLICY_STUB_DROPPED" in codes
    # 보존된 것의 category 확인 (정렬: Compliance < Performance < Security)
    cats = [p["category"] for p in norm["policies"]]
    assert cats == ["Compliance", "Performance", "Security"]


def test_spack_validation_all_policies_stub_yields_empty_list():
    """모든 policy 가 stub 이면 빈 list — FE 가 '6개 카드' 대신 'Policy 없음' 노출."""
    raw = {
        "policies": [
            {"id": "POL-01"},
            {"id": "POL-02"},
            {"id": "POL-03", "category": "", "description": ""},
        ],
    }
    norm, report = normalize_spack(raw)
    assert norm["policies"] == []
    codes = [v.code for v in report.violations]
    assert "POLICY_STUB_DROPPED" in codes


def test_spack_validation_api_missing_story_ref():
    raw = {
        "apis": [
            {"name": "noref", "method": "POST", "endpoint": "/x"},  # related_story_id 누락
        ],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_MISSING_STORY_REF" in codes


def test_spack_validation_empty_method_endpoint_description():
    # [2026-06] 이름만 있고 method/endpoint/description 비운 API → 경고로 표면화.
    raw = {
        "apis": [
            {"id": "API-01", "name": "채팅 상호작용", "related_story_id": "Story-01.1"},  # method/endpoint/desc 없음
        ],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_METHOD_MISSING" in codes
    assert "API_ENDPOINT_MISSING" in codes
    assert "API_DESCRIPTION_MISSING" in codes


def test_spack_validation_entity_description_missing():
    raw = {
        "entities": [
            {"id": "ENT-01", "name": "Agent", "attributes": [{"name": "id", "type": "string"}]},  # description 없음
        ],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "ENTITY_DESCRIPTION_MISSING" in codes


def test_spack_validation_filled_api_no_missing_warnings():
    # 채워진 API/Entity 는 위 경고가 뜨지 않아야 한다(오탐 방지).
    raw = {
        "apis": [{"id": "API-01", "name": "주문 생성", "method": "POST", "endpoint": "/orders",
                  "description": "주문을 생성한다", "related_story_id": "Story-01.1"}],
        "entities": [{"id": "ENT-01", "name": "Order", "description": "주문",
                      "attributes": [{"name": "id", "type": "string"}]}],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_METHOD_MISSING" not in codes
    assert "API_ENDPOINT_MISSING" not in codes
    assert "ENTITY_DESCRIPTION_MISSING" not in codes


def test_spack_schema_requires_api_and_entity_core_fields():
    # 스키마 비대칭 버그 회귀방지 — apis/entities item 에 required 가 명시돼야 한다.
    from app.pipelines.design_pipeline.schemas import SPACK_AGENT_SCHEMA
    api_req = SPACK_AGENT_SCHEMA["properties"]["apis"]["items"]["required"]
    ent_req = SPACK_AGENT_SCHEMA["properties"]["entities"]["items"]["required"]
    assert {"method", "endpoint", "description"}.issubset(set(api_req))
    assert "description" in ent_req


# ─── DDD cross-validation ───────────────────────────────────────


def _basic_spack_normalized():
    raw = {
        "entities": [{"name": "Ticket"}, {"name": "Account"}],
        "apis": [{"name": "A", "method": "POST", "endpoint": "/a", "related_story_id": "Story-01.1"}],
        "policies": [],
    }
    norm, _ = normalize_spack(raw)
    return norm


def test_ddd_cross_validation_aggregate_name_mismatch():
    """Spack Entity 'Ticket' 이 DDD 에 없음 → DDD_MISSING_SPACK_ENTITY 발생."""
    ns = _basic_spack_normalized()
    ddd_raw = {
        "contexts": [{"name": "Core"}],
        "aggregates": [
            {"name": "TicketAggregate", "context_id": "CTX-01"},  # Pluralize-like 변형
            {"name": "Account", "context_id": "CTX-01"},
        ],
        "entities": [],
        "events": [],
        "spack_entity_mapping": [
            {"spack_entity_id": "ENT-01", "ddd_location": "AGG-01"},
            {"spack_entity_id": "ENT-02", "ddd_location": "AGG-02"},
        ],
    }
    _, report = normalize_ddd(ddd_raw, ns)
    codes = [v.code for v in report.violations]
    assert "DDD_MISSING_SPACK_ENTITY" in codes  # 'Ticket' 누락
    assert "DDD_UNKNOWN_NAME" in codes          # 'TicketAggregate' 가 Spack 에 없음


def test_ddd_cross_validation_mapping_missing_entity():
    ns = _basic_spack_normalized()
    ddd_raw = {
        "contexts": [{"name": "Core"}],
        "aggregates": [
            {"name": "Ticket", "context_id": "CTX-01"},
            {"name": "Account", "context_id": "CTX-01"},
        ],
        "entities": [],
        "events": [],
        "spack_entity_mapping": [
            {"spack_entity_id": "ENT-01", "ddd_location": "AGG-01"},
            # ENT-02 누락
        ],
    }
    _, report = normalize_ddd(ddd_raw, ns)
    codes = [v.code for v in report.violations]
    assert "DDD_MAPPING_MISSING_ENTITY" in codes


def test_ddd_cross_validation_mapping_duplicate_and_unknown():
    ns = _basic_spack_normalized()
    ddd_raw = {
        "contexts": [{"name": "Core"}],
        "aggregates": [
            {"name": "Ticket", "context_id": "CTX-01"},
            {"name": "Account", "context_id": "CTX-01"},
        ],
        "entities": [], "events": [],
        "spack_entity_mapping": [
            {"spack_entity_id": "ENT-01", "ddd_location": "AGG-01"},
            {"spack_entity_id": "ENT-01", "ddd_location": "AGG-02"},  # 중복
            {"spack_entity_id": "ENT-99", "ddd_location": "AGG-99"},  # Spack 에 없음
        ],
    }
    _, report = normalize_ddd(ddd_raw, ns)
    codes = [v.code for v in report.violations]
    assert "DDD_MAPPING_DUPLICATE_ENTITY" in codes
    assert "DDD_MAPPING_UNKNOWN_ENTITY" in codes


def test_ddd_normalize_is_idempotent():
    ns = _basic_spack_normalized()
    ddd_raw = {
        "contexts": [{"name": "Core"}, {"name": "Billing"}],
        "aggregates": [
            {"name": "Ticket", "context_id": "CTX-01"},
            {"name": "Account", "context_id": "CTX-02"},
        ],
        "entities": [], "events": [],
        "spack_entity_mapping": [
            {"spack_entity_id": "ENT-01", "ddd_location": "AGG-01"},
            {"spack_entity_id": "ENT-02", "ddd_location": "AGG-02"},
        ],
    }
    import copy
    a, _ = normalize_ddd(copy.deepcopy(ddd_raw), ns)
    b, _ = normalize_ddd(copy.deepcopy(ddd_raw), ns)
    aa = {k: v for k, v in a.items() if not k.startswith("_")}
    bb = {k: v for k, v in b.items() if not k.startswith("_")}
    assert aa == bb


# ─── Architecture cross-validation ──────────────────────────────


def _ddd_normalized_with_two_aggs():
    ns_spack = _basic_spack_normalized()
    ddd_raw = {
        "contexts": [{"name": "Core"}],
        "aggregates": [
            {"name": "Account", "context_id": "CTX-01"},
            {"name": "Ticket", "context_id": "CTX-01"},
        ],
        "entities": [], "events": [],
        "spack_entity_mapping": [
            {"spack_entity_id": "ENT-01", "ddd_location": "AGG-01"},
            {"spack_entity_id": "ENT-02", "ddd_location": "AGG-02"},
        ],
    }
    n_ddd, _ = normalize_ddd(ddd_raw, ns_spack)
    return ns_spack, n_ddd


def test_arch_cross_api_mapping_missing():
    ns_spack, n_ddd = _ddd_normalized_with_two_aggs()
    arch_raw = {
        "services": [
            {"name": "Backend", "type": "Backend API", "tech_stack": "Spring Boot",
             "owned_aggregates": ["Account", "Ticket"]},
        ],
        "databases": [],
        "connections": [],
        "api_service_mapping": [],  # Spack 의 API-01 누락
    }
    _, report = normalize_architecture(arch_raw, ns_spack, n_ddd)
    codes = [v.code for v in report.violations]
    assert "ARCH_API_UNMAPPED" in codes


def test_arch_cross_owned_aggregates_missing():
    ns_spack, n_ddd = _ddd_normalized_with_two_aggs()
    arch_raw = {
        "services": [
            {"name": "Backend", "type": "Backend API", "tech_stack": "Spring Boot",
             "owned_aggregates": ["Account"]},  # Ticket 누락
        ],
        "databases": [],
        "connections": [],
        "api_service_mapping": [{"api_id": "API-01", "service_id": "SVC-01"}],
    }
    _, report = normalize_architecture(arch_raw, ns_spack, n_ddd)
    codes = [v.code for v in report.violations]
    assert "ARCH_AGG_UNOWNED" in codes


def test_arch_cross_aggregate_multi_owned():
    ns_spack, n_ddd = _ddd_normalized_with_two_aggs()
    arch_raw = {
        "services": [
            {"name": "Backend A", "type": "Backend API", "tech_stack": "Spring Boot",
             "owned_aggregates": ["Account", "Ticket"]},
            {"name": "Backend B", "type": "Backend API", "tech_stack": "Spring Boot",
             "owned_aggregates": ["Ticket"]},  # Ticket 이 2개 Service 에 owned
        ],
        "databases": [],
        "connections": [],
        "api_service_mapping": [{"api_id": "API-01", "service_id": "SVC-01"}],
    }
    _, report = normalize_architecture(arch_raw, ns_spack, n_ddd)
    codes = [v.code for v in report.violations]
    assert "ARCH_AGG_MULTI_OWNED" in codes


def test_arch_tech_stack_normalization_autofix():
    ns_spack, n_ddd = _ddd_normalized_with_two_aggs()
    arch_raw = {
        "services": [
            {"name": "Mobile Web", "type": "Frontend", "tech_stack": "vuejs"},  # 별칭
            {"name": "Backend", "type": "Backend API", "tech_stack": "spring boot",
             "owned_aggregates": ["Account", "Ticket"]},
        ],
        "databases": [{"name": "Primary", "type": "Relational", "tech_stack": "postgres"}],  # 별칭
        "connections": [],
        "api_service_mapping": [{"api_id": "API-01", "service_id": "SVC-02"}],
    }
    norm, report = normalize_architecture(arch_raw, ns_spack, n_ddd)
    # tech_stack 정규화 확인
    techs = {s["name"]: s["tech_stack"] for s in norm["services"]}
    assert techs["Mobile Web"] == "Vue.js"
    assert techs["Backend"] == "Spring Boot"
    assert norm["databases"][0]["tech_stack"] == "PostgreSQL"
    # auto-fixed 로 기록
    fixed_codes = [v.code for v in report.auto_fixed]
    assert fixed_codes.count("ARCH_TECH_STACK_NORMALIZED") == 3


def test_arch_frontend_with_owned_aggregates_warns():
    ns_spack, n_ddd = _ddd_normalized_with_two_aggs()
    arch_raw = {
        "services": [
            {"name": "Mobile Web", "type": "Frontend", "tech_stack": "Vue.js",
             "owned_aggregates": ["Ticket"]},  # 규칙 위반
            {"name": "Backend", "type": "Backend API", "tech_stack": "Spring Boot",
             "owned_aggregates": ["Account", "Ticket"]},
        ],
        "databases": [],
        "connections": [],
        "api_service_mapping": [{"api_id": "API-01", "service_id": "SVC-02"}],
    }
    _, report = normalize_architecture(arch_raw, ns_spack, n_ddd)
    codes = [v.code for v in report.violations]
    assert "ARCH_FRONTEND_HAS_AGGREGATES" in codes


def test_arch_normalize_sorting_and_ids():
    ns_spack, n_ddd = _ddd_normalized_with_two_aggs()
    arch_raw = {
        "services": [
            {"id": "SVC-X", "name": "Z Backend", "type": "Backend API", "tech_stack": "Spring Boot",
             "owned_aggregates": ["Account", "Ticket"]},
            {"id": "SVC-Y", "name": "A Web", "type": "Frontend", "tech_stack": "Vue.js"},
        ],
        "databases": [],
        "connections": [],
        "api_service_mapping": [{"api_id": "API-01", "service_id": "SVC-X"}],
    }
    norm, _ = normalize_architecture(arch_raw, ns_spack, n_ddd)
    # 정렬: Frontend < Backend API → A Web 가 SVC-01
    assert norm["services"][0]["name"] == "A Web"
    assert norm["services"][0]["id"] == "SVC-01"
    assert norm["services"][1]["id"] == "SVC-02"
    # api_service_mapping 의 service_id 가 SVC-X → SVC-02 로 갱신
    assert norm["api_service_mapping"][0]["service_id"] == "SVC-02"


# ─── Summary ───────────────────────────────────────────────────


def test_summarize_reports_aggregates_counts():
    raw_spack = {"policies": [{"category": "Bad", "description": "x"}]}
    _, s_rep = normalize_spack(raw_spack)
    ns = _basic_spack_normalized()
    ddd_raw = {
        "contexts": [{"name": "Core"}],
        "aggregates": [{"name": "WrongName", "context_id": "CTX-01"}],
        "entities": [], "events": [],
        "spack_entity_mapping": [],
    }
    _, d_rep = normalize_ddd(ddd_raw, ns)
    summary = summarize_reports(s_rep, d_rep)
    assert summary["total_errors"] >= 1  # DDD_MISSING_SPACK_ENTITY 가 error
    assert summary["total_warnings"] >= 1  # POLICY_CATEGORY_UNKNOWN
    assert summary["healthy"] is False
    assert "top_violation_codes" in summary
    assert len(summary["stages"]) == 2


# ─── 추가 검증 (review 결과 보강) ──────────────────────────────


def test_ddd_event_missing_story_ref_detected():
    """Event 가 related_story_id 누락 → DDD_EVENT_MISSING_STORY_REF warning."""
    ns = _basic_spack_normalized()
    ddd_raw = {
        "contexts": [{"name": "Core"}],
        "aggregates": [
            {"name": "Account", "context_id": "CTX-01"},
            {"name": "Ticket", "context_id": "CTX-01"},
        ],
        "entities": [],
        "events": [
            {"name": "TicketIssued", "published_by_aggregate_id": "AGG-02"},  # related_story_id 누락
            {"name": "AccountClosed", "related_story_id": "Story-01.1", "published_by_aggregate_id": "AGG-01"},  # OK
        ],
        "spack_entity_mapping": [
            {"spack_entity_id": "ENT-01", "ddd_location": "AGG-01"},
            {"spack_entity_id": "ENT-02", "ddd_location": "AGG-02"},
        ],
    }
    _, report = normalize_ddd(ddd_raw, ns)
    codes = [v.code for v in report.violations]
    # 1건만 (AccountClosed 는 OK)
    assert codes.count("DDD_EVENT_MISSING_STORY_REF") == 1


def test_ddd_event_dangling_aggregate_detected():
    """Event 의 published_by_aggregate_id 가 실재 Aggregate 가 아니면 error."""
    ns = _basic_spack_normalized()
    ddd_raw = {
        "contexts": [{"name": "Core"}],
        "aggregates": [
            {"name": "Account", "context_id": "CTX-01"},
            {"name": "Ticket", "context_id": "CTX-01"},
        ],
        "entities": [],
        "events": [
            {"name": "Mystery", "related_story_id": "Story-01.1",
             "published_by_aggregate_id": "AGG-99"},  # AGG-99 없음
        ],
        "spack_entity_mapping": [
            {"spack_entity_id": "ENT-01", "ddd_location": "AGG-01"},
            {"spack_entity_id": "ENT-02", "ddd_location": "AGG-02"},
        ],
    }
    _, report = normalize_ddd(ddd_raw, ns)
    codes = [v.code for v in report.violations]
    assert "DDD_EVENT_DANGLING_AGGREGATE" in codes


def test_arch_connection_dangling_detected():
    """Connection 의 source/target 이 services + databases 어디에도 없으면 error."""
    ns_spack, n_ddd = _ddd_normalized_with_two_aggs()
    arch_raw = {
        "services": [
            {"name": "Backend", "type": "Backend API", "tech_stack": "Spring Boot",
             "owned_aggregates": ["Account", "Ticket"]},
        ],
        "databases": [],
        "connections": [
            {"source_id": "SVC-999", "target_id": "SVC-01", "protocol": "x"},  # SVC-999 없음
        ],
        "api_service_mapping": [{"api_id": "API-01", "service_id": "SVC-01"}],
    }
    _, report = normalize_architecture(arch_raw, ns_spack, n_ddd)
    codes = [v.code for v in report.violations]
    assert "ARCH_CONN_DANGLING" in codes


def test_arch_api_mapped_to_frontend_detected():
    """API 가 Frontend Service 에 매핑되면 warning (Backend/Gateway 만 API 구현 주체)."""
    ns_spack, n_ddd = _ddd_normalized_with_two_aggs()
    arch_raw = {
        "services": [
            {"name": "Web", "type": "Frontend", "tech_stack": "Vue.js"},
            {"name": "Backend", "type": "Backend API", "tech_stack": "Spring Boot",
             "owned_aggregates": ["Account", "Ticket"]},
        ],
        "databases": [],
        "connections": [],
        "api_service_mapping": [
            {"api_id": "API-01", "service_id": "SVC-01"},  # SVC-01 = Web (Frontend) → 위반
        ],
    }
    _, report = normalize_architecture(arch_raw, ns_spack, n_ddd)
    codes = [v.code for v in report.violations]
    assert "ARCH_API_MAPPED_TO_FRONTEND" in codes


def test_arch_normalize_is_idempotent():
    """같은 입력 두 번 normalize 시 결과 동일 (Architecture 누락 회귀 방지)."""
    ns_spack, n_ddd = _ddd_normalized_with_two_aggs()
    arch_raw = {
        "services": [
            {"id": "SVC-X", "name": "Z Backend", "type": "Backend API", "tech_stack": "spring",
             "owned_aggregates": ["Account", "Ticket"]},
            {"id": "SVC-Y", "name": "A Web", "type": "Frontend", "tech_stack": "vuejs"},
        ],
        "databases": [
            {"id": "DB-X", "name": "Primary", "type": "RDB", "tech_stack": "postgres"},
        ],
        "connections": [
            {"source_id": "SVC-X", "target_id": "DB-X", "protocol": "JDBC", "description": "x"},
        ],
        "api_service_mapping": [
            {"api_id": "API-01", "service_id": "SVC-X"},
        ],
    }
    import copy
    a, _ = normalize_architecture(copy.deepcopy(arch_raw), ns_spack, n_ddd)
    b, _ = normalize_architecture(copy.deepcopy(arch_raw), ns_spack, n_ddd)
    aa = {k: v for k, v in a.items() if not k.startswith("_")}
    bb = {k: v for k, v in b.items() if not k.startswith("_")}
    assert aa == bb


def test_empty_inputs_do_not_crash():
    """완전 빈 LLM 출력 ({}) 도 안전하게 처리."""
    ns, sr = normalize_spack({})
    assert ns["apis"] == [] and ns["entities"] == [] and ns["policies"] == []
    assert sr.error_count == 0 and sr.warning_count == 0

    nd, dr = normalize_ddd({}, ns)
    assert nd["aggregates"] == [] and nd["entities"] == [] and nd["events"] == []
    assert dr.error_count == 0

    na, ar = normalize_architecture({}, ns, nd)
    assert na["services"] == [] and na["databases"] == [] and na["connections"] == []
    assert ar.error_count == 0


def test_full_pipeline_idempotency_with_shuffled_input():
    """LLM 이 같은 의미·다른 순서/ID 를 줘도 정규화 결과는 동일 (실제 멱등성)."""
    spack_a = {
        "apis": [
            {"id": "API-3", "name": "A", "method": "GET", "endpoint": "/x", "related_story_id": "Story-01.1"},
            {"id": "API-7", "name": "B", "method": "POST", "endpoint": "/y", "related_story_id": "Story-01.1"},
        ],
        "entities": [{"id": "ENT-9", "name": "Ticket"}, {"id": "ENT-2", "name": "Account"}],
        "policies": [],
    }
    # 2번째: 순서 뒤바뀜 + 다른 임의 ID
    spack_b = {
        "apis": [
            {"id": "API-XYZ", "name": "B", "method": "POST", "endpoint": "/y", "related_story_id": "Story-01.1"},
            {"id": "API-ABC", "name": "A", "method": "GET", "endpoint": "/x", "related_story_id": "Story-01.1"},
        ],
        "entities": [{"id": "garbage", "name": "Account"}, {"id": "garbage2", "name": "Ticket"}],
        "policies": [],
    }
    na, _ = normalize_spack(spack_a)
    nb, _ = normalize_spack(spack_b)
    aa = {k: v for k, v in na.items() if not k.startswith("_")}
    bb = {k: v for k, v in nb.items() if not k.startswith("_")}
    assert aa == bb


def test_summarize_healthy_when_no_errors():
    # 모두 통과하는 입력
    raw_spack = {
        "apis": [{"name": "x", "method": "POST", "endpoint": "/x", "related_story_id": "Story-01.1"}],
        "entities": [{"name": "Ticket"}, {"name": "Account"}],
        "policies": [{"category": "Performance", "description": "fast"}],
    }
    n_spack, s_rep = normalize_spack(raw_spack)
    ddd_raw = {
        "contexts": [{"name": "Core"}],
        "aggregates": [
            {"name": "Account", "context_id": "CTX-01"},
            {"name": "Ticket", "context_id": "CTX-01"},
        ],
        "entities": [], "events": [],
        "spack_entity_mapping": [
            {"spack_entity_id": "ENT-01", "ddd_location": "AGG-01"},
            {"spack_entity_id": "ENT-02", "ddd_location": "AGG-02"},
        ],
    }
    n_ddd, d_rep = normalize_ddd(ddd_raw, n_spack)
    arch_raw = {
        "services": [
            {"name": "Backend", "type": "Backend API", "tech_stack": "Spring Boot",
             "owned_aggregates": ["Account", "Ticket"]},
        ],
        "databases": [],
        "connections": [],
        "api_service_mapping": [{"api_id": "API-01", "service_id": "SVC-01"}],
    }
    _, a_rep = normalize_architecture(arch_raw, n_spack, n_ddd)
    summary = summarize_reports(s_rep, d_rep, a_rep)
    assert summary["total_errors"] == 0
    assert summary["healthy"] is True


# ─── [A-1 — 2026-05-25] Entity attributes 객체화 ────────────────────────
#
# normalize_entity_attributes 는 5가지 입력 형태를 모두 객체 list 로 흡수.
# 이 헬퍼가 design pipeline / Neo4j fetch / lint / fix_spec / create_md 모두에서
# 사용되므로 회귀 시 어디서든 깨짐. 광범위 단위 테스트로 보호.


def test_attributes_none_returns_empty():
    assert normalize_entity_attributes(None) == []


def test_attributes_empty_list_returns_empty():
    assert normalize_entity_attributes([]) == []


def test_attributes_empty_string_returns_empty():
    assert normalize_entity_attributes("") == []


def test_attributes_legacy_string_list_migrates_to_unknown_type():
    """기존 Neo4j 에 저장된 string list 가 read 시 자동 마이그레이션."""
    out = normalize_entity_attributes(["plantId", "height", "leafCount"])
    assert len(out) == 3
    assert out[0] == {
        "name": "plantId",
        "type": "unknown",
        "required": False,
        "constraint": "",
        "description": "",
    }
    assert all(a["type"] == "unknown" for a in out)
    assert has_legacy_unknown_types(out) is True


def test_attributes_object_list_preserves_fields():
    raw = [
        {
            "name": "plantId",
            "type": "uuid",
            "required": True,
            "constraint": "",
            "description": "식물 식별자",
        },
        {
            "name": "height",
            "type": "double",
            "required": True,
            "constraint": ">0",
            "description": "cm 단위",
        },
    ]
    out = normalize_entity_attributes(raw)
    assert len(out) == 2
    assert out[0]["type"] == "uuid"
    assert out[0]["required"] is True
    assert out[1]["constraint"] == ">0"
    assert has_legacy_unknown_types(out) is False


def test_attributes_object_missing_type_falls_back_to_unknown():
    """type 필드 누락 / 빈 문자열은 'unknown' 으로 마이그레이션 시그널 유지."""
    out = normalize_entity_attributes([{"name": "x"}, {"name": "y", "type": ""}])
    assert len(out) == 2
    assert out[0]["type"] == "unknown"
    assert out[1]["type"] == "unknown"


def test_attributes_object_missing_name_is_dropped():
    """name 누락 항목은 drop. LLM 의 더러운 출력 흡수."""
    out = normalize_entity_attributes(
        [{"name": "ok", "type": "int"}, {"type": "string"}, {"name": "", "type": "x"}]
    )
    assert len(out) == 1
    assert out[0]["name"] == "ok"


def test_attributes_mixed_list_both_forms_handled():
    """객체와 legacy string 이 섞여 있어도 모두 객체 list 로."""
    out = normalize_entity_attributes(
        [{"name": "a", "type": "uuid"}, "b", {"name": "c", "type": "int"}]
    )
    assert [a["name"] for a in out] == ["a", "b", "c"]
    assert out[0]["type"] == "uuid"
    assert out[1]["type"] == "unknown"  # legacy 마이그레이션
    assert out[2]["type"] == "int"


def test_attributes_duplicate_names_keep_first():
    """LLM 이 중복 출력 시 첫 항목만 유지 (재시도가 결과를 흔들지 않음)."""
    out = normalize_entity_attributes(
        [
            {"name": "x", "type": "uuid"},
            {"name": "x", "type": "string"},
            {"name": "y", "type": "int"},
        ]
    )
    assert [a["name"] for a in out] == ["x", "y"]
    assert out[0]["type"] == "uuid"


def test_attributes_json_string_object_list_parsed():
    """Neo4j 저장 형태 (JSON string) 가 read 시 parse 됨."""
    raw = '[{"name": "plantId", "type": "uuid", "required": true}]'
    out = normalize_entity_attributes(raw)
    assert len(out) == 1
    assert out[0]["name"] == "plantId"
    assert out[0]["type"] == "uuid"
    assert out[0]["required"] is True


def test_attributes_json_string_legacy_list_parsed_and_migrated():
    """Neo4j 에 저장된 legacy JSON ('[\"a\", \"b\"]') 도 마이그레이션."""
    out = normalize_entity_attributes('["a", "b"]')
    assert [a["name"] for a in out] == ["a", "b"]
    assert all(a["type"] == "unknown" for a in out)


def test_attributes_unknown_field_silently_dropped():
    """schema 외 필드는 silently drop — 일관성 유지."""
    out = normalize_entity_attributes(
        [{"name": "x", "type": "int", "ghost_field": "should be dropped"}]
    )
    assert "ghost_field" not in out[0]
    assert set(out[0].keys()) == {"name", "type", "required", "constraint", "description"}


def test_serialize_attributes_for_neo4j_round_trip():
    """Neo4j 저장 → read 가 형태 보존."""
    raw = [
        {
            "name": "plantId",
            "type": "uuid",
            "required": True,
            "constraint": "",
            "description": "식별자",
        }
    ]
    serialized = serialize_attributes_for_neo4j(raw)
    assert isinstance(serialized, str)
    # 비ASCII 보존 — ensure_ascii=False
    assert "식별자" in serialized
    restored = normalize_entity_attributes(serialized)
    assert restored == raw


def test_serialize_legacy_string_list_round_trip_migrates():
    """과거 데이터 (string list) 가 serialize → read 시 객체 list 로 복원."""
    serialized = serialize_attributes_for_neo4j(["plantId", "height"])
    restored = normalize_entity_attributes(serialized)
    assert [a["name"] for a in restored] == ["plantId", "height"]
    assert all(a["type"] == "unknown" for a in restored)


def test_serialize_empty_yields_json_empty_array():
    """빈 list 도 '[]' 로 저장. None 과 구분 가능."""
    assert serialize_attributes_for_neo4j([]) == "[]"
    assert serialize_attributes_for_neo4j(None) == "[]"


def test_spack_normalize_object_attributes_pass_through():
    """객체 형태 attributes 가 normalize 후 보존되고 violation 없음."""
    raw = {
        "entities": [
            {
                "name": "Plant",
                "attributes": [
                    {"name": "id", "type": "uuid", "required": True},
                    {"name": "height", "type": "double", "required": True, "constraint": ">0"},
                ],
            }
        ],
        "apis": [], "policies": [],
    }
    norm, report = normalize_spack(raw)
    ent = norm["entities"][0]
    assert len(ent["attributes"]) == 2
    assert ent["attributes"][0]["type"] == "uuid"
    codes = [v.code for v in report.violations]
    assert "ENTITY_ATTRIBUTES_MISSING" not in codes
    assert "ENTITY_ATTRIBUTES_LEGACY_UNKNOWN_TYPE" not in codes


def test_spack_normalize_legacy_string_attributes_detected_as_unknown():
    """legacy string list 가 들어오면 마이그레이션 + INFO 위반 기록."""
    raw = {
        "entities": [{"name": "Plant", "attributes": ["id", "height"]}],
        "apis": [], "policies": [],
    }
    norm, report = normalize_spack(raw)
    ent = norm["entities"][0]
    # 객체 list 로 변환됨
    assert all(isinstance(a, dict) for a in ent["attributes"])
    assert all(a["type"] == "unknown" for a in ent["attributes"])
    codes = [v.code for v in report.violations]
    assert "ENTITY_ATTRIBUTES_LEGACY_UNKNOWN_TYPE" in codes


def test_spack_normalize_missing_attributes_warns():
    """attributes 미정의 → WARNING."""
    raw = {
        "entities": [{"name": "Plant"}],  # attributes 없음
        "apis": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "ENTITY_ATTRIBUTES_MISSING" in codes


# ─── [A-2 — 2026-05-25] API payload 정규화 ──────────────────────────────


def _import_payload():
    from app.pipelines.design_validator.api_payload import (
        decode_apis_payload,
        extract_path_param_names,
        is_body_expected,
        normalize_api_payload,
        normalize_request_body,
        normalize_response_body,
        serialize_api_payload_for_neo4j,
    )
    return {
        "decode_apis_payload": decode_apis_payload,
        "extract_path_param_names": extract_path_param_names,
        "is_body_expected": is_body_expected,
        "normalize_api_payload": normalize_api_payload,
        "normalize_request_body": normalize_request_body,
        "normalize_response_body": normalize_response_body,
        "serialize_api_payload_for_neo4j": serialize_api_payload_for_neo4j,
    }


def test_payload_request_body_none_returns_empty_skeleton():
    fns = _import_payload()
    out = fns["normalize_request_body"](None)
    assert out == {"content_type": "", "fields": [], "example": ""}


def test_payload_request_body_object_normalizes_fields():
    fns = _import_payload()
    out = fns["normalize_request_body"]({
        "content_type": "application/json",
        "fields": [
            {"name": "height", "type": "double", "required": True, "constraint": ">0"},
            "leafCount",  # legacy string → migrate
        ],
        "example": {"height": 12.5, "leafCount": 8},
    })
    assert out["content_type"] == "application/json"
    assert len(out["fields"]) == 2
    assert out["fields"][0]["type"] == "double"
    assert out["fields"][1]["type"] == "unknown"  # legacy
    # example dict → JSON string (안정 보관)
    assert "12.5" in out["example"]


def test_payload_request_body_json_string_parsed():
    """Neo4j 가 JSON string 으로 저장한 형태가 read 시 복원."""
    fns = _import_payload()
    raw = '{"content_type":"application/json","fields":[{"name":"x","type":"int"}],"example":""}'
    out = fns["normalize_request_body"](raw)
    assert out["content_type"] == "application/json"
    assert out["fields"][0]["name"] == "x"


def test_payload_response_body_status_coerced_to_int():
    fns = _import_payload()
    out = fns["normalize_response_body"]({"status": "201", "fields": []})
    assert out["status"] == 201
    out2 = fns["normalize_response_body"]({"status": "garbage", "fields": []})
    assert out2["status"] == 0
    out3 = fns["normalize_response_body"]({"fields": []})
    assert out3["status"] == 0


def test_payload_extract_path_param_names():
    fns = _import_payload()
    assert fns["extract_path_param_names"]("/api/v1/plants/{plantId}/growth") == ["plantId"]
    assert fns["extract_path_param_names"](
        "/api/v1/users/{userId}/plants/{plantId}"
    ) == ["userId", "plantId"]
    assert fns["extract_path_param_names"]("/api/v1/plants") == []
    assert fns["extract_path_param_names"](None) == []


def test_payload_is_body_expected():
    fns = _import_payload()
    assert fns["is_body_expected"]("POST") is True
    assert fns["is_body_expected"]("put") is True
    assert fns["is_body_expected"]("PATCH") is True
    assert fns["is_body_expected"]("GET") is False
    assert fns["is_body_expected"]("DELETE") is False
    assert fns["is_body_expected"](None) is False


def test_payload_normalize_api_inplace_fills_all_four_fields():
    fns = _import_payload()
    api = {"id": "API-01", "method": "GET", "endpoint": "/x"}
    fns["normalize_api_payload"](api)
    assert api["path_params"] == []
    assert api["query_params"] == []
    assert api["request_body"]["fields"] == []
    assert api["response_body"]["status"] == 0


def test_payload_serialize_for_neo4j_returns_four_strings():
    fns = _import_payload()
    api = {
        "id": "API-01",
        "method": "POST",
        "endpoint": "/plants/{plantId}/growth",
        "path_params": [{"name": "plantId", "type": "uuid"}],
        "request_body": {
            "content_type": "application/json",
            "fields": [{"name": "height", "type": "double", "required": True,
                       "constraint": ">0", "description": "cm 단위"}],
            "example": "",
        },
        "response_body": {
            "status": 201,
            "fields": [{"name": "id", "type": "uuid"}],
        },
    }
    out = fns["serialize_api_payload_for_neo4j"](api)
    # [A-3] error_cases / auth 추가로 6개 필드.
    assert set(out.keys()) == {
        "path_params", "query_params", "request_body", "response_body",
        "error_cases", "auth",
    }
    assert "cm 단위" in out["request_body"]
    assert all(isinstance(v, str) for v in out.values())
    # 원본 mutate 회피 확인
    assert isinstance(api["request_body"], dict)


def test_payload_decode_apis_round_trip():
    fns = _import_payload()
    original_req = {
        "content_type": "application/json",
        "fields": [{"name": "height", "type": "double", "required": True,
                    "constraint": ">0", "description": "cm"}],
        "example": '{"height": 12.5}',
    }
    api = {
        "id": "API-01", "method": "POST", "endpoint": "/x",
        "path_params": [],
        "query_params": [],
        "request_body": original_req,
        "response_body": {"status": 201, "content_type": "application/json",
                          "fields": [], "example": ""},
    }
    serialized = fns["serialize_api_payload_for_neo4j"](api)
    neo_row = {**api, **serialized}
    restored = fns["decode_apis_payload"]([neo_row])
    assert restored[0]["request_body"] == original_req
    assert restored[0]["response_body"]["status"] == 201


def test_payload_decode_legacy_api_without_fields_yields_empty_skeletons():
    """기존 API 노드 (4개 필드 미존재) 도 read 시 빈 객체로 정규화 → 깨지지 않음."""
    fns = _import_payload()
    legacy = [{"id": "API-01", "method": "GET", "endpoint": "/x", "description": "..."}]
    restored = fns["decode_apis_payload"](legacy)
    assert restored[0]["path_params"] == []
    assert restored[0]["query_params"] == []
    assert restored[0]["request_body"] == {"content_type": "", "fields": [], "example": ""}
    assert restored[0]["response_body"] == {"status": 0, "content_type": "", "fields": [], "example": ""}


def test_payload_example_dict_coerced_to_json_string():
    fns = _import_payload()
    out = fns["normalize_request_body"]({
        "fields": [],
        "example": {"x": 1, "y": "한글"},
    })
    assert isinstance(out["example"], str)
    assert "한글" in out["example"]
    assert '"x"' in out["example"]


def test_spack_normalize_request_body_missing_for_post_warns():
    """POST/PUT/PATCH 인데 request_body.fields 비어 있으면 WARNING."""
    raw = {
        "apis": [
            {"name": "create", "method": "POST", "endpoint": "/x",
             "related_story_id": "Story-01.1"},
        ],
        "entities": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_REQUEST_BODY_MISSING" in codes


def test_spack_normalize_request_body_not_required_for_get():
    """GET 은 request_body 누락 OK."""
    raw = {
        "apis": [
            {"name": "list", "method": "GET", "endpoint": "/x",
             "related_story_id": "Story-01.1"},
        ],
        "entities": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_REQUEST_BODY_MISSING" not in codes


def test_spack_normalize_path_param_mismatch_detected():
    """endpoint 의 {plantId} 가 path_params 에 없으면 WARNING."""
    raw = {
        "apis": [
            {"name": "get", "method": "GET",
             "endpoint": "/api/v1/plants/{plantId}/growth",
             "related_story_id": "Story-01.1",
             "path_params": []},
        ],
        "entities": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_PATH_PARAM_UNDECLARED" in codes


def test_spack_normalize_path_param_orphan_detected():
    """path_params 에 있는데 endpoint 에 없으면 WARNING."""
    raw = {
        "apis": [
            {"name": "list", "method": "GET", "endpoint": "/api/v1/plants",
             "related_story_id": "Story-01.1",
             "path_params": [{"name": "plantId", "type": "uuid"}]},
        ],
        "entities": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_PATH_PARAM_ORPHAN" in codes


def test_spack_normalize_full_api_passes_without_payload_warnings():
    """완전 명세된 API 는 payload 관련 violation 없음."""
    raw = {
        "apis": [
            {
                "name": "create growth",
                "method": "POST",
                "endpoint": "/api/v1/plants/{plantId}/growth",
                "related_story_id": "Story-03.1",
                "path_params": [{"name": "plantId", "type": "uuid", "required": True,
                                "constraint": "", "description": "식물 식별자"}],
                "request_body": {
                    "content_type": "application/json",
                    "fields": [{"name": "height", "type": "double", "required": True,
                               "constraint": ">0", "description": "cm"}],
                    "example": "",
                },
                "response_body": {
                    "status": 201,
                    "content_type": "application/json",
                    "fields": [{"name": "id", "type": "uuid", "required": True,
                               "constraint": "", "description": ""}],
                    "example": "",
                },
            },
        ],
        "entities": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_REQUEST_BODY_MISSING" not in codes
    assert "API_RESPONSE_BODY_MISSING" not in codes
    assert "API_PATH_PARAM_UNDECLARED" not in codes
    assert "API_PATH_PARAM_ORPHAN" not in codes


# ─── [A-3 — 2026-05-25] error_cases + auth ──────────────────────────────


def _import_a3():
    from app.pipelines.design_validator.api_payload import (
        normalize_auth,
        normalize_error_cases,
    )
    return {"normalize_auth": normalize_auth,
            "normalize_error_cases": normalize_error_cases}


def test_error_cases_none_returns_empty():
    fns = _import_a3()
    assert fns["normalize_error_cases"](None) == []


def test_error_cases_status_out_of_range_dropped():
    """200/300 대는 success 영역이라 error_cases 에 들어오면 안 됨 → drop."""
    fns = _import_a3()
    raw = [
        {"status": 200, "code": "OK"},
        {"status": 401, "code": "AUTH"},
        {"status": 999, "code": "INVALID"},
        {"status": "garbage", "code": "X"},
    ]
    out = fns["normalize_error_cases"](raw)
    assert [c["status"] for c in out] == [401]


def test_error_cases_sorted_by_status_for_determinism():
    """status 오름차순 정렬 — 같은 입력 다른 순서여도 결과 동일."""
    fns = _import_a3()
    raw = [{"status": 422}, {"status": 401}, {"status": 404}, {"status": 403}]
    out = fns["normalize_error_cases"](raw)
    assert [c["status"] for c in out] == [401, 403, 404, 422]


def test_error_cases_duplicate_status_first_wins():
    """LLM 이 같은 status 두 번 출력하면 첫 것만 유지."""
    fns = _import_a3()
    raw = [
        {"status": 404, "code": "PLANT_NOT_FOUND", "message": "식물 없음"},
        {"status": 404, "code": "OTHER", "message": "다른 메시지"},
    ]
    out = fns["normalize_error_cases"](raw)
    assert len(out) == 1
    assert out[0]["code"] == "PLANT_NOT_FOUND"


def test_error_cases_json_string_parsed():
    fns = _import_a3()
    raw = '[{"status": 401, "code": "AUTH_REQUIRED"}]'
    out = fns["normalize_error_cases"](raw)
    assert len(out) == 1
    assert out[0]["status"] == 401


def test_error_cases_status_as_string_coerced():
    fns = _import_a3()
    out = fns["normalize_error_cases"]([{"status": "404", "code": "NF"}])
    assert out[0]["status"] == 404


def test_error_cases_single_object_promoted_to_list():
    fns = _import_a3()
    out = fns["normalize_error_cases"]({"status": 401, "code": "AUTH"})
    assert len(out) == 1
    assert out[0]["status"] == 401


def test_error_cases_preserves_lineage_quote():
    """PRD 원문 발췌 보존 — 추적성 핵심."""
    fns = _import_a3()
    out = fns["normalize_error_cases"]([
        {"status": 404, "lineage_quote": "식물을 찾을 수 없는 경우 404 반환"},
    ])
    assert out[0]["lineage_quote"] == "식물을 찾을 수 없는 경우 404 반환"


def test_auth_none_defaults_to_required_no_roles():
    """auth 누락 → 보수적 default (인증 필요, 역할 무관)."""
    fns = _import_a3()
    out = fns["normalize_auth"](None)
    assert out == {
        "required": True,
        "required_roles": [],
        "ownership_check": "",
        "description": "",
    }


def test_auth_anonymous_allowed():
    fns = _import_a3()
    out = fns["normalize_auth"]({"required": False})
    assert out["required"] is False


def test_auth_roles_normalized_unique():
    """role 중복 제거 + 순서 안정."""
    fns = _import_a3()
    out = fns["normalize_auth"]({
        "required": True,
        "required_roles": ["owner", "admin", "owner", "viewer"],
    })
    assert out["required_roles"] == ["owner", "admin", "viewer"]


def test_auth_invalid_roles_filtered():
    fns = _import_a3()
    out = fns["normalize_auth"]({
        "required_roles": ["owner", 42, None, "", "admin"],
    })
    assert out["required_roles"] == ["owner", "admin"]


def test_auth_json_string_parsed():
    fns = _import_a3()
    raw = '{"required": true, "required_roles": ["admin"], "ownership_check": "x"}'
    out = fns["normalize_auth"](raw)
    assert out["required"] is True
    assert out["required_roles"] == ["admin"]
    assert out["ownership_check"] == "x"


def test_auth_ownership_check_preserves_korean():
    fns = _import_a3()
    out = fns["normalize_auth"]({
        "ownership_check": "Plant.ownerId == requester.userId",
        "description": "본인 소유 식물만",
    })
    assert "ownerId" in out["ownership_check"]
    assert "본인 소유" in out["description"]


def test_spack_normalize_error_cases_missing_yields_info():
    """error_cases 없으면 INFO."""
    raw = {
        "apis": [
            {"name": "list", "method": "GET", "endpoint": "/x",
             "related_story_id": "Story-01.1"},
        ],
        "entities": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_ERROR_CASES_MISSING" in codes


def test_spack_normalize_post_without_422_warns():
    """POST 인데 422 (validation error) 누락 → INFO."""
    raw = {
        "apis": [
            {"name": "create", "method": "POST", "endpoint": "/x",
             "related_story_id": "Story-01.1",
             "error_cases": [{"status": 401}, {"status": 500}]},
        ],
        "entities": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_VALIDATION_ERROR_CASE_MISSING" in codes


def test_spack_normalize_path_param_without_404_warns():
    """endpoint 에 {plantId} 있는데 404 누락 → INFO."""
    raw = {
        "apis": [
            {"name": "get", "method": "GET",
             "endpoint": "/api/v1/plants/{plantId}",
             "related_story_id": "Story-01.1",
             "path_params": [{"name": "plantId", "type": "uuid"}],
             "error_cases": [{"status": 401}]},
        ],
        "entities": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_NOT_FOUND_CASE_MISSING" in codes


def test_spack_normalize_auth_required_without_401_warns():
    """auth.required=True 인데 401 없음 → INFO."""
    raw = {
        "apis": [
            {"name": "create", "method": "POST", "endpoint": "/x",
             "related_story_id": "Story-01.1",
             "auth": {"required": True, "required_roles": ["owner"]},
             "error_cases": [{"status": 422}]},
        ],
        "entities": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_AUTH_ERROR_CASE_MISSING" in codes


def test_spack_normalize_anonymous_api_no_401_no_warning():
    """auth.required=False 인 익명 API 는 401 없어도 OK."""
    raw = {
        "apis": [
            {"name": "ping", "method": "GET", "endpoint": "/health",
             "related_story_id": "Story-01.1",
             "auth": {"required": False, "required_roles": []},
             "error_cases": [{"status": 503}]},
        ],
        "entities": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "API_AUTH_ERROR_CASE_MISSING" not in codes


def test_spack_normalize_fully_specified_api_no_a3_warnings():
    """완전 명세 — A-3 관련 violation 없음."""
    raw = {
        "apis": [
            {
                "name": "기록 생성", "method": "POST",
                "endpoint": "/api/v1/plants/{plantId}/growth",
                "related_story_id": "Story-03.1",
                "path_params": [{"name": "plantId", "type": "uuid", "required": True}],
                "request_body": {
                    "fields": [{"name": "height", "type": "double", "required": True}],
                },
                "response_body": {
                    "status": 201,
                    "fields": [{"name": "id", "type": "uuid"}],
                },
                "auth": {"required": True, "required_roles": ["owner"],
                         "ownership_check": "Plant.ownerId == requester.userId",
                         "description": "본인 식물만"},
                "error_cases": [
                    {"status": 401, "code": "AUTH_REQUIRED"},
                    {"status": 403, "code": "FORBIDDEN_OWNER"},
                    {"status": 404, "code": "PLANT_NOT_FOUND"},
                    {"status": 422, "code": "VALIDATION_ERROR"},
                ],
            },
        ],
        "entities": [], "policies": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    for code in [
        "API_ERROR_CASES_MISSING",
        "API_VALIDATION_ERROR_CASE_MISSING",
        "API_NOT_FOUND_CASE_MISSING",
        "API_AUTH_ERROR_CASE_MISSING",
        "API_REQUEST_BODY_MISSING",
        "API_RESPONSE_BODY_MISSING",
    ]:
        assert code not in codes, f"{code} 가 완전 명세 API 에 발생함"


# ─── [D-1 — 2026-05-25] DDD detail (invariants / payload / domain attrs) ────


def _import_d1():
    from app.pipelines.design_validator.ddd_detail import (
        decode_aggregates_detail,
        decode_domain_entities_detail,
        decode_domain_events_detail,
        normalize_invariants,
        serialize_invariants_for_neo4j,
    )
    return {
        "decode_aggregates_detail": decode_aggregates_detail,
        "decode_domain_entities_detail": decode_domain_entities_detail,
        "decode_domain_events_detail": decode_domain_events_detail,
        "normalize_invariants": normalize_invariants,
        "serialize_invariants_for_neo4j": serialize_invariants_for_neo4j,
    }


def test_invariants_none_returns_empty():
    fns = _import_d1()
    assert fns["normalize_invariants"](None) == []


def test_invariants_string_list_preserved():
    fns = _import_d1()
    out = fns["normalize_invariants"](["leafCount >= 0", "min < max"])
    assert out == ["leafCount >= 0", "min < max"]


def test_invariants_json_string_parsed():
    fns = _import_d1()
    out = fns["normalize_invariants"]('["x > 0", "y != null"]')
    assert out == ["x > 0", "y != null"]


def test_invariants_object_list_extracts_rule():
    """LLM 이 객체로 잘못 출력해도 'rule'/'description'/'invariant' 키에서 추출."""
    fns = _import_d1()
    out = fns["normalize_invariants"]([
        {"rule": "leafCount >= 0"},
        {"description": "amount > 0"},
        {"invariant": "status in enum"},
        {"garbage": "ignored"},
    ])
    assert out == ["leafCount >= 0", "amount > 0", "status in enum"]


def test_invariants_single_string_promoted():
    fns = _import_d1()
    out = fns["normalize_invariants"]("leafCount >= 0")
    assert out == ["leafCount >= 0"]


def test_invariants_duplicate_first_wins():
    fns = _import_d1()
    out = fns["normalize_invariants"](["x > 0", "y > 0", "x > 0"])
    assert out == ["x > 0", "y > 0"]


def test_invariants_serialize_round_trip():
    fns = _import_d1()
    raw = ["leafCount >= 0", "temperatureMin < temperatureMax (℃)"]
    s = fns["serialize_invariants_for_neo4j"](raw)
    assert isinstance(s, str)
    assert "℃" in s
    restored = fns["normalize_invariants"](s)
    assert restored == raw


def test_decode_aggregates_detail_restores_invariants():
    fns = _import_d1()
    aggregates = [
        {"id": "AGG-01", "name": "Plant",
         "invariants": '["leafCount >= 0"]'},
        {"id": "AGG-02", "name": "User"},
    ]
    out = fns["decode_aggregates_detail"](aggregates)
    assert out[0]["invariants"] == ["leafCount >= 0"]
    assert out[1]["invariants"] == []
    assert "invariants" not in aggregates[1]


def test_decode_domain_entities_detail_uses_attribute_helper():
    fns = _import_d1()
    entities = [
        {"id": "DE-01", "name": "Foo",
         "attributes": '[{"name":"x","type":"int","required":true}]'},
    ]
    out = fns["decode_domain_entities_detail"](entities)
    assert len(out[0]["attributes"]) == 1
    assert out[0]["attributes"][0]["name"] == "x"
    assert out[0]["attributes"][0]["type"] == "int"


def test_decode_domain_events_detail_restores_payload():
    fns = _import_d1()
    events = [
        {
            "id": "EVT-01", "name": "PlantGrowthDataRecorded",
            "payload_fields": [
                {"name": "growthDataId", "type": "uuid", "required": True},
                {"name": "plantId", "type": "uuid", "required": True},
                {"name": "occurredAt", "type": "datetime", "required": True},
            ],
        },
        {"id": "EVT-02", "name": "Legacy"},
    ]
    out = fns["decode_domain_events_detail"](events)
    assert len(out[0]["payload_fields"]) == 3
    assert out[0]["payload_fields"][0]["name"] == "growthDataId"
    assert out[1]["payload_fields"] == []


# ─── [D-2 — 2026-05-25] Architecture detail (deployment / auth / ext deps) ──


def _import_d2():
    from app.pipelines.design_validator.arch_detail import (
        decode_connections_auth,
        decode_services_detail,
        normalize_connection_auth,
        normalize_deployment,
        normalize_external_dependencies,
        serialize_deployment_for_neo4j,
        serialize_external_dependencies_for_neo4j,
    )
    return {
        "decode_connections_auth": decode_connections_auth,
        "decode_services_detail": decode_services_detail,
        "normalize_connection_auth": normalize_connection_auth,
        "normalize_deployment": normalize_deployment,
        "normalize_external_dependencies": normalize_external_dependencies,
        "serialize_deployment_for_neo4j": serialize_deployment_for_neo4j,
        "serialize_external_dependencies_for_neo4j":
            serialize_external_dependencies_for_neo4j,
    }


def test_deployment_none_returns_defaults():
    fns = _import_d2()
    out = fns["normalize_deployment"](None)
    assert out == {"port": 0, "replicas": 1, "health_check_path": "",
                   "env_vars": [], "scaling_policy": "manual"}


def test_deployment_object_preserves_fields():
    fns = _import_d2()
    out = fns["normalize_deployment"]({
        "port": 8080,
        "replicas": 3,
        "health_check_path": "/actuator/health",
        "env_vars": ["DATABASE_URL", "JWT_SECRET", "REDIS_URL"],
        "scaling_policy": "auto-cpu",
    })
    assert out["port"] == 8080
    assert out["replicas"] == 3
    assert out["env_vars"] == ["DATABASE_URL", "JWT_SECRET", "REDIS_URL"]
    assert out["scaling_policy"] == "auto-cpu"


def test_deployment_env_vars_unique_preserve_order():
    fns = _import_d2()
    out = fns["normalize_deployment"]({
        "env_vars": ["A", "B", "A", "C", "B"],
    })
    assert out["env_vars"] == ["A", "B", "C"]


def test_deployment_replicas_min_1():
    """replicas 가 0 또는 음수면 1 로 강제."""
    fns = _import_d2()
    out = fns["normalize_deployment"]({"replicas": 0})
    assert out["replicas"] == 1
    out2 = fns["normalize_deployment"]({"replicas": -5})
    assert out2["replicas"] == 1


def test_deployment_scaling_unknown_fallback_manual():
    fns = _import_d2()
    out = fns["normalize_deployment"]({"scaling_policy": "unknown-policy"})
    assert out["scaling_policy"] == "manual"


def test_deployment_json_string_parsed():
    fns = _import_d2()
    out = fns["normalize_deployment"]('{"port": 8080, "replicas": 2}')
    assert out["port"] == 8080
    assert out["replicas"] == 2


def test_deployment_port_as_string_coerced():
    fns = _import_d2()
    out = fns["normalize_deployment"]({"port": "8080"})
    assert out["port"] == 8080


def test_external_dependencies_normalized():
    fns = _import_d2()
    out = fns["normalize_external_dependencies"]([
        {"name": "Auth0", "type": "OAuth provider", "purpose": "인증"},
        {"name": "Stripe", "type": "Payment", "purpose": "결제"},
        {"name": "Auth0", "type": "duplicate"},  # 중복 — drop
        {"type": "missing name"},                  # name 누락 — drop
    ])
    assert len(out) == 2
    assert out[0]["name"] == "Auth0"
    assert out[1]["name"] == "Stripe"


def test_connection_auth_enum_normalization():
    fns = _import_d2()
    assert fns["normalize_connection_auth"]("mTLS") == "mTLS"
    # case insensitive
    assert fns["normalize_connection_auth"]("MTLS") == "mTLS"
    assert fns["normalize_connection_auth"]("Bearer") == "bearer"
    # 외부 값 → none fallback
    assert fns["normalize_connection_auth"]("custom-token") == "none"
    assert fns["normalize_connection_auth"](None) == "none"
    assert fns["normalize_connection_auth"]("") == "none"


def test_decode_services_detail_round_trip():
    fns = _import_d2()
    services = [{
        "id": "SVC-01", "name": "Backend",
        "deployment": '{"port": 8080, "replicas": 2, "env_vars": ["DB_URL"]}',
        "external_dependencies": '[{"name": "Stripe", "type": "Payment"}]',
    }]
    out = fns["decode_services_detail"](services)
    assert out[0]["deployment"]["port"] == 8080
    assert out[0]["deployment"]["env_vars"] == ["DB_URL"]
    assert out[0]["external_dependencies"][0]["name"] == "Stripe"


def test_decode_services_detail_legacy_yields_defaults():
    """기존 service (deployment 미존재) 도 read 시 안전 default."""
    fns = _import_d2()
    services = [{"id": "SVC-01", "name": "Old"}]
    out = fns["decode_services_detail"](services)
    assert out[0]["deployment"]["replicas"] == 1
    assert out[0]["external_dependencies"] == []


def test_decode_connections_auth_legacy_yields_none():
    fns = _import_d2()
    connections = [
        {"source_id": "A", "target_id": "B", "auth": "mTLS"},
        {"source_id": "A", "target_id": "C"},  # legacy
    ]
    out = fns["decode_connections_auth"](connections)
    assert out[0]["auth"] == "mTLS"
    assert out[1]["auth"] == "none"


def test_deployment_round_trip_preserves_korean():
    fns = _import_d2()
    raw = {
        "port": 8080,
        "health_check_path": "/health",
        "env_vars": ["DATABASE_URL"],
        "scaling_policy": "auto-cpu",
    }
    s = fns["serialize_deployment_for_neo4j"](raw)
    assert isinstance(s, str)
    restored = fns["normalize_deployment"](s)
    assert restored["port"] == 8080
    assert restored["env_vars"] == ["DATABASE_URL"]


# ─── [#3 — 2026-05-25] Spack screens 정규화 ─────────────────────────────


def test_screens_normalization_drops_invalid():
    """name 또는 path 누락 → drop + SCREEN_INVALID WARNING."""
    raw = {
        "apis": [{"id": "API-01", "name": "x", "method": "GET", "endpoint": "/x",
                  "related_story_id": "Story-01.1"}],
        "entities": [], "policies": [],
        "screens": [
            {"id": "S1", "name": "OK", "path": "/ok"},
            {"id": "S2", "name": "", "path": "/missing-name"},
            {"id": "S3", "name": "missing-path"},
        ],
    }
    norm, report = normalize_spack(raw)
    assert len(norm["screens"]) == 1
    codes = [v.code for v in report.violations]
    assert codes.count("SCREEN_INVALID") == 2


def test_screens_path_duplicate_first_wins():
    raw = {
        "apis": [{"id": "API-01", "name": "x", "method": "GET", "endpoint": "/x",
                  "related_story_id": "Story-01.1"}],
        "entities": [], "policies": [],
        "screens": [
            {"name": "First",  "path": "/dup"},
            {"name": "Second", "path": "/dup"},
        ],
    }
    norm, report = normalize_spack(raw)
    assert len(norm["screens"]) == 1
    assert norm["screens"][0]["name"] == "First"
    codes = [v.code for v in report.violations]
    assert "SCREEN_PATH_DUPLICATE" in codes


def test_screens_unknown_api_drops_with_warning():
    raw = {
        "apis": [{"id": "API-01", "name": "x", "method": "GET", "endpoint": "/x",
                  "related_story_id": "Story-01.1"}],
        "entities": [], "policies": [],
        "screens": [
            {"name": "OK", "path": "/x", "calls_apis": ["API-01", "API-GHOST"]},
        ],
    }
    norm, report = normalize_spack(raw)
    sc = norm["screens"][0]
    assert sc["calls_apis"] == ["API-01"]  # ghost drop
    codes = [v.code for v in report.violations]
    assert "SCREEN_UNKNOWN_API" in codes


def test_no_screens_for_apis_info_when_3plus():
    raw = {
        "apis": [
            {"id": f"API-{i:02d}", "name": "x", "method": "GET",
             "endpoint": f"/x{i}", "related_story_id": "Story-01.1"}
            for i in range(1, 5)
        ],
        "entities": [], "policies": [],
        "screens": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "NO_SCREENS_FOR_APIS" in codes


def test_screens_id_renumbered_deterministically():
    """screens id 가 SCREEN-01, SCREEN-02 순서로 재부여."""
    raw = {
        "apis": [], "entities": [], "policies": [],
        "screens": [
            {"id": "RANDOM-X", "name": "A", "path": "/a"},
            {"id": "RANDOM-Y", "name": "B", "path": "/b"},
        ],
    }
    norm, _ = normalize_spack(raw)
    assert [s["id"] for s in norm["screens"]] == ["SCREEN-01", "SCREEN-02"]


def test_screens_empty_for_backend_only_no_violation():
    """API 2개 이하 또는 backend only 시스템은 screens 비어있어도 violation 없음."""
    raw = {
        "apis": [
            {"id": "API-01", "name": "x", "method": "GET", "endpoint": "/x",
             "related_story_id": "Story-01.1"},
        ],
        "entities": [], "policies": [],
        "screens": [],
    }
    _, report = normalize_spack(raw)
    codes = [v.code for v in report.violations]
    assert "NO_SCREENS_FOR_APIS" not in codes
