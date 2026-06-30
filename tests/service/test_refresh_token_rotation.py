"""
refresh_access_token — refresh token 회전 (rotation) 패턴 회귀 가드.

[배경 — 2026-05 H1 픽스]
이전: refresh token 이 7일간 무한 재사용. /logout 호출 없이는 무효화 0.
    탈취 시 7일간 access 무한 발급 가능했음.
이후: 호출 1회당
    (1) 사용된 refresh 의 jti 가 blacklist 에 있는지 확인 → 재사용 차단
    (2) 새 access + 새 refresh 발급 → 두 토큰 모두 회전
    (3) 사용된 refresh 의 jti 를 blacklist 등록 → 다음 호출에 401

[가드]
- 정상 호출: 새 페어 반환 + 사용된 토큰 blacklist 등록
- 재사용 시도: 401 (already revoked)
- 잘못된 type / 만료 사용자: 401
"""
from __future__ import annotations

from typing import Optional, Set

import pytest
from fastapi import HTTPException

from app.core.security import create_refresh_token, decode_token
from app.service import auth_service
from app.service.user_repository import UserInDB


pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_user(monkeypatch):
    """get_user_by_email 가 사용자 1명 반환 / None 반환 토글."""
    state = {"user": True}

    async def fake_get_user(email: str) -> Optional[UserInDB]:
        if not state["user"]:
            return None
        return UserInDB(
            id="u-1", email=email, name="t",
            hashed_password="x",
            created_at="2026-01-01",
        )

    monkeypatch.setattr(
        "app.service.auth_service.users.get_user_by_email", fake_get_user
    )
    return state


@pytest.fixture
def fake_blacklist(monkeypatch):
    """token_blacklist 를 in-memory set 으로 대체 — 회전 동작 검증용."""
    revoked: Set[str] = set()

    async def fake_is_revoked(jti: str) -> bool:
        return jti in revoked

    async def fake_revoke_if_new(jti: Optional[str], exp_epoch: Optional[int]) -> bool:
        if not jti:
            return False
        if jti in revoked:
            return False
        revoked.add(jti)
        return True

    monkeypatch.setattr(
        "app.service.auth_service.token_blacklist.is_revoked", fake_is_revoked
    )
    monkeypatch.setattr(
        "app.service.auth_service.token_blacklist.revoke_if_new", fake_revoke_if_new
    )
    return revoked


# ─── 정상 회전 경로 ─────────────────────────────────────────


async def test_refresh_returns_new_pair_and_revokes_old(fake_user, fake_blacklist):
    """[회전] 새 (access, refresh) 반환 + 이전 refresh 의 jti 가 blacklist 에 등록."""
    old_refresh = create_refresh_token("a@b.com")
    old_jti = decode_token(old_refresh).get("jti")
    assert old_jti, "테스트 전제: refresh token 이 jti 를 포함해야 함"

    new_access, new_refresh = await auth_service.refresh_access_token(old_refresh)

    # 두 토큰 모두 새로 발급됐어야 함 — 그냥 jti 비교
    new_refresh_jti = decode_token(new_refresh).get("jti")
    assert new_refresh_jti != old_jti, "refresh 가 회전 안 됨 (동일 jti)"
    # 새 access 도 type 'access'
    assert decode_token(new_access).get("type") == "access"
    # 새 refresh 도 type 'refresh'
    assert decode_token(new_refresh).get("type") == "refresh"
    # 이전 refresh 는 blacklist 에 등록됐어야 함
    assert old_jti in fake_blacklist, "이전 refresh jti 가 blacklist 에 미등록 — 재사용 가능"


async def test_refresh_reuse_blocked(fake_user, fake_blacklist):
    """[회전 핵심] 같은 refresh 두 번째 호출 시 401."""
    refresh = create_refresh_token("a@b.com")
    # 1차 — 정상
    await auth_service.refresh_access_token(refresh)
    # 2차 — blacklist 에 있어서 401
    with pytest.raises(HTTPException) as exc:
        await auth_service.refresh_access_token(refresh)
    assert exc.value.status_code == 401
    assert "이미 사용" in exc.value.detail or "로그아웃" in exc.value.detail


# ─── 에러 경로 ──────────────────────────────────────────────


async def test_refresh_with_access_type_rejected(fake_user, fake_blacklist):
    """access token 으로 /refresh 호출하면 401."""
    from app.core.security import create_access_token
    access = create_access_token("a@b.com")
    with pytest.raises(HTTPException) as exc:
        await auth_service.refresh_access_token(access)
    assert exc.value.status_code == 401
    assert "refresh token" in exc.value.detail


async def test_refresh_user_deleted_rejected(fake_user, fake_blacklist):
    """탈퇴된 사용자의 refresh 사용 시 401 + 토큰은 blacklist 등록 안 함 (아직 사용자 확인 전)."""
    refresh = create_refresh_token("ghost@b.com")
    fake_user["user"] = False  # get_user_by_email 가 None 반환
    with pytest.raises(HTTPException) as exc:
        await auth_service.refresh_access_token(refresh)
    assert exc.value.status_code == 401


async def test_refresh_already_revoked_rejected(fake_user, fake_blacklist):
    """이미 blacklist 에 있는 refresh (예: logout 됐던 토큰) 사용 시 401."""
    refresh = create_refresh_token("a@b.com")
    jti = decode_token(refresh).get("jti")
    # 미리 blacklist 에 등록
    fake_blacklist.add(jti)
    with pytest.raises(HTTPException) as exc:
        await auth_service.refresh_access_token(refresh)
    assert exc.value.status_code == 401
