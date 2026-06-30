"""
admin alert throttle 테스트 — Redis 기반 + in-memory 폴백 (Phase 4).

[검증]
  - Redis 가용: SET NX EX 로 첫 호출 허용 / 재호출 throttle.
  - Redis 장애: in-memory 폴백으로도 throttle 동작.
  - throttle_key 없으면 항상 발송.
"""
from __future__ import annotations

import pytest

from app.core import billing_notifications as bn


@pytest.fixture(autouse=True)
def _clear_memory():
    bn._recent_alerts.clear()
    yield
    bn._recent_alerts.clear()


class _FakeRedis:
    """SET NX EX 흉내 — 이미 있는 키면 None 반환."""

    def __init__(self):
        self.store = {}

    async def set(self, name, value, ex=None, nx=False):
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True


async def test_throttle_redis_allows_then_blocks(monkeypatch):
    fake = _FakeRedis()

    async def fake_get_pool():
        return fake

    monkeypatch.setattr("app.queue.client.get_pool", fake_get_pool)

    assert await bn._acquire_throttle("k1") is True   # 첫 호출 허용
    assert await bn._acquire_throttle("k1") is False  # 재호출 throttle
    assert await bn._acquire_throttle("k2") is True   # 다른 키는 허용


async def test_throttle_falls_back_to_memory_on_redis_error(monkeypatch):
    async def boom_get_pool():
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.queue.client.get_pool", boom_get_pool)

    # Redis 장애 → in-memory 폴백으로도 throttle 정상 동작.
    assert await bn._acquire_throttle("kx") is True
    assert await bn._acquire_throttle("kx") is False


async def test_send_admin_alert_throttled_skips_email(monkeypatch):
    fake = _FakeRedis()

    async def fake_get_pool():
        return fake

    monkeypatch.setattr("app.queue.client.get_pool", fake_get_pool)
    monkeypatch.setattr(bn.settings, "RESEND_API_KEY", "re_x", raising=False)
    monkeypatch.setattr(bn.settings, "ADMIN_EMAILS", "admin@x.com", raising=False)

    sent = []

    async def fake_send_email(**kwargs):
        sent.append(kwargs)
        return "mid"

    monkeypatch.setattr(bn.email_lib, "send_email", fake_send_email)
    monkeypatch.setattr(
        bn.email_lib, "render_admin_alert_email",
        lambda **kw: ("subj", "<html>", "text"),
    )

    await bn.send_admin_alert(severity="warning", title="t", message="m", throttle_key="dup")
    await bn.send_admin_alert(severity="warning", title="t", message="m", throttle_key="dup")
    # 두 번째는 throttle 되어 이메일 1회만.
    assert len(sent) == 1
