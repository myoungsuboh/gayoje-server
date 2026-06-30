"""전국공연행사정보표준데이터 어댑터 (INGEST PoC slice).

공공데이터포털 표준데이터셋 '전국공연행사정보표준데이터' 1차 출처.
- fetch_raw: 공공 OpenAPI httpx 호출 → raw record dict 목록. (PoC 는 라이브 키가 없어
  녹화 샘플 픽스처로 대체 — 라이브 전환 시 이 함수가 실제 호출.)
- normalize: 표준 필드 → NormalizedEvent 매핑 + 가요제 키워드 필터(비대상 → None).
출처(provenance)는 전 단계 보존 — source_system/source_record_id/payload_hash/raw_payload.

⚠️ 표준데이터는 1차 공공 출처. 경쟁사 가공 DB 스크래핑 아님(법적 원칙 준수).
정밀 분류(LLM)는 비정형 소스(eGov 게시판)용 — 구조화된 표준데이터는 규칙 매핑으로 충분.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

SOURCE_SYSTEM = "data_go_kr:nationwide_performance_event_std"

# 가요제/노래대회 선별 키워드(제목 기준).
_GAYOJE_KEYWORDS = (
    "가요제", "노래대회", "가요대회", "트로트", "보컬", "싱어",
    "동요제", "합창", "음악경연", "노래자랑",
)

# 표준데이터 필드명 후보(응답 키 변형 방어 — 라이브 스키마 확정 전 다중 후보).
_F_TITLE = ("공연행사명", "fstvlNm", "공연행사이름", "title")
_F_HOST = ("주최기관명", "mnnstNm", "주관기관명", "host")
_F_ADDR = ("소재지도로명주소", "소재지지번주소", "rdnmadr", "address")
_F_VENUE = ("공연시설명", "fcltyNm", "venue")
_F_START = ("공연행사시작일자", "행사시작일자", "fstvlStartDate", "startDate")
_F_END = ("공연행사종료일자", "행사종료일자", "fstvlEndDate", "endDate")
_F_URL = ("홈페이지주소", "homepageUrl", "url")
_F_ID = ("관리번호", "id")


def _first(raw: dict, keys: tuple[str, ...]) -> Optional[str]:
    for k in keys:
        v = raw.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return None


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    s = s.strip().replace(".", "-").replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _payload_hash(raw: dict) -> str:
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_id(raw: dict, title: str) -> str:
    """natural key — 명시 ID 없으면 (제목|시작일|시설) 안정 해시."""
    explicit = _first(raw, _F_ID)
    if explicit:
        return explicit
    basis = f"{title}|{_first(raw, _F_START) or ''}|{_first(raw, _F_VENUE) or ''}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:24]


def is_gayoje(title: Optional[str]) -> bool:
    return bool(title) and any(k in title for k in _GAYOJE_KEYWORDS)


@dataclass
class NormalizedEvent:
    source_system: str
    source_record_id: str
    source_url: Optional[str]
    payload_hash: str
    raw_payload: dict
    title: str
    host_org: Optional[str]
    region_name: Optional[str]
    venue: Optional[str]
    start_date: Optional[date]
    end_date: Optional[date]


def normalize(raw: dict) -> Optional[NormalizedEvent]:
    """표준 레코드 → NormalizedEvent. 가요제 아니면 None(필터)."""
    title = _first(raw, _F_TITLE)
    if not title or not is_gayoje(title):
        return None
    return NormalizedEvent(
        source_system=SOURCE_SYSTEM,
        source_record_id=_record_id(raw, title),
        source_url=_first(raw, _F_URL),
        payload_hash=_payload_hash(raw),
        raw_payload=raw,
        title=title,
        host_org=_first(raw, _F_HOST),
        region_name=_first(raw, _F_ADDR),
        venue=_first(raw, _F_VENUE),
        start_date=_parse_date(_first(raw, _F_START)),
        end_date=_parse_date(_first(raw, _F_END)),
    )


def _extract_records(data: Any) -> list[dict]:
    """공공 API 응답에서 record 목록 추출 (평면 list / response.body.items.item 등)."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        body = (data.get("response") or {}).get("body") or data.get("body") or {}
        items = body.get("items") if isinstance(body, dict) else None
        if items is None:
            items = data.get("items")
        if isinstance(items, dict):
            items = items.get("item")
        if isinstance(items, list):
            return [r for r in items if isinstance(r, dict)]
        if isinstance(items, dict):
            return [items]
    return []


async def fetch_raw(
    service_key: str,
    *,
    base_url: str,
    num_of_rows: int = 100,
    page_no: int = 1,
    timeout_sec: int = 20,
    client: Any = None,
) -> list[dict]:
    """공공데이터포털 표준데이터 OpenAPI 호출 → raw record dict 목록.

    PoC 는 라이브 서비스키가 없어 호출하지 않고 샘플 픽스처로 대체한다.
    라이브 전환: DATA_GO_KR_SERVICE_KEY + base_url 설정 후 이 함수로 실제 수집.
    """
    import httpx

    params = {
        "serviceKey": service_key,
        "numOfRows": num_of_rows,
        "pageNo": page_no,
        "type": "json",
    }
    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout_sec)
    try:
        resp = await client.get(base_url, params=params)
        resp.raise_for_status()
        return _extract_records(resp.json())
    finally:
        if owns:
            await client.aclose()
