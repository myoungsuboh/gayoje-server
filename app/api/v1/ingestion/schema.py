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


class BoardInfo(CamelModel):
    name: str
    source_system: str
    base_url: str


class CrawlRunRequest(CamelModel):
    board: str          # 카탈로그 name (예: dongjak_culture)
    max_pages: int = 3  # 목록 스윕 페이지 수(과도한 요청 방지 상한)


class CrawlRunResponse(CamelModel):
    board: str
    source_system: str
    crawled: int        # 스캔한 게시글 수
    gayoje: int         # 가요제로 선별된 수
    blocked: bool       # robots 불허로 중단됐는지
    counts: dict        # 저장 결과(inserted/updated/unchanged…)
    posts: list[dict]   # 선별된 가요제(title, detailUrl)
