"""소스 어댑터 레지스트리 (INGEST-E1-T2).

SOURCE_KEY → 어댑터 인스턴스. 신규 소스 = 어댑터 추가 + 여기 등록.
"""
from __future__ import annotations

from app.api.v1.ingestion.adapters.base import BaseSourceAdapter
from app.api.v1.ingestion.adapters.cultural_festival import CulturalFestivalAdapter
from app.api.v1.ingestion.adapters.standard_performance import (
    StandardPerformanceAdapter,
)
from app.api.v1.ingestion.adapters.tour_api import TourApiAdapter

_ADAPTERS: dict[str, BaseSourceAdapter] = {
    a.SOURCE_KEY: a
    for a in (
        StandardPerformanceAdapter(),
        CulturalFestivalAdapter(),
        TourApiAdapter(),
    )
}


def get_adapter(source_key: str) -> BaseSourceAdapter:
    """SOURCE_KEY 로 어댑터 조회. 미등록이면 KeyError."""
    if source_key not in _ADAPTERS:
        raise KeyError(source_key)
    return _ADAPTERS[source_key]


def list_adapter_keys() -> list[str]:
    return sorted(_ADAPTERS)


def all_adapters() -> list[BaseSourceAdapter]:
    return list(_ADAPTERS.values())
