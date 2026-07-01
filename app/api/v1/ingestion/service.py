"""INGEST 저장 서비스 — 어댑터로 정규화된 이벤트 멱등 upsert.

raw record 목록 → adapter.normalize(가요제 필터) → (source_system, source_record_id)
기준 upsert. payload_hash 로 변경 판별(미변경은 no-op). 출처(provenance) 전 보존.
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.festivals.models import FestivalEvent
from app.api.v1.ingestion.adapters.base import (
    BaseSourceAdapter,
    NormalizedEvent,
    parse_date,
    payload_hash,
)


async def upsert_event(session: AsyncSession, ev: NormalizedEvent) -> str:
    """단건 upsert — 'inserted' | 'updated' | 'unchanged'."""
    existing = await session.scalar(
        select(FestivalEvent).where(
            FestivalEvent.source_system == ev.source_system,
            FestivalEvent.source_record_id == ev.source_record_id,
        )
    )
    if existing is None:
        session.add(
            FestivalEvent(
                source_system=ev.source_system,
                source_record_id=ev.source_record_id,
                source_url=ev.source_url,
                payload_hash=ev.payload_hash,
                raw_payload=ev.raw_payload,
                title=ev.title,
                host_org=ev.host_org,
                region_name=ev.region_name,
                venue=ev.venue,
                start_date=ev.start_date,
                end_date=ev.end_date,
            )
        )
        return "inserted"

    if existing.payload_hash == ev.payload_hash:
        return "unchanged"

    existing.source_url = ev.source_url
    existing.payload_hash = ev.payload_hash
    existing.raw_payload = ev.raw_payload
    existing.title = ev.title
    existing.host_org = ev.host_org
    existing.region_name = ev.region_name
    existing.venue = ev.venue
    existing.start_date = ev.start_date
    existing.end_date = ev.end_date
    return "updated"


def _empty_counts() -> dict:
    return {"inserted": 0, "updated": 0, "unchanged": 0, "skipped_non_gayoje": 0}


async def ingest_records(
    session: AsyncSession, adapter: BaseSourceAdapter, raw_records: list[dict]
) -> dict:
    """어댑터로 raw record 를 정규화·필터·upsert. 결과 카운트 반환."""
    counts = _empty_counts()
    for raw in raw_records:
        ev = adapter.normalize(raw)
        if ev is None:
            counts["skipped_non_gayoje"] += 1
            continue
        counts[await upsert_event(session, ev)] += 1
    return counts


# 경로형 상세 id (/view/{id}, /read/{id}) — 군산 등 모듈형.
_BOARD_PATH_ID = re.compile(r"/(?:view|read|article)/(\d+)")
# 쿼리형 상세 id. 'idx' 앞 경계로 s_idx(페이지네이션) 오매칭 배제.
_BOARD_ID_PAT = re.compile(
    r"(?:nttId|nttNo|pblprfrNo|articleNo|bltnNo|seq|action-value|(?<![a-z_])idx)=(\d+)", re.I
)


def _board_record_id(detail_url: str) -> str:
    """게시판 상세 URL 에서 안정적 record id 추출(경로형 /view/{id} 우선 → 쿼리형), 없으면 URL 해시."""
    m = _BOARD_PATH_ID.search(detail_url)
    if m:
        return m.group(1)
    m = _BOARD_ID_PAT.search(detail_url)
    if m:
        return m.group(1)
    return hashlib.sha1(detail_url.encode("utf-8")).hexdigest()[:24]


async def ingest_board_posts(
    session: AsyncSession, source_system: str, posts: list[dict]
) -> dict:
    """크롤로 얻은 가요제 게시글(제목+상세URL)을 멱등 upsert.

    posts 는 crawl_board 결과의 이미 가요제-필터된 목록(dict: title, detail_url).
    날짜/장소/주최는 상세 본문 파싱(후속) 전까지 NULL. 제목·링크·출처만 저장(재호스팅 안 함).
    """
    counts = _empty_counts()
    for p in posts:
        # 상세 보강 필드(있으면). 날짜는 원시 문자열 → date 변환. payload_hash 에 포함되어
        # 상세가 나중에 채워지면 재수집 시 update 로 반영됨.
        raw = {
            "title": p["title"],
            "detail_url": p["detail_url"],
            "source_system": source_system,
            "start_date": p.get("start_date"),
            "end_date": p.get("end_date"),
            "venue": p.get("venue"),
            "host_org": p.get("host_org"),
        }
        ev = NormalizedEvent(
            source_system=source_system,
            source_record_id=_board_record_id(p["detail_url"]),
            source_url=p["detail_url"],
            payload_hash=payload_hash(raw),
            raw_payload=raw,
            title=p["title"],
            host_org=p.get("host_org"),
            region_name=None,
            venue=p.get("venue"),
            start_date=parse_date(p.get("start_date")),
            end_date=parse_date(p.get("end_date")),
        )
        counts[await upsert_event(session, ev)] += 1
    return counts
