"""
Entity attributes 정규화 — string list ↔ 객체 list ↔ JSON string 사이의
호환 변환을 한 곳에서 흡수.

[A-1 — 2026-05-25 attributes 객체화]
이전 schema: attributes = ["plantId", "height", ...]  (이름만)
이후 schema: attributes = [
    {"name": "plantId", "type": "uuid", "required": True,
     "constraint": "", "description": "식물 식별자"}, ...
]

배경:
attributes 가 string list 였을 때 PRD 의 타입/제약/단위가 모두 휘발돼
AI 코딩 에이전트가 임의 schema 를 만들었음. plant 예시에서 `height`
가 cm 인지 mm 인지, leafCount 가 양수 제약을 가지는지 등이 누락.

설계 결정:
1. **객체 list 가 canonical 형태** — LLM 출력 / 정규화 결과 / MD 입력 모두 이 형태.
2. **Neo4j 저장은 JSON string** — Neo4j property 는 primitive list 만 허용해
   객체 list 직접 저장 불가. cypher.py 가 SET 직전 json.dumps 로 직렬화.
3. **read 헬퍼가 모든 입력 형태 흡수** — string list (legacy), 객체 list,
   JSON string, None 4가지 모두 객체 list 로 복원.
4. **backward compat 우선** — 기존 Neo4j 에 저장된 string list 데이터를
   파괴적 마이그레이션 없이 read 시 type="unknown" 객체로 변환해 노출.

이 모듈은 design pipeline / lint pipeline / fix spec pipeline /
query repository / create_md pipeline 모두 import 해서 공용 사용.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_ALLOWED_FIELDS = {"name", "type", "required", "constraint", "description"}
_REQUIRED_FIELDS = ("name", "type")
_UNKNOWN_TYPE = "unknown"


def _coerce_attr_object(raw: Any) -> Optional[Dict[str, Any]]:
    """단일 속성 객체 정규화. None 반환 시 호출자가 drop."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    attr_type = raw.get("type")
    if not isinstance(attr_type, str) or not attr_type.strip():
        attr_type = _UNKNOWN_TYPE
    out: Dict[str, Any] = {
        "name": name.strip(),
        "type": attr_type.strip(),
        "required": bool(raw.get("required", False)),
        "constraint": str(raw.get("constraint") or "").strip(),
        "description": str(raw.get("description") or "").strip(),
    }
    # 알려지지 않은 필드는 silently drop — schema 일관성.
    return out


def _migrate_legacy_string(s: str) -> Optional[Dict[str, Any]]:
    """legacy string ("plantId") → 객체. type="unknown" 으로 표시해
    downstream (MD, lint) 이 마이그레이션 필요성을 인지하도록."""
    s = s.strip()
    if not s:
        return None
    return {
        "name": s,
        "type": _UNKNOWN_TYPE,
        "required": False,
        "constraint": "",
        "description": "",
    }


def normalize_entity_attributes(raw: Any) -> List[Dict[str, Any]]:
    """
    어떤 입력 형태든 객체 list 로 정규화. 호출처별 분기 제거.

    수용 입력:
      - None / 빈 list / "" → []
      - 객체 list [{"name":"...", ...}, ...] → 그대로 (필드 정규화)
      - string list ["a", "b"]              → migrate (type=unknown)
      - JSON string '[{...}]'                → parse 후 객체 list 처리
      - JSON string '["a", "b"]'             → parse 후 string list migrate
      - mixed list [{...}, "legacy"]         → 각각 처리

    이름이 빈 항목은 drop. 이름 중복은 첫 번째만 유지 (안전).
    """
    if raw is None:
        return []

    # JSON string 케이스 — Neo4j 저장 형태.
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            raw = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            # 단일 string 으로 저장된 (있을 수 없는 케이스 방어).
            logger.warning("attributes JSON 파싱 실패, 단일 legacy 처리: %r", s[:60])
            obj = _migrate_legacy_string(s)
            return [obj] if obj else []

    if not isinstance(raw, list):
        # dict 단일 객체로 잘못 들어온 경우 list 로 승격.
        if isinstance(raw, dict):
            obj = _coerce_attr_object(raw)
            return [obj] if obj else []
        return []

    seen_names: set = set()
    out: List[Dict[str, Any]] = []
    for item in raw:
        obj: Optional[Dict[str, Any]] = None
        if isinstance(item, str):
            obj = _migrate_legacy_string(item)
        elif isinstance(item, dict):
            obj = _coerce_attr_object(item)
        # 그 외 형태 (int, None 등) 는 silently drop.
        if obj is None:
            continue
        if obj["name"] in seen_names:
            # 중복 이름은 첫 번째만 유지 (마지막 우선이면 LLM 의 retry 가 결과 흔듦).
            continue
        seen_names.add(obj["name"])
        out.append(obj)
    return out


def serialize_attributes_for_neo4j(attrs: Any) -> str:
    """Neo4j SET 직전 사용. 항상 JSON string 으로 직렬화.

    빈 list 도 '[]' 로 저장해 read 시 None vs empty 구분 가능
    (값이 없으면 attribute property 자체 미존재).
    """
    normalized = normalize_entity_attributes(attrs)
    return json.dumps(normalized, ensure_ascii=False)


def has_legacy_unknown_types(attrs: List[Dict[str, Any]]) -> bool:
    """이 list 안에 마이그레이트된 (type=unknown) 항목이 하나라도 있는지.
    create_md 가 이 신호를 MD 에 노출하기 위함."""
    return any(a.get("type") == _UNKNOWN_TYPE for a in attrs)


def decode_entities_attributes(
    entities: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Neo4j fetch 결과의 entity list 의 attributes 를 객체 list 로 복원.

    각 entity 의 attributes 필드가 JSON string / string list / 객체 list / None /
    무엇이든 들어와도 객체 list 로 통일. 다른 필드는 그대로.

    원본 mutate 회피 (호출자가 fetch 캐시를 공유할 수 있음). 새 dict 반환.
    """
    out: List[Dict[str, Any]] = []
    for e in entities or []:
        if not isinstance(e, dict):
            continue
        copy = dict(e)
        copy["attributes"] = normalize_entity_attributes(e.get("attributes"))
        out.append(copy)
    return out
