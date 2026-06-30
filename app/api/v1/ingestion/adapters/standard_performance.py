"""전국공연행사정보표준데이터 어댑터 (INGEST-E2-T3).

공공데이터포털 표준데이터셋 '전국공연행사정보표준데이터' 1차 출처.
"""
from __future__ import annotations

from typing import Any

from app.api.v1.ingestion.adapters.base import BaseSourceAdapter, http_fetch_records


class StandardPerformanceAdapter(BaseSourceAdapter):
    SOURCE_KEY = "standard_performance"
    SOURCE_SYSTEM = "data_go_kr:nationwide_performance_event_std"
    # 실 API 검증(2026-07): 엔드포인트·필드명 라이브 응답으로 확정.
    DEFAULT_BASE_URL = (
        "https://api.data.go.kr/openapi/tn_pubr_public_pblprfr_event_info_api"
    )
    # 실 응답 키(eventNm/opar/eventStartDate 등) — 관리번호(ID) 필드 없음 → 합성.
    FIELD_CANDIDATES = {
        "title": ("eventNm",),
        "host": ("mnnstNm", "auspcInsttNm", "insttNm"),  # 주최·주관·제공기관
        "address": ("rdnmadr", "lnmadr"),                # 도로명·지번
        "venue": ("opar",),                              # 장소/공연장
        "start": ("eventStartDate",),
        "end": ("eventEndDate",),
        "url": ("homepageUrl",),
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
            extra_params={"type": "json"},  # 표준데이터: type=json
            client=client,
        )
