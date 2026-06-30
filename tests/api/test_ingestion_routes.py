"""INGEST-E13 회귀 — 수집 라우터(/api/v1/ingestion/sources, /run).

httpx AsyncClient(ASGITransport)로 앱과 동일 이벤트루프에서 실행 — async DB 엔진
정합(루프 불일치 회피).
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from app.api.v1.ingestion import registry
from app.api.v1.ingestion.adapters.base import extract_records
from app.core.config import settings
from app.main import app

pytestmark = pytest.mark.asyncio

_FIX = Path(__file__).parent.parent / "ingest" / "fixtures"


def _sample() -> list[dict]:
    raw = (_FIX / "standard_performance_sample.json").read_text(encoding="utf-8")
    return extract_records(json.loads(raw))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_list_sources():
    async with _client() as c:
        r = await c.get("/api/v1/ingestion/sources")
    assert r.status_code == 200
    keys = {s["sourceKey"] for s in r.json()}
    assert {"standard_performance", "cultural_festival", "tour_api"} <= keys


async def test_run_unknown_source_404(monkeypatch):
    monkeypatch.setattr(settings, "DATA_GO_KR_SERVICE_KEY", "k")
    async with _client() as c:
        r = await c.post("/api/v1/ingestion/run", json={"source": "nope"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


async def test_run_no_service_key_503(monkeypatch):
    monkeypatch.setattr(settings, "DATA_GO_KR_SERVICE_KEY", None)
    async with _client() as c:
        r = await c.post(
            "/api/v1/ingestion/run", json={"source": "standard_performance"}
        )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "service_unavailable"


async def test_run_ingests_and_stores(tmp_path, monkeypatch):
    from app.infra import base, db
    import app.api.v1.festivals.models  # noqa: F401 — 테이블 등록

    await db.dispose_engine()
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path.as_posix()}/run.db"
    )
    await db.create_all(base.Base.metadata)
    monkeypatch.setattr(settings, "DATA_GO_KR_SERVICE_KEY", "testkey")

    sample = _sample()

    async def mock_fetch(service_key, **kw):
        return sample

    monkeypatch.setattr(
        registry.get_adapter("standard_performance"), "fetch_raw", mock_fetch
    )

    async with _client() as c:
        r = await c.post(
            "/api/v1/ingestion/run", json={"source": "standard_performance"}
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fetched"] == 3
    assert body["counts"]["inserted"] == 2
    assert body["counts"]["skipped_non_gayoje"] == 1
    await db.dispose_engine()
