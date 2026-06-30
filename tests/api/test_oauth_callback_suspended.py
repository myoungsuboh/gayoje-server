"""GitHub callback — 정지된 사용자는 FE 로 ?error=suspended redirect."""
from __future__ import annotations
from typing import Optional
import pytest

from app.api import auth_routes
from app.service.user_repository import UserInDB, UserPublic

pytestmark = pytest.mark.asyncio


def _public(email="a@x.com") -> UserPublic:
    return UserPublic(id="1", email=email, name="A",
                      subscription_type="free", is_admin=False)


def _indb(*, is_suspended=False, reason: Optional[str] = None) -> UserInDB:
    return UserInDB(
        id="1", email="a@x.com", name="A", hashed_password="",
        subscription_type="free", is_admin=False,
        is_suspended=is_suspended, suspended_reason=reason,
    )


@pytest.fixture
def common_github_mocks(monkeypatch):
    class _S:
        github_oauth_enabled = True
        admin_emails_list = []
        github_oauth_scopes_list = []
        FRONTEND_OAUTH_CALLBACK_URL = "https://app/callback"
    monkeypatch.setattr("app.api.auth_routes.settings", _S, raising=False)

    monkeypatch.setattr(
        "app.api.auth_routes.github_oauth.verify_state_token",
        lambda s: {"mode": "login"},
    )
    async def fake_exchange(code): return "gh-token"
    monkeypatch.setattr(
        "app.api.auth_routes.github_oauth.exchange_code_for_token", fake_exchange
    )
    async def fake_fetch(token):
        return {"github_id": 1, "login": "x", "email": "a@x.com", "name": "A"}
    monkeypatch.setattr(
        "app.api.auth_routes.github_oauth.fetch_github_user", fake_fetch
    )
    return monkeypatch


async def test_github_callback_suspended_redirects_with_error(common_github_mocks):
    monkeypatch = common_github_mocks
    async def fake_find_by_gh(gh_id): return _public()
    monkeypatch.setattr(
        "app.api.auth_routes.users.find_by_github_id", fake_find_by_gh
    )
    async def fake_get_by_email(email):
        return _indb(is_suspended=True, reason="abuse")
    monkeypatch.setattr(
        "app.api.auth_routes.users.get_user_by_email", fake_get_by_email
    )

    resp = await auth_routes.github_callback_route(code="c", state="s", error=None)
    loc = resp.headers.get("location", "")
    # error 파라미터에 suspended 가 포함되어야 함
    assert "suspended" in loc
    # 토큰이 발급되지 않아야 함
    assert "access_token=" not in loc
