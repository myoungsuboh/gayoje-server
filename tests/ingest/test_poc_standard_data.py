"""북극성 PoC — 공공데이터 표준데이터 1건 → 정규화 → DB 저장 (수집 증명).

녹화 샘플 픽스처(전국공연행사정보표준데이터) → 가요제 필터·정규화 → sqlite(PG stand-in)
저장. 증명 항목: 가요제 선별, 출처(provenance) 보존, 날짜 파싱, 멱등 재실행, 변경 감지.
DATABASE_URL 만 PG 로 바꾸면 동일 코드로 Postgres 저장.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.api.v1.festivals.models import FestivalEvent
from app.api.v1.ingestion.adapters.base import extract_records
from app.api.v1.ingestion.adapters.standard_performance import (
    StandardPerformanceAdapter,
)
from app.api.v1.ingestion.service import ingest_records
from app.core.config import settings

pytestmark = pytest.mark.asyncio

_ADAPTER = StandardPerformanceAdapter()
SOURCE_SYSTEM = _ADAPTER.SOURCE_SYSTEM
_FIXTURE = Path(__file__).parent / "fixtures" / "standard_performance_sample.json"


def _load_sample() -> list[dict]:
    """녹화 응답에서 record 목록 추출(매 호출 fresh copy)."""
    return extract_records(json.loads(_FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture
async def db_ready(tmp_path, monkeypatch):
    """격리된 파일 sqlite + 스키마 생성(다중 커넥션 공유 위해 파일 사용)."""
    from app.infra import base, db
    import app.api.v1.festivals.models  # noqa: F401 — 테이블 등록

    await db.dispose_engine()
    url = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/poc.db"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    await db.create_all(base.Base.metadata)
    yield db
    await db.dispose_engine()


async def test_poc_fetch_normalize_store(db_ready):
    records = _load_sample()
    assert len(records) == 3  # 표준 응답에서 3건 추출

    async with db_ready.session_scope() as s:
        counts = await ingest_records(s, _ADAPTER,records)

    # 2건 가요제(강변가요제·트로트 노래대회) 저장, 1건(교향악단) 필터
    assert counts["inserted"] == 2
    assert counts["skipped_non_gayoje"] == 1
    assert counts["updated"] == 0

    async with db_ready.session_scope() as s:
        rows = (await s.scalars(select(FestivalEvent))).all()

    assert len(rows) == 2
    titles = [r.title for r in rows]
    assert any("가요제" in t for t in titles)
    assert any("노래대회" in t for t in titles)

    for r in rows:
        # 출처(provenance) 보존
        assert r.source_system == SOURCE_SYSTEM
        assert r.source_record_id
        assert r.payload_hash and len(r.payload_hash) == 64
        assert isinstance(r.raw_payload, dict) and r.raw_payload  # 원본 보존
        assert r.region_name and r.host_org  # 필드 매핑

    # 날짜 파싱 — YYYYMMDD 형식(트로트 노래대회)도 정상 변환
    trot = next(r for r in rows if "노래대회" in r.title)
    assert trot.start_date is not None and trot.start_date.year == 2026
    assert trot.end_date is not None and trot.end_date.day == 21


async def test_poc_idempotent_rerun(db_ready):
    async with db_ready.session_scope() as s:
        await ingest_records(s, _ADAPTER,_load_sample())
    async with db_ready.session_scope() as s:
        counts2 = await ingest_records(s, _ADAPTER,_load_sample())
    # 동일 재수집 → 모두 unchanged (멱등)
    assert counts2["unchanged"] == 2
    assert counts2["inserted"] == 0
    # 행 수 그대로
    async with db_ready.session_scope() as s:
        total = len((await s.scalars(select(FestivalEvent))).all())
    assert total == 2


async def test_poc_change_detection_updates(db_ready):
    async with db_ready.session_scope() as s:
        await ingest_records(s, _ADAPTER, _load_sample())

    # 실 표준데이터는 관리번호가 없어 ID 는 (title|eventStartDate|opar) 합성 →
    # ID 기준이 아닌 필드(주최 mnnstNm) 를 바꿔 payload_hash 만 달라지게 → update.
    changed = _load_sample()
    for rec in changed:
        if rec.get("eventNm") == "제30회 강변가요제":
            rec["mnnstNm"] = "춘천시(주최 변경)"
            break

    async with db_ready.session_scope() as s:
        counts = await ingest_records(s, _ADAPTER, changed)
    assert counts["updated"] == 1
    assert counts["unchanged"] == 1

    async with db_ready.session_scope() as s:
        row = await s.scalar(
            select(FestivalEvent).where(FestivalEvent.title == "제30회 강변가요제")
        )
    assert row is not None and row.host_org == "춘천시(주최 변경)"
