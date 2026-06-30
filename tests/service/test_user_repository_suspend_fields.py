"""User 노드의 suspend 관련 필드가 UserInDB 에 정상 매핑되는지 검증."""
from __future__ import annotations
import pytest
from app.service import user_repository

pytestmark = pytest.mark.asyncio


async def test_get_user_by_email_includes_suspend_fields(monkeypatch):
    """Cypher 응답에 suspend 필드가 있으면 UserInDB 에 그대로 매핑."""
    fake_row = {
        "user": {
            "id": "u1", "email": "a@x.com", "name": "A",
            "hashed_password": "h", "github_username": "",
            "subscription_type": "free", "is_admin": False,
            "auto_progress": True,
            "is_suspended": True,
            "suspended_at": "2026-05-18T10:00:00Z",
            "suspended_reason": "abuse",
            "suspended_by_email": "admin@x.com",
            "unsuspended_at": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-05-18T10:00:00Z",
        }
    }

    async def fake_run(cypher, params=None, database=None):
        return [fake_row]

    monkeypatch.setattr(
        "app.service.user_repository.neo4j_client.run_cypher", fake_run
    )

    user = await user_repository.get_user_by_email("a@x.com")
    assert user is not None
    assert user.is_suspended is True
    assert user.suspended_at == "2026-05-18T10:00:00Z"
    assert user.suspended_reason == "abuse"
    assert user.suspended_by_email == "admin@x.com"
    assert user.unsuspended_at is None


async def test_get_user_by_email_defaults_suspend_false_when_missing(monkeypatch):
    """legacy 사용자 — suspend 필드가 응답에 없어도 default false."""
    fake_row = {
        "user": {
            "id": "u1", "email": "a@x.com", "name": "A",
            "hashed_password": "h", "github_username": "",
            "subscription_type": "free", "is_admin": False,
            "auto_progress": True,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-05-18T10:00:00Z",
        }
    }

    async def fake_run(cypher, params=None, database=None):
        return [fake_row]

    monkeypatch.setattr(
        "app.service.user_repository.neo4j_client.run_cypher", fake_run
    )

    user = await user_repository.get_user_by_email("a@x.com")
    assert user.is_suspended is False
    assert user.suspended_at is None
    assert user.suspended_reason is None
