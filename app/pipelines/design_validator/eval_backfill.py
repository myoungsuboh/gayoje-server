"""
eval-score 그래프 dict 백필 — get_*_graph 결과를 normalize_* 가 기대하는 모양으로 보정.

[배경]
Neo4j 저장 구조와 normalize_* 가 읽는 키가 달라, 보정 없이는 매핑 엣지가 누락된 것으로
오인돼 전 항목이 false ERROR 처리(점수 0 고착)된다. 이 함수가 spack 의 관계 정보 등을
arch/ddd dict 에 복원해 false-positive 를 제거한다.
(실제로 미매핑이면 여전히 ERROR — 검증 로직은 그대로 유지.)

순수 함수 — LLM/IO 없음. 입력 dict 를 in-place 보정한다.
(eval_score_routes 핸들러에서 추출 — 행위 동일.)
"""
from __future__ import annotations

from typing import Any, Dict


def backfill_graph_dicts(
    spack_dict: Dict[str, Any],
    ddd_dict: Dict[str, Any],
    arch_dict: Dict[str, Any],
) -> None:
    """get_*_graph dict 3종을 normalize_* 입력 모양으로 in-place 보정.

    - arch.api_service_mapping  ← spack.api_service_rels      (ARCH_API_UNMAPPED false 방지)
    - ddd.spack_entity_mapping  ← spack.entity_mapping_rels    (DDD_MAPPING_MISSING_ENTITY false 방지)
    - ddd.entities/events       ← ddd.domain_entities/events   (DDD_MISSING_SPACK_ENTITY false 방지)
    - service.owned_aggregates  ← service.owned_aggregate_names (ARCH_AGG_UNOWNED false 방지)
    """
    # api_service_mapping 은 (API)-[:HANDLED_BY]->(ArchService) 엣지라 get_architecture_graph 엔
    # 없다. spack 의 api_service_rels 에서 복원해 주입.
    if not arch_dict.get("api_service_mapping"):
        arch_dict["api_service_mapping"] = [
            {"api_id": r.get("source_id"), "service_id": r.get("target_id")}
            for r in (spack_dict.get("api_service_rels") or [])
            if r.get("source_id") and r.get("target_id")
        ]

    # spack_entity_mapping(SPACK Entity→DDD)은 (Entity)-[:MAPPED_TO]->(Aggregate|DomainEntity)
    # 엣지라 get_ddd_graph 엔 없다. entity_mapping_rels 에서 복원해 주입.
    if not ddd_dict.get("spack_entity_mapping"):
        ddd_dict["spack_entity_mapping"] = [
            {
                "spack_entity_id": r.get("source_id"),
                "ddd_location": r.get("target_id"),
                "ddd_role": r.get("role"),
            }
            for r in (spack_dict.get("entity_mapping_rels") or [])
            if r.get("source_id") and r.get("target_id")
        ]

    # get_ddd_graph 는 domain_entities/domain_events(그래프 모양)로 주는데 normalize_ddd 는
    # entities/events(LLM-출력 모양)를 읽는다. 별칭 추가(스코어러는 domain_* 를 쓰므로 안전).
    if "entities" not in ddd_dict:
        ddd_dict["entities"] = ddd_dict.get("domain_entities") or []
    if "events" not in ddd_dict:
        ddd_dict["events"] = ddd_dict.get("domain_events") or []

    # owned_aggregates 는 ArchService 노드에 owned_aggregate_names 속성으로 저장되는데
    # normalize_architecture 는 owned_aggregates 를 읽는다. 이름 불일치 backfill.
    for _svc in arch_dict.get("services") or []:
        if not _svc.get("owned_aggregates") and _svc.get("owned_aggregate_names"):
            _svc["owned_aggregates"] = list(_svc.get("owned_aggregate_names") or [])
