"""
[2026-05-19] Cross-jump 매핑 cypher 단위 테스트 (Phase 1).

검증 대상:
- build_save_ddd_query 가 spack_entity_mapping 을 받으면 MAPPED_TO 관계 생성 cypher 발행
- build_save_architecture_query 가 owned_aggregates / api_service_mapping 을
  받으면 OWNED_BY / HANDLED_BY 관계 생성 + svc.owned_aggregate_names 속성 저장
- 매핑이 비어있는 경우에도 safe — cypher 에 매핑 블록이 들어가지 않음

cypher 실행은 안 함 (Neo4j 없이 동작) — 문자열/파라미터만 검증.
"""
from __future__ import annotations

from app.pipelines.design_pipeline import (
    build_save_architecture_query,
    build_save_ddd_query,
)


# ─── DDD: spack_entity_mapping → MAPPED_TO 관계 ────────────────


class TestDddSpackEntityMapping:
    def test_mapping_produces_mapped_to_rel_cypher(self):
        ddd = {
            "contexts": [{"id": "CTX-01", "name": "Order Context"}],
            "aggregates": [{"id": "AGG-01", "name": "Order", "context_id": "CTX-01"}],
            "entities": [],
            "events": [],
            "spack_entity_mapping": [
                {
                    "spack_entity_id": "ENT-01",
                    "spack_name": "Order",
                    "ddd_location": "AGG-01",
                    "ddd_role": "aggregate_root",
                },
            ],
        }
        cypher, params = build_save_ddd_query("proj-x", ddd)
        # cypher 에 MAPPED_TO 관계 생성 키워드 들어가야 함
        assert "MAPPED_TO" in cypher
        assert "spack_entity_mapping" in cypher
        # 파라미터로 매핑 데이터 전달
        assert params.get("spack_entity_mapping") == ddd["spack_entity_mapping"]

    def test_empty_mapping_skips_block(self):
        ddd = {
            "contexts": [],
            "aggregates": [],
            "entities": [],
            "events": [],
            "spack_entity_mapping": [],
        }
        cypher, params = build_save_ddd_query("proj-x", ddd)
        # 빈 mapping 이면 매핑 블록 생략 (안전)
        assert "MAPPED_TO" not in cypher
        assert "spack_entity_mapping" not in params

    def test_missing_mapping_key_safe(self):
        """spack_entity_mapping 키 자체가 없어도 동작."""
        ddd = {"contexts": [], "aggregates": [], "entities": [], "events": []}
        cypher, params = build_save_ddd_query("proj-x", ddd)
        assert "MAPPED_TO" not in cypher

    def test_role_persisted(self):
        ddd = {
            "contexts": [],
            "aggregates": [],
            "entities": [],
            "events": [],
            "spack_entity_mapping": [
                {"spack_entity_id": "E1", "ddd_location": "A1", "ddd_role": "entity"},
            ],
        }
        cypher, _ = build_save_ddd_query("p", ddd)
        # role 을 관계 속성으로 SET
        assert "r.role = m.ddd_role" in cypher

    def test_mapping_has_name_fallback_for_id_drift(self):
        """[2026-06] LLM 이 spack_entity_id/ddd_location id 를 틀려도 name(=spack_name)
        으로 폴백 매칭해 MAPPED_TO 를 복원 — DDD_MAPPING_MISSING_ENTITY false-positive 방지.
        """
        ddd = {
            "contexts": [],
            "aggregates": [{"id": "AGG-01", "name": "Order", "context_id": "CTX-01"}],
            "entities": [],
            "events": [],
            "spack_entity_mapping": [
                {
                    "spack_entity_id": "ENT-99",   # 일부러 틀린 id (실제는 ENT-01)
                    "spack_name": "Order",          # name 은 절대 규칙상 정확
                    "ddd_location": "AGG-99",       # 일부러 틀린 id
                    "ddd_role": "aggregate_root",
                },
            ],
        }
        cypher, _ = build_save_ddd_query("p", ddd)
        # source/target 모두 id 매칭 + name(spack_name) 폴백을 coalesce 로 결합해야 함.
        assert "m.spack_name" in cypher
        assert "e_by_id" in cypher and "e_by_name" in cypher
        assert "t_by_id" in cypher and "t_by_name" in cypher
        assert "coalesce(e_by_id, e_by_name)" in cypher
        assert "coalesce(t_by_id, t_by_name)" in cypher


# ─── Architecture: owned_aggregates → OWNED_BY + 노드 속성 ────


class TestArchOwnedAggregates:
    def test_owned_aggregates_persisted_as_node_property(self):
        arch = {
            "services": [
                {
                    "id": "SVC-01",
                    "name": "OrderService",
                    "type": "Backend API",
                    "owned_aggregates": ["Order", "OrderItem"],
                },
            ],
            "databases": [],
            "connections": [],
        }
        cypher, params = build_save_architecture_query("p", arch)
        # flat services 에 owned_aggregate_names 가 들어가야 함
        flat = params["services"]
        assert flat[0]["_owned_aggregate_names"] == ["Order", "OrderItem"]
        # cypher 에 SET svc.owned_aggregate_names 들어가야 함
        assert "svc.owned_aggregate_names" in cypher

    def test_owned_aggregates_produces_owned_by_rel_cypher(self):
        arch = {
            "services": [
                {
                    "id": "SVC-01",
                    "name": "OrderService",
                    "type": "Backend API",
                    "owned_aggregates": ["Order"],
                },
            ],
            "databases": [],
            "connections": [],
        }
        cypher, _ = build_save_architecture_query("p", arch)
        # OWNED_BY 관계 생성 cypher 포함
        assert "OWNED_BY" in cypher
        # 이름으로 Aggregate 매칭
        assert "Aggregate {name: aggName" in cypher

    def test_empty_owned_aggregates_safe(self):
        arch = {
            "services": [{"id": "SVC-01", "name": "Svc", "type": "Backend API"}],
            "databases": [],
            "connections": [],
        }
        cypher, params = build_save_architecture_query("p", arch)
        # owned_aggregates 가 없으면 빈 list 로 저장 (UNWIND 가 0 행 처리)
        assert params["services"][0]["_owned_aggregate_names"] == []


# ─── Architecture: api_service_mapping → HANDLED_BY ──────────


class TestArchApiServiceMapping:
    def test_mapping_produces_handled_by_rel_cypher(self):
        arch = {
            "services": [],
            "databases": [],
            "connections": [],
            "api_service_mapping": [
                {"api_id": "API-01", "service_id": "SVC-01", "reason": "주문 처리"},
            ],
        }
        cypher, params = build_save_architecture_query("p", arch)
        assert "HANDLED_BY" in cypher
        assert "api_service_mapping" in cypher
        assert params.get("api_service_mapping") == arch["api_service_mapping"]

    def test_empty_mapping_skips_block(self):
        arch = {
            "services": [],
            "databases": [],
            "connections": [],
            "api_service_mapping": [],
        }
        cypher, params = build_save_architecture_query("p", arch)
        assert "HANDLED_BY" not in cypher
        assert "api_service_mapping" not in params

    def test_reason_persisted_as_rel_property(self):
        arch = {
            "services": [],
            "databases": [],
            "connections": [],
            "api_service_mapping": [
                {"api_id": "API-01", "service_id": "SVC-01", "reason": "X"},
            ],
        }
        cypher, _ = build_save_architecture_query("p", arch)
        assert "r.reason = m.reason" in cypher


# ─── get_*_graph response schemas — CrossMappingRel 노출 ────


def test_spack_graph_has_cross_mapping_fields():
    from app.service.query_repository import SpackGraph

    g = SpackGraph()
    # default 빈 list (Pydantic 기본값)
    assert g.entity_mapping_rels == []
    assert g.api_service_rels == []


def test_ddd_graph_has_aggregate_service_rels():
    from app.service.query_repository import DddGraph

    g = DddGraph()
    assert g.aggregate_service_rels == []


def test_clean_cross_rel_list_normalizes_dict_input():
    from app.service.query_repository import _clean_cross_rel_list

    raw = [
        {
            "source_id": "ENT-01",
            "target_id": "AGG-01",
            "target_name": "Order",
            "target_kind": "aggregate",
            "role": "aggregate_root",
            "type": "MAPPED_TO",
        },
        # 필수 키 누락 — 드롭
        {"target_id": "X", "type": "T"},
        # type 누락 — 드롭
        {"source_id": "A", "target_id": "B"},
    ]
    out = _clean_cross_rel_list(raw)
    assert len(out) == 1
    assert out[0].source_id == "ENT-01"
    assert out[0].target_name == "Order"
    assert out[0].role == "aggregate_root"


def test_clean_cross_rel_list_empty_input():
    from app.service.query_repository import _clean_cross_rel_list

    assert _clean_cross_rel_list(None) == []
    assert _clean_cross_rel_list([]) == []
    assert _clean_cross_rel_list("not a list") == []
