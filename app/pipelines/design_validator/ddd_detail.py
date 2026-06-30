"""
DDD 노드의 detail 필드 정규화.

[D-1 — 2026-05-25 DDD detail 격상]
이전 DDD schema: Aggregate/Entity/Event 가 id/name/description/lineage 만.
이후: Domain 코드 생성에 필요한 디테일도 보존.

- Aggregate.invariants: ["leafCount >= 0", ...] string list — 도메인 규칙
- DomainEntity.attributes: SPACK Entity 와 동일 객체 list 재사용
- DomainEvent.payload_fields: 발행 시 전달 데이터 (객체 list)

이 모듈은 attributes 와 payload 모두 객체 list 라 normalize_entity_attributes
를 재사용 — 코드 중복 회피.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from app.pipelines.design_validator.attributes import normalize_entity_attributes

logger = logging.getLogger(__name__)


def normalize_invariants(raw: Any) -> List[str]:
    """invariants 정규화 — string list.

    수용 입력:
      - None / 빈 list / "" → []
      - string list 그대로
      - JSON string '["a", "b"]' → parse 후 list
      - 객체 list [{"rule": "..."}] → "rule" 또는 "description" 키만 추출 (LLM
        이 잘못 객체로 줘도 흡수)
      - 단일 string → 단일 element list 로 승격
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        # JSON 시도
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                raw = parsed
            elif isinstance(parsed, str):
                return [parsed.strip()] if parsed.strip() else []
            else:
                return []
        except (json.JSONDecodeError, ValueError):
            # 평문 string → 단일 invariant 로
            return [s]
    if not isinstance(raw, list):
        return []

    out: List[str] = []
    seen: set = set()
    for item in raw:
        rule: str = ""
        if isinstance(item, str):
            rule = item.strip()
        elif isinstance(item, dict):
            # LLM 이 객체로 잘못 출력했을 때 추출 시도
            rule = str(
                item.get("rule")
                or item.get("description")
                or item.get("invariant")
                or ""
            ).strip()
        if rule and rule not in seen:
            seen.add(rule)
            out.append(rule)
    return out


def serialize_invariants_for_neo4j(raw: Any) -> str:
    """JSON string 으로 직렬화. 빈 list 도 '[]' 로 저장."""
    return json.dumps(normalize_invariants(raw), ensure_ascii=False)


def decode_aggregates_detail(
    aggregates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Aggregate list 의 invariants 복원. 원본 mutate 회피."""
    out: List[Dict[str, Any]] = []
    for a in aggregates or []:
        if not isinstance(a, dict):
            continue
        copy = dict(a)
        copy["invariants"] = normalize_invariants(a.get("invariants"))
        out.append(copy)
    return out


def decode_domain_entities_detail(
    entities: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """DomainEntity list 의 attributes 복원 (SPACK 와 동일 헬퍼 재사용)."""
    out: List[Dict[str, Any]] = []
    for e in entities or []:
        if not isinstance(e, dict):
            continue
        copy = dict(e)
        copy["attributes"] = normalize_entity_attributes(e.get("attributes"))
        out.append(copy)
    return out


def decode_domain_events_detail(
    events: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """DomainEvent list 의 payload_fields 복원."""
    out: List[Dict[str, Any]] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        copy = dict(ev)
        copy["payload_fields"] = normalize_entity_attributes(ev.get("payload_fields"))
        out.append(copy)
    return out
