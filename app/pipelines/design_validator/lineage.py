from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Set
from .types import Violation, ValidationReport, SEVERITY_WARNING, SEVERITY_INFO


_LINEAGE_CONFIDENCE_VALID = {"direct", "inferred", "none"}
# Story ID 정규화 패턴: PRD 의 다양한 표기를 Story-XX.Y zero-pad 형태로
_STORY_ID_PATTERN = re.compile(r"^Story-(\d+)\.(\d+)$")
# [2026-06-12] 구분자에 '_' 허용 — Neo4j Story 노드 id('story_01_1') 흡수.
# get_spack_graph 가 DERIVED_FROM 엣지에서 복원한 related_stories 의 story_id 가
# 이 형식인데, 이전 패턴은 변환 실패 → drop → 그래프에 연결이 있어도 eval 이
# '미연결 + UNNORMALIZABLE warning(tier4 감점)' 으로 오판했다 (연결 채우기의
# Entity 엣지 생성으로 표면화). 기존 매칭 표기는 결과 동일 — 흡수 범위만 확대.
# [2026-06-13] 경계 가드 추가 — '틀린 연결은 빈 연결보다 나쁘다' 원칙에 맞춰
#   (?<![A-Za-z]): 'prehistory_5_9'·'backstory_2_4' 등 'story' 로 끝나는 단어의
#       오매칭 차단 (이전엔 Story-05.9 로 날조).
#   (?![.\-_]?\d): 'Story-1-2-3'·'Story-1.2.3' 같은 3+컴포넌트를 잘라 'Story-01.2' 로
#       만들던 오매칭 차단 — 정규화 실패(drop)가 잘못된 링크보다 안전.
_STORY_ID_LOOSE_PATTERN = re.compile(
    r"(?<![A-Za-z])Story[-_ ]?(\d+)[.\-_](\d+)(?![.\-_]?\d)", re.IGNORECASE
)
# evidence_quote 최대 길이 — prompt 강제 50자, 여유 두고 80 까지 허용
_LINEAGE_QUOTE_MAX_LEN = 80


def _normalize_story_id(raw: Any) -> Optional[str]:
    """
    임의 표기의 story id 를 'Story-XX.Y' (Epic 번호 zero-pad) 로 정규화.
    예: 'Story 1.1' → 'Story-01.1', '1.1' → 'Story-01.1', '[Story 12.3]' → 'Story-12.3'.
    매칭 안 되면 None.
    """
    if raw is None:
        return None
    s = str(raw).strip().strip("[]")
    # 이미 정규 형태면 그대로 반환 (zero-pad 검증)
    m = _STORY_ID_PATTERN.match(s)
    if m:
        return f"Story-{int(m.group(1)):02d}.{m.group(2)}"
    # 느슨 매칭
    m = _STORY_ID_LOOSE_PATTERN.search(s)
    if m:
        return f"Story-{int(m.group(1)):02d}.{m.group(2)}"
    return None


def normalize_lineage(
    raw: Any,
    *,
    node_id: str,
    stage: str,
    report: "ValidationReport",
    valid_story_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    LLM 이 만든 lineage 객체를 정규화 + 검증.

    [정규화 동작]
    - lineage 자체가 dict 아니면 confidence=none + related_stories=[] 빈 객체로
    - confidence 가 enum 외 값이면 'none' 으로 강제
    - related_stories 의 각 항목이 dict 가 아니면 drop
    - story_id 를 _normalize_story_id 로 정규화 (실패 시 drop)
    - quote 길이 80 초과면 truncate + warning
    - valid_story_ids 가 주어지면 그 set 안에 있는 것만 유지 (cross-stage 무결성)
    - confidence='none' 이면 related_stories 강제 []
    - related_stories 가 비었으면 confidence 'none' 으로 강등

    [report 에 기록되는 위반 종류]
    - LINEAGE_INVALID_SHAPE (warning): lineage 가 dict 아님
    - LINEAGE_CONFIDENCE_INVALID (warning): enum 외 값
    - LINEAGE_STORY_ID_UNNORMALIZABLE (warning): story_id 정규화 실패 → drop
    - LINEAGE_STORY_ID_UNKNOWN (warning): PRD 에 없는 story_id → drop
    - LINEAGE_QUOTE_TOO_LONG (info): truncate 됨
    - LINEAGE_MISSING (warning): 노드에 lineage 자체가 없거나 빈 객체

    Returns: 정규화된 lineage dict (항상 형태 일관).
    """
    # 1) 누락 / 잘못된 타입 흡수
    if not isinstance(raw, dict):
        if raw is not None:
            report.add(Violation(
                code="LINEAGE_INVALID_SHAPE", severity=SEVERITY_WARNING, stage=stage,
                message=f"{node_id}: lineage 가 dict 가 아님 — 빈 lineage 로 대체",
                item_id=node_id,
            ))
        else:
            report.add(Violation(
                code="LINEAGE_MISSING", severity=SEVERITY_WARNING, stage=stage,
                message=f"{node_id}: lineage 누락 — 기본값 (confidence=none, []) 적용",
                item_id=node_id,
            ))
        return {"confidence": "none", "related_stories": []}

    # 2) confidence 정규화
    confidence = str(raw.get("confidence") or "").lower().strip()
    if confidence not in _LINEAGE_CONFIDENCE_VALID:
        if confidence:
            report.add(Violation(
                code="LINEAGE_CONFIDENCE_INVALID", severity=SEVERITY_WARNING, stage=stage,
                message=(
                    f"{node_id}: confidence='{confidence}' invalid "
                    f"(허용: {sorted(_LINEAGE_CONFIDENCE_VALID)}) — 'none' 으로 강등"
                ),
                item_id=node_id,
            ))
        confidence = "none"

    # 3) related_stories 정규화
    out_stories: List[Dict[str, Any]] = []
    for s in (raw.get("related_stories") or []):
        if not isinstance(s, dict):
            continue
        norm_id = _normalize_story_id(s.get("story_id"))
        if not norm_id:
            report.add(Violation(
                code="LINEAGE_STORY_ID_UNNORMALIZABLE", severity=SEVERITY_WARNING, stage=stage,
                message=(
                    f"{node_id}: lineage story_id='{s.get('story_id')}' 정규화 실패 — drop"
                ),
                item_id=node_id, detail={"raw": s.get("story_id")},
            ))
            continue
        # 4) valid_story_ids 가 주어지면 PRD 에 실존 story 만 유지
        if valid_story_ids is not None and norm_id not in valid_story_ids:
            report.add(Violation(
                code="LINEAGE_STORY_ID_UNKNOWN", severity=SEVERITY_WARNING, stage=stage,
                message=(
                    f"{node_id}: lineage story_id='{norm_id}' 가 PRD 에 없음 — drop"
                ),
                item_id=node_id, detail={"story_id": norm_id},
            ))
            continue
        # 5) quote 길이 검증
        quote = str(s.get("quote") or "").strip()
        if len(quote) > _LINEAGE_QUOTE_MAX_LEN:
            report.add_fixed(Violation(
                code="LINEAGE_QUOTE_TOO_LONG", severity=SEVERITY_INFO, stage=stage,
                message=(
                    f"{node_id}: lineage quote {len(quote)}자 → {_LINEAGE_QUOTE_MAX_LEN}자 truncate"
                ),
                item_id=node_id,
            ))
            quote = quote[:_LINEAGE_QUOTE_MAX_LEN].rstrip() + "…"
        out_stories.append({"story_id": norm_id, "quote": quote})

    # 6) 의미 일관성 — none 이면 stories 비움 / stories 비면 none 강등
    if confidence == "none":
        out_stories = []
    elif not out_stories:
        # direct/inferred 인데 stories 비었음 → none 강등 (정직성)
        confidence = "none"

    return {"confidence": confidence, "related_stories": out_stories}
