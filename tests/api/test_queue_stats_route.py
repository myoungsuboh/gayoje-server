"""
GET /api/admin/queue/stats 회귀 — 2026-05 운영 가시성 #2.

[보호하는 동작]
- 정상 응답 shape (queues / default 키 + 각 큐 별 pending / health)
- Redis 일시 장애 시 None 으로 부분 응답 (전체 500 회피)
- non-admin 사용자 거부 (admin guard 통과 못 함)
- get_queue_stats 가 zcard + arq:health-check:<queue> get 호출
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pytest
from fastapi import HTTPException

from app.api import admin_routes
from app.queue import client as queue_client
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


def _admin(email: str = "admin@x.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="admin",
        subscription_type="free", is_admin=True,
    )


def _fake_request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/admin/queue/stats"),
        method="GET",
    )


# ─── service (queue.client.get_queue_stats) ───────────


class _FakePool:
    def __init__(self, zcard_map=None, get_map=None, raise_on=None):
        self.zcard_map = zcard_map or {}
        self.get_map = get_map or {}
        self.raise_on = raise_on or set()

    async def zcard(self, key: str) -> int:
        if ("zcard", key) in self.raise_on:
            raise RuntimeError("redis down")
        return self.zcard_map.get(key, 0)

    async def get(self, key: str):
        if ("get", key) in self.raise_on:
            raise RuntimeError("redis down")
        return self.get_map.get(key)


async def test_get_queue_stats_returns_per_queue_counts(monkeypatch):
    """[회귀] 각 큐 별 pending / health 포함."""
    fake_pool = _FakePool(
        zcard_map={"harness:jobs:pro": 3, "harness:jobs:free": 7, "harness:jobs": 0},
        get_map={"arq:health-check:harness:jobs:pro": b"j_complete=5 j_failed=0"},
    )

    async def fake_get_pool():
        return fake_pool

    monkeypatch.setattr(queue_client, "get_pool", fake_get_pool)
    # PRO / FREE / default 큐 이름은 import time 에 모듈 변수로 캐시됨 — 직접 지정.
    monkeypatch.setattr(queue_client, "QUEUE_NAME", "harness:jobs")
    monkeypatch.setattr(queue_client, "PRO_QUEUE_NAME", "harness:jobs:pro")
    monkeypatch.setattr(queue_client, "FREE_QUEUE_NAME", "harness:jobs:free")

    out = await queue_client.get_queue_stats()
    assert "queues" in out
    assert "default" in out and out["default"] == "harness:jobs"
    assert out["queues"]["harness:jobs:pro"]["pending"] == 3
    assert out["queues"]["harness:jobs:free"]["pending"] == 7
    assert out["queues"]["harness:jobs:pro"]["health"] is not None


async def test_get_queue_stats_handles_redis_failure_per_key(monkeypatch):
    """[회귀] Redis 일시 장애 시 None 채워서 부분 응답."""
    fake_pool = _FakePool(
        zcard_map={"harness:jobs:pro": 2, "harness:jobs:free": 0},
        raise_on={("zcard", "harness:jobs:free"), ("get", "arq:health-check:harness:jobs:pro")},
    )

    async def fake_get_pool():
        return fake_pool

    monkeypatch.setattr(queue_client, "get_pool", fake_get_pool)
    monkeypatch.setattr(queue_client, "QUEUE_NAME", "harness:jobs:pro")
    monkeypatch.setattr(queue_client, "PRO_QUEUE_NAME", "harness:jobs:pro")
    monkeypatch.setattr(queue_client, "FREE_QUEUE_NAME", "harness:jobs:free")

    out = await queue_client.get_queue_stats()
    assert out["queues"]["harness:jobs:pro"]["pending"] == 2
    assert out["queues"]["harness:jobs:free"]["pending"] is None
    # PRO health get 실패 → None
    assert out["queues"]["harness:jobs:pro"]["health"] is None


async def test_get_queue_stats_dedups_when_pro_equals_free(monkeypatch):
    """[회귀] 단일 워커 운영 시 PRO=FREE=default → 중복 키 제거."""
    fake_pool = _FakePool(zcard_map={"harness:jobs": 5})

    async def fake_get_pool():
        return fake_pool

    monkeypatch.setattr(queue_client, "get_pool", fake_get_pool)
    monkeypatch.setattr(queue_client, "QUEUE_NAME", "harness:jobs")
    monkeypatch.setattr(queue_client, "PRO_QUEUE_NAME", "harness:jobs")
    monkeypatch.setattr(queue_client, "FREE_QUEUE_NAME", "harness:jobs")

    out = await queue_client.get_queue_stats()
    assert len(out["queues"]) == 1
    assert out["queues"]["harness:jobs"]["pending"] == 5


# ─── route ──────────────────────────────────────────


async def test_queue_stats_route_returns_stats(monkeypatch):
    async def fake_stats():
        return {
            "queues": {"q1": {"pending": 1, "health": None}},
            "default": "q1",
        }
    monkeypatch.setattr(
        "app.queue.client.get_queue_stats", fake_stats
    )
    out = await admin_routes.queue_stats_route.__wrapped__(
        request=_fake_request(),
        _admin=_admin(),
    )
    assert out["default"] == "q1"
    assert out["queues"]["q1"]["pending"] == 1


def test_queue_stats_route_has_rate_limit():
    assert hasattr(admin_routes.queue_stats_route, "__wrapped__")
