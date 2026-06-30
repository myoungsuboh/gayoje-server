"""
auto_progress 필드 — User 모델 + cypher + update_user 통합 회귀 가드.

[배경 — 2026-05]
검수 게이트 모드 도입: User.auto_progress (default true) 가 false 면 postMeeting
이 CPS 만 생성하고 PRD/Design 자동 진행 안 함. FE 가 stage 별 명시 트리거.

[가드]
- UserInDB / UserPublic 에 auto_progress 필드 + default true
- GET cypher 가 auto_progress 노출
- UPDATE cypher 가 auto_progress 조건부 갱신
- update_user 시그니처에 auto_progress 인자
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import user_repository
from app.service.user_repository import UserInDB, UserPublic, update_user


pytestmark = pytest.mark.asyncio


class _FakeRun:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher: str, params: Optional[Dict[str, Any]] = None,
                       database: Optional[str] = None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        return self._responses.pop(0) if self._responses else []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses=None):
        fake = _FakeRun(responses)
        monkeypatch.setattr(
            "app.service.user_repository.neo4j_client.run_cypher", fake
        )
        return fake
    return _setup


# ─── 모델 default ──────────────────────────────────────


def test_user_in_db_default_auto_progress_true():
    u = UserInDB(
        id="u1", email="x@y.com", name="t", hashed_password="x",
    )
    assert u.auto_progress is True


def test_user_public_default_auto_progress_true():
    u = UserPublic(id="u1", email="x@y.com", name="t")
    assert u.auto_progress is True


def test_user_public_from_db_carries_auto_progress():
    """from_db 가 auto_progress 도 옮김."""
    db = UserInDB(
        id="u1", email="x@y.com", name="t", hashed_password="x",
        auto_progress=False,
    )
    pub = UserPublic.from_db(db)
    assert pub.auto_progress is False


# ─── cypher 회귀 가드 ───────────────────────────────────


def test_get_cypher_returns_auto_progress():
    """_GET_USER_BY_EMAIL_CYPHER 응답에 auto_progress 포함."""
    cypher = user_repository._GET_USER_BY_EMAIL_CYPHER
    assert "auto_progress" in cypher
    # legacy 사용자 (필드 없음) 호환 — COALESCE 로 default true
    assert "COALESCE(u.auto_progress, true)" in cypher


def test_update_cypher_conditional_sets_auto_progress():
    """_UPDATE_USER_CYPHER 가 $auto_progress IS NOT NULL 일 때만 SET — false 도 유효 갱신."""
    cypher = user_repository._UPDATE_USER_CYPHER
    assert "$auto_progress IS NOT NULL" in cypher
    assert "u.auto_progress =" in cypher


# ─── update_user 함수 ──────────────────────────────────


async def test_update_user_passes_auto_progress_param(fake_run):
    """update_user(..., auto_progress=False) → cypher 에 auto_progress=False 바인딩."""
    fake = fake_run([
        [{"user": {
            "id": "u", "email": "x@y.com", "name": "t",
            "github_username": "", "subscription_type": "free",
            "is_admin": False, "auto_progress": False,
            "updated_at": "2026-01-01",
        }}]
    ])
    out = await update_user(email="x@y.com", auto_progress=False)
    assert out is not None
    assert out.auto_progress is False
    # cypher 호출 시 auto_progress 가 False 로 전달됨
    assert fake.calls[0]["params"]["auto_progress"] is False


async def test_update_user_auto_progress_none_keeps_existing(fake_run):
    """auto_progress=None (default) → 기존값 유지 (cypher COALESCE 로직 검증)."""
    fake = fake_run([
        [{"user": {
            "id": "u", "email": "x@y.com", "name": "t",
            "github_username": "", "subscription_type": "free",
            "is_admin": False, "auto_progress": True,
            "updated_at": "2026-01-01",
        }}]
    ])
    out = await update_user(email="x@y.com", name="new_name")
    assert out.auto_progress is True
    # 명시적으로 None 전달
    assert fake.calls[0]["params"]["auto_progress"] is None


# ─── UserResponse schema ───────────────────────────────


def test_user_response_includes_auto_progress():
    from app.schemas import UserResponse
    assert "auto_progress" in UserResponse.model_fields


def test_update_me_request_optional_auto_progress():
    from app.schemas import UpdateMeRequest
    # None default — 미전달 시 None
    req = UpdateMeRequest()
    assert req.auto_progress is None
    # true/false 둘 다 통과
    assert UpdateMeRequest(auto_progress=True).auto_progress is True
    assert UpdateMeRequest(auto_progress=False).auto_progress is False
