from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ─── Domain types ────────────────────────────────────────────────────


@dataclass(frozen=True)
class DesignInput:
    """createDesign 입력."""

    project_name: str


@dataclass
class DesignResult:
    """
    Design 파이프라인 응답.

    [2026-05] `health` top-level 필드 추가 — FE 가 cross-stage 정합성 위반을
    배지/토스트로 표시. 이전에 diagnostic.design_health nested 라서 FE 가 놓치기
    쉽얈음 (실서비스급 UX 미달 항목).
    """
    project_name: str
    master_prd_id: Optional[str]
    spack: Dict[str, Any] = field(default_factory=dict)
    ddd: Dict[str, Any] = field(default_factory=dict)
    architecture: Dict[str, Any] = field(default_factory=dict)
    diagnostic: Dict[str, Any] = field(default_factory=dict)
    # design_validator 결과 요약 — fail-open 정책상 에러 있어도 응답은 반환되나
    # 사용자에게 명시적으로 노출. FE 는 `health.has_errors == true` 면 경고 배지.
    health: Dict[str, Any] = field(default_factory=dict)
