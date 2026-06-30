"""
[Phase 3 — 2026-05-18] session_registry 단위 테스트.

검증:
- record / unregister 기본 흐름
- list_sessions 정상 / 빈 케이스 / 만료 정리
- get_session_email 회수
- Redis 실패 시 fail-open (raise 없음)
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.core import session_registry


pytestmark = pytest.mark.asyncio


class _FakeRedis:
    """Redis async client 의 hset/expireat/zadd/zrange/hgetall/zremrangebyscore/zrem/delete/hget 모킹."""

    def __init__(self):
        # key -> hash dict OR sorted set as list[(member, score)]
        self.hashes: Dict[str, Dict[str, Any]] = {}
        self.zsets: Dict[str, List[tuple]] = {}
        self.failed = False

    async def hset(self, key, mapping=None, **_):
        if self.failed:
            raise RuntimeError("redis down")
        self.hashes.setdefault(key, {})
        for k, v in (mapping or {}).items():
            self.hashes[key][k] = v

    async def expireat(self, key, when):
        pass  # simulated — fake ignores TTL

    async def zadd(self, key, mapping):
        zs = self.zsets.setdefault(key, [])
        for member, score in mapping.items():
            # remove existing same member, then add
            zs[:] = [(m, s) for (m, s) in zs if m != member]
            zs.append((member, score))

    async def zrange(self, key, start, stop):
        zs = self.zsets.get(key, [])
        zs_sorted = sorted(zs, key=lambda x: x[1])
        members = [m for m, _ in zs_sorted]
        if stop == -1:
            return members[start:]
        return members[start:stop + 1]

    async def zrem(self, key, *members):
        zs = self.zsets.get(key, [])
        before = len(zs)
        self.zsets[key] = [(m, s) for (m, s) in zs if m not in members]
        return before - len(self.zsets[key])

    async def zremrangebyscore(self, key, min_, max_):
        zs = self.zsets.get(key, [])
        before = len(zs)
        self.zsets[key] = [(m, s) for (m, s) in zs if not (min_ <= s <= max_)]
        return before - len(self.zsets[key])

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def delete(self, *keys):
        for k in keys:
            self.hashes.pop(k, None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    async def _get_redis():
        return r
    monkeypatch.setattr(
        "app.core.session_registry._redis", _get_redis
    )
    return r


# ─── record_session ─────────────────────────────────────────


async def test_record_session_writes_hash_and_zset(fake_redis):
    """record_session → HASH + ZSET 동시 기록."""
    await session_registry.record_session(
        email="alice@example.com",
        jti="jti-a",
        exp_epoch=9999999999,
        user_agent="Mozilla",
        ip="1.2.3.4",
        device_label="Chrome on macOS",
    )
    assert "session:jti-a" in fake_redis.hashes
    h = fake_redis.hashes["session:jti-a"]
    assert h["email"] == "alice@example.com"
    assert h["ua"] == "Mozilla"
    assert h["ip"] == "1.2.3.4"
    assert h["device_label"] == "Chrome on macOS"
    assert "created_at" in h

    assert "user_sessions:alice@example.com" in fake_redis.zsets
    members = [m for m, _ in fake_redis.zsets["user_sessions:alice@example.com"]]
    assert "jti-a" in members


async def test_record_session_silent_on_redis_failure(monkeypatch):
    """Redis 실패 시 raise 없음 (fail-open)."""
    async def _broken():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(
        "app.core.session_registry._redis", _broken
    )
    # raise 안 해야 함
    await session_registry.record_session(
        email="alice@example.com",
        jti="jti-a",
        exp_epoch=9999999999,
    )


# ─── unregister_session ──────────────────────────────────────


async def test_unregister_session_removes_from_hash_and_zset(fake_redis):
    """unregister → HASH 삭제 + ZSET 에서 제거."""
    await session_registry.record_session(
        email="alice@example.com", jti="jti-a", exp_epoch=9999999999,
    )
    await session_registry.unregister_session("jti-a")
    assert "session:jti-a" not in fake_redis.hashes
    members = [m for m, _ in fake_redis.zsets.get("user_sessions:alice@example.com", [])]
    assert "jti-a" not in members


# ─── list_sessions ──────────────────────────────────────────


async def test_list_sessions_returns_all_with_meta(fake_redis):
    """여러 세션 등록 후 list → 모두 회수 + 최신순 정렬."""
    await session_registry.record_session(
        email="alice@example.com", jti="j1", exp_epoch=9999999999,
        device_label="A",
    )
    # 두 번째가 더 늦은 created_at 이 되도록 약간 차이
    import time
    time.sleep(0.001)
    await session_registry.record_session(
        email="alice@example.com", jti="j2", exp_epoch=9999999999,
        device_label="B",
    )

    sessions = await session_registry.list_sessions("alice@example.com")
    assert len(sessions) == 2
    # 최신순 (j2 가 j1 보다 늦게 등록)
    assert sessions[0].jti == "j2"
    assert sessions[1].jti == "j1"


async def test_list_sessions_empty_when_no_sessions(fake_redis):
    out = await session_registry.list_sessions("nobody@example.com")
    assert out == []


async def test_list_sessions_cleans_expired_zset_entries(fake_redis):
    """ZSET 의 만료된 entries (score < now) 는 lazy cleanup."""
    # 과거 시점 score (만료)
    import time
    past_ms = int(time.time() * 1000) - 1_000_000
    fake_redis.zsets["user_sessions:alice@example.com"] = [
        ("j-expired", past_ms),
    ]
    # 만료 HASH 는 없는 상태 (이미 evict)

    out = await session_registry.list_sessions("alice@example.com")
    assert out == []
    # 정리됨
    assert "user_sessions:alice@example.com" in fake_redis.zsets
    members = [m for m, _ in fake_redis.zsets["user_sessions:alice@example.com"]]
    assert "j-expired" not in members


# ─── get_session_email ──────────────────────────────────────


async def test_get_session_email_returns_owner(fake_redis):
    await session_registry.record_session(
        email="alice@example.com", jti="jx", exp_epoch=9999999999,
    )
    email = await session_registry.get_session_email("jx")
    assert email == "alice@example.com"


async def test_get_session_email_returns_none_when_missing(fake_redis):
    email = await session_registry.get_session_email("unknown")
    assert email is None
