"""
Stage markers (Redis) — postMeeting 등이 진행 단계를 redis 키에 기록.
FE polling 이 단계 표시에 사용.

Key: harness:job:{job_id}:stage
TTL: 3600s (_STAGE_TTL_SEC)
Values: cps_running | prd_running | done
"""
from __future__ import annotations

import pytest

from app.queue.jobs import _STAGE_KEY_PREFIX, _STAGE_TTL_SEC, _set_job_stage
from tests.conftest import FakeRedis, make_arq_ctx

pytestmark = pytest.mark.asyncio


async def test_set_job_stage_writes_key_with_ttl():
    redis = FakeRedis()
    ctx = make_arq_ctx(job_id="job-x", redis=redis)
    await _set_job_stage(ctx, "cps_running")
    key = f"{_STAGE_KEY_PREFIX}job-x:stage"
    assert redis.store[key] == "cps_running"
    assert redis.ttls[key] == _STAGE_TTL_SEC


async def test_set_job_stage_overwrites_previous_value():
    redis = FakeRedis()
    ctx = make_arq_ctx(job_id="job-x", redis=redis)
    await _set_job_stage(ctx, "cps_running")
    await _set_job_stage(ctx, "prd_running")
    await _set_job_stage(ctx, "done")
    assert redis.store[f"{_STAGE_KEY_PREFIX}job-x:stage"] == "done"


async def test_set_job_stage_skips_without_redis():
    """redis 없으면 silently skip — 단위 테스트 호환용."""
    ctx = make_arq_ctx(job_id="job-x", redis=None)
    await _set_job_stage(ctx, "done")  # should not raise


async def test_set_job_stage_skips_without_job_id():
    redis = FakeRedis()
    ctx = make_arq_ctx(job_id=None, redis=redis)
    await _set_job_stage(ctx, "done")
    assert redis.store == {}


async def test_set_job_stage_swallows_redis_error():
    """redis.set 실패가 job 결과를 망치면 안 됨."""
    class _FailingRedis:
        async def set(self, *args, **kwargs):
            raise RuntimeError("redis down")

    ctx = make_arq_ctx(job_id="job-x", redis=_FailingRedis())
    await _set_job_stage(ctx, "cps_running")  # should not raise
