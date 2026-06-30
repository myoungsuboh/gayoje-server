"""
PATCH /api/v2/cps 회귀 가드 — 검수 게이트 사용자 직접 편집.

[검증]
- ownership 가드 (타인 프로젝트 → 403)
- project_name 누락 → 422 (Pydantic)
- 빈 content → 422
- 500KB 초과 content → 422
- master 없으면 404
- 정상 → repository 호출 + UpdateCpsResponse 반환
- rate limit 데코레이터 적용 확인 (__wrapped__)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import query_routes
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


def _user(email: str = "owner@x.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="t",
        subscription_type="free", is_admin=False,
    )


def _fake_request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/v2/cps"),
        method="PATCH",
    )


@pytest.fixture
def allow_ownership(monkeypatch):
    async def fake_assert(email, project): return None
    monkeypatch.setattr(
        "app.api.query_routes.ownership_repository.assert_owns", fake_assert
    )


@pytest.fixture
def deny_ownership(monkeypatch):
    async def fake_assert(email, project):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")
    monkeypatch.setattr(
        "app.api.query_routes.ownership_repository.assert_owns", fake_assert
    )


@pytest.fixture
def fake_update(monkeypatch):
    state = {"return": {"master_id": "m1", "last_updated": 1700000000000}}
    # [2026-05-18 Phase 2] client_updated_at kwarg 도 받음 (optimistic locking).
    async def fake(project, content, *, client_updated_at=None, team_id=""):
        state["last_call"] = {
            "project": project,
            "content": content,
            "client_updated_at": client_updated_at,
        }
        return state["return"]
    monkeypatch.setattr(
        "app.api.query_routes.q.update_master_cps_markdown", fake
    )
    return state


# ─── 정상 ──────────────────────────────────────────────


async def test_update_returns_master_id(allow_ownership, fake_update):
    payload = query_routes.UpdateCpsRequest(
        project_name="my_project",
        content="# new content",
    )
    out = await query_routes.update_cps_route.__wrapped__(
        request=_fake_request(),
        payload=payload,
        current_user=_user(),
    )
    assert out.master_id == "m1"
    assert fake_update["last_call"]["project"] == "my_project"
    assert fake_update["last_call"]["content"] == "# new content"


# ─── ownership ─────────────────────────────────────────


async def test_update_denies_when_not_owner(deny_ownership):
    """[IDOR] 타인 프로젝트 수정 시도 → 403."""
    payload = query_routes.UpdateCpsRequest(
        project_name="victim",
        content="evil",
    )
    with pytest.raises(HTTPException) as exc:
        await query_routes.update_cps_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user("attacker@evil.com"),
        )
    assert exc.value.status_code == 403


# ─── 404 ──────────────────────────────────────────────


async def test_update_404_when_master_missing(allow_ownership, fake_update):
    """master 없으면 repository 가 None → 라우트 404."""
    fake_update["return"] = None
    payload = query_routes.UpdateCpsRequest(
        project_name="fresh_project",
        content="hello",
    )
    with pytest.raises(HTTPException) as exc:
        await query_routes.update_cps_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user(),
        )
    assert exc.value.status_code == 404
    assert "postMeeting" in exc.value.detail


# ─── Pydantic 검증 ─────────────────────────────────────


def test_request_rejects_empty_content():
    with pytest.raises(Exception) as exc:  # ValidationError
        query_routes.UpdateCpsRequest(project_name="p", content="")
    assert "min_length" in str(exc.value) or "at least" in str(exc.value)


def test_request_rejects_oversized_content():
    """500KB 초과 거부 — abuse 방어."""
    with pytest.raises(Exception) as exc:
        query_routes.UpdateCpsRequest(
            project_name="p",
            content="a" * 500_001,
        )
    assert "max_length" in str(exc.value) or "at most" in str(exc.value)


def test_request_rejects_missing_project_name():
    with pytest.raises(Exception):
        query_routes.UpdateCpsRequest(content="x")


# ─── rate limit decoration ──────────────────────────


def test_update_cps_route_has_rate_limit():
    """[회귀] @limiter.limit 데코레이터 → __wrapped__ 존재."""
    assert hasattr(query_routes.update_cps_route, "__wrapped__"), (
        "update_cps_route 에 @limiter.limit 누락 — DoS 표면"
    )


# ─── [2026-05-18 Phase 2] Optimistic Locking ────────────


def test_request_accepts_client_updated_at():
    """client_updated_at 필드 옵셔널 — 있으면 conflict check 활성, 없으면 legacy."""
    p1 = query_routes.UpdateCpsRequest(project_name="p", content="x")
    assert p1.client_updated_at is None
    p2 = query_routes.UpdateCpsRequest(
        project_name="p", content="x", client_updated_at=1700000000000,
    )
    assert p2.client_updated_at == 1700000000000


async def test_update_passes_client_updated_at_to_repo(allow_ownership, fake_update):
    """라우트 → repository 호출 시 client_updated_at 전달 확인."""
    payload = query_routes.UpdateCpsRequest(
        project_name="my_project",
        content="# new content",
        client_updated_at=1700000000000,
    )
    await query_routes.update_cps_route.__wrapped__(
        request=_fake_request(),
        payload=payload,
        current_user=_user(),
    )
    assert fake_update["last_call"]["client_updated_at"] == 1700000000000


async def test_update_409_on_conflict(allow_ownership, monkeypatch):
    """다른 디바이스가 먼저 편집 → repository 가 OptimisticLockConflict raise → 409."""
    from app.service.query_repository import OptimisticLockConflict

    async def fake_conflict(project, content, *, client_updated_at=None, team_id=""):
        raise OptimisticLockConflict(
            "다른 디바이스에서 먼저 편집됐습니다. 새로고침 후 다시 시도해주세요."
        )
    monkeypatch.setattr(
        "app.api.query_routes.q.update_master_cps_markdown", fake_conflict
    )

    payload = query_routes.UpdateCpsRequest(
        project_name="p",
        content="new",
        client_updated_at=1700000000000,
    )
    with pytest.raises(HTTPException) as exc:
        await query_routes.update_cps_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user(),
        )
    assert exc.value.status_code == 409
    assert "다른 디바이스" in exc.value.detail
