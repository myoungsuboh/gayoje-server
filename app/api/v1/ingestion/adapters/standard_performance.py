"""전국공연행사정보표준데이터 어댑터 (INGEST-E2-T3).

공공데이터포털 표준데이터셋 '전국공연행사정보표준데이터' 1차 출처.
"""
from __future__ import annotations

from typing import Any

from app.api.v1.ingestion.adapters.base import BaseSourceAdapter, http_fetch_records


class StandardPerformanceAdapter(BaseSourceAdapter):
    SOURCE_KEY = "standard_performance"
    SOURCE_SYSTEM = "data_go_kr:nationwide_performance_event_std"
    DEFAULT_BASE_URL = (
        "https://api.data.go.kr/openapi/tn_pubr_public_performance_event_api"
    )
    FIELD_CANDIDATES = {
        "title": ("공연행사명", "fstvlNm", "title"),
        "host": ("주최기관명", "주관기관명", "mnnstNm"),
        "address": ("소재지도로명주소", "소재지지번주소", "rdnmadr"),
        "venue": ("공연시설명", "fcltyNm"),
        "start": ("공연행사시작일자", "행사시작일자", "fstvlStartDate"),
        "end": ("공연행사종료일자", "행사종료일자", "fstvlEndDate"),
        "url": ("홈페이지주소", "homepageUrl"),
        "id": ("관리번호", "id"),
    }

    async def fetch_raw(
        self,
        service_key: str,
        *,
        base_url: str | None = None,
        num_of_rows: int = 100,
        page_no: int = 1,
        client: Any = None,
    ) -> list[dict]:
        return await http_fetch_records(
            service_key,
            base_url or self.DEFAULT_BASE_URL,
            num_of_rows=num_of_rows,
            page_no=page_no,
            client=client,
        )
