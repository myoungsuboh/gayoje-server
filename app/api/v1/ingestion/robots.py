"""robots.txt 준수 게이트 (INGEST-E3-T2).

크롤 전 robots.txt 를 확인해 허용된 경로만 가져온다. '1차 공공 출처(지자체) only'
원칙과 함께 법적 준수의 핵심 게이트. host 단위 캐시 + crawl-delay 존중.

정책:
- robots.txt 200 + 규칙  → 규칙 적용(can_fetch).
- robots.txt 404/빈응답  → 제약 없음(전체 허용).
- robots.txt 401/403     → 접근 거부 → 보수적 불허.
- robots.txt fetch 실패  → 확인 불가 → 보수적 불허(크롤 안 함).
"""
from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser


class RobotsGate:
    def __init__(self, user_agent: str, *, cache_ttl: float = 3600.0):
        self.user_agent = user_agent
        self.cache_ttl = cache_ttl
        # host → (parser|None, fetched_at). parser None = 크롤 불허(확인 불가/거부).
        self._cache: dict[str, tuple[Optional[RobotFileParser], float]] = {}

    def _now(self) -> float:
        return time.time()

    @staticmethod
    def _host(url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    async def _parser(self, client: Any, url: str) -> Optional[RobotFileParser]:
        host = self._host(url)
        cached = self._cache.get(host)
        if cached and self._now() - cached[1] < self.cache_ttl:
            return cached[0]

        parser: Optional[RobotFileParser]
        try:
            r = await client.get(host + "/robots.txt")
            status = r.status_code
            if status == 200 and (r.text or "").strip():
                parser = RobotFileParser()
                parser.parse(r.text.splitlines())
            elif status in (401, 403):
                parser = None  # 접근 거부 → 불허
            else:
                parser = RobotFileParser()
                parser.parse([])  # 404/빈응답 → 제약 없음(허용)
        except Exception:  # noqa: BLE001 — fetch 실패 → 확인 불가 → 불허
            parser = None

        self._cache[host] = (parser, self._now())
        return parser

    async def allowed(self, client: Any, url: str) -> bool:
        """url 을 user_agent 로 크롤 가능한지. 확인 불가/거부면 False(보수적)."""
        parser = await self._parser(client, url)
        if parser is None:
            return False
        return parser.can_fetch(self.user_agent, url)

    async def crawl_delay(self, client: Any, url: str) -> Optional[float]:
        """robots 의 Crawl-delay(초). 미지정/불허면 None."""
        parser = await self._parser(client, url)
        if parser is None:
            return None
        d = parser.crawl_delay(self.user_agent)
        return float(d) if d is not None else None
