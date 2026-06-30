"""mcp_token_repository 단위 테스트.

Neo4j 는 monkeypatch 로 인메모리 fake 사용 — 실제 DB 없이 cypher 입력만 검증.
"""
from __future__ import annotations

import pytest

from app.service import mcp_token_repository as repo

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_run(monkeypatch):
    """run_cypher 호출을 (query, params) 튜플 리스트로 캡처."""
    calls: list[tuple[str, dict]] = []

    async def _run(query: str, params: dict | None = None):
        calls.append((query, params or {}))
        return []  # cypher 결과는 각 테스트에서 override

    monkeypatch.setattr(
        "app.service.mcp_token_repository.neo4j_client.run_cypher", _run
    )
    return calls


async def test_ensure_constraints_runs_constraint_cypher(fake_run):
    await repo.ensure_constraints()
    assert any("mcp_token_jti_unique" in q for q, _ in fake_run)


async def test_ensure_constraints_swallows_errors(monkeypatch):
    """Neo4j 미연결이어도 부팅 막지 않음 — warning 로그만."""

    async def _raise(*a, **kw):
        raise RuntimeError("NEO4J_URI not set")

    monkeypatch.setattr(
        "app.service.mcp_token_repository.neo4j_client.run_cypher", _raise
    )
    # 예외 안 던져야 함
    await repo.ensure_constraints()


async def test_create_mcp_token_record_returns_row(monkeypatch):
    fixed_now = "2026-05-18T10:00:00Z"
    fixed_jti = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(repo, "_utc_now_iso", lambda: fixed_now)
    monkeypatch.setattr(repo, "_expires_at_iso", lambda days: "2026-08-16T10:00:00Z")

    # CREATE cypher 응답을 모방 — 첫 호출은 count, 두 번째는 CREATE 반환
    async def _run(query: str, params: dict | None = None):
        if "count(t)" in query.lower():
            return [{"active": 0}]
        return [{
            "row": {
                "jti": fixed_jti,
                "label": "노트북-Cursor",
                "created_at": fixed_now,
                "last_used_at": None,
                "expires_at": "2026-08-16T10:00:00Z",
                "revoked": False,
            }
        }]
    monkeypatch.setattr(
        "app.service.mcp_token_repository.neo4j_client.run_cypher", _run
    )

    row = await repo.create_mcp_token_record(
        email="u@e.com", jti=fixed_jti, label="노트북-Cursor", exp_days=90,
    )
    assert row.jti == fixed_jti
    assert row.label == "노트북-Cursor"
    assert row.revoked is False


async def test_create_mcp_token_record_rejects_when_limit_exceeded(monkeypatch):
    async def _run(query: str, params: dict | None = None):
        if "count(t)" in query.lower():
            return [{"active": repo.MAX_ACTIVE_TOKENS_PER_USER}]
        return []
    monkeypatch.setattr(
        "app.service.mcp_token_repository.neo4j_client.run_cypher", _run
    )

    with pytest.raises(repo.McpTokenLimitExceeded):
        await repo.create_mcp_token_record(
            email="u@e.com", jti="x", label="L", exp_days=90,
        )


async def test_list_tokens_returns_user_rows(monkeypatch):
    captured = {}
    async def _run(query, params=None):
        captured["q"] = query
        captured["p"] = params
        return [{
            "row": {
                "jti": "j1", "label": "A",
                "created_at": "2026-05-01T00:00:00Z",
                "last_used_at": "2026-05-17T00:00:00Z",
                "expires_at": "2026-08-01T00:00:00Z",
                "revoked": False,
            }
        }]
    monkeypatch.setattr(
        "app.service.mcp_token_repository.neo4j_client.run_cypher", _run
    )
    rows = await repo.list_tokens_for_user("u@e.com")
    assert len(rows) == 1
    assert rows[0].jti == "j1"
    assert captured["p"] == {"email": "u@e.com"}


async def test_peek_revoke_target_returns_exp_when_owner(monkeypatch):
    async def _run(query, params=None):
        if params.get("email") == "owner@e.com":
            return [{"expires_at": "2026-08-01T00:00:00+00:00"}]
        return []
    monkeypatch.setattr(
        "app.service.mcp_token_repository.neo4j_client.run_cypher", _run
    )
    exp = await repo.peek_revoke_target("owner@e.com", "j1")
    assert isinstance(exp, int)
    assert exp > 0

    miss = await repo.peek_revoke_target("intruder@e.com", "j1")
    assert miss is None


async def test_mark_token_revoked_returns_true_on_success(monkeypatch):
    async def _run(query, params=None):
        if "SET t.revoked = true" in query:
            return [{"jti": params["jti"]}] if params["jti"] == "j1" else []
        return []
    monkeypatch.setattr(
        "app.service.mcp_token_repository.neo4j_client.run_cypher", _run
    )
    assert await repo.mark_token_revoked("owner@e.com", "j1") is True
    assert await repo.mark_token_revoked("owner@e.com", "ghost") is False


async def test_touch_last_used_is_silent_on_missing(monkeypatch):
    async def _run(query, params=None):
        return []
    monkeypatch.setattr(
        "app.service.mcp_token_repository.neo4j_client.run_cypher", _run
    )
    # 존재하지 않는 jti 도 예외 없이 통과 (best-effort)
    await repo.touch_last_used("ghost-jti")


async def test_touch_last_used_swallows_exceptions(monkeypatch):
    async def _raise(*a, **kw):
        raise RuntimeError("neo4j down")
    monkeypatch.setattr(
        "app.service.mcp_token_repository.neo4j_client.run_cypher", _raise
    )
    # 예외 안 던지고 정상 리턴
    await repo.touch_last_used("any")
