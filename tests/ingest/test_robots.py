"""INGEST-E3-T2 회귀 — robots.txt 준수 게이트."""
from __future__ import annotations

import pytest

from app.api.v1.ingestion.robots import RobotsGate

pytestmark = pytest.mark.asyncio


class _Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """host prefix → (status, text) 매핑으로 robots.txt 응답 모사. get 호출 수 기록."""

    def __init__(self, robots_map):
        self.robots_map = robots_map
        self.calls = 0

    async def get(self, url):
        self.calls += 1
        for host, (status, text) in self.robots_map.items():
            if url.startswith(host):
                return _Resp(status, text)
        return _Resp(404, "")


async def test_disallow_all_blocks():
    gate = RobotsGate("gayoje-bot")
    client = _FakeClient({"https://x.go.kr": (200, "User-agent: *\nDisallow: /")})
    assert await gate.allowed(client, "https://x.go.kr/bbs/list.do") is False


async def test_allow_when_only_specific_disallowed():
    gate = RobotsGate("gayoje-bot")
    client = _FakeClient({"https://y.go.kr": (200, "User-agent: *\nDisallow: /admin/")})
    assert await gate.allowed(client, "https://y.go.kr/bbs/list.do") is True
    assert await gate.allowed(client, "https://y.go.kr/admin/secret") is False


async def test_no_robots_404_allows():
    gate = RobotsGate("gayoje-bot")
    client = _FakeClient({"https://z.go.kr": (404, "")})
    assert await gate.allowed(client, "https://z.go.kr/bbs/list.do") is True


async def test_fetch_error_denies_conservatively():
    class _ErrClient:
        calls = 0

        async def get(self, url):
            raise RuntimeError("network down")

    gate = RobotsGate("gayoje-bot")
    assert await gate.allowed(_ErrClient(), "https://w.go.kr/bbs/list.do") is False


async def test_403_denies():
    gate = RobotsGate("gayoje-bot")
    client = _FakeClient({"https://f.go.kr": (403, "forbidden")})
    assert await gate.allowed(client, "https://f.go.kr/bbs") is False


async def test_crawl_delay_parsed():
    gate = RobotsGate("gayoje-bot")
    client = _FakeClient({"https://d.go.kr": (200, "User-agent: *\nCrawl-delay: 2")})
    assert await gate.crawl_delay(client, "https://d.go.kr/x") == 2.0


async def test_host_cache_reused():
    gate = RobotsGate("gayoje-bot")
    client = _FakeClient({"https://c.go.kr": (200, "User-agent: *\nDisallow: /admin/")})
    await gate.allowed(client, "https://c.go.kr/a")
    await gate.allowed(client, "https://c.go.kr/b")
    assert client.calls == 1  # 같은 host → robots 1회만 fetch
