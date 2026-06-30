"""
admin_repository 단위 테스트 — 사용자 목록/상세, 구독 변경, admin 토글의 핵심 로직.

특히 결제와 직접 연결되는 last-admin 보호 + race-safe atomic cypher 가 정상 동작하는지
검증한다. neo4j_client.run_cypher 를 fake 로 대체.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import admin_repository
from app.service.admin_repository import AdminUserRow

pytestmark = pytest.mark.asyncio


class _FakeRunCypher:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(
        self,
        cypher: str,
        params: Optional[Dict[str, Any]] = None,
        database: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self.calls.append({"cypher": cypher, "params": params or {}})
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses=None) -> _FakeRunCypher:
        fake = _FakeRunCypher(responses=responses)
        monkeypatch.setattr(
            "app.service.admin_repository.neo4j_client.run_cypher", fake
        )
        return fake

    return _setup


# ─── list_users ────────────────────────────────────────────────


async def test_list_users_normalizes_query_and_pagination(fake_run):
    """검색어는 lowercase 로 정규화 + limit/offset clamping."""
    fake = fake_run([
        [{"user": _admin_row("a@x.com", is_admin=True, sub="pro")}],
        [{"total": 1}],
    ])
    out = await admin_repository.list_users(q=" Foo ", limit=99999, offset=-5)
    assert out["total"] == 1
    assert len(out["users"]) == 1
    assert out["users"][0].email == "a@x.com"
    assert out["limit"] == 200  # clamp to max
    assert out["offset"] == 0   # clamp to >=0
    # 1st cypher = LIST with normalized q
    assert fake.calls[0]["params"]["q"] == "foo"


async def test_list_users_empty_q_returns_all(fake_run):
    fake_run([[], [{"total": 0}]])
    out = await admin_repository.list_users(q="")
    assert out["total"] == 0
    assert out["users"] == []


# ─── change_subscription ───────────────────────────────────────


async def test_change_subscription_records_history(fake_run):
    """구독 변경은 user 갱신 + change 노드 + 메타 반환을 모두 검증."""
    fake = fake_run([[{
        "result": {
            "user": _admin_row("u@x.com", is_admin=False, sub="pro"),
            "change": {
                "id": "ch-1", "from_type": "free", "to_type": "pro",
                "reason": "promo", "changed_by_email": "admin@x.com",
                "changed_at": "2026-01-01T00:00:00",
            },
        }
    }]])
    out = await admin_repository.change_subscription(
        target_email="u@x.com", to_type="pro",
        reason="promo", changed_by_email="admin@x.com",
    )
    assert out is not None
    assert out["user"].subscription_type == "pro"
    assert out["change"].from_type == "free"
    assert out["change"].to_type == "pro"
    assert out["change"].reason == "promo"
    # cypher 가 from_type 을 user 의 현재 값에서 가져오는지 확인
    assert "from_type: from_type" in fake.calls[0]["cypher"]


async def test_change_subscription_resets_reset_at_only(fake_run):
    """
    [BUG 2 회귀] 등급 변경 cypher 는 reset_at 을 갱신 (새 cycle 시작) 하되,
    카운터(usage_meeting_count / usage_total_tokens / usage_total_chars)는 건드리지
    않아야 함. 정책: 결제 시 누적 가치 보존 + reset_at 새로.
    """
    fake = fake_run([[{
        "result": {
            "user": _admin_row("u@x.com", is_admin=False, sub="pro"),
            "change": {
                "id": "ch-1", "from_type": "free", "to_type": "pro",
                "reason": "promo", "changed_by_email": "admin@x.com",
                "changed_at": "2026-01-01T00:00:00",
            },
        }
    }]])
    await admin_repository.change_subscription(
        target_email="u@x.com", to_type="pro",
        reason=None, changed_by_email="admin@x.com",
    )
    cypher = fake.calls[0]["cypher"]
    # reset_at 은 갱신
    assert "u.usage_reset_at = datetime() + duration({months: 1})" in cypher, (
        "등급 변경 시 reset_at 이 새로 설정돼야 함 — 새 등급 한도가 적용되는 새 cycle."
    )
    # 카운터는 절대 건드리지 않음 — 가치 보존 정책
    assert "u.usage_meeting_count = 0" not in cypher, (
        "등급 변경 cypher 가 meeting_count 를 0 으로 리셋하면 안 됨 — 정책: 카운터 보존."
    )
    assert "u.usage_total_tokens = 0" not in cypher
    assert "u.usage_total_chars = 0" not in cypher


async def test_change_subscription_duration_sets_ends_at(fake_run):
    """[2026-06 기간제] duration_months 를 cypher 파라미터로 전달 + ends_at CASE 존재."""
    fake = fake_run([[{
        "result": {
            "user": _admin_row("u@x.com", is_admin=False, sub="pro"),
            "change": {
                "id": "ch-1", "from_type": "free", "to_type": "pro",
                "reason": None, "changed_by_email": "admin@x.com",
                "changed_at": "2026-01-01T00:00:00",
            },
        }
    }]])
    await admin_repository.change_subscription(
        target_email="u@x.com", to_type="pro",
        reason=None, changed_by_email="admin@x.com", duration_months=1,
    )
    assert fake.calls[0]["params"]["duration_months"] == 1
    cypher = fake.calls[0]["cypher"]
    assert "u.subscription_ends_at = CASE" in cypher
    assert "duration({months: $duration_months})" in cypher
    # free 또는 영구(None)면 null — CASE 조건 회귀 가드
    assert "$duration_months IS NULL OR $to_type = 'free' THEN null" in cypher


async def test_change_subscription_permanent_when_duration_omitted(fake_run):
    """duration_months 미지정 = 영구 → 파라미터 None (Paddle 웹훅 경로가 이 기본을 씀)."""
    fake = fake_run([[{
        "result": {
            "user": _admin_row("u@x.com", is_admin=False, sub="pro"),
            "change": {
                "id": "ch-1", "from_type": "free", "to_type": "pro",
                "reason": None, "changed_by_email": "admin@x.com",
                "changed_at": "2026-01-01T00:00:00",
            },
        }
    }]])
    await admin_repository.change_subscription(
        target_email="u@x.com", to_type="pro",
        reason=None, changed_by_email="admin@x.com",
    )
    assert fake.calls[0]["params"]["duration_months"] is None


async def test_change_subscription_rejects_invalid_type():
    with pytest.raises(ValueError, match="invalid subscription_type"):
        await admin_repository.change_subscription(
            target_email="u@x.com", to_type="enterprise",
            reason=None, changed_by_email="admin@x.com",
        )


# ─── set_admin (핵심: atomic last-admin 보호) ──────────────────


async def test_set_admin_promotes_ok(fake_run):
    fake_run([[{
        "result": {"status": "ok", "user": _admin_row("u@x.com", is_admin=True)}
    }]])
    out = await admin_repository.set_admin(target_email="u@x.com", is_admin=True)
    assert out["status"] == "ok"
    assert out["user"].is_admin is True


async def test_set_admin_blocks_last_admin(fake_run):
    """cypher 가 last_admin 상태를 반환하면 status='last_admin' + 메시지."""
    fake_run([[{
        "result": {"status": "last_admin", "message": "마지막 관리자입니다."}
    }]])
    out = await admin_repository.set_admin(target_email="me@x.com", is_admin=False)
    assert out["status"] == "last_admin"
    assert "관리자" in (out.get("message") or "")
    assert "user" not in out


async def test_set_admin_returns_not_found_on_empty(fake_run):
    fake_run([[]])
    out = await admin_repository.set_admin(target_email="missing@x.com", is_admin=True)
    assert out["status"] == "not_found"


async def test_set_admin_cypher_is_atomic(fake_run):
    """admin_count 계산 → conditional SET → 반환이 단일 cypher 안에 묶였는지 회귀."""
    fake = fake_run([[{
        "result": {"status": "ok", "user": _admin_row("u@x.com")}
    }]])
    await admin_repository.set_admin(target_email="u@x.com", is_admin=False)
    cypher = fake.calls[0]["cypher"]
    # 핵심 회귀 가드: 동일 cypher 안에 count + would_orphan + SET 이 모두 있어야 함
    assert "WHERE a.is_admin = true" in cypher
    assert "would_orphan" in cypher
    assert "SET u.is_admin = $is_admin" in cypher


# ─── count_admins ─────────────────────────────────────────────


async def test_count_admins(fake_run):
    fake_run([[{"admins": 3}]])
    n = await admin_repository.count_admins()
    assert n == 3


# ─── helpers ──────────────────────────────────────────────────


def _admin_row(
    email: str,
    *,
    is_admin: bool = False,
    sub: str = "free",
) -> Dict[str, Any]:
    """admin_repository 가 기대하는 user dict 형태."""
    return {
        "id": "id-" + email,
        "email": email,
        "name": email.split("@")[0],
        "github_username": "",
        "subscription_type": sub,
        "subscription_updated_at": "2026-01-01T00:00:00",
        "is_admin": is_admin,
        "created_at": "2025-12-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }


# ─── 토큰 사용% (관리자 대시보드 — 2026-05-27) ───────────────────────
def test_row_to_admin_user_fills_token_usage_when_present():
    """목록 row 에 usage_total_tokens 가 있으면 등급 한도 대비 token_pct 채움."""
    from app.service.admin_repository import _row_to_admin_user
    from app.core.quota import get_limit

    row = {
        "email": "u@x.com",
        "subscription_type": "free",
        "usage_total_tokens": 50_000,
    }
    user = _row_to_admin_user(row)
    assert user is not None
    assert user.token_used == 50_000
    assert user.token_limit == get_limit("free", "total_tokens")
    assert user.token_pct == round(50_000 / user.token_limit * 100, 1)


def test_row_to_admin_user_token_none_when_absent():
    """usage_total_tokens 없는 row(detail/change)는 token 필드 None."""
    from app.service.admin_repository import _row_to_admin_user

    user = _row_to_admin_user({"email": "u@x.com", "subscription_type": "pro"})
    assert user is not None
    assert user.token_used is None
    assert user.token_pct is None
