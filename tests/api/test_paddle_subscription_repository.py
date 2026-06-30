"""
PaddleSubscription 영속화 repository — upsert/get 계약 검증 (neo4j 모킹).

웹훅이 받은 구독 스냅샷(customer_id 등)을 저장해야 고객포털 세션 생성과
FE 구독현황 표시가 가능하다.
"""
from __future__ import annotations

import pytest

from app.service import paddle_subscription_repository as repo

pytestmark = pytest.mark.asyncio


def _capture_cypher(monkeypatch, rows):
    calls = []

    async def fake_run_cypher(cypher, params=None):
        calls.append({"cypher": cypher, "params": params or {}})
        return rows

    monkeypatch.setattr(repo.neo4j_client, "run_cypher", fake_run_cypher)
    return calls


async def test_upsert_normalizes_email_and_passes_fields(monkeypatch):
    calls = _capture_cypher(monkeypatch, [{"s": {"subscription_id": "sub_1"}, "apply": True}])
    snap, applied = await repo.upsert(
        email=" U@X.com ",
        subscription_id="sub_1",
        customer_id="ctm_1",
        status="active",
        price_id="pri_pro_m",
        current_period_end="2026-07-10T00:00:00Z",
    )
    assert snap == {"subscription_id": "sub_1"}
    assert applied is True
    params = calls[0]["params"]
    assert params["email"] == "u@x.com"  # 소문자/공백 정규화 — 웹훅 email 과 User 노드 매칭 보장
    assert params["subscription_id"] == "sub_1"
    assert params["customer_id"] == "ctm_1"
    assert params["status"] == "active"
    assert params["price_id"] == "pri_pro_m"
    assert params["current_period_end"] == "2026-07-10T00:00:00Z"


async def test_upsert_passes_occurred_at_and_cypher_guards_ordering(monkeypatch):
    """occurred_at 정규화 전달 + cypher 가 out-of-order 가드(<= $occurred_at)를 포함."""
    calls = _capture_cypher(monkeypatch, [{"s": {}, "apply": True}])
    await repo.upsert(
        email="u@x.com", subscription_id="sub_1", customer_id="ctm_1",
        status="active", price_id=None, current_period_end=None,
        occurred_at="2026-06-10T12:00:00Z",
    )
    assert calls[0]["params"]["occurred_at"] == "2026-06-10T12:00:00.000000Z"
    assert "s.occurred_at <= $occurred_at" in calls[0]["cypher"]


def test_norm_occurred_at_fixed_precision():
    """[사전순=시간순 보장] 밀리초 자릿수가 달라도 고정 포맷(6자리)으로 정규화 —
    '…00Z' vs '…00.000Z' 류 사전순 역전 방지. 파싱 불가 문자열은 '' (순서 정보 없음)."""
    f = repo._norm_occurred_at
    assert f("2026-06-10T12:00:00Z") == "2026-06-10T12:00:00.000000Z"
    assert f("2026-06-10T12:00:00.123Z") == "2026-06-10T12:00:00.123000Z"
    assert f("2026-06-10T12:00:00.123456Z") == "2026-06-10T12:00:00.123456Z"
    # 정규화 후엔 사전순 비교가 시간순과 일치
    assert f("2026-06-10T12:00:00Z") < f("2026-06-10T12:00:00.123Z")
    assert f("") == ""
    assert f("not-a-date") == ""


async def test_upsert_stale_returns_existing_snapshot_not_applied(monkeypatch):
    """cypher apply=false (stale) → (기존 스냅샷, False) 반환 — 호출자가 entitlement skip 판단."""
    calls = _capture_cypher(monkeypatch, [{"s": {"status": "canceled"}, "apply": False}])
    snap, applied = await repo.upsert(
        email="u@x.com", subscription_id="sub_1", customer_id="ctm_1",
        status="active", price_id=None, current_period_end=None,
        occurred_at="2026-06-01T00:00:00Z",
    )
    assert snap == {"status": "canceled"}
    assert applied is False
    assert calls  # cypher 호출 자체는 일어남


async def test_upsert_none_optionals_become_empty(monkeypatch):
    calls = _capture_cypher(monkeypatch, [{"s": {}, "apply": True}])
    await repo.upsert(
        email="u@x.com", subscription_id="sub_1", customer_id="ctm_1",
        status="canceled", price_id=None, current_period_end=None,
    )
    params = calls[0]["params"]
    assert params["price_id"] == ""
    assert params["current_period_end"] == ""


async def test_upsert_returns_none_when_user_missing(monkeypatch):
    _capture_cypher(monkeypatch, [])
    snap, applied = await repo.upsert(
        email="ghost@x.com", subscription_id="s", customer_id="c",
        status="active", price_id=None, current_period_end=None,
    )
    assert snap is None
    assert applied is False


async def test_get_by_email_returns_snapshot(monkeypatch):
    calls = _capture_cypher(monkeypatch, [{"s": {"customer_id": "ctm_1", "status": "active"}}])
    r = await repo.get_by_email("U@X.COM")
    assert r == {"customer_id": "ctm_1", "status": "active"}
    assert calls[0]["params"]["email"] == "u@x.com"


async def test_get_by_email_none_when_missing(monkeypatch):
    _capture_cypher(monkeypatch, [])
    assert await repo.get_by_email("u@x.com") is None
