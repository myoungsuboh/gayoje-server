"""
spec 노드 placeholder/garbage 판별 — 생성 위생(L1)의 결정적 방어선.

[배경 — 2026-05-28]
빈약한 회의록에서 LLM 이 "정의 불가 / 미정 / 명세서 부재" 같은 placeholder 로 PRD
템플릿 칸을 채우면, label("Epic"/"Story") 만으로 세는 spec_count 가 이를 우회해
master 에 누더기가 누적됐다(운영 사고: Product Vision 6개, Section 2·3 불일치).

이 모듈은 그 placeholder 를 **LLM 무관·결정적**으로 감지한다. 호출자(파이프라인)는
이를 써서 placeholder spec 을 카운트/저장에서 제외 → 실질 spec 0 이면 기존 no_changes
경로로 빠져 master 오염을 막는다.

오탐(정상 입력 차단)이 더 위험하므로 **강신호만** 매칭한다(보수적):
  - 빈/공백
  - 정확히 "미정" (합성어 "미정산..." 은 통과)
  - "정의 불가 / 명세서 부재 / 내용 없음 / 추후 정의 / TBD" 등 자인 마커
  - 미치환 템플릿 브래킷("[Role A]", "[도메인명 ...]" 등)
"""
from __future__ import annotations

from typing import Any, Dict

# spec 본문이 "못 채웠다"를 자인하는 강신호 (소문자 비교).
_PLACEHOLDER_MARKERS = (
    "정의 불가",
    "정의불가",
    "정의가 불가",
    "명세서 부재",
    "cps 부재",
    "내용 없음",
    "해당 없음",
    "추후 정의",
    "추후 결정",
    "tbd",
)
# 미치환 OUTPUT SCHEMA 템플릿 브래킷 — LLM 이 예시 칸을 그대로 흘린 경우.
_TEMPLATE_BRACKET_PREFIXES = (
    "[role",
    "[story",
    "[epic",
    "[screen",
    "[도메인",
    "[구체",
    "[조건",
    "[행위",
    "[가치",
)

# spec 노드에서 본문으로 볼 필드 우선순위.
_SUMMARY_FIELDS = ("summary", "name", "title", "description")


def is_placeholder_text(value: Any) -> bool:
    """spec 본문(summary/name 등)이 placeholder/garbage 면 True.

    보수적: 강신호만 잡아 정상 입력 오탐을 피한다.
    """
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    if text == "미정":
        return True
    low = text.lower()
    if any(marker in low for marker in _PLACEHOLDER_MARKERS):
        return True
    if any(low.startswith(prefix) for prefix in _TEMPLATE_BRACKET_PREFIXES):
        return True
    return False


def spec_node_text(node: Dict[str, Any]) -> str:
    """spec 노드의 대표 본문 텍스트 (summary > name > title > description)."""
    if not isinstance(node, dict):
        return ""
    props = node.get("properties") or {}
    if not isinstance(props, dict):
        return ""
    for field in _SUMMARY_FIELDS:
        val = props.get(field)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def is_meaningful_spec_node(node: Dict[str, Any]) -> bool:
    """spec 노드가 실질 본문을 가지면 True, placeholder/빈 본문이면 False."""
    return not is_placeholder_text(spec_node_text(node))
