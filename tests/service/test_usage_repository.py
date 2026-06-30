"""
usage_repository 단위 테스트 — Neo4j Cypher 모킹.

[검증 범위]
- get_usage: row 정규화 + 사용자 없을 때 None
- try_increment_meeting_count: exceeded=True 시 SET 없음 / False 시 +1
- add_tokens / add_chars: 누적 + 음수 입력 방어
- reset_usage: admin 초기화 cypher
- Cypher 안에 정책(한도 숫자) baked-in 되지 않았는지 — 정책 변경 회귀 가드
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import usage_repository
from app.service.usage_repository import (
    IncrementResult,
    Usage,
    add_chars,
    add_tokens,
    get_usage,
    reset_usage,
    try_increment_meeting_count,
)

pytestmark = pytest.mark.asyncio


class _FakeRunCypher:
    """neo4j_client.run_cypher 대체. 호출 기록 + 미리 큐잉된 응답 반환."""

    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(
        self,
        cypher: str,
        params: Optional[Dict[str, Any]] = None,
        database: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self.calls.append({"cypher": cypher, "params": params or {}, "database": database})
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses: Optional[List[List[Dict[str, Any]]]] = None) -> _FakeRunCypher:
        fake = _FakeRunCypher(responses=responses)
        monkeypatch.setattr(
            "app.service.usage_repository.neo4j_client.run_cypher", fake
        )
        return fake

    return _setup


# ─── get_usage ──────────────────────────────────────────────


async def test_get_usage_returns_normalized_usage(fake_run):
    fake = fake_run(
        [
            [
                {
                    "usage": {
                        "email": "a@b.com",
                        "subscription_type": "pro",
                        "meeting_count": 7,
                        "total_tokens": 12345,
                        "total_chars": 98765,
                        "reset_at": "2026-06-17T00:00:00.000000000Z",
                    }
                }
            ]
        ]
    )
    out = await get_usage("a@b.com")
    assert isinstance(out, Usage)
    assert out.email == "a@b.com"
    assert out.subscription_type == "pro"
    assert out.meeting_count == 7
    assert out.total_tokens == 12345
    assert out.total_chars == 98765
    # 2026-05 reset_at 매핑
    assert out.reset_at == "2026-06-17T00:00:00.000000000Z"
    # email 파라미터 바인딩 확인
    assert fake.calls[0]["params"] == {"email": "a@b.com"}


async def test_get_usage_returns_none_when_user_missing(fake_run):
    fake_run([[]])
    assert await get_usage("ghost@example.com") is None


# ─── 2026-05 월간 reset ──────────────────────────────────


async def test_get_usage_reset_at_optional(fake_run):
    """legacy 데이터 — reset_at 없는 응답도 안전 처리 (None)."""
    fake_run(
        [
            [
                {
                    "usage": {
                        "email": "legacy@b.com",
                        "subscription_type": "free",
                        "meeting_count": 0,
                        "total_tokens": 0,
                        "total_chars": 0,
                        # reset_at 누락 (legacy)
                    }
                }
            ]
        ]
    )
    out = await get_usage("legacy@b.com")
    assert out is not None
    assert out.reset_at is None


async def test_get_usage_cypher_contains_reset_logic(fake_run):
    """get_usage cypher 안에 월간 reset self-healing 로직 임베드 — 회귀 가드."""
    fake = fake_run(
        [[{"usage": {"email": "a@b.com", "subscription_type": "free",
                     "meeting_count": 0, "total_tokens": 0, "total_chars": 0,
                     "reset_at": None}}]]
    )
    await get_usage("a@b.com")
    cypher = fake.calls[0]["cypher"]
    # reset_at 필드 체크
    assert "usage_reset_at" in cypher
    # 자동 reset 로직 — duration months 사용
    assert "duration({months: 1})" in cypher or "duration({ months: 1})" in cypher
    # FOREACH + CASE conditional reset
    assert "FOREACH" in cypher


async def test_get_usage_maps_subscription_ends_at(fake_run):
    """[2026-06 기간제] get_usage 가 subscription_ends_at 을 매핑."""
    fake_run(
        [[{"usage": {"email": "a@b.com", "subscription_type": "pro",
                     "meeting_count": 0, "total_tokens": 0, "total_chars": 0,
                     "subscription_ends_at": "2026-07-17T00:00:00.000000000Z"}}]]
    )
    out = await get_usage("a@b.com")
    assert out is not None
    assert out.subscription_ends_at == "2026-07-17T00:00:00.000000000Z"


async def test_get_usage_cypher_has_expiry_self_heal(fake_run):
    """[2026-06 회귀 가드] get_usage cypher 에 '만료 시 free 강등' self-heal 절이 임베드.
    (FOREACH 실동작은 testcontainers 가 검증 — fake 는 cypher 텍스트만 가드.)"""
    fake = fake_run(
        [[{"usage": {"email": "a@b.com", "subscription_type": "free",
                     "meeting_count": 0, "total_tokens": 0, "total_chars": 0}}]]
    )
    await get_usage("a@b.com")
    cypher = fake.calls[0]["cypher"]
    assert "datetime() >= u.subscription_ends_at" in cypher
    assert "u.subscription_type = 'free'" in cypher
    assert "u.subscription_ends_at = null" in cypher


async def test_try_increment_includes_reset_at(fake_run):
    """try_increment_meeting_count 결과에 reset_at 포함 (한도 초과 응답에 사용)."""
    fake_run(
        [
            [
                {
                    "result": {
                        "exceeded": True,
                        "current": 5,
                        "limit": 5,
                        "subscription_type": "free",
                        "reset_at": "2026-06-17T00:00:00.000000000Z",
                    }
                }
            ]
        ]
    )
    r = await try_increment_meeting_count("a@b.com", 5)
    assert r is not None
    assert r.exceeded is True
    assert r.reset_at == "2026-06-17T00:00:00.000000000Z"


async def test_reset_usage_cypher_does_not_touch_reset_at(fake_run):
    """
    [BUG 3 정책] admin reset_usage 는 카운터만 0 으로 리셋하고 reset_at 은 건드리지
    않음. 사용자 cycle 은 유지 ("이번 cycle 살려주자" 의도). 새 cycle 부여하려면
    admin 이 등급 변경 (change_subscription) 사용 — 그 cypher 가 reset_at 갱신.
    """
    fake = fake_run([[{"email": "a@b.com"}]])
    await reset_usage("a@b.com")
    cypher = fake.calls[0]["cypher"]
    # reset_at SET 없어야 함 (cycle 유지 정책)
    assert "u.usage_reset_at" not in cypher, (
        "admin reset_usage cypher 가 reset_at 을 건드리면 abuse 가능 — "
        "관리자가 무한 reset 호출 → 사용자 cycle 무한 확장."
    )
    # 카운터 3종은 모두 0 으로
    assert "u.usage_meeting_count = 0" in cypher
    assert "u.usage_total_tokens = 0" in cypher
    assert "u.usage_total_chars = 0" in cypher


async def test_get_usage_defaults_to_free_and_zero(fake_run):
    """노드는 있지만 usage_* 필드가 아직 없는 기존 사용자 — COALESCE 로 0/free default."""
    fake_run(
        [
            [
                {
                    "usage": {
                        "email": "old@b.com",
                        "subscription_type": "free",
                        "meeting_count": 0,
                        "total_tokens": 0,
                        "total_chars": 0,
                    }
                }
            ]
        ]
    )
    out = await get_usage("old@b.com")
    assert out is not None
    assert out.meeting_count == 0
    assert out.subscription_type == "free"


async def test_get_usage_handles_null_usage_field(fake_run):
    """row 는 있지만 usage 가 null (방어적)."""
    fake_run([[{"usage": None}]])
    assert await get_usage("x@y.com") is None


# ─── try_increment_meeting_count ────────────────────────────


async def test_try_increment_under_limit_returns_not_exceeded(fake_run):
    """현재 0, 한도 5 → exceeded=False, current=1."""
    fake = fake_run(
        [
            [
                {
                    "result": {
                        "exceeded": False,
                        "current": 1,
                        "limit": 5,
                        "subscription_type": "free",
                    }
                }
            ]
        ]
    )
    out = await try_increment_meeting_count("a@b.com", limit=5)
    assert isinstance(out, IncrementResult)
    assert out.exceeded is False
    assert out.current == 1
    assert out.limit == 5
    # cypher 가 limit 을 param 으로 받았는지 (cypher 안에 5 가 하드코딩 안 됐는지)
    assert fake.calls[0]["params"] == {"email": "a@b.com", "limit": 5}


async def test_try_increment_at_limit_returns_exceeded(fake_run):
    """현재 5, 한도 5 → exceeded=True, current 유지."""
    fake_run(
        [
            [
                {
                    "result": {
                        "exceeded": True,
                        "current": 5,
                        "limit": 5,
                        "subscription_type": "free",
                    }
                }
            ]
        ]
    )
    out = await try_increment_meeting_count("a@b.com", limit=5)
    assert out is not None
    assert out.exceeded is True
    assert out.current == 5


async def test_try_increment_returns_none_when_user_missing(fake_run):
    fake_run([[]])
    out = await try_increment_meeting_count("ghost@x.com", limit=5)
    assert out is None


async def test_try_increment_cypher_has_no_hardcoded_limits(fake_run):
    """Cypher 안에 정책(숫자) baked-in 되지 않았는지 — 정책 변경 회귀 가드.

    한도가 cypher param 으로 들어가야 함. cypher 본문에 5, 100, 5000 등이 나오면
    정책이 cypher 에 박혔다는 신호 — 발견 즉시 거부.
    """
    fake = fake_run(
        [[{"result": {"exceeded": False, "current": 1, "limit": 5, "subscription_type": "free"}}]]
    )
    await try_increment_meeting_count("a@b.com", limit=5)
    cypher = fake.calls[0]["cypher"]
    # FOREACH + CASE WHEN 조건부 SET 패턴 사용
    assert "FOREACH" in cypher
    assert "exceeded" in cypher
    # 한도 숫자가 cypher 본문에 박혀있으면 안 됨 (param 만 사용)
    for hardcoded in (" 5 ", " 100 ", " 5000 ", " 100000 ", " 5000000 "):
        assert hardcoded not in cypher, (
            f"한도 값 {hardcoded!r} 가 cypher 에 하드코딩됨 — param $limit 사용해야 함"
        )


async def test_try_increment_atomic_check_and_set_in_single_cypher(fake_run):
    """한 번의 cypher 호출 안에서 check + SET 이 모두 끝나는지 (race 안전성 핵심)."""
    fake = fake_run(
        [[{"result": {"exceeded": False, "current": 1, "limit": 5, "subscription_type": "free"}}]]
    )
    await try_increment_meeting_count("a@b.com", limit=5)
    # 단일 cypher 호출
    assert len(fake.calls) == 1
    # cypher 안에 MATCH + WITH + FOREACH(SET) 가 모두 있어야 함
    c = fake.calls[0]["cypher"]
    assert "MATCH (u:User" in c
    assert "SET u.usage_meeting_count" in c


# ─── add_tokens ────────────────────────────────────────────


async def test_add_tokens_accumulates(fake_run):
    fake = fake_run([[{"total": 12345}]])
    out = await add_tokens("a@b.com", 100)
    assert out == 12345
    assert fake.calls[0]["params"] == {"email": "a@b.com", "delta": 100}
    assert "COALESCE(u.usage_total_tokens, 0) + $delta" in fake.calls[0]["cypher"]


async def test_add_tokens_ignores_zero(fake_run):
    """delta 0 은 cypher 호출 안 함 — 불필요한 라운드트립 회피."""
    fake = fake_run([[{"total": 0}]])
    out = await add_tokens("a@b.com", 0)
    assert out is None
    assert len(fake.calls) == 0


async def test_add_tokens_ignores_negative(fake_run, caplog):
    """음수 입력 (잘못된 LLM usage 응답 방어) — warning + no-op."""
    fake = fake_run([[{"total": 100}]])
    out = await add_tokens("a@b.com", -50)
    assert out is None
    assert len(fake.calls) == 0


async def test_add_tokens_returns_none_when_user_missing(fake_run):
    fake_run([[]])
    out = await add_tokens("ghost@x.com", 100)
    assert out is None


async def test_add_tokens_lite_bucket_uses_lite_cypher(fake_run):
    """[2026-06] bucket='lite' → lite cypher (월간 lite_tokens + 일일 lite_daily 동시 +N)."""
    fake = fake_run([[{"daily_total": 700_000}]])
    out = await add_tokens("a@b.com", 50_000, bucket="lite")
    assert out == 700_000  # 일일 누적 반환
    cypher = fake.calls[0]["cypher"]
    assert "usage_lite_tokens" in cypher and "usage_lite_daily_tokens" in cypher
    assert "usage_lite_daily_reset_at" in cypher  # 일일 self-healing reset


async def test_add_tokens_main_bucket_default(fake_run):
    """bucket 미지정 → main (기존 동작, total_tokens cypher)."""
    fake = fake_run([[{"total": 999}]])
    out = await add_tokens("a@b.com", 100)
    assert out == 999
    assert "usage_total_tokens" in fake.calls[0]["cypher"]
    assert "usage_lite_daily_tokens" not in fake.calls[0]["cypher"]


# ─── add_chars ─────────────────────────────────────────────


async def test_add_chars_accumulates(fake_run):
    fake = fake_run([[{"total": 5000}]])
    out = await add_chars("a@b.com", 1234)
    assert out == 5000
    assert fake.calls[0]["params"] == {"email": "a@b.com", "delta": 1234}


async def test_add_chars_ignores_non_positive(fake_run):
    fake_run([])  # 호출 자체가 없어야 함
    assert await add_chars("a@b.com", 0) is None
    assert await add_chars("a@b.com", -10) is None


# ─── reset_usage ──────────────────────────────────────────


async def test_reset_usage_zeroes_all_counters(fake_run):
    fake = fake_run([[{"email": "a@b.com"}]])
    ok = await reset_usage("a@b.com")
    assert ok is True
    cypher = fake.calls[0]["cypher"]
    assert "u.usage_meeting_count = 0" in cypher
    assert "u.usage_total_tokens = 0" in cypher
    assert "u.usage_total_chars = 0" in cypher


async def test_reset_usage_returns_false_when_user_missing(fake_run):
    fake_run([[]])
    assert await reset_usage("ghost@x.com") is False


# ─── Cypher injection safety ────────────────────────────────


async def test_email_is_parameterized(fake_run):
    """모든 함수가 email 을 $email 로만 받고 cypher 본문에 보간하지 않는지."""
    dangerous = "x@y.com'} ) DETACH DELETE u //"
    fake = fake_run(
        [
            [],  # get_usage
            [],  # try_increment
            [],  # add_tokens (delta>0 라 호출됨)
            [],  # reset
        ]
    )
    await get_usage(dangerous)
    await try_increment_meeting_count(dangerous, limit=5)
    await add_tokens(dangerous, 100)
    await reset_usage(dangerous)
    for call in fake.calls:
        assert dangerous not in call["cypher"]
        assert call["params"]["email"] == dangerous
