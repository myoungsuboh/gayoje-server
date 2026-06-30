"""INGEST 공통 어댑터 프레임워크 (INGEST-E1-T2).

BaseSourceAdapter — 소스별 어댑터 베이스. 서브클래스는 SOURCE_KEY/SOURCE_SYSTEM +
FIELD_CANDIDATES(정규화 필드→raw 키 후보) + fetch_raw 만 구현하면, normalize(가요제
키워드 필터·필드 매핑·날짜 파싱·출처 보존)는 공통으로 제공된다.
→ 신규 소스 추가 = 서브클래스 + 레지스트리 등록.

모든 소스는 1차 공공 출처(공공데이터포털 표준데이터·TourAPI 등). 경쟁사 가공 DB 아님.
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

# 가요제/노래대회 선별 키워드(제목 기준) — 모든 어댑터 공통.
GAYOJE_KEYWORDS = (
    "가요제", "노래대회", "가요대회", "트로트", "보컬", "싱어",
    "동요제", "합창", "음악경연", "노래자랑",
)


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


def is_gayoje(title: Optional[str]) -> bool:
    return bool(title) and any(k in title for k in GAYOJE_KEYWORDS)


def first_value(raw: dict, keys: tuple[str, ...]) -> Optional[str]:
    for k in keys:
        v = raw.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return None


def parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    s = s.strip().replace(".", "-").replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def payload_hash(raw: dict) -> str:
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extract_records(data: Any) -> list[dict]:
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


async def http_fetch_records(
    service_key: str,
    base_url: str,
    *,
    num_of_rows: int = 100,
    page_no: int = 1,
    timeout_sec: int = 20,
    extra_params: Optional[dict] = None,
    client: Any = None,
) -> list[dict]:
    """공공 OpenAPI httpx 호출 → record 목록. (라이브 키 없으면 호출하지 않음.)"""
    import httpx

    params = {
        "serviceKey": service_key,
        "numOfRows": num_of_rows,
        "pageNo": page_no,
        "type": "json",
    }
    if extra_params:
        params.update(extra_params)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout_sec)
    try:
        resp = await client.get(base_url, params=params)
        resp.raise_for_status()
        return extract_records(resp.json())
    finally:
        if owns:
            await client.aclose()


class BaseSourceAdapter(ABC):
    """소스 어댑터 베이스 — 공통 normalize 제공, fetch_raw 만 서브클래스 구현."""

    SOURCE_KEY: str = ""            # 레지스트리/엔드포인트 식별자 (예: "standard_performance")
    SOURCE_SYSTEM: str = ""         # provenance source_system 값
    DEFAULT_BASE_URL: str = ""      # 라이브 API base URL
    # 정규화 필드 → raw 키 후보(우선순위 순).
    FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {}

    def field(self, raw: dict, name: str) -> Optional[str]:
        return first_value(raw, self.FIELD_CANDIDATES.get(name, ()))

    def record_id(self, raw: dict, title: str) -> str:
        explicit = self.field(raw, "id")
        if explicit:
            return explicit
        basis = f"{title}|{self.field(raw, 'start') or ''}|{self.field(raw, 'venue') or ''}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:24]

    def normalize(self, raw: dict) -> Optional[NormalizedEvent]:
        """raw → NormalizedEvent. 가요제 아니면 None(필터)."""
        title = self.field(raw, "title")
        if not title or not is_gayoje(title):
            return None
        return NormalizedEvent(
            source_system=self.SOURCE_SYSTEM,
            source_record_id=self.record_id(raw, title),
            source_url=self.field(raw, "url"),
            payload_hash=payload_hash(raw),
            raw_payload=raw,
            title=title,
            host_org=self.field(raw, "host"),
            region_name=self.field(raw, "address"),
            venue=self.field(raw, "venue"),
            start_date=parse_date(self.field(raw, "start")),
            end_date=parse_date(self.field(raw, "end")),
        )

    @abstractmethod
    async def fetch_raw(self, service_key: str, **kwargs: Any) -> list[dict]:
        """소스 API 호출 → raw record 목록. (PoC 는 샘플 픽스처로 대체.)"""
        raise NotImplementedError
