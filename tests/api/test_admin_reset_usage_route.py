"""
admin reset_usage 라우트 통합 테스트.

[검증]
1. usage_repository.reset_usage 호출 + 성공/실패 분기
2. audit_repository.write 호출 (ACTION_USAGE_RESET + reason payload)
3. 404 — 사용자 없음
4. 정책 정합성: reset_at 미터치는 usage_repository 단위 테스트 (test_usage_repository.py)
   에서 검증. 본 라우트 테스트는 위임 호출 + audit 만.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.api import admin_routes
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


def _admin(email: str = "admin@x.com") -> UserPublic:
    return UserPublic(
        id="a-1",
        email=email,
        name="Admin",
        subscription_type="free",
        is_admin=True,
    )


def _fake_request() -> SimpleNamespace:
    """slowapi limiter 가 request 객체 검사 → SimpleNamespace 우회 (lineage test 와 동일 패턴)."""
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/admin/users/x/reset-usage"),
        method="POST",
    )


@pytest.fixture
def audit_recorder(monkeypatch):
    calls: List[Dict[str, Any]] = []

    async def fake_write(**kwargs):
        calls.append(kwargs)
        return "audit-id"

    monkeypatch.setattr(
        "app.api.admin_routes.audit_repository.write", fake_write
    )
    return calls


@pytest.fixture
def reset_recorder(monkeypatch):
    calls: List[str] = []
    state = {"ok": True}

    async def fake_reset(email: str) -> bool:
        calls.append(email)
        return state["ok"]

    monkeypatch.setattr(
        "app.api.admin_routes.usage_repository.reset_usage", fake_reset
    )
    return calls, state


# ─── 정상 경로 ────────────────────────────────────────────────


async def test_reset_usage_success_calls_repository_and_audit(
    reset_recorder, audit_recorder
):
    """정상 흐름 — repository 호출 + audit 로그 작성 + 응답 본문."""
    calls, _state = reset_recorder
    payload = admin_routes.ResetUsageRequest(reason="CS 처리 — 결제 오류 보상")
    result = await admin_routes.reset_user_usage_route.__wrapped__(
        request=_fake_request(),
        email="user@x.com",
        payload=payload,
        admin=_admin(),
    )
    assert result["success"] is True
    assert result["email"] == "user@x.com"
    assert result["reason"] == "CS 처리 — 결제 오류 보상"
    # repository 호출 확인
    assert calls == ["user@x.com"]
    # audit 로그 확인
    assert len(audit_recorder) == 1
    audit = audit_recorder[0]
    assert audit["actor_email"] == "admin@x.com"
    assert audit["action"] == "usage_reset"
    assert audit["target_email"] == "user@x.com"
    assert audit["payload"] == {"reason": "CS 처리 — 결제 오류 보상"}


async def test_reset_usage_works_with_no_reason(reset_recorder, audit_recorder):
    """reason 미지정 — None 으로 audit 기록."""
    await admin_routes.reset_user_usage_route.__wrapped__(
        request=_fake_request(),
        email="user@x.com",
        payload=admin_routes.ResetUsageRequest(),
        admin=_admin(),
    )
    assert audit_recorder[0]["payload"] == {"reason": None}


# ─── 실패 경로 ────────────────────────────────────────────────


async def test_reset_usage_returns_404_when_user_missing(
    reset_recorder, audit_recorder
):
    """repository 가 False (사용자 없음) → 404 + audit 안 씀."""
    _calls, state = reset_recorder
    state["ok"] = False
    with pytest.raises(HTTPException) as exc:
        await admin_routes.reset_user_usage_route.__wrapped__(
            request=_fake_request(),
            email="ghost@x.com",
            payload=admin_routes.ResetUsageRequest(reason="test"),
            admin=_admin(),
        )
    assert exc.value.status_code == 404
    assert "찾을 수 없" in exc.value.detail
    # 사용자 없으면 audit 도 안 씀 (404 가 raise 후)
    assert audit_recorder == []
