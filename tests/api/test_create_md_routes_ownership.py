"""
create_md_routes.py IDOR 회귀 가드.

[배경]
2026-05 보안 감사 발견: POST /api/v2/pipelines/create_md 가 ownership 검증
없이 인증만 통과 → 본인 JWT 보유자가 타인 프로젝트의 Spack/DDD/Architecture
설계를 MD 로 변환해서 응답 본문으로 받을 수 있음. + LLM 3회 병렬 호출 (가장
큰 단일 토큰 소비) 이라 본인 quota 도 빠르게 abuse.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import create_md_routes
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
        "app.api.create_md_routes.ownership_repository.assert_owns", fake_assert
    )


async def test_create_md_denies_when_not_owner(deny_ownership):
    payload = create_md_routes.CreateMdRequest(project_name="victim_project")
    with pytest.raises(HTTPException) as exc:
        await create_md_routes.create_md_route.__wrapped__(
            request=_fake_request(),
            payload=payload, wait=False, current_user=_user(),
        )
    assert exc.value.status_code == 403
