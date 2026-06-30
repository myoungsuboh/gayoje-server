"""
gateway_compat 의 postMeeting 비동기 큐잉 + getJobStatus 핸들러 단위 테스트.

[배경 — 2026-05]
이전 _h_post_meeting 은 CPS + PRD 파이프라인을 동기 실행 → 1~4분 소요.
Cloudflare 프록시 ~100s timeout 으로 배치 처리(V2/V3) 빈번히 실패.
이제 enqueue_post_meeting 으로 큐에 등록하고 즉시 task_id 반환.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.api import gateway_compat_routes as gw


pytestmark = pytest.mark.asyncio


# [2026-05-18 Phase 1 동시접속] _h_post_meeting / _h_create_cps 가 호출하는
# meeting_log_exists 사전 체크를 default False (충돌 없음) 로 mock.
# 충돌 케이스를 검증하는 신규 테스트는 이 fixture 를 override 해서 True 반환.
@pytest.fixture(autouse=True)
def _stub_meeting_log_exists(monkeypatch):
    async def _no(_project, _version, team_id=""):
        return False
    monkeypatch.setattr(gw.query_repository, "meeting_log_exists", _no)


# ─── _h_post_meeting — enqueue 호출 검증 ──────────────────────────


async def test_h_post_meeting_enqueues_and_returns_task_id(monkeypatch):
    """body 의 필드들이 enqueue_post_meeting 으로 그대로 전달되고 task_id 반환."""
    enqueue_calls: List[Dict[str, Any]] = []

    async def fake_enqueue(**kwargs):
        enqueue_calls.append(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(gw, "enqueue_post_meeting", fake_enqueue)

    body = {
        "project_name": "food",
        "version": "v1.1",
        "date": "2026-05-17",
        "meeting_content": "오늘 미팅 내용...",
        "previous_cps_id": "doc_cps_food_v1_0",
        "previous_prd_id": "doc_prd_food_v1_0",
    }
    out = await gw._h_post_meeting(body, {}, user_email="u@example.com")

    # 응답 shape
    assert "result" in out
    result = out["result"]
    assert result["status"] == "accepted"
    assert isinstance(result["task_id"], str) and len(result["task_id"]) > 0

    # enqueue 인자 검증
    assert len(enqueue_calls) == 1
    kw = enqueue_calls[0]
    assert kw["task_id"] == result["task_id"]
    assert kw["project_name"] == "food"
    assert kw["version"] == "v1.1"
    assert kw["date"] == "2026-05-17"
    assert kw["meeting_content"] == "오늘 미팅 내용..."
    assert kw["previous_cps_id"] == "doc_cps_food_v1_0"
    assert kw["previous_prd_id"] == "doc_prd_food_v1_0"
    assert kw["user_email"] == "u@example.com"


async def test_h_post_meeting_accepts_camelcase_keys(monkeypatch):
    """legacy FE 가 camelCase 로 보낼 수 있어 양쪽 모두 흡수."""
    captured = {}

    async def fake_enqueue(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(gw, "enqueue_post_meeting", fake_enqueue)

    body = {
        "projectName": "food",
        "version": "v1",
        "date": "2026-05-17",
        "meetingContent": "내용",
        "previousCpsId": "doc_cps_food_v1",
        "previousPrdId": None,
    }
    await gw._h_post_meeting(body, {}, user_email="u@example.com")
    assert captured["project_name"] == "food"
    assert captured["meeting_content"] == "내용"
    assert captured["previous_cps_id"] == "doc_cps_food_v1"


async def test_h_post_meeting_enqueue_failure_raises_503(monkeypatch):
    """enqueue 실패 시 503 으로 명시 — FE 가 재시도 안내 가능."""
    from fastapi import HTTPException

    async def fake_enqueue(**kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(gw, "enqueue_post_meeting", fake_enqueue)

    with pytest.raises(HTTPException) as exc_info:
        await gw._h_post_meeting(
            {"project_name": "x", "version": "v1", "date": "d", "meeting_content": "c"},
            {},
            user_email="u@example.com",
        )
    assert exc_info.value.status_code == 503
    assert "queue unavailable" in exc_info.value.detail


# ─── _h_create_cps — 검수 모드 (CPS 단독) ───────────────────────


async def test_h_create_cps_enqueues_cps_only(monkeypatch):
    """검수 모드 — body 의 필드들이 enqueue_cps 로 그대로 전달되고 task_id 반환.
    PRD 관련 파라미터(previous_prd_id) 는 전달 안 됨 — CPS 단독 흐름.
    """
    enqueue_calls: list = []

    async def fake_enqueue_cps(**kwargs):
        enqueue_calls.append(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(gw, "enqueue_cps", fake_enqueue_cps)

    body = {
        "project_name": "plant",
        "version": "v1.5",
        "date": "2026-05-18",
        "meeting_content": "오늘 회의...",
        "previous_cps_id": "doc_cps_plant_v1_4",
    }
    out = await gw._h_create_cps(body, {}, user_email="reviewer@example.com")

    assert "result" in out
    result = out["result"]
    assert result["status"] == "accepted"
    assert isinstance(result["task_id"], str) and len(result["task_id"]) > 0

    assert len(enqueue_calls) == 1
    kw = enqueue_calls[0]
    assert kw["task_id"] == result["task_id"]
    assert kw["project_name"] == "plant"
    assert kw["version"] == "v1.5"
    assert kw["meeting_content"] == "오늘 회의..."
    assert kw["previous_cps_id"] == "doc_cps_plant_v1_4"
    assert kw["user_email"] == "reviewer@example.com"
    # PRD 관련 키는 enqueue_cps 시그니처에 없으므로 미전달.
    assert "previous_prd_id" not in kw


async def test_h_create_cps_accepts_camelcase_keys(monkeypatch):
    captured = {}

    async def fake_enqueue_cps(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(gw, "enqueue_cps", fake_enqueue_cps)

    body = {
        "projectName": "plant",
        "version": "v2",
        "date": "2026-05-18",
        "meetingContent": "내용",
        "previousCpsId": "doc_cps_plant_v1",
    }
    await gw._h_create_cps(body, {}, user_email="u@example.com")
    assert captured["project_name"] == "plant"
    assert captured["meeting_content"] == "내용"
    assert captured["previous_cps_id"] == "doc_cps_plant_v1"


async def test_h_create_cps_enqueue_failure_raises_503(monkeypatch):
    from fastapi import HTTPException

    async def fake_enqueue_cps(**kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(gw, "enqueue_cps", fake_enqueue_cps)
    with pytest.raises(HTTPException) as exc_info:
        await gw._h_create_cps(
            {"project_name": "x", "version": "v1", "date": "d", "meeting_content": "c"},
            {},
            user_email="u@example.com",
        )
    assert exc_info.value.status_code == 503
    assert "queue unavailable" in exc_info.value.detail


def test_cps_action_registered_in_dispatch():
    """[회귀] 'cps' action 이 _DISPATCH + LLM/Meeting/Ownership 셋에 모두 등록."""
    assert "cps" in gw._DISPATCH
    assert "cps" in gw._LLM_HANDLERS
    assert "cps" in gw._MEETING_CREATING_HANDLERS
    assert "cps" in gw._OWNERSHIP_CREATE


# ─── _h_get_job_status — ownership 검증 위임 ─────────────────────


async def test_h_get_job_status_returns_status(monkeypatch):
    """query.task_id 로 get_job_status_for_user 호출, _wrap 된 응답 반환."""
    captured = {}

    async def fake_status(task_id, user_email):
        captured["task_id"] = task_id
        captured["user_email"] = user_email
        return {
            "task_id": task_id,
            "project_name": "food",
            "status": "complete",
            "result": {"cps_master_id": "doc_cps_master_food"},
            "error": None,
            "enqueue_time": 1700000000000,
            "finish_time": 1700000060000,
        }

    monkeypatch.setattr(gw, "get_job_status_for_user", fake_status)

    out = await gw._h_get_job_status({}, {"task_id": "abc123"}, user_email="u@example.com")

    assert captured == {"task_id": "abc123", "user_email": "u@example.com"}
    assert out["result"]["status"] == "complete"
    assert out["result"]["task_id"] == "abc123"


async def test_h_get_job_status_accepts_camel_key(monkeypatch):
    async def fake_status(task_id, user_email):
        return {"task_id": task_id, "project_name": "x", "status": "queued"}

    monkeypatch.setattr(gw, "get_job_status_for_user", fake_status)
    out = await gw._h_get_job_status({}, {"taskId": "xyz"}, user_email="u@example.com")
    assert out["result"]["task_id"] == "xyz"


async def test_h_get_job_status_missing_task_id_raises_422():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await gw._h_get_job_status({}, {}, user_email="u@example.com")
    assert exc_info.value.status_code == 422
    assert "task_id" in exc_info.value.detail


# ─── Dispatcher 등록 정합성 ─────────────────────────────────────


def test_get_job_status_registered_in_dispatch():
    assert "getJobStatus" in gw._DISPATCH
    assert gw._DISPATCH["getJobStatus"] is gw._h_get_job_status


def test_get_job_status_in_user_email_required():
    """dispatcher 가 user_email kwarg 를 주입해야 _h_get_job_status 시그니처 충족."""
    assert "getJobStatus" in gw._USER_EMAIL_REQUIRED_HANDLERS


def test_get_job_status_in_ownership_free():
    """getJobStatus 는 dispatcher 가 project 추출 못 함 → 핸들러 내부 검증으로 위임."""
    assert "getJobStatus" in gw._OWNERSHIP_FREE


def test_post_meeting_still_in_ownership_create():
    """비동기 전환 후에도 ownership claim 은 dispatcher 가 진입 시 처리 (변동 없음)."""
    assert "postMeeting" in gw._OWNERSHIP_CREATE
    assert "postMeeting" in gw._LLM_HANDLERS
    assert "postMeeting" in gw._MEETING_CREATING_HANDLERS


# ─── [2026-05-18 Phase 1 동시접속] (project, version) 충돌 차단 ──────


async def test_h_post_meeting_rejects_existing_version(monkeypatch):
    """이미 같은 (project, version) Meeting_Log 가 있으면 409 응답.

    PC + 모바일 동시 저장 → log_id 충돌 + LLM 2배 + 미팅 카운트 2배 차감 방지.
    """
    # 이 테스트만 meeting_log_exists True 반환 (충돌 시뮬레이션)
    async def _yes(_project, _version, team_id=""):
        return True
    monkeypatch.setattr(gw.query_repository, "meeting_log_exists", _yes)

    # enqueue 가 호출되지 않아야 함 — 사전 차단
    enqueue_calls = []
    async def fake_enqueue(**kwargs):
        enqueue_calls.append(kwargs)
        return kwargs["task_id"]
    monkeypatch.setattr(gw, "enqueue_post_meeting", fake_enqueue)

    body = {
        "project_name": "food",
        "version": "v1.1",
        "meeting_content": "x" * 250,
    }
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await gw._h_post_meeting(body, {}, user_email="alice@example.com")
    assert exc.value.status_code == 409
    assert "v1.1" in str(exc.value.detail)
    assert "다른 디바이스" in str(exc.value.detail)
    # enqueue 호출 없어야 함 (LLM 비용 보호)
    assert enqueue_calls == []


async def test_h_create_cps_rejects_existing_version(monkeypatch):
    """동일 시나리오 — _h_create_cps (검수 모드) 도 같은 차단."""
    async def _yes(_project, _version, team_id=""):
        return True
    monkeypatch.setattr(gw.query_repository, "meeting_log_exists", _yes)

    enqueue_calls = []
    async def fake_enqueue_cps(**kwargs):
        enqueue_calls.append(kwargs)
        return kwargs["task_id"]
    monkeypatch.setattr(gw, "enqueue_cps", fake_enqueue_cps)

    body = {
        "project_name": "food",
        "version": "v2.0",
        "meeting_content": "x" * 250,
    }
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await gw._h_create_cps(body, {}, user_email="alice@example.com")
    assert exc.value.status_code == 409
    assert "v2.0" in str(exc.value.detail)
    assert enqueue_calls == []
