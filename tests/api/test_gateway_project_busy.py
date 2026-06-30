"""
getProjectBusy — 다른 기기/탭의 진행 중 작업 감지 (2026-06 멀티디바이스 이중작업).

FE 가 plan 페이지에서 "다른 기기에서 처리 중" 배너 표시용으로 폴링.
실제 차단은 enqueue 의 409 PROJECT_BUSY — 이건 표시용 read-only.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import gateway_compat_routes as gw
from app.core import concurrency

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_pool(monkeypatch):
    pool = object()

    async def _get_pool():
        return pool
    monkeypatch.setattr(gw, "get_pool", _get_pool)
    return pool


async def test_busy_true_when_inflight(monkeypatch, fake_pool):
    seen = {}

    async def fake_busy(redis, project_key):
        seen["key"] = project_key
        return True
    monkeypatch.setattr(concurrency, "is_project_busy", fake_busy)

    out = await gw._h_get_project_busy({}, {"projectName": "projX"})
    body = out["result"] if "result" in out else out
    assert body["busy"] is True and body["project_name"] == "projX"
    assert seen["key"] == "projX"  # 개인 프로젝트 — 이름 그대로 scoped


async def test_busy_false_when_free(monkeypatch, fake_pool):
    async def fake_busy(redis, project_key):
        return False
    monkeypatch.setattr(concurrency, "is_project_busy", fake_busy)

    out = await gw._h_get_project_busy({}, {"projectName": "projX"})
    body = out["result"] if "result" in out else out
    assert body["busy"] is False


async def test_team_query_uses_scoped_key(monkeypatch, fake_pool):
    seen = {}

    async def fake_busy(redis, project_key):
        seen["key"] = project_key
        return False
    monkeypatch.setattr(concurrency, "is_project_busy", fake_busy)

    await gw._h_get_project_busy({}, {"projectName": "projX", "teamId": "team-5"})
    assert "team-5" in seen["key"] and "projX" in seen["key"]


async def test_missing_project_name_422(fake_pool):
    with pytest.raises(HTTPException) as ei:
        await gw._h_get_project_busy({}, {})
    assert ei.value.status_code == 422


def test_action_registered_with_ownership():
    """action 등록 + ownership READ 분류 (본인 프로젝트만 실데이터, 비소유는 200-empty)."""
    assert gw._DISPATCH["getProjectBusy"] is gw._h_get_project_busy
    # [2026-06] read 로 재분류 — 비소유/미claim 은 403 대신 busy:false 빈응답.
    assert "getProjectBusy" in gw._OWNERSHIP_READ
    assert "getProjectBusy" not in gw._OWNERSHIP_ACCESS


# ─── [감사 G1] sync deleteMeeting 의 프로젝트 락 ─────────────────


async def test_sync_delete_busy_returns_409(monkeypatch, fake_pool):
    """다른 작업이 락 보유 중이면 sync delete 는 15s 대기 후 409 PROJECT_BUSY.

    FE 배치 pre-cleanup 이 정확히 이 경로를 호출 — merge 중 delete 가 master 를
    rebuild 하던 race 의 사용자 노출면.
    """
    from contextlib import asynccontextmanager
    from app.core import master_lock

    @asynccontextmanager
    async def always_timeout(redis, project_key, holder, *, wait_timeout=None):
        raise master_lock.MasterLockTimeout("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(master_lock, "master_write_lock", always_timeout)

    with pytest.raises(HTTPException) as ei:
        await gw._h_delete_meeting(
            {"project_name": "projX", "version": "v1"}, {}, user_email="u@b.com",
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "PROJECT_BUSY"


async def test_sync_design_busy_returns_409(monkeypatch, fake_pool):
    """[2026-06 후속] legacy sync createSpack 경로도 락 — 구버전 번들/외부 호출이
    워커 design 잡과 겹쳐 설계 그래프 stage 가 섞이는 것 차단."""
    from contextlib import asynccontextmanager
    from app.core import master_lock

    @asynccontextmanager
    async def always_timeout(redis, project_key, holder, *, wait_timeout=None):
        raise master_lock.MasterLockTimeout("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(master_lock, "master_write_lock", always_timeout)

    with pytest.raises(HTTPException) as ei:
        await gw._h_create_design(
            {"projectName": "projX"}, {}, user_email="u@b.com",
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "PROJECT_BUSY"


async def test_sync_delete_acquires_lock_and_runs(monkeypatch, fake_pool):
    """락이 비어 있으면 delete 가 락 안에서 실행 + 결과 정상 반환."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.core import master_lock

    seen = {"key": None, "wait": None}

    @asynccontextmanager
    async def spy_lock(redis, project_key, holder, *, wait_timeout=None):
        seen["key"] = project_key
        seen["wait"] = wait_timeout
        yield

    monkeypatch.setattr(master_lock, "master_write_lock", spy_lock)

    @asynccontextmanager
    async def fake_tracked(**kw):
        yield SimpleNamespace()
    monkeypatch.setattr(gw, "tracked_pipeline_context", fake_tracked)

    async def fake_delete(ctx, payload):
        return SimpleNamespace(
            status="success", message="", project_name="projX",
            deleted_version="v1", remaining_cps_count=0, remaining_prd_count=0,
            cps_master_rebuilt=True, prd_master_rebuilt=True,
        )
    monkeypatch.setattr(gw, "run_delete_meeting_pipeline", fake_delete)

    out = await gw._h_delete_meeting(
        {"project_name": "projX", "version": "v1"}, {}, user_email="u@b.com",
    )
    assert out["result"]["status"] == "success"
    assert seen["key"] == "projX"      # 개인 프로젝트 scoped key
    assert seen["wait"] == 15.0        # sync 짧은 대기
