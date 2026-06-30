"""
neo4j_client.run_cypher 의 transient 재시도 테스트.

[2026-05-27] 운영 ServiceUnavailable(연결 defunct)이 일시 blip 에도 그대로
사용자 Network Error 로 노출되던 문제. run_cypher 가 ServiceUnavailable 시
새 세션(=새 연결)으로 재시도하도록 함. ServiceUnavailable 은 서버 도달 실패
=쿼리 미실행이라 재시도가 멱등적으로 안전.
"""
from __future__ import annotations

import asyncio

import pytest
from neo4j.exceptions import ServiceUnavailable

from app.clients import neo4j_client

pytestmark = pytest.mark.asyncio


async def _noop(*_a, **_k):
    return None


class _FakeResult:
    def __aiter__(self):
        async def gen():
            yield {"x": 1}
        return gen()


class _FakeSession:
    def __init__(self, behavior):
        self._behavior = behavior

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def run(self, _cypher, _params):
        return self._behavior()


class _FakeDriver:
    def __init__(self, behavior):
        self._behavior = behavior

    def session(self, database=None):  # noqa: ARG002
        return _FakeSession(self._behavior)


async def test_run_cypher_retries_then_succeeds(monkeypatch):
    """ServiceUnavailable 2회 후 3번째 성공 — 재시도로 회복."""
    monkeypatch.setattr(asyncio, "sleep", _noop)
    calls = {"n": 0}

    def behavior():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ServiceUnavailable("connection defunct")
        return _FakeResult()

    async def fake_get_driver():
        return _FakeDriver(behavior)

    monkeypatch.setattr(neo4j_client, "get_driver", fake_get_driver)

    rows = await neo4j_client.run_cypher("RETURN 1")
    assert rows == [{"x": 1}]
    assert calls["n"] == 3  # 2 retries + success


async def test_run_cypher_raises_after_exhausting_retries(monkeypatch):
    """계속 ServiceUnavailable 면 (Neo4j 다운) 최종적으로 raise — 무한 재시도 안 함."""
    monkeypatch.setattr(asyncio, "sleep", _noop)
    calls = {"n": 0}

    def behavior():
        calls["n"] += 1
        raise ServiceUnavailable("neo4j down")

    async def fake_get_driver():
        return _FakeDriver(behavior)

    monkeypatch.setattr(neo4j_client, "get_driver", fake_get_driver)

    with pytest.raises(ServiceUnavailable):
        await neo4j_client.run_cypher("RETURN 1")
    assert calls["n"] >= 2  # 최소 1회 이상 재시도 후 포기
