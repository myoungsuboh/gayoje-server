"""INGEST PoC 데모 (북극성 — 수집 증명).

공공데이터 표준데이터 샘플 → 가요제 정규화 → DB(DATABASE_URL) 저장 후 출력.

실행:
    python scripts/poc_ingest.py
DATABASE_URL 미설정 시 로컬 sqlite(gayoje_poc.db). PG 로 증명하려면:
    DATABASE_URL=postgresql+asyncpg://user:pw@host:5432/gayoje python scripts/poc_ingest.py
라이브 수집(샘플 대신 실 API)으로 전환하려면 DATA_GO_KR_SERVICE_KEY 설정 후
adapters.standard_performance.fetch_raw() 로 records 를 가져오면 된다.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


async def _run() -> None:
    os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./gayoje_poc.db")

    from sqlalchemy import select

    from app.api.v1.festivals.models import FestivalEvent
    from app.api.v1.ingestion.adapters.standard_performance import _extract_records
    from app.api.v1.ingestion.service import ingest_records
    from app.infra import base, db

    await db.create_all(base.Base.metadata)

    fixture = Path("tests/ingest/fixtures/standard_performance_sample.json")
    records = _extract_records(json.loads(fixture.read_text(encoding="utf-8")))
    print(f"[fetch] 표준데이터 샘플 {len(records)}건")

    async with db.session_scope() as session:
        counts = await ingest_records(session, records)
    print(f"[normalize+store] {counts}")

    async with db.session_scope() as session:
        rows = (await session.scalars(select(FestivalEvent))).all()
    print(f"[readback] 저장된 가요제 {len(rows)}건:")
    for r in rows:
        print(
            f"  - {r.title} | 주최={r.host_org} | {r.start_date}~{r.end_date} "
            f"| 지역={r.region_name} | src={r.source_system}#{r.source_record_id}"
        )

    await db.dispose_engine()


if __name__ == "__main__":
    asyncio.run(_run())
