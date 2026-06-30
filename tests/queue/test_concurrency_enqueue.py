"""
_enqueue 동시성 게이트 — heavy job 초과 시 429, 비-heavy 는 우회 (2026-06).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.queue import client
from app.core import concurrency

pytestmark = pytest.mark.asyncio


class _FakePool:
    def __init__(self):
        self.enqueued = []

    async def enqueue_job(self, function, _job_id=None, _queue_name=None, **kwargs):
        self.enqueued.append((function, _job_id, _queue_name))
        return type("J", (), {"job_id": _job_id})()


@pytest.fixture
def fake_pool(monkeypatch):
    pool = _FakePool()

    async def _get_pool():
        return pool
    monkeypatch.setattr(client, "get_pool", _get_pool)
    # 등급별 큐 조회(get_usage)도 우회 — Neo4j 없이.
    async def _q(_email):
        return "harness:queue"
    monkeypatch.setattr(client, "_select_queue_for_user", _q)
    return pool


async def test_heavy_job_over_limit_raises_429(monkeypatch, fake_pool):
    async def deny(redis, email, task_id, *, limit=2):
        return False
    monkeypatch.setattr(concurrency, "try_acquire_slot", deny)

    with pytest.raises(HTTPException) as ei:
        await client._enqueue("design_pipeline_job", "task-1", user_email="u@b.com")
    assert ei.value.status_code == 429
    assert ei.value.detail["code"] == "CONCURRENCY_LIMIT"
    # 거부됐으니 enqueue 안 됨
    assert fake_pool.enqueued == []


async def test_heavy_job_under_limit_enqueues(monkeypatch, fake_pool):
    async def allow(redis, email, task_id, *, limit=2):
        return True
    monkeypatch.setattr(concurrency, "try_acquire_slot", allow)

    out = await client._enqueue("design_pipeline_job", "task-2", user_email="u@b.com")
    assert out == "task-2"
    assert fake_pool.enqueued and fake_pool.enqueued[0][0] == "design_pipeline_job"


async def test_non_heavy_job_skips_concurrency(monkeypatch, fake_pool):
    calls = {"n": 0}

    async def spy(*a, **k):
        calls["n"] += 1
        return True
    monkeypatch.setattr(concurrency, "try_acquire_slot", spy)

    # 비-heavy(lineage) → 게이트 우회
    await client._enqueue("analyze_lineage_job", "task-3", user_email="u@b.com")
    assert calls["n"] == 0
    assert fake_pool.enqueued[0][0] == "analyze_lineage_job"


async def test_heavy_job_without_email_skips_concurrency(monkeypatch, fake_pool):
    calls = {"n": 0}

    async def spy(*a, **k):
        calls["n"] += 1
        return True
    monkeypatch.setattr(concurrency, "try_acquire_slot", spy)

    # user_email 없음 → 추적 불가, 게이트 우회 (system job 등)
    await client._enqueue("design_pipeline_job", "task-4")
    assert calls["n"] == 0


# ─── 프로젝트 단위 409 게이트 (2026-06 멀티디바이스 이중작업) ─────────


async def test_master_write_job_project_busy_raises_409(monkeypatch, fake_pool):
    """같은 프로젝트 inflight → 409 PROJECT_BUSY + 계정 슬롯 반납."""
    async def allow_slot(redis, email, task_id, *, limit=2):
        return True
    monkeypatch.setattr(concurrency, "try_acquire_slot", allow_slot)

    async def deny_project(redis, project_key, task_id):
        return False
    monkeypatch.setattr(concurrency, "try_acquire_project", deny_project)

    released = []
    async def spy_release(redis, email, task_id):
        released.append((email, task_id))
    monkeypatch.setattr(concurrency, "release_slot", spy_release)

    with pytest.raises(HTTPException) as ei:
        await client._enqueue(
            "post_meeting_pipeline_job", "task-pm",
            user_email="u@b.com", project_name="projX",
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "PROJECT_BUSY"
    # 거부 시 위에서 잡은 계정 슬롯 반납 — 누수 시 25분 차단 방지.
    assert released == [("u@b.com", "task-pm")]
    assert fake_pool.enqueued == []


async def test_master_write_job_project_free_enqueues(monkeypatch, fake_pool):
    async def allow(*a, **k):
        return True
    monkeypatch.setattr(concurrency, "try_acquire_slot", allow)
    monkeypatch.setattr(concurrency, "try_acquire_project", allow)

    out = await client._enqueue(
        "post_meeting_pipeline_job", "task-ok",
        user_email="u@b.com", project_name="projX",
    )
    assert out == "task-ok"
    assert fake_pool.enqueued[0][0] == "post_meeting_pipeline_job"


async def test_non_master_write_heavy_job_skips_project_gate(monkeypatch, fake_pool):
    """lint 등 프로젝트 공유 산출물 미접촉 heavy 잡은 게이트 우회 (과차단 방지)."""
    async def allow(redis, email, task_id, *, limit=2):
        return True
    monkeypatch.setattr(concurrency, "try_acquire_slot", allow)

    calls = {"n": 0}
    async def spy(*a, **k):
        calls["n"] += 1
        return True
    monkeypatch.setattr(concurrency, "try_acquire_project", spy)

    await client._enqueue(
        "run_lint_job", "task-l", user_email="u@b.com", project_name="projX",
    )
    assert calls["n"] == 0
    assert fake_pool.enqueued[0][0] == "run_lint_job"


async def test_design_job_blocked_when_project_busy(monkeypatch, fake_pool):
    """[2026-06 후속] design 도 프로젝트 게이트 대상 — 다른 기기에서 merge/design
    이 도는 중 'DESIGN 만들기' 를 누르면 409 PROJECT_BUSY."""
    async def allow_slot(redis, email, task_id, *, limit=2):
        return True
    monkeypatch.setattr(concurrency, "try_acquire_slot", allow_slot)

    async def deny_project(redis, project_key, task_id):
        return False
    monkeypatch.setattr(concurrency, "try_acquire_project", deny_project)

    released = []
    async def spy_release(redis, email, task_id):
        released.append((email, task_id))
    monkeypatch.setattr(concurrency, "release_slot", spy_release)

    with pytest.raises(HTTPException) as ei:
        await client._enqueue(
            "design_pipeline_job", "task-d",
            user_email="u@b.com", project_name="projX",
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "PROJECT_BUSY"
    assert released == [("u@b.com", "task-d")]  # 계정 슬롯 반납
    assert fake_pool.enqueued == []


async def test_project_gate_uses_team_scoped_key(monkeypatch, fake_pool):
    """team_id 가 있으면 scoped key 로 게이트 — 팀 멤버 간 충돌도 감지."""
    async def allow_slot(redis, email, task_id, *, limit=2):
        return True
    monkeypatch.setattr(concurrency, "try_acquire_slot", allow_slot)

    seen_keys = []
    async def spy_project(redis, project_key, task_id):
        seen_keys.append(project_key)
        return True
    monkeypatch.setattr(concurrency, "try_acquire_project", spy_project)

    await client._enqueue(
        "cps_pipeline_job", "task-t",
        user_email="u@b.com", project_name="projX", team_id="team-9",
    )
    assert seen_keys and "team-9" in seen_keys[0] and "projX" in seen_keys[0]
