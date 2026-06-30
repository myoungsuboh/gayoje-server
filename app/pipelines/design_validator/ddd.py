from __future__ import annotations
from typing import Any, Dict, List, Optional, Set, Tuple
from .types import Violation, ValidationReport, SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO
from .lineage import normalize_lineage
from .attributes import normalize_entity_attributes
from .ddd_detail import normalize_invariants


def normalize_ddd(
    ddd: Dict[str, Any],
    normalized_spack: Dict[str, Any],
    *,
    valid_story_ids: Optional[Set[str]] = None,
) -> Tuple[Dict[str, Any], ValidationReport]:
    """
    DDD 출력 정규화 + Spack 기준 cross-검증.

    정규화:
      1. contexts / aggregates / entities / events 정렬
      2. ID 재부여 — CTX-01, AGG-01, DENT-01, EVT-01 시퀀스
      3. context_id / aggregate_id / published_by_aggregate_id 의 old → new ID 재매핑

    Cross-검증 (Spack 기준):
      - 모든 Spack Entity name 이 DDD Aggregate name ∪ DomainEntity name 에 등장
        (글자 단위 동일 — pluralize / suffix 변형 금지)
      - spack_entity_mapping 의 모든 spack_entity_id 가 Spack 에 실재
      - mapping 완전성 — 모든 Spack entity 가 정확히 1번씩 등장 (누락·중복 catch)
    """
    report = ValidationReport(stage="ddd")

    contexts = list(ddd.get("contexts") or [])
    aggregates = list(ddd.get("aggregates") or [])
    entities = list(ddd.get("entities") or [])
    events = list(ddd.get("events") or [])
    mapping = list(ddd.get("spack_entity_mapping") or [])

    # ─ 정렬 ─
    contexts = sorted(contexts, key=lambda c: str(c.get("name") or ""))
    aggregates = sorted(
        aggregates,
        key=lambda a: (str(a.get("context_id") or "~"), str(a.get("name") or "")),
    )
    entities = sorted(
        entities,
        key=lambda e: (str(e.get("aggregate_id") or "~"), str(e.get("name") or "")),
    )
    events = sorted(
        events,
        key=lambda v: (str(v.get("related_story_id") or "~"), str(v.get("name") or "")),
    )

    # ─ ID 재부여 (정렬 결과 = ID 시퀀스) + old → new 매핑 ─
    ctx_remap: Dict[str, str] = {}
    for idx, c in enumerate(contexts, start=1):
        new_id = f"CTX-{idx:02d}"
        old_id = str(c.get("id") or "")
        if old_id and old_id != new_id:
            ctx_remap[old_id] = new_id
            report.add_fixed(Violation(
                code="DDD_CTX_ID_REASSIGNED", severity=SEVERITY_INFO, stage="ddd",
                message=f"Context id {old_id} → {new_id}",
                item_id=new_id, detail={"old": old_id, "new": new_id},
            ))
        c["id"] = new_id

    agg_remap: Dict[str, str] = {}
    for idx, a in enumerate(aggregates, start=1):
        new_id = f"AGG-{idx:02d}"
        old_id = str(a.get("id") or "")
        if old_id and old_id != new_id:
            agg_remap[old_id] = new_id
            report.add_fixed(Violation(
                code="DDD_AGG_ID_REASSIGNED", severity=SEVERITY_INFO, stage="ddd",
                message=f"Aggregate id {old_id} → {new_id}",
                item_id=new_id, detail={"old": old_id, "new": new_id},
            ))
        a["id"] = new_id
        # context_id 재매핑
        if a.get("context_id") in ctx_remap:
            a["context_id"] = ctx_remap[a["context_id"]]

    for idx, e in enumerate(entities, start=1):
        new_id = f"DENT-{idx:02d}"
        old_id = str(e.get("id") or "")
        if old_id and old_id != new_id:
            report.add_fixed(Violation(
                code="DDD_ENT_ID_REASSIGNED", severity=SEVERITY_INFO, stage="ddd",
                message=f"DomainEntity id {old_id} → {new_id}",
                item_id=new_id, detail={"old": old_id, "new": new_id},
            ))
        e["id"] = new_id
        if e.get("aggregate_id") in agg_remap:
            e["aggregate_id"] = agg_remap[e["aggregate_id"]]

    for idx, v in enumerate(events, start=1):
        new_id = f"EVT-{idx:02d}"
        old_id = str(v.get("id") or "")
        if old_id and old_id != new_id:
            report.add_fixed(Violation(
                code="DDD_EVT_ID_REASSIGNED", severity=SEVERITY_INFO, stage="ddd",
                message=f"Event id {old_id} → {new_id}",
                item_id=new_id, detail={"old": old_id, "new": new_id},
            ))
        v["id"] = new_id
        if v.get("published_by_aggregate_id") in agg_remap:
            v["published_by_aggregate_id"] = agg_remap[v["published_by_aggregate_id"]]

        # 검증: event 의 PRD Story 추적성 (프롬프트 강제)
        if not v.get("related_story_id"):
            report.add(Violation(
                code="DDD_EVENT_MISSING_STORY_REF", severity=SEVERITY_WARNING, stage="ddd",
                message=(
                    f"Event '{v.get('name')}' 가 related_story_id 누락 "
                    f"(어떤 Story 에서 유발되는지 추적 불가)"
                ),
                item_id=new_id,
            ))
        # 검증: event 의 published_by_aggregate_id 가 실재 Aggregate 인지
        pub_agg = v.get("published_by_aggregate_id")
        if pub_agg:
            agg_ids: Set[str] = {a["id"] for a in aggregates}
            if pub_agg not in agg_ids:
                report.add(Violation(
                    code="DDD_EVENT_DANGLING_AGGREGATE", severity=SEVERITY_ERROR, stage="ddd",
                    message=(
                        f"Event '{v.get('name')}' 의 published_by_aggregate_id "
                        f"'{pub_agg}' 가 어느 Aggregate 와도 매칭 안 됨"
                    ),
                    item_id=new_id, detail={"published_by_aggregate_id": pub_agg},
                ))

    # ─ Cross-검증: Spack Entity name vs DDD name set ─
    spack_entities = list(normalized_spack.get("entities") or [])
    spack_entity_names: Set[str] = {
        str(e.get("name") or "") for e in spack_entities if e.get("name")
    }
    ddd_names: Set[str] = (
        {str(a.get("name") or "") for a in aggregates if a.get("name")}
        | {str(e.get("name") or "") for e in entities if e.get("name")}
    )

    missing_in_ddd = spack_entity_names - ddd_names
    for name in sorted(missing_in_ddd):
        report.add(Violation(
            code="DDD_MISSING_SPACK_ENTITY", severity=SEVERITY_ERROR, stage="cross",
            message=(
                f"Spack Entity '{name}' 가 DDD 의 Aggregate / DomainEntity 중 어디에도 "
                f"등장하지 않음 (이름 변형 또는 누락)"
            ),
            detail={"entity_name": name},
        ))

    extra_in_ddd = ddd_names - spack_entity_names
    for name in sorted(extra_in_ddd):
        report.add(Violation(
            code="DDD_UNKNOWN_NAME", severity=SEVERITY_WARNING, stage="cross",
            message=(
                f"DDD 에 '{name}' 이름이 등장하지만 Spack Entity 중에 없음. "
                f"새 entity 추정 또는 이름 변형 가능성."
            ),
            detail={"name": name},
        ))

    # [B3 — 2026-05 lineage] Aggregate lineage 정규화.
    # Aggregate 는 cross-story (1~3 Story) 가 정상이라 related_stories 가 2~3개 됨.
    # [A — 2026-05] valid_story_ids 전달 시 PRD 부재 story_id drop.
    for a in aggregates:
        a["lineage"] = normalize_lineage(
            a.get("lineage"),
            node_id=str(a.get("id") or "AGG-?"),
            stage="ddd", report=report,
            valid_story_ids=valid_story_ids,
        )

    # [C — 2026-05 lineage] DomainEntity (entities) lineage 정규화.
    # DomainEntity 는 Aggregate 의 하위 — 보통 단일 Story 와 연관 (1~2 stories).
    for de in entities:
        de["lineage"] = normalize_lineage(
            de.get("lineage"),
            node_id=str(de.get("id") or "DENT-?"),
            stage="ddd", report=report,
            valid_story_ids=valid_story_ids,
        )

    # [D-1 — 2026-05-25] DDD detail 정규화 + 검증.
    # Aggregate.invariants / DomainEntity.attributes / DomainEvent.payload_fields
    for a in aggregates:
        a["invariants"] = normalize_invariants(a.get("invariants"))
        if not a["invariants"]:
            report.add(Violation(
                code="AGGREGATE_INVARIANTS_MISSING",
                severity=SEVERITY_INFO, stage="ddd",
                message=(
                    f"Aggregate '{a.get('name')}' 의 invariants 가 비어있음 — "
                    "도메인 규칙 미정의. AI 가 임의 검증 로직 작성 위험."
                ),
                item_id=a.get("id"),
            ))

    for de in entities:
        de["attributes"] = normalize_entity_attributes(de.get("attributes"))
        if not de["attributes"]:
            report.add(Violation(
                code="DOMAIN_ENTITY_ATTRIBUTES_MISSING",
                severity=SEVERITY_INFO, stage="ddd",
                message=(
                    f"DomainEntity '{de.get('name')}' 의 attributes 가 비어있음 — "
                    "도메인 모델 필드 미정의."
                ),
                item_id=de.get("id"),
            ))

    for ev in events:
        ev["payload_fields"] = normalize_entity_attributes(ev.get("payload_fields"))
        if not ev["payload_fields"]:
            report.add(Violation(
                code="DOMAIN_EVENT_PAYLOAD_MISSING",
                severity=SEVERITY_INFO, stage="ddd",
                message=(
                    f"DomainEvent '{ev.get('name')}' 의 payload_fields 가 비어있음 — "
                    "이벤트 핸들러가 처리할 데이터 불명."
                ),
                item_id=ev.get("id"),
            ))

    # ─ spack_entity_mapping 검증 ─
    spack_entity_ids: Set[str] = {
        str(e.get("id") or "") for e in spack_entities if e.get("id")
    }
    mapped_ids: List[str] = [str(m.get("spack_entity_id") or "") for m in mapping]

    # 누락: Spack ID 중에 mapping 에 없는 것
    unmapped = spack_entity_ids - set(mapped_ids)
    for sid in sorted(unmapped):
        report.add(Violation(
            code="DDD_MAPPING_MISSING_ENTITY", severity=SEVERITY_ERROR, stage="cross",
            message=f"Spack Entity {sid} 가 spack_entity_mapping 에 누락",
            item_id=sid,
        ))

    # 중복: 한 spack_entity_id 가 mapping 에 2번 이상
    seen: Dict[str, int] = {}
    for sid in mapped_ids:
        seen[sid] = seen.get(sid, 0) + 1
    for sid, n in seen.items():
        if n > 1:
            report.add(Violation(
                code="DDD_MAPPING_DUPLICATE_ENTITY", severity=SEVERITY_WARNING, stage="cross",
                message=f"Spack Entity {sid} 가 spack_entity_mapping 에 {n}회 등장",
                item_id=sid, detail={"count": n},
            ))

    # mapping 의 spack_entity_id 가 실제 Spack 에 있는 ID 인지
    for m in mapping:
        sid = str(m.get("spack_entity_id") or "")
        if sid and sid not in spack_entity_ids:
            report.add(Violation(
                code="DDD_MAPPING_UNKNOWN_ENTITY", severity=SEVERITY_ERROR, stage="cross",
                message=f"spack_entity_mapping 의 spack_entity_id '{sid}' 가 Spack 에 없음",
                item_id=sid,
            ))

    normalized: Dict[str, Any] = {
        "contexts": contexts,
        "aggregates": aggregates,
        "entities": entities,
        "events": events,
        "spack_entity_mapping": mapping,
        "_agg_remap": agg_remap,  # Architecture 단계에서 owned_aggregates 재매핑용
    }
    return normalized, report
