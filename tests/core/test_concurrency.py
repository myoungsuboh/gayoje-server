"""
concurrency — 계정당 동시 무거운 job 제한 (2026-06).

ZSET 기반 acquire/release + stale 회수 + 가용성 우선(오류/누락 시 통과) 검증.
"""
from __future__ import annotations

import time

import pytest

from app.core import concurrency
from app.core.concurrency import try_acquire_slot, release_slot, HEAVY_JOBS

pytestmark = pytest.mark.asyncio


class FakeRedis:
    """ZSET 부분만 구현한 인메모리 가짜 Redis."""

    def __init__(self):
        self.z: dict[str, dict[str, float]] = {}
        self.raise_on = set()  # 오류 주입할 메서드명

    async def zremrangebyscore(self, key, mn, mx):
        if "zremrangebyscore" in self.raise_on:
            raise RuntimeError("redis down")
        d = self.z.get(key, {})
        rm = [m for m, s in d.items() if s <= mx]
        for m in rm:
            del d[m]
        return len(rm)

    async def zcard(self, key):
        return len(self.z.get(key, {}))

    async def zscore(self, key, member):
        return self.z.get(key, {}).get(member)

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)

    async def zrem(self, key, member):
        self.z.get(key, {}).pop(member, None)

    async def expire(self, key, sec):
        pass


def test_heavy_jobs_set():
    # 무거운 사용자 job 포함, 내부/비-LLM job 제외.
    assert "post_meeting_pipeline_job" in HEAVY_JOBS
    assert "design_pipeline_job" in HEAVY_JOBS
    assert "cps_pipeline_job" in HEAVY_JOBS and "prd_pipeline_job" in HEAVY_JOBS
    assert "prefetch_extract_job" not in HEAVY_JOBS
    assert "analyze_lineage_job" not in HEAVY_JOBS
    assert "delete_meeting_job" not in HEAVY_JOBS


async def test_acquire_under_limit_then_block_at_limit():
    r = FakeRedis()
    assert await try_acquire_slot(r, "u@b.com", "t1", limit=2) is True
    assert await try_acquire_slot(r, "u@b.com", "t2", limit=2) is True
    # 한도(2) 도달 — 세 번째 거부
    assert await try_acquire_slot(r, "u@b.com", "t3", limit=2) is False
    assert await r.zcard("harness:inflight:u@b.com") == 2


async def test_release_frees_slot():
    r = FakeRedis()
    await try_acquire_slot(r, "u@b.com", "t1", limit=1)
    assert await try_acquire_slot(r, "u@b.com", "t2", limit=1) is False  # 꽉 참
    await release_slot(r, "u@b.com", "t1")
    # 해제 후 다시 가능
    assert await try_acquire_slot(r, "u@b.com", "t2", limit=1) is True


async def test_duplicate_task_id_not_double_counted():
    r = FakeRedis()
    await try_acquire_slot(r, "u@b.com", "t1", limit=1)
    # 같은 task_id 재제출 — 한도 꽉 차도 통과(새 슬롯 아님)
    assert await try_acquire_slot(r, "u@b.com", "t1", limit=1) is True
    assert await r.zcard("harness:inflight:u@b.com") == 1


async def test_stale_entries_reclaimed():
    r = FakeRedis()
    # job_timeout 보다 오래된 stale 슬롯 2개를 직접 주입
    old = time.time() - (concurrency._STALE_AGE_SEC + 100)
    r.z["harness:inflight:u@b.com"] = {"dead1": old, "dead2": old}
    # acquire 시 stale 정리 → count 0 → 추가 가능
    assert await try_acquire_slot(r, "u@b.com", "t1", limit=2) is True
    assert await r.zscore("harness:inflight:u@b.com", "dead1") is None


async def test_passthrough_when_no_email_or_redis():
    r = FakeRedis()
    assert await try_acquire_slot(r, "", "t1", limit=1) is True
    assert await try_acquire_slot(None, "u@b.com", "t1", limit=1) is True


async def test_redis_error_allows_through():
    """Redis 오류 시 통과 — 게이트가 정상 사용자를 막는 사고 방지(가용성 우선)."""
    r = FakeRedis()
    r.raise_on.add("zremrangebyscore")
    assert await try_acquire_slot(r, "u@b.com", "t1", limit=1) is True


async def test_per_account_isolation():
    r = FakeRedis()
    await try_acquire_slot(r, "a@b.com", "t1", limit=1)
    # 다른 계정은 독립 — 영향 없음
    assert await try_acquire_slot(r, "c@d.com", "t1", limit=1) is True


# ─── 프로젝트 단위 inflight (2026-06 멀티디바이스 이중작업) ──────────


async def test_project_acquire_limit_one():
    """프로젝트당 1개 — 두 번째 task 는 거부."""
    r = FakeRedis()
    assert await concurrency.try_acquire_project(r, "projX", "t1") is True
    assert await concurrency.try_acquire_project(r, "projX", "t2") is False
    # 다른 프로젝트는 무관.
    assert await concurrency.try_acquire_project(r, "projY", "t3") is True


async def test_project_same_task_id_passes():
    """같은 task_id 재제출(arq dedup 경로)은 통과."""
    r = FakeRedis()
    assert await concurrency.try_acquire_project(r, "projX", "t1") is True
    assert await concurrency.try_acquire_project(r, "projX", "t1") is True


async def test_project_release_frees_slot():
    r = FakeRedis()
    await concurrency.try_acquire_project(r, "projX", "t1")
    await concurrency.release_project(r, "projX", "t1")
    assert await concurrency.try_acquire_project(r, "projX", "t2") is True


async def test_project_stale_reclaimed():
    """크래시로 release 못 한 마커는 STALE_AGE 후 자동 회수."""
    r = FakeRedis()
    key = "harness:inflight:project:projX"
    r.z[key] = {"dead-task": time.time() - concurrency._STALE_AGE_SEC - 10}
    assert await concurrency.try_acquire_project(r, "projX", "t-new") is True


async def test_project_fail_open():
    """redis 없음/오류 → 통과 (가용성 우선)."""
    assert await concurrency.try_acquire_project(None, "projX", "t1") is True
    assert await concurrency.try_acquire_project(FakeRedis(), "", "t1") is True
    r = FakeRedis()
    r.raise_on.add("zremrangebyscore")
    assert await concurrency.try_acquire_project(r, "projX", "t1") is True


def test_master_write_jobs_members():
    """프로젝트 가드 대상 — 프로젝트 공유 산출물을 쓰는 잡 6종.

    delete_meeting_job 은 HEAVY_JOBS(계정 슬롯) 미대상이지만 master rebuild 라
    프로젝트 게이트 대상. design/autofill 은 master CPS/PRD 가 아니라 설계
    그래프(Wipe-and-Redraw)를 써서 추가 — 동시 실행 시 stage 혼합/패치 유실.
    cleanup_master_prd_job 은 post_meeting 이 auto-enqueue 하므로 게이트에
    걸리면 배치 UX 가 깨져 의도적으로 제외 (워커 락만).
    """
    assert concurrency.MASTER_WRITE_JOBS == {
        "post_meeting_pipeline_job", "cps_pipeline_job", "prd_pipeline_job",
        "delete_meeting_job", "design_pipeline_job", "autofill_api_specs_job",
    }
    assert "cleanup_master_prd_job" not in concurrency.MASTER_WRITE_JOBS


async def test_is_project_busy_reflects_inflight():
    r = FakeRedis()
    assert await concurrency.is_project_busy(r, "projX") is False
    await concurrency.try_acquire_project(r, "projX", "t1")
    assert await concurrency.is_project_busy(r, "projX") is True
    await concurrency.release_project(r, "projX", "t1")
    assert await concurrency.is_project_busy(r, "projX") is False


async def test_is_project_busy_fail_open_false():
    """redis 없음/오류 → False (표시용 — 미표시가 보수적 차단보다 안전)."""
    assert await concurrency.is_project_busy(None, "projX") is False
    r = FakeRedis()
    r.raise_on.add("zremrangebyscore")
    assert await concurrency.is_project_busy(r, "projX") is False
