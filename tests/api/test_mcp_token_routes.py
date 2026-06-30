"""mcp_token_routes 단위 테스트.

라우트는 Depends(get_current_user) 를 쓰므로 user 는 직접 주입,
repository / token_blacklist 는 mock.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import mcp_token_routes as routes
from app.service.user_repository import UserPublic

pytestmark = pytest.mark.asyncio


def _user(email: str = "u@e.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="t",
        subscription_type="free", is_admin=False, auto_progress=True,
    )


async def test_issue_returns_plaintext_token_once(monkeypatch):
    captured = {}

    def fake_create(email, exp_days):
        captured["email"] = email
        return ("plaintext-token", "jti-1")

    async def fake_repo(*, email, jti, label, exp_days):
        captured["jti"] = jti
        captured["label"] = label
        from app.service.mcp_token_repository import McpTokenRow
        return McpTokenRow(
            jti=jti, label=label,
            created_at="2026-05-18T00:00:00+00:00",
            expires_at="2026-08-16T00:00:00+00:00",
            revoked=False,
        )

    monkeypatch.setattr(routes.security, "create_mcp_token", fake_create)
    monkeypatch.setattr(
        routes.mcp_token_repository, "create_mcp_token_record", fake_repo
    )

    from app.schemas import McpTokenIssueRequest
    resp = await routes.issue_mcp_token_route(
        McpTokenIssueRequest(label="노트북-Cursor"), current_user=_user(),
    )
    assert resp.token == "plaintext-token"
    assert resp.jti == "jti-1"
    assert captured["label"] == "노트북-Cursor"


async def test_issue_returns_400_on_limit_exceeded(monkeypatch):
    from app.service.mcp_token_repository import McpTokenLimitExceeded
    monkeypatch.setattr(routes.security, "create_mcp_token", lambda e, exp_days: ("t", "j"))

    async def fake_repo(**kw):
        raise McpTokenLimitExceeded("limit")
    monkeypatch.setattr(
        routes.mcp_token_repository, "create_mcp_token_record", fake_repo
    )

    from app.schemas import McpTokenIssueRequest
    with pytest.raises(HTTPException) as exc:
        await routes.issue_mcp_token_route(
            McpTokenIssueRequest(label="L"), current_user=_user(),
        )
    assert exc.value.status_code == 400


async def test_list_returns_summaries(monkeypatch):
    from app.service.mcp_token_repository import McpTokenRow
    async def fake_list(email):
        return [McpTokenRow(
            jti="j1", label="A",
            created_at="2026-05-01T00:00:00+00:00",
            last_used_at=None,
            expires_at="2026-08-01T00:00:00+00:00",
            revoked=False,
        )]
    monkeypatch.setattr(
        routes.mcp_token_repository, "list_tokens_for_user", fake_list
    )
    resp = await routes.list_mcp_tokens_route(current_user=_user())
    assert len(resp) == 1
    assert resp[0].jti == "j1"


async def test_revoke_calls_blacklist_before_mark_revoked(monkeypatch):
    """race 안전: Redis 가 Neo4j 마킹보다 먼저 호출되어야 한다."""
    call_order: list[str] = []

    async def fake_peek(email, jti):
        call_order.append("peek")
        return 99999

    async def fake_blacklist(jti, exp_epoch):
        call_order.append("blacklist")
        assert exp_epoch == 99999

    async def fake_mark(email, jti):
        call_order.append("mark")
        return True

    monkeypatch.setattr(
        routes.mcp_token_repository, "peek_revoke_target", fake_peek
    )
    monkeypatch.setattr(routes.token_blacklist, "revoke", fake_blacklist)
    monkeypatch.setattr(
        routes.mcp_token_repository, "mark_token_revoked", fake_mark
    )

    await routes.revoke_mcp_token_route(jti="j1", current_user=_user())
    assert call_order == ["peek", "blacklist", "mark"]


async def test_revoke_returns_404_when_not_owner(monkeypatch):
    async def fake_peek(email, jti):
        return None  # 소유 아님 / 이미 회수
    monkeypatch.setattr(
        routes.mcp_token_repository, "peek_revoke_target", fake_peek
    )
    with pytest.raises(HTTPException) as exc:
        await routes.revoke_mcp_token_route(jti="ghost", current_user=_user())
    assert exc.value.status_code == 404
