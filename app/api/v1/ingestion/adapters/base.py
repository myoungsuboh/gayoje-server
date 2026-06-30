"""INGEST 공통 어댑터 프레임워크 (INGEST-E1-T2).

BaseSourceAdapter — 소스별 어댑터 베이스. 서브클래스는 SOURCE_KEY/SOURCE_SYSTEM +
FIELD_CANDIDATES(정규화 필드→raw 키 후보) + fetch_raw 만 구현하면, normalize(가요제
키워드 필터·필드 매핑·날짜 파싱·출처 보존)는 공통으로 제공된다.
→ 신규 소스 추가 = 서브클래스 + 레지스트리 등록.

모든 소스는 1차 공공 출처(공공데이터포털 표준데이터·TourAPI 등). 경쟁사 가공 DB 아님.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

logger = logging.getLogger("gayoje.ingest")

# ===== 가요제/노래대회 선별 규칙 (제목 기준) =====
# [배경] 실데이터(전국공연행사정보표준데이터) 점검 결과, 단순 키워드(합창/싱어/보컬/
# 트로트 단독)는 합창단 정기연주회·싱어롱콘서트 등 '엉뚱한 행사'를 대량 오선별했다.
# → '가창 관련어' + '경연/대회 신호' 조합 규칙으로 정밀화(precision 우선).
#
# 규칙: STRONG(단독 확정) OR (가창어 SINGING AND 경연어 CONTEST).
# 공백 제거 후 매칭("노래 대회" → "노래대회"). 추후 실데이터로 계속 보정.

# 단독으로 가요제/노래대회 확정.
_STRONG_GAYOJE = (
    "가요제", "가요대회", "노래대회", "노래자랑", "동요제",
    "트로트가요제", "트롯가요제",
)
# 가창 관련어(이것만으로는 부족 — 경연어와 함께여야).
_SINGING = ("가요", "노래", "트로트", "트롯", "보컬", "가창", "k팝", "케이팝")
# 경연/대회 신호.
_CONTEST = ("대회", "경연", "콘테스트", "오디션", "선발", "챔피언", "자랑")


def is_gayoje(title: Optional[str]) -> bool:
    """제목이 가요제/노래대회 성격인지 판정 (precision 우선 규칙)."""
    if not title:
        return False
    t = title.replace(" ", "")
    low = t.lower()
    if any(k in t for k in _STRONG_GAYOJE):
        return True
    has_singing = any(s in low for s in _SINGING)
    has_contest = any(c in t for c in _CONTEST)
    return has_singing and has_contest


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
    timeout_sec: int = 30,
    extra_params: Optional[dict] = None,
    client: Any = None,
    max_retries: int = 3,
    backoff_base: float = 1.0,
) -> list[dict]:
    """공공 OpenAPI httpx 호출 → record 목록 (재시도/백오프 포함, INGEST-E2-T1 quick).

    transient 오류(타임아웃·연결오류·5xx·429)는 지수 백오프로 재시도, 소진 시 raise.
    4xx(429 제외)는 재시도 무의미하므로 즉시 raise. 정상 200 의 비-JSON(에러 XML 등)은 [].
    data.go.kr 응답 값의 unescaped 제어문자 방어로 strict=False 파싱.
    """
    import httpx

    # 응답 포맷 파라미터는 소스마다 다름(표준데이터=type, TourAPI=_type) → 어댑터가
    # extra_params 로 지정. 여기서 강제하지 않는다.
    params = {
        "serviceKey": service_key,
        "numOfRows": num_of_rows,
        "pageNo": page_no,
    }
    if extra_params:
        params.update(extra_params)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout_sec)
    try:
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.get(base_url, params=params)
                # 5xx / 429 → transient 으로 보고 재시도.
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"transient HTTP {resp.status_code}",
                        request=resp.request, response=resp,
                    )
                resp.raise_for_status()  # 그 외 4xx 는 즉시 raise(재시도 무의미)
                try:
                    data = json.loads(resp.text, strict=False)
                except json.JSONDecodeError:
                    return []
                return extract_records(data)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
            except httpx.HTTPStatusError as e:
                code = e.response.status_code if e.response is not None else 0
                if not (code >= 500 or code == 429):
                    raise  # 비-transient 4xx → 즉시 실패
                last_exc = e
            if attempt < max_retries:
                delay = backoff_base * (2 ** attempt) + random.uniform(0, 0.4)
                logger.warning(
                    "ingest fetch 재시도 %d/%d (%s, page=%s) — %.1fs 후",
                    attempt + 1, max_retries,
                    type(last_exc).__name__, page_no, delay,
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc
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
