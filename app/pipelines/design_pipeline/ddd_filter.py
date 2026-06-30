"""
DDD 신뢰도 필터 — 코드 생성 입력에서 confidence=none DDD 제외.

[2026-05-27] "전시용 vs 코드-입력용 신뢰도 분리" 정책.
화면(getDDD/DddTab)은 confidence 3단계(direct/inferred/none)를 **그대로 다** 보여주되,
LLM 코드 생성 입력에서는 PRD 근거가 전혀 없는(none) aggregate/domain_entity 를 빼서
부실 DDD 가 다운스트림 코드를 오염시키는 것을 막는다.

적용 지점:
  - design_pipeline: Architecture 에이전트 입력(ddd_json_for_downstream)
  - create_md_pipeline: 바이브 코딩 패키지 ddd_md 입력

정책:
  - 명시적 confidence=='none' 만 제외. inferred 는 유지(프롬프트가 '추정-검증필요'로
    해석하도록 지시). direct 유지.
  - lineage 정보 자체가 없는 옛 데이터는 보존 — 정보 부재를 제외 근거로 쓰지 않음.
  - context / event 는 confidence(lineage)가 없으므로 항상 그대로.

두 입력의 구조 차이를 모두 흡수:
  - confidence: nested(item["lineage"]["confidence"]) 또는 flat(item["lineage_confidence"])
  - 필드명: pipeline 은 entities/events, DddGraph 는 domain_entities/domain_events
"""
from __future__ import annotations

from typing import Any, Dict

# confidence 가 붙는 노드 컬렉션 — pipeline(entities) / DddGraph(domain_entities) 둘 다.
_CONFIDENCE_KEYS = ("aggregates", "entities", "domain_entities")


def _confidence_of(item: Dict[str, Any]) -> str:
    """노드의 lineage confidence 추출 — nested/flat 모두. 정보 없으면 'unknown'."""
    lineage = item.get("lineage")
    if isinstance(lineage, dict) and lineage.get("confidence"):
        return str(lineage["confidence"])
    flat = item.get("lineage_confidence")
    if flat:
        return str(flat)
    return "unknown"  # lineage 정보 자체 없음(옛 데이터) — 제외하지 않음


def filter_ddd_for_codegen(ddd: Dict[str, Any]) -> Dict[str, Any]:
    """confidence=='none' aggregate/domain_entity 를 제외한 복사본 반환 (원본 불변).

    context/event 및 lineage 정보 없는 노드는 그대로 유지. 화면용 원본에는 영향 없음.
    """
    out = dict(ddd)
    for key in _CONFIDENCE_KEYS:
        items = ddd.get(key)
        if isinstance(items, list):
            out[key] = [it for it in items if _confidence_of(it) != "none"]
    return out
