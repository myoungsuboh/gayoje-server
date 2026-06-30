"""
Paddle 웹훅 — 서명검증 / 등급매핑 / entitlement 갱신 단위 테스트.

라우트(main 경유)는 fastmcp 의존이라 여기선 순수 함수 + _handle_event 를 직접 검증.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.api import paddle_webhook_routes as pw
from app.core.config import settings
from app.core.subscription import (
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    SUBSCRIPTION_PRO_MAX,
    SUBSCRIPTION_PRO_PLUS,
)

pytestmark = pytest.mark.asyncio

_SECRET = "pdl_ntfset_test_secret"


def _sign(raw: bytes, ts: str = "1700000000", secret: str = _SECRET) -> str:
    mac = hmac.new(secret.encode(), f"{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={mac}"


# ─── 서명 검증 ───────────────────────────────────────────────


def test_signature_valid():
    body = b'{"event_id":"evt_1"}'
    assert pw.verify_paddle_signature(body, _sign(body), _SECRET) is True


def test_signature_tampered_body():
    body = b'{"event_id":"evt_1"}'
    sig = _sign(body)
    assert pw.verify_paddle_signature(b'{"event_id":"evil"}', sig, _SECRET) is False


def test_signature_wrong_secret():
    body = b'{"a":1}'
    assert pw.verify_paddle_signature(body, _sign(body), "other_secret") is False


def test_signature_malformed_header():
    body = b'{"a":1}'
    assert pw.verify_paddle_signature(body, "garbage", _SECRET) is False
    assert pw.verify_paddle_signature(body, None, _SECRET) is False
    assert pw.verify_paddle_signature(body, "ts=123", _SECRET) is False  # h1 없음


# ─── 등급 매핑 ───────────────────────────────────────────────


def _patch_prices(monkeypatch):
    monkeypatch.setattr(settings, "PADDLE_PRICE_PRO", "pri_pro")
    monkeypatch.setattr(settings, "PADDLE_PRICE_PRO_PLUS", "pri_plus")
    monkeypatch.setattr(settings, "PADDLE_PRICE_PRO_MAX", "pri_max")


def test_paddle_price_to_tier_includes_yearly(monkeypatch):
    """[버그 가드] env 계약은 월/연 6개 — 연간 price 미매핑이면 연간 구독 등급부여가 누락된다."""
    _patch_prices(monkeypatch)
    monkeypatch.setattr(settings, "PADDLE_PRICE_PRO_Y", "pri_pro_y")
    monkeypatch.setattr(settings, "PADDLE_PRICE_PRO_PLUS_Y", "pri_plus_y")
    monkeypatch.setattr(settings, "PADDLE_PRICE_PRO_MAX_Y", "pri_max_y")
    m = settings.paddle_price_to_tier
    assert m["pri_pro"] == SUBSCRIPTION_PRO
    assert m["pri_pro_y"] == SUBSCRIPTION_PRO
    assert m["pri_plus_y"] == SUBSCRIPTION_PRO_PLUS
    assert m["pri_max_y"] == SUBSCRIPTION_PRO_MAX


def test_paddle_price_to_tier_skips_unset(monkeypatch):
    """미설정(None) 항목은 매핑에서 제외 — 빈 키 오염 방지."""
    monkeypatch.setattr(settings, "PADDLE_PRICE_PRO", "pri_pro")
    for f in ("PADDLE_PRICE_PRO_PLUS", "PADDLE_PRICE_PRO_MAX",
              "PADDLE_PRICE_PRO_Y", "PADDLE_PRICE_PRO_PLUS_Y", "PADDLE_PRICE_PRO_MAX_Y"):
        monkeypatch.setattr(settings, f, None)
    assert settings.paddle_price_to_tier == {"pri_pro": SUBSCRIPTION_PRO}


def test_tier_from_items(monkeypatch):
    _patch_prices(monkeypatch)
    assert pw._tier_from_items([{"price": {"id": "pri_plus"}}]) == SUBSCRIPTION_PRO_PLUS
    assert pw._tier_from_items([{"price": {"id": "pri_max"}}]) == SUBSCRIPTION_PRO_MAX
    assert pw._tier_from_items([{"price": {"id": "pri_unknown"}}]) is None
    assert pw._tier_from_items([]) is None


def test_user_email_from_data():
    assert pw._user_email_from_data({"custom_data": {"user_email": "A@B.com "}}) == "a@b.com"
    assert pw._user_email_from_data({"custom_data": {}}) is None
    assert pw._user_email_from_data({}) is None


# ─── _handle_event (entitlement) ─────────────────────────────


@pytest.fixture
def capture_change(monkeypatch):
    calls = []

    async def fake_change(*, target_email, to_type, reason, changed_by_email):
        calls.append({"email": target_email, "to_type": to_type, "reason": reason})
        return {"user": {"email": target_email}}  # 성공 (None 아님)

    monkeypatch.setattr(pw.admin_repository, "change_subscription", fake_change)
    return calls


async def test_grant_subscription_created(monkeypatch, capture_change):
    _patch_prices(monkeypatch)
    data = {"custom_data": {"user_email": "u@b.com"}, "items": [{"price": {"id": "pri_plus"}}]}
    assert await pw._handle_event("subscription.created", data) is True
    assert capture_change == [{"email": "u@b.com", "to_type": SUBSCRIPTION_PRO_PLUS, "reason": "paddle:subscription.created"}]


async def test_revoke_subscription_canceled(monkeypatch, capture_change):
    _patch_prices(monkeypatch)
    data = {"custom_data": {"user_email": "u@b.com"}, "items": [{"price": {"id": "pri_pro"}}]}
    assert await pw._handle_event("subscription.canceled", data) is True
    assert capture_change[0]["to_type"] == SUBSCRIPTION_FREE


async def test_ignore_unhandled_event(monkeypatch, capture_change):
    assert await pw._handle_event("transaction.paid", {"custom_data": {"user_email": "u@b.com"}}) is False
    assert capture_change == []


async def test_skip_when_no_email(monkeypatch, capture_change):
    _patch_prices(monkeypatch)
    assert await pw._handle_event("subscription.created", {"items": [{"price": {"id": "pri_pro"}}]}) is False
    assert capture_change == []


async def test_skip_when_unmapped_price(monkeypatch, capture_change):
    _patch_prices(monkeypatch)
    data = {"custom_data": {"user_email": "u@b.com"}, "items": [{"price": {"id": "pri_???"}}]}
    assert await pw._handle_event("subscription.created", data) is False
    assert capture_change == []


async def test_grant_event_with_terminal_status_revokes(monkeypatch, capture_change):
    """[순서 꼬임 방어] subscription.updated 가 status=canceled/paused 를 실어오면
    grant 이벤트여도 free 강등 — canceled 직후 도착한 updated 가 등급을 재부여하면 안 된다."""
    _patch_prices(monkeypatch)
    for terminal in ("canceled", "paused"):
        capture_change.clear()
        data = {
            "status": terminal,
            "custom_data": {"user_email": "u@b.com"},
            "items": [{"price": {"id": "pri_plus"}}],
        }
        assert await pw._handle_event("subscription.updated", data) is True
        assert capture_change[0]["to_type"] == SUBSCRIPTION_FREE, f"status={terminal}"


async def test_grant_event_with_active_status_grants(monkeypatch, capture_change):
    """active/trialing/past_due(dunning 유예)/미제공 상태는 정상 등급 부여."""
    _patch_prices(monkeypatch)
    for ok_status in ("active", "trialing", "past_due", ""):
        capture_change.clear()
        data = {
            "status": ok_status,
            "custom_data": {"user_email": "u@b.com"},
            "items": [{"price": {"id": "pri_plus"}}],
        }
        assert await pw._handle_event("subscription.updated", data) is True
        assert capture_change[0]["to_type"] == SUBSCRIPTION_PRO_PLUS, f"status={ok_status}"


# ─── 구독 스냅샷 영속화 (포털 customer_id 소스) ──────────────


@pytest.fixture
def capture_upsert(monkeypatch):
    calls = []

    async def fake_upsert(**kw):
        calls.append(kw)
        return {"subscription_id": kw.get("subscription_id")}, True  # (snapshot, applied)

    monkeypatch.setattr(pw.paddle_subscription_repository, "upsert", fake_upsert)
    return calls


async def test_stale_event_skips_entitlement(monkeypatch, capture_change):
    """[순서 꼬임 방어 2] 스냅샷이 stale(이미 더 최신 상태 반영됨)이면 entitlement 도 변경하지 않는다
    — 늦게 재전달된 옛 active 이벤트가 해지자 등급을 되살리면 안 된다."""
    _patch_prices(monkeypatch)

    async def stale_upsert(**kw):
        return {"status": "canceled"}, False  # 기존(더 최신) 스냅샷 유지, applied=False

    monkeypatch.setattr(pw.paddle_subscription_repository, "upsert", stale_upsert)
    data = {
        "id": "sub_1", "customer_id": "ctm_1", "status": "active",
        "custom_data": {"user_email": "u@b.com"},
        "items": [{"price": {"id": "pri_pro"}}],
    }
    assert await pw._handle_event("subscription.updated", data, occurred_at="2026-06-01T00:00:00Z") is False
    assert capture_change == []


async def test_upsert_user_missing_does_not_block_entitlement(monkeypatch, capture_change):
    """스냅샷의 User 부재(None)는 stale 이 아니다 — entitlement 경로가 스스로 판단하게 진행."""
    _patch_prices(monkeypatch)

    async def none_upsert(**kw):
        return None, False

    monkeypatch.setattr(pw.paddle_subscription_repository, "upsert", none_upsert)
    data = {
        "id": "sub_1", "customer_id": "ctm_1", "status": "active",
        "custom_data": {"user_email": "u@b.com"},
        "items": [{"price": {"id": "pri_pro"}}],
    }
    assert await pw._handle_event("subscription.created", data) is True
    assert capture_change[0]["to_type"] == SUBSCRIPTION_PRO


async def test_grant_persists_subscription_snapshot(monkeypatch, capture_change, capture_upsert):
    _patch_prices(monkeypatch)
    data = {
        "id": "sub_123",
        "customer_id": "ctm_456",
        "status": "active",
        "custom_data": {"user_email": "u@b.com"},
        "items": [{"price": {"id": "pri_plus"}}],
        "current_billing_period": {"starts_at": "2026-06-10T00:00:00Z", "ends_at": "2026-07-10T00:00:00Z"},
    }
    assert await pw._handle_event("subscription.created", data, occurred_at="2026-06-10T12:00:00Z") is True
    assert capture_upsert == [{
        "email": "u@b.com",
        "subscription_id": "sub_123",
        "customer_id": "ctm_456",
        "status": "active",
        "price_id": "pri_plus",
        "current_period_end": "2026-07-10T00:00:00Z",
        "occurred_at": "2026-06-10T12:00:00Z",
    }]


async def test_revoke_persists_canceled_status(monkeypatch, capture_change, capture_upsert):
    _patch_prices(monkeypatch)
    data = {
        "id": "sub_123",
        "customer_id": "ctm_456",
        "status": "canceled",
        "custom_data": {"user_email": "u@b.com"},
        "items": [{"price": {"id": "pri_pro"}}],
    }
    assert await pw._handle_event("subscription.canceled", data) is True
    assert capture_upsert[0]["status"] == "canceled"
    assert capture_change[0]["to_type"] == SUBSCRIPTION_FREE


async def test_snapshot_skipped_without_ids_but_entitlement_proceeds(monkeypatch, capture_change, capture_upsert):
    """id/customer_id 없는 페이로드 — 스냅샷은 skip 하되 등급 부여는 진행."""
    _patch_prices(monkeypatch)
    data = {"custom_data": {"user_email": "u@b.com"}, "items": [{"price": {"id": "pri_pro"}}]}
    assert await pw._handle_event("subscription.created", data) is True
    assert capture_upsert == []
    assert capture_change[0]["to_type"] == SUBSCRIPTION_PRO


async def test_snapshot_failure_does_not_break_entitlement(monkeypatch, capture_change):
    """스냅샷 영속화 실패(일시 DB 오류)가 핵심(등급 부여)을 막으면 안 된다."""
    _patch_prices(monkeypatch)

    async def boom(**kw):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(pw.paddle_subscription_repository, "upsert", boom)
    data = {
        "id": "sub_1", "customer_id": "ctm_1", "status": "active",
        "custom_data": {"user_email": "u@b.com"},
        "items": [{"price": {"id": "pri_pro"}}],
    }
    assert await pw._handle_event("subscription.created", data) is True
    assert capture_change[0]["to_type"] == SUBSCRIPTION_PRO


async def test_grant_but_user_missing(monkeypatch):
    _patch_prices(monkeypatch)

    async def fake_change(**kw):
        return None  # 사용자 없음

    monkeypatch.setattr(pw.admin_repository, "change_subscription", fake_change)
    data = {"custom_data": {"user_email": "ghost@b.com"}, "items": [{"price": {"id": "pri_pro"}}]}
    assert await pw._handle_event("subscription.created", data) is False
