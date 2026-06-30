"""INGEST-E2-T1 회귀 — http_fetch_records 재시도/백오프."""
from __future__ import annotations

import json

import httpx
import pytest

from app.api.v1.ingestion.adapters.base import http_fetch_records

pytestmark = pytest.mark.asyncio

_OK_BODY = json.dumps({"response": {"body": {"items": [{"eventNm": "테스트 가요제"}]}}})


class _FakeClient:
    """httpx.AsyncClient 대체 — 실패 횟수/상태코드 시퀀스로 transient 모사."""

    def __init__(self, *, fail_times=0, fail_exc=None, status_sequence=None):
        self.calls = 0
        self.fail_times = fail_times
        self.fail_exc = fail_exc or httpx.ReadTimeout("boom")
        self.status_sequence = status_sequence

    async def get(self, url, params=None):
        self.calls += 1
        req = httpx.Request("GET", url)
        if self.status_sequence is not None:
            idx = min(self.calls - 1, len(self.status_sequence) - 1)
            code = self.status_sequence[idx]
            return httpx.Response(
                code, text=_OK_BODY if code == 200 else "err", request=req
            )
        if self.calls <= self.fail_times:
            raise self.fail_exc
        return httpx.Response(200, text=_OK_BODY, request=req)

    async def aclose(self):
        pass


async def test_retry_succeeds_after_transient_timeouts():
    fake = _FakeClient(fail_times=2)
    recs = await http_fetch_records("k", "http://x", client=fake, backoff_base=0.01)
    assert fake.calls == 3  # 2 실패 + 1 성공
    assert recs == [{"eventNm": "테스트 가요제"}]


async def test_retry_exhausted_raises():
    fake = _FakeClient(fail_times=99)
    with pytest.raises(httpx.TimeoutException):
        await http_fetch_records(
            "k", "http://x", client=fake, max_retries=2, backoff_base=0.01
        )
    assert fake.calls == 3  # 초기 + 2 재시도


async def test_5xx_retried_then_succeeds():
    fake = _FakeClient(status_sequence=[503, 200])
    recs = await http_fetch_records("k", "http://x", client=fake, backoff_base=0.01)
    assert fake.calls == 2
    assert recs == [{"eventNm": "테스트 가요제"}]


async def test_4xx_not_retried():
    fake = _FakeClient(status_sequence=[400, 200])
    with pytest.raises(httpx.HTTPStatusError):
        await http_fetch_records("k", "http://x", client=fake, backoff_base=0.01)
    assert fake.calls == 1  # 400 즉시 실패(재시도 안 함)
