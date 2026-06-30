from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# 심각도 분류
SEVERITY_ERROR = "error"        # 데이터 무결성 위반 — 외부 참조 깨질 위험
SEVERITY_WARNING = "warning"    # 규약 위반 but 동작은 됨
SEVERITY_INFO = "info"          # 자동 교정된 항목 (참고용)


@dataclass
class Violation:
    """검증 위반 한 건."""
    code: str               # 예: 'POLICY_CATEGORY_UNKNOWN', 'AGG_NAME_MISMATCH'
    severity: str           # error | warning | info
    stage: str              # 'spack' | 'ddd' | 'arch' | 'cross'
    message: str
    item_id: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None


@dataclass
class ValidationReport:
    """단일 stage 의 정합성 보고. diagnostic 에 직렬화되어 들어감."""
    stage: str                                  # 'spack' | 'ddd' | 'arch'
    violations: List[Violation] = field(default_factory=list)
    auto_fixed: List[Violation] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == SEVERITY_ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == SEVERITY_WARNING)

    def add(self, v: Violation) -> None:
        self.violations.append(v)

    def add_fixed(self, v: Violation) -> None:
        self.auto_fixed.append(v)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "auto_fixed_count": len(self.auto_fixed),
            "violations": [
                {
                    "code": v.code, "severity": v.severity, "stage": v.stage,
                    "message": v.message, "item_id": v.item_id, "detail": v.detail,
                }
                for v in self.violations
            ],
            "auto_fixed": [
                {
                    "code": v.code, "severity": v.severity, "stage": v.stage,
                    "message": v.message, "item_id": v.item_id, "detail": v.detail,
                }
                for v in self.auto_fixed
            ],
        }
