"""
2026-05 보안 점검 #3 — Quota race 회귀 가드.

`_TRY_INCREMENT_MEETING_COUNT_CYPHER` / `_ADD_TOKENS_CYPHER` /
`_ADD_CHARS_CYPHER` 가 노드 exclusive lock 을 조기 획득하도록 구성됐는지 검증.

[Fix #3 의 핵심 가설]
Neo4j 의 노드 write lock 은 `SET u.<x> = ...` 시점에 획득. WITH 로 변수 캡쳐
후 SET 하면 두 transaction 이 동시에 READ → 동시에 SET → 한 증가 누락 = quota
우회. 시작 직후 `SET u.usage_updated_at = datetime()` 으로 lock 을 먼저 잡으면
두 번째 transaction 은 첫 transaction commit 까지 block → 갱신된 값을 읽음.

[회귀 가드 contract]
1. MATCH 다음 첫 쓰기 동작은 `SET u.usage_updated_at = datetime()` 이어야 함.
2. 그 SET 은 `WITH u, COALESCE(u.usage_meeting_count, 0)` 같은 read 보다 먼저 와야 함.
3. 세 cypher 모두 동일 패턴 (consistent quota-related 라서).
"""
from __future__ import annotations

import re

import pytest

from app.service import usage_repository as ur


CYPHERS = {
    "TRY_INCREMENT": ur._TRY_INCREMENT_MEETING_COUNT_CYPHER,
    "ADD_TOKENS": ur._ADD_TOKENS_CYPHER,
    "ADD_CHARS": ur._ADD_CHARS_CYPHER,
}


@pytest.mark.parametrize("name,cypher", list(CYPHERS.items()))
def test_lock_acquisition_set_present(name, cypher):
    """[회귀] 세 cypher 모두 SET u.usage_updated_at = datetime() 포함."""
    assert "SET u.usage_updated_at = datetime()" in cypher, (
        f"{name}: lock 획득용 SET 누락"
    )


@pytest.mark.parametrize("name,cypher", list(CYPHERS.items()))
def test_lock_acquired_before_count_read(name, cypher):
    """
    [회귀] 첫 SET 이 counter 를 읽는 WITH 보다 먼저 와야 lock 획득 효과 발생.

    위치 검사 — `SET u.usage_updated_at` 이 `COALESCE(u.usage_meeting_count` 또는
    `COALESCE(u.usage_total_tokens` / `chars` 보다 먼저.
    """
    lock_set_pos = cypher.find("SET u.usage_updated_at = datetime()")
    assert lock_set_pos >= 0, f"{name}: lock SET 없음"

    # counter read 위치 — 첫 등장.
    count_reads = [
        cypher.find("COALESCE(u.usage_meeting_count"),
        cypher.find("COALESCE(u.usage_total_tokens"),
        cypher.find("COALESCE(u.usage_total_chars"),
    ]
    first_count_read = min([p for p in count_reads if p >= 0], default=-1)

    if first_count_read < 0:
        # ADD_TOKENS / ADD_CHARS 같이 직접 SET 만 하는 경우는 OK.
        return
    assert lock_set_pos < first_count_read, (
        f"{name}: lock SET 이 counter read ({first_count_read}) 보다 늦음 "
        f"({lock_set_pos}) → race 가드 무효"
    )


def test_try_increment_does_not_capture_then_set():
    """
    [회귀] 가장 중요한 cypher — meeting count 증가가 captured `current` 변수
    위에서 SET 되는 패턴 유지 (그것 자체는 OK), 단 그 전에 lock 이 잡혀있어야.

    검증: `(current >= limit) AS exceeded` 가 등장하면 그보다 앞에 lock SET 있어야.
    """
    c = ur._TRY_INCREMENT_MEETING_COUNT_CYPHER
    lock_pos = c.find("SET u.usage_updated_at = datetime()")
    check_pos = c.find("(current >= limit) AS exceeded")
    assert lock_pos >= 0
    assert check_pos >= 0
    assert lock_pos < check_pos, (
        "lock SET 이 (current >= limit) 체크보다 늦음 → 두 transaction "
        "동시 진입 시 quota 우회 가능"
    )


def test_try_increment_set_increments_correctly():
    """[회귀] exceeded=false 경로에서 SET current+1 패턴 유지."""
    c = ur._TRY_INCREMENT_MEETING_COUNT_CYPHER
    # 정확히 current+1 으로 증가하는지 — 다른 값 (e.g. +0, +2) 으로 바뀌면 실패.
    assert "SET u.usage_meeting_count = current + 1" in c


# ─── service 단 (mock) — 호출 자체는 잘 되는지 ──────────────────────


class _FakeRun:
    def __init__(self, response=None):
        self.calls = []
        self._response = response or []

    async def __call__(self, cypher, params=None, database=None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        return self._response


@pytest.mark.asyncio
async def test_try_increment_passes_lock_cypher(monkeypatch):
    """
    [회귀] try_increment_meeting_count 가 lock 패턴 포함된 cypher 로 호출되는지.
    """
    fake = _FakeRun(
        [{"result": {
            "exceeded": False, "current": 1, "limit": 5,
            "subscription_type": "free", "reset_at": "2026-06-17T00:00:00Z",
        }}]
    )
    monkeypatch.setattr(
        "app.service.usage_repository.neo4j_client.run_cypher", fake
    )
    out = await ur.try_increment_meeting_count("u@x.com", limit=5)
    assert out is not None
    assert out.current == 1
    # cypher 자체에 lock SET 포함 검증
    assert "SET u.usage_updated_at = datetime()" in fake.calls[0]["cypher"]
