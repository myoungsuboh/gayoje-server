"""
master_lock — 프로젝트 master 쓰기 직렬화 (2026-06 멀티디바이스 이중작업 차단).

[배경]
웹 배치 + 모바일 단건이 같은 프로젝트에 동시 merge 하면 "master 읽기 → LLM →
쓰기" 사이 lost update. master_write_lock 이 project_key 단위로 직렬화.

검증: 즉시획득 / 직렬화(대기 후 획득) / 타임아웃 / holder 비교 해제 /
fail-open(redis None·오류) / 동시 컨텍스트 상호배제.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core import master_lock
from app.core.master_lock import MasterLockTimeout, master_write_lock

pytestmark = pytest.mark.asyncio


class FakeRedis:
    """SET NX EX / GET / DELETE 만 구현한 인메모리 가짜 Redis."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.raise_on = set()

    async def set(self, key, value, nx=False, ex=None):
        if "set" in self.raise_on:
            raise RuntimeError("redis down")
        if nx and key in self.kv:
            return None  # redis 동작: NX 실패 시 None
        self.kv[key] = value
        return True

    async def get(self, key):
        if "get" in self.raise_on:
            raise RuntimeError("redis down")
        return self.kv.get(key)

    async def delete(self, key):
        self.kv.pop(key, None)


@pytest.fixture(autouse=True)
def _fast_lock(monkeypatch):
    """테스트는 빠른 폴링/타임아웃 — 운영 상수에 매달리지 않게."""
    monkeypatch.setattr(master_lock, "_POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(master_lock, "_WAIT_TIMEOUT_SEC", 0.2)


async def test_acquires_immediately_when_free():
    r = FakeRedis()
    async with master_write_lock(r, "projA", "job1"):
        assert r.kv["harness:lock:master:projA"] == "job1"
    # 종료 후 해제.
    assert "harness:lock:master:projA" not in r.kv


async def test_serializes_two_holders():
    """잡힌 락은 해제될 때까지 대기 → 직렬화 (핵심 동작)."""
    r = FakeRedis()
    order: list[str] = []

    async def work(holder: str, hold_sec: float):
        async with master_write_lock(r, "projA", holder):
            order.append(f"{holder}:in")
            await asyncio.sleep(hold_sec)
            order.append(f"{holder}:out")

    # A 가 먼저 잡고 0.05s 보유 — B 는 대기 후 진입해야.
    await asyncio.gather(work("A", 0.05), work("B", 0.01))
    # 임계구역이 겹치지 않음: in/out 이 쌍으로 닫힌 뒤 다음이 들어감.
    assert order in (["A:in", "A:out", "B:in", "B:out"],
                     ["B:in", "B:out", "A:in", "A:out"])


async def test_timeout_raises():
    r = FakeRedis()
    r.kv["harness:lock:master:projA"] = "someone-else"  # 영영 안 풀리는 락
    with pytest.raises(MasterLockTimeout):
        async with master_write_lock(r, "projA", "job1"):
            pass


async def test_release_only_own_holder():
    """TTL 만료 후 다른 잡이 잡은 락을, 뒤늦게 끝난 잡이 지우면 안 됨."""
    r = FakeRedis()
    await master_lock._release(r, "projA", "job1")  # 락 없음 — no-op
    r.kv["harness:lock:master:projA"] = "job2"      # 남의 락
    await master_lock._release(r, "projA", "job1")
    assert r.kv["harness:lock:master:projA"] == "job2"  # 안 지워짐
    await master_lock._release(r, "projA", "job2")
    assert "harness:lock:master:projA" not in r.kv      # 자기 건 지움


async def test_fail_open_when_redis_none_or_error():
    """redis None / set 오류 → 잠금 없이 통과 (가용성 우선)."""
    async with master_write_lock(None, "projA", "job1"):
        pass  # 통과

    r = FakeRedis()
    r.raise_on.add("set")
    async with master_write_lock(r, "projA", "job1"):
        pass  # set 실패 → 통과 (예외 없음)


async def test_no_lock_when_project_key_empty():
    r = FakeRedis()
    async with master_write_lock(r, "", "job1"):
        pass
    assert r.kv == {}


async def test_different_projects_do_not_block():
    """다른 프로젝트끼리는 병렬 — 락 키 격리."""
    r = FakeRedis()
    entered = []

    async def work(project: str):
        async with master_write_lock(r, project, project):
            entered.append(project)
            await asyncio.sleep(0.05)

    # 동시에 진입 가능해야 (직렬이면 0.1s 이상 걸림 → 타임아웃 0.2 안에 둘 다 즉시 in)
    await asyncio.wait_for(asyncio.gather(work("projA"), work("projB")), timeout=0.15)
    assert sorted(entered) == ["projA", "projB"]


async def test_reentrant_for_same_holder():
    """[감사 E1] 크래시로 락이 남은 상태에서 같은 holder(arq 재시도, 동일 job_id)
    는 재진입 — 자기 락에 5분 막혀 재시도를 소모하던 엣지 회복."""
    r = FakeRedis()
    r.kv["harness:lock:master:projA"] = "job1"  # 크래시 잔존 락 (release 안 됨)
    async with master_write_lock(r, "projA", "job1"):
        pass  # 즉시 재진입 (대기/타임아웃 없음)
    assert "harness:lock:master:projA" not in r.kv  # 종료 시 해제


async def test_wait_timeout_override():
    """wait_timeout 인자 — sync 라우트용 짧은 대기."""
    import time as _t
    r = FakeRedis()
    r.kv["harness:lock:master:projA"] = "someone-else"
    t0 = _t.monotonic()
    with pytest.raises(MasterLockTimeout):
        async with master_write_lock(r, "projA", "job1", wait_timeout=0.05):
            pass
    assert _t.monotonic() - t0 < 1.0  # 기본 0.2 보다도 짧게 끝남
