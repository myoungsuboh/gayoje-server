"""festivals 도메인 스키마 — 공개 브라우즈 응답(camelCase).

목록/상세 읽기 전용. 저장 파이프라인(ingestion)과 분리. CamelModel → startDate/hostOrg 등
camelCase 직렬화. ORM(FestivalEvent)에서 from_attributes 로 직접 검증.
"""
from __future__ import annotations

from datetime import date

from app.common.schemas import CamelModel


class FestivalListItem(CamelModel):
    id: int
    title: str
    region_name: str | None = None
    venue: str | None = None
    host_org: str | None = None
    start_date: date | None = None
    end_date: date | None = None


class FestivalListResponse(CamelModel):
    items: list[FestivalListItem]
    total: int
    limit: int
    offset: int


class FestivalDetail(CamelModel):
    id: int
    title: str
    host_org: str | None = None
    region_name: str | None = None
    venue: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    source_url: str | None = None
    source_system: str
