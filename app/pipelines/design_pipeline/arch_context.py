"""
Downstream LLM 입력 최적화 — Spack/DDD를 필요한 필드만 남기는 slim 함수 모음.

[2026-05-27 성능] DDD / Architecture 에이전트 프롬프트 크기를 최소화해 Gemini 응답 시간 단축.

DDD 입력 — slim_spack_for_ddd:
  유지: entities[id, name, description] (Aggregate name 인용 + spack_entity_mapping)
  제거: APIs / policies / screens / entity attributes

Architecture 입력 — slim_spack_for_arch + slim_ddd_for_arch:
  Spack: APIs[id/name/method/endpoint/description] + entities[id/name]
  DDD: contexts[id/name] + aggregates[id/name/context_id/lineage.confidence]
  제거: payload/error/auth/attributes/policies/screens, invariants, domain_entities, events

대형 PRD 기준 ~50KB → ~5KB 수준으로 축소.
"""
from __future__ import annotations

from typing import Any, Dict


def slim_spack_for_ddd(spack: Dict[str, Any]) -> Dict[str, Any]:
    """DDD 에이전트 입력용 Spack slim.

    DDD 프롬프트가 실제 사용하는 Spack 정보는 entity 명칭·설명·ID가 전부.
    Aggregate name = SPACK Entity name (절대 규칙), spack_entity_mapping 의 spack_entity_id
    참조. description 은 LLM 이 Aggregate Root 여부를 판단하는 context 제공 용도.
    API/policy/screen/entity attributes 는 DDD 출력에 불필요.
    """
    slim_entities = [
        {k: ent[k] for k in ("id", "name", "description") if k in ent}
        for ent in (spack.get("entities") or [])
    ]
    return {"entities": slim_entities}


def slim_spack_for_arch(spack: Dict[str, Any]) -> Dict[str, Any]:
    """Architecture 에이전트 입력용 Spack slim.

    api_service_mapping에 필요한 API 식별 정보와
    owned_aggregates 인용 규칙(ABSOLUTE CONSTRAINTS #5)을 위한 Entity 명칭만 유지.
    """
    slim_apis = [
        {k: api[k] for k in ("id", "name", "method", "endpoint", "description", "related_story_id") if k in api}
        for api in (spack.get("apis") or [])
    ]
    slim_entities = [
        {k: ent[k] for k in ("id", "name") if k in ent}
        for ent in (spack.get("entities") or [])
    ]
    return {"apis": slim_apis, "entities": slim_entities}


def slim_ddd_for_arch(ddd: Dict[str, Any]) -> Dict[str, Any]:
    """Architecture 에이전트 입력용 DDD slim.

    서비스 경계 결정(Bounded Context 1:1 정렬)과
    owned_aggregates 완전성 보장에 필요한 Context/Aggregate 식별 정보만 유지.
    lineage.confidence는 inferred 경고 프롬프트 지시 준수를 위해 유지.
    domain_entities / events는 Architecture 출력 결정에 불필요하므로 제외.
    """
    slim_contexts = [
        {k: ctx[k] for k in ("id", "name") if k in ctx}
        for ctx in (ddd.get("contexts") or [])
    ]
    slim_aggregates = []
    for agg in (ddd.get("aggregates") or []):
        entry: Dict[str, Any] = {k: agg[k] for k in ("id", "name", "context_id") if k in agg}
        lineage = agg.get("lineage")
        if isinstance(lineage, dict) and lineage.get("confidence"):
            entry["lineage"] = {"confidence": lineage["confidence"]}
        slim_aggregates.append(entry)
    return {"contexts": slim_contexts, "aggregates": slim_aggregates}
