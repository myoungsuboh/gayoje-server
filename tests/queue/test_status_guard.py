"""
queue.status_guard 단위 테스트 — Sprint 8 P0 ownership 가드.

[검증 시나리오]
1. task_id 가 not_found → 404 (정보 누설 방지)
2. project_name 회수 실패 (메타 expire) → 404 (동일)
3. ownership 검증 실패 (다른 사용자 task) → 403 (assert_owns 가 raise)
4. 정상 흐름 → info dict 반환 + project_name 포함

[가드의 핵심]
모든 status 라우트가 이 헬퍼를 거치므로, 한 곳만 안전하면 7개 라우트 모두 안전.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import HTTPException

from app.queue import status_guard


pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_status(monkeypatch):
    """get_job_status 의 응답을 테스트별로 주입."""
    def _setup(info: Dict[str, Any]):
        async def fake(_task_id):
            return info
        monkeypatch.setattr(status_guard, "get_job_status", fake)
    return _setup


@pytest.fixture
def mock_assert_owns(monkeypatch):
    """ownership_repository.assert_owns 동작 주입.

    raises=True 면 403, False 면 통과.
    """
    state = {"raises": False, "calls": []}

    async def fake(email, project):
        state["calls"].append((email, project))
        if state["raises"]:
            raise HTTPException(
                status_code=403,
                detail="해당 프로젝트에 접근 권한이 없습니다.",
            )

    monkeypatch.setattr(
        status_guard.ownership_repository, "assert_owns", fake
    )
    return state


# ─── 시나리오 1: task_id not_found ─────────────────────────────────


async def test_not_found_raises_404(mock_status, mock_assert_owns):
    mock_status({"task_id": "x", "project_name": None, "status": "not_found"})
    with pytest.raises(HTTPException) as exc_info:
        await status_guard.get_job_status_for_user("x", "alice@x")
    assert exc_info.value.status_code == 404
    # 비교: ownership 검증은 호출되지 않음 (단축회로)
    assert mock_assert_owns["calls"] == []


# ─── 시나리오 2: project_name 회수 실패 ────────────────────────────


async def test_missing_project_name_raises_404(mock_status, mock_assert_owns):
    """info() 가 메타 expire 로 project_name None — 정보 누설 방지 위해 404."""
    mock_status({
        "task_id": "x", "project_name": None, "status": "complete",
        "result": {"secret": "data"},
    })
    with pytest.raises(HTTPException) as exc_info:
        await status_guard.get_job_status_for_user("x", "alice@x")
    assert exc_info.value.status_code == 404
    assert mock_assert_owns["calls"] == []
    # 결과 값이 응답에 누설되지 않음 (raise 됐으므로 dict 반환 안 함)


async def test_missing_project_name_empty_string_raises_404(mock_status, mock_assert_owns):
    """falsy (빈 문자열) 도 404 — boolean 단순 검사로 안전."""
    mock_status({"task_id": "x", "project_name": "", "status": "queued"})
    with pytest.raises(HTTPException) as exc_info:
        await status_guard.get_job_status_for_user("x", "alice@x")
    assert exc_info.value.status_code == 404


# ─── 시나리오 3: ownership 실패 ────────────────────────────────────


async def test_other_users_task_raises_403(mock_status, mock_assert_owns):
    """다른 사용자가 만든 task — assert_owns 가 403 raise → 그대로 전파."""
    mock_status({
        "task_id": "x", "project_name": "alice_proj", "status": "complete",
        "result": {"data": "secret"},
    })
    mock_assert_owns["raises"] = True
    with pytest.raises(HTTPException) as exc_info:
        await status_guard.get_job_status_for_user("x", "bob@x")
    assert exc_info.value.status_code == 403
    # assert_owns 가 정확히 1회 호출됐고 인자가 (bob, alice_proj)
    assert mock_assert_owns["calls"] == [("bob@x", "alice_proj")]


# ─── 시나리오 4: 정상 흐름 ───────────────────────────────────────


async def test_owner_gets_full_info(mock_status, mock_assert_owns):
    """본인 task — assert_owns 통과 + info 반환."""
    info = {
        "task_id": "x",
        "project_name": "alice_proj",
        "status": "complete",
        "result": {"data": "x"},
        "enqueue_time": 1700,
        "finish_time": 1800,
    }
    mock_status(info)
    out = await status_guard.get_job_status_for_user("x", "alice@x")
    assert out == info
    assert mock_assert_owns["calls"] == [("alice@x", "alice_proj")]


async def test_running_job_still_owner_gated(mock_status, mock_assert_owns):
    """status=in_progress 라도 ownership 검증 — 결과 없을 때도 우회 차단."""
    mock_status({
        "task_id": "x", "project_name": "alice_proj",
        "status": "in_progress",
    })
    out = await status_guard.get_job_status_for_user("x", "alice@x")
    assert out["status"] == "in_progress"
    assert mock_assert_owns["calls"] == [("alice@x", "alice_proj")]


# ─── 회귀 방지: 빠짐 없는 분기 ────────────────────────────────────


async def test_404_detail_does_not_leak_task_id(mock_status, mock_assert_owns):
    """404 detail 에 task_id 포함하지 않음 — 존재 여부 누설 방지."""
    mock_status({"task_id": "secret-task-id-123", "project_name": None, "status": "not_found"})
    with pytest.raises(HTTPException) as exc_info:
        await status_guard.get_job_status_for_user("secret-task-id-123", "x@x")
    assert "secret-task-id-123" not in str(exc_info.value.detail)
