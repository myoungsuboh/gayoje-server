"""TourAPI(한국관광공사) 축제·행사 어댑터 (INGEST-E2-T6).

TourAPI searchFestival — 행사 목록(영문 키: title/addr1/eventstartdate 등).
1차 출처(한국관광공사 공식 OpenAPI).
"""
from __future__ import annotations

from typing import Any, Optional

from app.api.v1.ingestion.adapters.base import BaseSourceAdapter, http_fetch_records


class TourApiAdapter(BaseSourceAdapter):
    SOURCE_KEY = "tour_api"
    SOURCE_SYSTEM = "korea_tourism_org:tour_api_festival"
    DEFAULT_BASE_URL = "https://apis.data.go.kr/B551011/KorService2/searchFestival2"
    FIELD_CANDIDATES = {
        "title": ("title",),
        "host": ("sponsor1", "organizer"),
        "address": ("addr1", "addr2"),
        "venue": ("eventplace", "addr1"),
        "start": ("eventstartdate",),
        "end": ("eventenddate",),
        "url": ("homepage", "url"),
        "id": ("contentid",),
    }

    async def fetch_raw(
        self,
        service_key: str,
        *,
        base_url: str | None = None,
        num_of_rows: int = 100,
        page_no: int = 1,
        area_code: Optional[str] = None,
        client: Any = None,
    ) -> list[dict]:
        extra = {"MobileOS": "ETC", "MobileApp": "gayoje", "_type": "json"}
        if area_code:
            extra["areaCode"] = area_code
        return await http_fetch_records(
            service_key,
            base_url or self.DEFAULT_BASE_URL,
            num_of_rows=num_of_rows,
            page_no=page_no,
            extra_params=extra,
            client=client,
        )
