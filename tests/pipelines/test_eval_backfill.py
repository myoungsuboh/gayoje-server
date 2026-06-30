"""
eval_backfill.backfill_graph_dicts 단위테스트.

get_*_graph dict ↔ normalize_* 입력 모양 불일치 보정(false-positive 방지)이
정확히 동작하는지 — eval_score_routes 핸들러에서 추출된 순수 함수.
"""
from app.pipelines.design_validator.eval_backfill import backfill_graph_dicts


def test_api_service_mapping_backfilled_from_spack_rels():
    spack = {"api_service_rels": [{"source_id": "API-01", "target_id": "svc-a"}]}
    ddd = {}
    arch = {}  # api_service_mapping 비어있음
    backfill_graph_dicts(spack, ddd, arch)
    assert arch["api_service_mapping"] == [{"api_id": "API-01", "service_id": "svc-a"}]


def test_existing_api_service_mapping_preserved():
    spack = {"api_service_rels": [{"source_id": "API-01", "target_id": "svc-a"}]}
    arch = {"api_service_mapping": [{"api_id": "KEEP", "service_id": "KEEP"}]}
    backfill_graph_dicts(spack, {}, arch)
    # 이미 값이 있으면 덮어쓰지 않음
    assert arch["api_service_mapping"] == [{"api_id": "KEEP", "service_id": "KEEP"}]


def test_rels_with_missing_ids_filtered():
    spack = {
        "api_service_rels": [
            {"source_id": "API-01", "target_id": "svc-a"},
            {"source_id": None, "target_id": "svc-b"},   # 제외
            {"source_id": "API-03"},                       # target 없음 → 제외
        ]
    }
    arch = {}
    backfill_graph_dicts(spack, {}, arch)
    assert arch["api_service_mapping"] == [{"api_id": "API-01", "service_id": "svc-a"}]


def test_spack_entity_mapping_backfilled_from_rels():
    spack = {
        "entity_mapping_rels": [
            {"source_id": "Ent-1", "target_id": "Agg-1", "role": "root"}
        ]
    }
    ddd = {}
    backfill_graph_dicts(spack, ddd, {})
    assert ddd["spack_entity_mapping"] == [
        {"spack_entity_id": "Ent-1", "ddd_location": "Agg-1", "ddd_role": "root"}
    ]


def test_ddd_entities_events_alias_from_domain():
    ddd = {"domain_entities": [{"id": "E1"}], "domain_events": [{"id": "Ev1"}]}
    backfill_graph_dicts({}, ddd, {})
    assert ddd["entities"] == [{"id": "E1"}]
    assert ddd["events"] == [{"id": "Ev1"}]


def test_ddd_entities_not_clobbered_when_present():
    ddd = {"entities": [{"id": "KEEP"}], "domain_entities": [{"id": "E1"}]}
    backfill_graph_dicts({}, ddd, {})
    assert ddd["entities"] == [{"id": "KEEP"}]


def test_owned_aggregates_backfilled_from_names():
    arch = {"services": [{"id": "svc-a", "owned_aggregate_names": ["Agg-1", "Agg-2"]}]}
    backfill_graph_dicts({}, {}, arch)
    assert arch["services"][0]["owned_aggregates"] == ["Agg-1", "Agg-2"]


def test_owned_aggregates_preserved_when_present():
    arch = {
        "services": [
            {"id": "svc-a", "owned_aggregates": ["KEEP"], "owned_aggregate_names": ["X"]}
        ]
    }
    backfill_graph_dicts({}, {}, arch)
    assert arch["services"][0]["owned_aggregates"] == ["KEEP"]


def test_empty_inputs_no_crash():
    spack, ddd, arch = {}, {}, {}
    backfill_graph_dicts(spack, ddd, arch)
    assert arch["api_service_mapping"] == []
    assert ddd["spack_entity_mapping"] == []
    assert ddd["entities"] == []
    assert ddd["events"] == []
