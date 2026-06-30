"""전국문화축제표준데이터 어댑터 (INGEST-E2-T4).

축제 데이터에서 가요제/노래대회 성격 행사를 선별. 1차 출처(공공데이터포털 표준데이터).
"""
from __future__ import annotations

from typing import Any

from app.api.v1.ingestion.adapters.base import BaseSourceAdapter, http_fetch_records


class CulturalFestivalAdapter(BaseSourceAdapter):
    SOURCE_KEY = "cultural_festival"
    SOURCE_SYSTEM = "data_go_kr:nationwide_cultural_festival_std"
    # 실 API 검증(2026-07): 엔드포인트·필드명 라이브 응답으로 확정.
    DEFAULT_BASE_URL = "https://api.data.go.kr/openapi/tn_pubr_public_cltur_fstvl_api"
    # 실 응답 키(fstvlNm/opar/fstvlStartDate 등). 관리번호 없음 → 합성 ID.
    FIELD_CANDIDATES = {
        "title": ("fstvlNm",),
        "host": ("mnnstNm", "auspcInsttNm", "insttNm"),  # 주최·주관·제공기관
        "address": ("rdnmadr", "lnmadr"),
        "venue": ("opar",),                              # 개최장소
        "start": ("fstvlStartDate",),
        "end": ("fstvlEndDate",),
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
            client=client,
        )
