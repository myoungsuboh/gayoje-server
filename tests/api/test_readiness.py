"""BE-E01-T06 회귀 — 헬스/레디니스 프로브(/healthz, /readyz, /version)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz_liveness():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_version_unversioned():
    r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "gayoje-server"
    assert body["version"]
    assert "serverTime" in body  # camelCase


@pytest.fixture
def deps_ok(monkeypatch):
    async def ok_db():
        return True

    async def ok_cypher(cypher, params=None):
        return [{"ok": 1}]

    class _Pool:
        async def ping(self):
            return True

    async def ok_pool():
        return _Pool()

    monkeypatch.setattr("app.main.check_db", ok_db)
    monkeypatch.setattr("app.main.neo4j_client.run_cypher", ok_cypher)
    monkeypatch.setattr("app.main.queue_client.get_pool", ok_pool)


def test_readyz_200_when_deps_ok(deps_ok):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_readyz_503_when_dep_down(monkeypatch):
    async def bad_db():
        raise RuntimeError("PG down")

    async def bad_cypher(cypher, params=None):
        raise RuntimeError("neo4j down")

    async def bad_pool():
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.main.check_db", bad_db)
    monkeypatch.setattr("app.main.neo4j_client.run_cypher", bad_cypher)
    monkeypatch.setattr("app.main.queue_client.get_pool", bad_pool)
    r = client.get("/readyz")
    assert r.status_code == 503
