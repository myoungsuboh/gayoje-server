"""데모용 시드 — 공개 API 2종의 실제 가요제를 기본 DB(gayoje_dev.db)에 적재.

공개 브라우즈 UI 데모의 데이터 소스. ingestion 파이프라인 재사용(is_gayoje 필터 →
정규화 → 멱등 upsert). 500행 페이지 + 재시도로 공개 API 지연에 견딤.
    실행: PYTHONPATH=. python scripts/seed_festivals.py
서버는 동일 DATABASE_URL(기본 gayoje_dev.db)을 읽으므로 config 변경 불요.
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select

import app.api.v1.festivals.models  # noqa: F401 — 테이블 등록
from app.api.v1.festivals.models import FestivalEvent
from app.api.v1.ingestion.adapters.base import http_fetch_records
from app.api.v1.ingestion.registry import get_adapter
from app.api.v1.ingestion.service import ingest_records
from app.core.config import settings
from app.infra import base, db

SOURCES = ("standard_performance", "cultural_festival")  # tour_api 는 가요제 0건


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"DB: {settings.DATABASE_URL}")
    await db.dispose_engine()
    await db.create_all(base.Base.metadata)
    key = settings.data_go_kr_service_keys[0]

    for src in SOURCES:
        adapter = get_adapter(src)
        for pg in range(1, 25):
            try:
                recs = await http_fetch_records(
                    key, adapter.DEFAULT_BASE_URL,
                    num_of_rows=500, page_no=pg,
                    extra_params={"type": "json"},
                    timeout_sec=40, max_retries=2,
                )
            except Exception as e:  # noqa: BLE001 — 느린 페이지 스킵(부분 적재 허용)
                print(f"  {src} p{pg} 실패 {type(e).__name__} — 스킵", flush=True)
                continue
            if not recs:
                break
            async with db.session_scope() as s:
                c = await ingest_records(s, adapter, recs)
            if c["inserted"] or c["updated"]:
                print(f"  {src} p{pg}: +{c['inserted']} inserted", flush=True)

    async with db.session_scope() as s:
        n = int(await s.scalar(select(func.count()).select_from(FestivalEvent)) or 0)
        rows = list((await s.scalars(select(FestivalEvent).order_by(FestivalEvent.id))).all())
    print(f"\n시드 완료: {n} 가요제")
    for r in rows:
        print(f"  [{r.id}] {r.title[:34]:34s} | {(r.region_name or '')[:12]:12s} | "
              f"{r.start_date} | 장소:{r.venue or '-'}")
    await db.dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
