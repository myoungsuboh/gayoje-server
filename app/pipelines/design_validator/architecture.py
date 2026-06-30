from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from .types import Violation, ValidationReport, SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO
from .lineage import normalize_lineage
from .spack import _normalize_tech_stack, _SERVICE_TYPE_ORDER


def normalize_architecture(
    arch: Dict[str, Any],
    normalized_spack: Dict[str, Any],
    normalized_ddd: Dict[str, Any],
    *,
    valid_story_ids: Optional[Set[str]] = None,
) -> Tuple[Dict[str, Any], ValidationReport]:
    """
    Architecture 출력 정규화 + Spack/DDD cross-검증.

    정규화:
      1. services / databases / connections / api_service_mapping 정렬
      2. ID 재부여 — SVC-01, DB-01 시퀀스
      3. tech_stack 표준 명칭으로 (예: 'vue' → 'Vue.js')
      4. owned_aggregates 의 옛 Aggregate ID 가 들어 있으면 새 ID 로 재매핑 (방어)

    Cross-검증:
      - api_service_mapping 의 api_id 모두 Spack 에 실재 + 정확히 1번씩
      - api_service_mapping 의 service_id 모두 services 안에 실재
      - 모든 DDD Aggregate name 이 어느 Backend Service.owned_aggregates 에 정확히 1번
      - tech_stack 미표준 명칭 (별칭) 자동 교정
      - Frontend Service 에 owned_aggregates 가 들어가면 경고
    """
    report = ValidationReport(stage="arch")

    services = list(arch.get("services") or [])
    databases = list(arch.get("databases") or [])
    connections = list(arch.get("connections") or [])
    api_service_mapping = list(arch.get("api_service_mapping") or [])

    # ─ 정렬 + tech_stack 정규화 ─
    services = sorted(
        services,
        key=lambda s: (
            _SERVICE_TYPE_ORDER.get(str(s.get("type") or ""), 99),
            str(s.get("name") or ""),
        ),
    )
    databases = sorted(
        databases,
        key=lambda d: (str(d.get("tech_stack") or "~"), str(d.get("name") or "")),
    )

    for s in services:
        original = s.get("tech_stack")
        normalized_ts = _normalize_tech_stack(original)
        if normalized_ts and normalized_ts != original:
            report.add_fixed(Violation(
                code="ARCH_TECH_STACK_NORMALIZED", severity=SEVERITY_INFO, stage="arch",
                message=f"Service tech_stack {original!r} -> {normalized_ts!r}",
                item_id=s.get("id"), detail={"old": original, "new": normalized_ts},
            ))
            s["tech_stack"] = normalized_ts

    for d in databases:
        original = d.get("tech_stack")
        normalized_ts = _normalize_tech_stack(original)
        if normalized_ts and normalized_ts != original:
            report.add_fixed(Violation(
                code="ARCH_TECH_STACK_NORMALIZED", severity=SEVERITY_INFO, stage="arch",
                message=f"Database tech_stack {original!r} -> {normalized_ts!r}",
                item_id=d.get("id"), detail={"old": original, "new": normalized_ts},
            ))
            d["tech_stack"] = normalized_ts

    # [B3 — 2026-05 lineage] Service lineage 정규화.
    # Service 는 1~4 Story 와 연관 가능. Frontend 는 보통 'inferred' 가 자연스러움.
    # [A — 2026-05] valid_story_ids 전달 시 PRD 부재 story_id drop.
    for s in services:
        s["lineage"] = normalize_lineage(
            s.get("lineage"),
            node_id=str(s.get("id") or "SVC-?"),
            stage="arch", report=report,
            valid_story_ids=valid_story_ids,
        )

    # [C — 2026-05 lineage] Database 도 lineage 정규화 (Service 와 동일 패턴).
    for d in databases:
        d["lineage"] = normalize_lineage(
            d.get("lineage"),
            node_id=str(d.get("id") or "DB-?"),
            stage="arch", report=report,
            valid_story_ids=valid_story_ids,
        )

    # ─ ID 재부여 ─
    svc_remap: Dict[str, str] = {}
    for idx, s in enumerate(services, start=1):
        new_id = f"SVC-{idx:02d}"
        old_id = str(s.get("id") or "")
        if old_id and old_id != new_id:
            svc_remap[old_id] = new_id
            report.add_fixed(Violation(
                code="ARCH_SVC_ID_REASSIGNED", severity=SEVERITY_INFO, stage="arch",
                message=f"Service id {old_id} -> {new_id}",
                item_id=new_id, detail={"old": old_id, "new": new_id},
            ))
        s["id"] = new_id

    db_remap: Dict[str, str] = {}
    for idx, d in enumerate(databases, start=1):
        new_id = f"DB-{idx:02d}"
        old_id = str(d.get("id") or "")
        if old_id and old_id != new_id:
            db_remap[old_id] = new_id
            report.add_fixed(Violation(
                code="ARCH_DB_ID_REASSIGNED", severity=SEVERITY_INFO, stage="arch",
                message=f"Database id {old_id} -> {new_id}",
                item_id=new_id, detail={"old": old_id, "new": new_id},
            ))
        d["id"] = new_id

    # ─ owned_aggregates: 옛 AGG ID 가 string 으로 들어 있으면 재매핑 (방어) ─
    agg_remap = normalized_ddd.get("_agg_remap") or {}
    if agg_remap:
        for s in services:
            owned = s.get("owned_aggregates") or []
            if not isinstance(owned, list):
                continue
            s["owned_aggregates"] = [agg_remap.get(x, x) for x in owned]

    # ─ connections 의 source_id/target_id 재매핑 + 정렬 + dangling 검증 ─
    for c in connections:
        for key in ("source_id", "target_id"):
            v = c.get(key)
            if v in svc_remap:
                c[key] = svc_remap[v]
            elif v in db_remap:
                c[key] = db_remap[v]
    connections = sorted(
        connections,
        key=lambda c: (
            str(c.get("source_id") or ""),
            str(c.get("target_id") or ""),
        ),
    )

    # 검증: connection 의 source/target 이 services + databases 중 어느 것도 못 가리키면
    # OPTIONAL MATCH 가 silent drop 하므로 데이터 손실. 명시적 경고.
    known_node_ids: Set[str] = (
        {s["id"] for s in services} | {d["id"] for d in databases}
    )
    for c in connections:
        for key in ("source_id", "target_id"):
            v = c.get(key)
            if v and v not in known_node_ids:
                report.add(Violation(
                    code="ARCH_CONN_DANGLING", severity=SEVERITY_ERROR, stage="arch",
                    message=(
                        f"Connection 의 {key}={v!r} 가 services / databases 어디에도 없음. "
                        f"Neo4j 저장 시 silent drop 됨."
                    ),
                    detail={
                        key: v,
                        "source_id": c.get("source_id"),
                        "target_id": c.get("target_id"),
                    },
                ))

    # ─ api_service_mapping 의 api_id / service_id 재매핑 + 정렬 ─
    api_id_remap = normalized_spack.get("_id_remap") or {}
    for m in api_service_mapping:
        aid = m.get("api_id")
        if aid in api_id_remap:
            m["api_id"] = api_id_remap[aid]
        sid = m.get("service_id")
        if sid in svc_remap:
            m["service_id"] = svc_remap[sid]
    api_service_mapping = sorted(
        api_service_mapping, key=lambda m: str(m.get("api_id") or "")
    )

    # ─ Cross-검증: api_service_mapping ─
    spack_api_ids: Set[str] = {
        str(a.get("id") or "") for a in (normalized_spack.get("apis") or []) if a.get("id")
    }
    arch_svc_ids: Set[str] = {s["id"] for s in services}
    mapped_api_ids: List[str] = [str(m.get("api_id") or "") for m in api_service_mapping]

    unmapped_apis = spack_api_ids - set(mapped_api_ids)
    for aid in sorted(unmapped_apis):
        report.add(Violation(
            code="ARCH_API_UNMAPPED", severity=SEVERITY_ERROR, stage="cross",
            message=f"Spack API {aid} 가 api_service_mapping 에 누락",
            item_id=aid,
        ))

    api_count: Dict[str, int] = {}
    for aid in mapped_api_ids:
        api_count[aid] = api_count.get(aid, 0) + 1
    for aid, n in api_count.items():
        if n > 1:
            report.add(Violation(
                code="ARCH_API_DUPLICATE_MAPPING", severity=SEVERITY_WARNING, stage="cross",
                message=f"API {aid} 가 api_service_mapping 에 {n}회 등장 (1:1 위반)",
                item_id=aid, detail={"count": n},
            ))
        if aid not in spack_api_ids:
            report.add(Violation(
                code="ARCH_API_UNKNOWN_IN_MAPPING", severity=SEVERITY_ERROR, stage="cross",
                message=f"api_service_mapping 의 api_id {aid!r} 가 Spack 에 없음",
                item_id=aid,
            ))

    # api 가 매핑된 service 의 type 도 검증 — API 는 Backend / Gateway 만 구현 가능
    svc_id_to_type: Dict[str, str] = {s["id"]: str(s.get("type") or "") for s in services}
    for m in api_service_mapping:
        sid = str(m.get("service_id") or "")
        if sid and sid not in arch_svc_ids:
            report.add(Violation(
                code="ARCH_API_MAPPING_DANGLING_SVC", severity=SEVERITY_ERROR, stage="cross",
                message=f"api_service_mapping 의 service_id {sid!r} 가 services 에 없음",
                item_id=sid,
            ))
            continue
        # Frontend service 에 API 가 매핑된 경우 — 프롬프트 규칙 위반
        if svc_id_to_type.get(sid) == "Frontend":
            report.add(Violation(
                code="ARCH_API_MAPPED_TO_FRONTEND", severity=SEVERITY_WARNING, stage="cross",
                message=(
                    f"API {m.get('api_id')!r} 가 Frontend Service {sid!r} 에 매핑됨. "
                    f"API 는 Backend / Gateway 가 구현 주체."
                ),
                item_id=m.get("api_id"), detail={"service_id": sid},
            ))

    # ─ Cross-검증: owned_aggregates ─
    ddd_agg_names: Set[str] = {
        str(a.get("name") or "")
        for a in (normalized_ddd.get("aggregates") or [])
        if a.get("name")
    }
    owned_by_count: Dict[str, List[str]] = {}  # name -> [svc_id, ...]
    for s in services:
        typ = str(s.get("type") or "")
        owned = s.get("owned_aggregates") or []
        if not isinstance(owned, list):
            continue
        if typ == "Frontend" and owned:
            report.add(Violation(
                code="ARCH_FRONTEND_HAS_AGGREGATES", severity=SEVERITY_WARNING, stage="arch",
                message=(
                    f"Frontend Service {s.get('name')!r} 가 owned_aggregates 를 가짐. "
                    f"프롬프트 규칙상 Backend 만 aggregate 소유."
                ),
                item_id=s.get("id"), detail={"owned": owned},
            ))
            continue
        for ag in owned:
            owned_by_count.setdefault(str(ag), []).append(str(s.get("id") or ""))

    for name in sorted(ddd_agg_names):
        if name not in owned_by_count:
            report.add(Violation(
                code="ARCH_AGG_UNOWNED", severity=SEVERITY_ERROR, stage="cross",
                message=f"DDD Aggregate {name!r} 가 어느 Backend Service 에도 owned 안 됨",
                detail={"aggregate_name": name},
            ))
        elif len(owned_by_count[name]) > 1:
            report.add(Violation(
                code="ARCH_AGG_MULTI_OWNED", severity=SEVERITY_WARNING, stage="cross",
                message=(
                    f"Aggregate {name!r} 가 {len(owned_by_count[name])}개 Service 에 owned "
                    f"(Bounded Context = Service 1:1 원칙 위반)"
                ),
                detail={"aggregate_name": name, "owned_by": owned_by_count[name]},
            ))

    for name, svc_ids in owned_by_count.items():
        if name and name not in ddd_agg_names:
            report.add(Violation(
                code="ARCH_AGG_UNKNOWN", severity=SEVERITY_WARNING, stage="cross",
                message=(
                    f"Service {svc_ids} 가 owned 한 {name!r} 이 DDD Aggregate 에 없음 "
                    f"(이름 변형 또는 가공의 aggregate)"
                ),
                detail={"name": name, "owned_by": svc_ids},
            ))

    normalized: Dict[str, Any] = {
        "services": services,
        "databases": databases,
        "connections": connections,
        "api_service_mapping": api_service_mapping,
    }
    return normalized, report


# ─── Aggregate Report ───────────────────────────────────────────


def summarize_reports(*reports: ValidationReport) -> Dict[str, Any]:
    """여러 stage 의 ValidationReport 를 한 summary 로 모음. diagnostic.design_health 에 들어감."""
    total_errors = sum(r.error_count for r in reports)
    total_warnings = sum(r.warning_count for r in reports)
    total_auto_fixed = sum(len(r.auto_fixed) for r in reports)

    code_counts: Dict[str, int] = {}
    for r in reports:
        for v in r.violations:
            code_counts[v.code] = code_counts.get(v.code, 0) + 1
    top_codes = sorted(code_counts.items(), key=lambda x: -x[1])[:5]

    return {
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "total_auto_fixed": total_auto_fixed,
        "healthy": total_errors == 0,
        "top_violation_codes": [{"code": c, "count": n} for c, n in top_codes],
        "stages": [r.to_dict() for r in reports],
    }
