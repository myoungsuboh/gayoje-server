"""
Design 단계 (Spack / DDD / Architecture) 의 cross-stage 정합성 검증 + 정규화.

기존 단일 파일을 하위 모듈로 분리:
  types.py        — Violation, ValidationReport, severity 상수
  lineage.py      — normalize_lineage, _normalize_story_id
  spack.py        — normalize_spack + 정렬/검증 헬퍼
  ddd.py          — normalize_ddd
  architecture.py — normalize_architecture, summarize_reports
"""
from .types import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    SEVERITY_INFO,
    Violation,
    ValidationReport,
)
from .lineage import normalize_lineage, _normalize_story_id
from .spack import normalize_spack
from .ddd import normalize_ddd
from .architecture import normalize_architecture, summarize_reports

__all__ = [
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "SEVERITY_INFO",
    "Violation",
    "ValidationReport",
    "normalize_lineage",
    "_normalize_story_id",
    "normalize_spack",
    "normalize_ddd",
    "normalize_architecture",
    "summarize_reports",
]
