"""
delete_routes.py IDOR 회귀 가드.

[배경]
2026-05 보안 감사 발견 — 가장 심각:
  - DELETE /api/v2/projects/{name}: 무권한 → 5-hop DETACH DELETE 로 타인의
    CPS/PRD/Skill/Meeting 모두 영구 삭제 가능 (회복 불가).
  - POST /api/v2/pipelines/delete_meeting: 무권한 → 타인 미팅 삭제 + 본인 토큰
    한도로 LLM 2회 호출 강제.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import delete_routes
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio

def _fake_request():
    """slowapi limiter request 객체 우회."""
    from types import SimpleNamespace
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/v2/test"),
        method="POST",
    )



def _user(email: str = "attacker@evil.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="t",
        subscription_type="free", is_admin=False,
    )


@pytest.fixture
def deny_ownership(monkeypatch):
    async def fake_assert(email: str, project: str):
        raise HTTPException(status_code=403, detail="해당 프로젝트에 대한 권한이 없습니다.")
    monkeypatch.setattr(
        "app.api.delete_routes.ownership_repository.assert_owns", fake_assert
    )


# ─── DELETE /projects/{name} ────────────────────────────────


async def test_delete_project_denies_when_not_owner(deny_ownership):
    """무권한 사용자가 DELETE 호출 시 403 — DETACH DELETE 절대 도달 안 함."""
    with pytest.raises(HTTPException) as exc:
        await delete_routes.delete_project_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project", current_user=_user(),
        )
    assert exc.value.status_code == 403


# ─── POST /pipelines/delete_meeting ─────────────────────────


async def test_delete_meeting_denies_when_not_owner(deny_ownership):
    """타인 미팅 삭제 시도 시 LLM 호출 전 403."""
    payload = delete_routes.DeleteMeetingRequest(
        project_name="victim_project", version="v1",
    )
    with pytest.raises(HTTPException) as exc:
        await delete_routes.delete_meeting_route.__wrapped__(
            request=_fake_request(),
            payload=payload, wait=False, current_user=_user(),
        )
    assert exc.value.status_code == 403
