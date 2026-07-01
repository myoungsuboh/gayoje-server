"""Festivals 공개 읽기 API 회귀 — GET /festivals(목록), /festivals/{id}(상세).

httpx AsyncClient(ASGITransport)로 앱과 동일 이벤트루프 실행(async DB 정합).
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest
from httpx import ASGITransport

from app.core.config import settings
from app.main import app

pytestmark = pytest.mark.asyncio


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def seeded(tmp_path, monkeypatch):
    from app.api.v1.festivals.models import FestivalEvent
    from app.infra import base, db

    await db.dispose_engine()
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path.as_posix()}/fest.db"
    )
    await db.create_all(base.Base.metadata)

    def _ev(rid, title, region, venue, host, start):
        return FestivalEvent(
            source_system="seed", source_record_id=rid, source_url="https://x/" + rid,
            payload_hash=rid, raw_payload={"t": title}, title=title,
            region_name=region, venue=venue, host_org=host,
            start_date=start, end_date=start,
        )

    async with db.session_scope() as s:
        s.add_all([
            _ev("1", "제29회 노들가요제", "서울특별시 동작구", "동작문화복지센터", "동작문화원", date(2026, 3, 28)),
            _ev("2", "통영가요제", "경상남도 통영시", "강구안 문화마당", "통영연예예술인협회", date(2026, 9, 5)),
        ])
    yield db
    await db.dispose_engine()


async def test_list_festivals(seeded):
    async with _client() as c:
        r = await c.get("/api/v1/festivals")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    # camelCase 직렬화 + 시작일 최신 우선(통영 2026-09 먼저).
    first = body["items"][0]
    assert first["title"] == "통영가요제"
    assert first["startDate"] == "2026-09-05"
    assert "regionName" in first and "venue" in first


async def test_list_region_filter(seeded):
    async with _client() as c:
        r = await c.get("/api/v1/festivals", params={"region": "서울"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "제29회 노들가요제"


async def test_detail_festival(seeded):
    async with _client() as c:
        r = await c.get("/api/v1/festivals/1")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "제29회 노들가요제"
    assert body["hostOrg"] == "동작문화원"
    assert body["venue"] == "동작문화복지센터"
    assert body["sourceUrl"].startswith("https://x/")


async def test_detail_not_found(seeded):
    async with _client() as c:
        r = await c.get("/api/v1/festivals/9999")
    assert r.status_code == 404
