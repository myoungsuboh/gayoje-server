"""
queue.client.get_job_status — Sprint 8 P0 변경 회귀 테스트.

[검증]
- info.kwargs 에서 project_name 회수
- not_found 면 project_name 도 None
- info() 실패 (메타 expire) → project_name None + 다른 필드는 정상 채워짐
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.queue import client


pytestmark = pytest.mark.asyncio


class _FakeJob:
    """arq Job 의 최소 인터페이스 — status/info/result."""

    def __init__(self, status_value, info_obj=None, result_obj=None, info_raises=False):
        self._status_value = status_value
        self._info_obj = info_obj
        self._result_obj = result_obj
        self._info_raises = info_raises

    async def status(self):
        return self._status_value

    async def info(self):
        if self._info_raises:
            raise RuntimeError("redis meta expired")
        return self._info_obj

    async def result(self, timeout=0.5):
        if self._result_obj is None:
            raise RuntimeError("no result")
        return self._result_obj


def _patch_job(monkeypatch, fake):
    async def _get_pool():
        return SimpleNamespace()
    monkeypatch.setattr(client, "get_pool", _get_pool)
    monkeypatch.setattr(client, "Job", lambda *a, **kw: fake)


from arq.jobs import JobStatus


# ─── not_found ──


async def test_not_found_returns_project_none(monkeypatch):
    fake = _FakeJob(status_value=JobStatus.not_found)
    _patch_job(monkeypatch, fake)
    out = await client.get_job_status("x")
    assert out == {"task_id": "x", "project_name": None, "status": "not_found"}


# ─── complete + project_name 회수 ──


async def test_complete_extracts_project_name_from_kwargs(monkeypatch):
    info_obj = SimpleNamespace(
        kwargs={"project_name": "alice_proj", "version": "v1.1"},
        enqueue_time=datetime(2024, 6, 1, 12, 0, 0),
        finish_time=datetime(2024, 6, 1, 12, 5, 0),
    )
    fake = _FakeJob(
        status_value=JobStatus.complete,
        info_obj=info_obj,
        result_obj={"data": "ok"},
    )
    _patch_job(monkeypatch, fake)
    out = await client.get_job_status("x")
    assert out["project_name"] == "alice_proj"
    assert out["status"] == "complete"
    assert out["result"] == {"data": "ok"}
    assert out["enqueue_time"] is not None
    assert out["finish_time"] is not None


# ─── info() 실패 (메타 expire) ──


async def test_info_failure_yields_project_none_but_status_intact(monkeypatch):
    """job 은 살아있지만 info() 가 실패 — project_name 만 None, status 는 유지."""
    fake = _FakeJob(status_value=JobStatus.in_progress, info_raises=True)
    _patch_job(monkeypatch, fake)
    out = await client.get_job_status("x")
    assert out["project_name"] is None
    assert out["status"] == "in_progress"


# ─── kwargs 에 project_name 없음 (옛 enqueue 잔재) ──


async def test_kwargs_without_project_name_yields_none(monkeypatch):
    info_obj = SimpleNamespace(
        kwargs={"version": "v1.1"},   # ← project_name 키 누락
        enqueue_time=None,
        finish_time=None,
    )
    fake = _FakeJob(status_value=JobStatus.queued, info_obj=info_obj)
    _patch_job(monkeypatch, fake)
    out = await client.get_job_status("x")
    assert out["project_name"] is None
    assert out["status"] == "queued"


async def test_kwargs_is_none_yields_none(monkeypatch):
    """info.kwargs 자체가 None 인 변종도 안전."""
    info_obj = SimpleNamespace(
        kwargs=None,
        enqueue_time=None,
        finish_time=None,
    )
    fake = _FakeJob(status_value=JobStatus.queued, info_obj=info_obj)
    _patch_job(monkeypatch, fake)
    out = await client.get_job_status("x")
    assert out["project_name"] is None
