"""ingestion 도메인 스키마."""
from __future__ import annotations

from app.common.schemas import CamelModel


class SourceInfo(CamelModel):
    source_key: str
    source_system: str


class IngestRunRequest(CamelModel):
    source: str  # SOURCE_KEY (예: standard_performance)
    num_of_rows: int = 100
    page_no: int = 1


class IngestRunResponse(CamelModel):
    source: str
    fetched: int
    counts: dict
