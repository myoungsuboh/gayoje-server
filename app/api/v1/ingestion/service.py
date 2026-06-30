"""INGEST 저장 서비스 — 어댑터로 정규화된 이벤트 멱등 upsert.

raw record 목록 → adapter.normalize(가요제 필터) → (source_system, source_record_id)
기준 upsert. payload_hash 로 변경 판별(미변경은 no-op). 출처(provenance) 전 보존.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.festivals.models import FestivalEvent
from app.api.v1.ingestion.adapters.base import BaseSourceAdapter, NormalizedEvent


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
