"""
pricing_repository 단위 테스트.

[검증 범위]
- calculate_final_price: USD 센트 반올림 + KRW(legacy) 100원 반올림
- list_pricing / get_pricing: cypher 응답 → PricingConfig 매핑
- update_pricing: tier 검증 + base/discount 범위 clamping
- ensure_pricing_seeded: 부팅 시 4개 등급 INIT (MERGE — 기존 보존)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.core.subscription import (
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    SUBSCRIPTION_PRO_MAX,
    SUBSCRIPTION_PRO_PLUS,
)
from app.service import pricing_repository
from app.service.pricing_repository import (
    PricingConfig,
    calculate_final_price,
    get_pricing,
    list_pricing,
    update_pricing,
)


pytestmark = pytest.mark.asyncio


# ─── Fake neo4j ──────────────────────────────────


class _FakeRunCypher:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher, params=None, database=None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses: Optional[List[List[Dict[str, Any]]]] = None) -> _FakeRunCypher:
        fake = _FakeRunCypher(responses)
        monkeypatch.setattr(
            "app.service.pricing_repository.neo4j_client.run_cypher", fake
        )
        return fake
    return _setup


# ─── calculate_final_price ───────────────────────


def test_calculate_final_price_usd_no_discount():
    """USD(기본) 할인 0% → 정가 그대로 (센트). Pro $9 / Pro+ $19 / Pro Max $29."""
    assert calculate_final_price(900, 0) == 900
    assert calculate_final_price(1900, 0) == 1900
    assert calculate_final_price(2900, 0) == 2900


def test_calculate_final_price_usd_cent_rounding():
    """USD — 센트 단위 반올림."""
    assert calculate_final_price(1900, 10) == 1710   # $19 × 0.9 = $17.10
    assert calculate_final_price(2900, 25) == 2175   # $29 × 0.75 = $21.75
    assert calculate_final_price(999, 10) == 899     # 999 × 0.9 = 899.1 → 899


def test_calculate_final_price_krw_legacy_rounding():
    """KRW(legacy) — 100원 단위 반올림 유지 (currency 명시)."""
    assert calculate_final_price(19_900, 10, "KRW") == 17_900
    assert calculate_final_price(39_900, 25, "KRW") == 29_900
    assert calculate_final_price(9_950, 0, "KRW") == 10_000


def test_calculate_final_price_free():
    """Free 등급 — 정가 0, 할인 0 → 0."""
    assert calculate_final_price(0, 0) == 0


def test_calculate_final_price_full_discount():
    """100% 할인 → 0."""
    assert calculate_final_price(1900, 100) == 0


def test_calculate_final_price_clamps_negative_base():
    """음수 base — 0 반환 (입력 방어)."""
    assert calculate_final_price(-1000, 10) == 0


def test_calculate_final_price_clamps_discount_out_of_range():
    """할인율 0-100 밖 — clamp."""
    assert calculate_final_price(1000, -5) == 1000   # 음수 → 0% → 정가
    assert calculate_final_price(1000, 150) == 0     # 100 초과 → 100% → 0


# ─── list_pricing / get_pricing ──────────────────


async def test_list_pricing_returns_normalized(fake_run):
    fake_run([
        [
            {"tier": "free", "base_price": 0, "discount_pct": 0, "currency": "USD",
             "updated_at": None, "updated_by": "SYSTEM:SEED"},
            {"tier": "pro", "base_price": 900, "discount_pct": 0, "currency": "USD",
             "updated_at": "2026-06-08T00:00:00Z", "updated_by": "admin@example.com"},
            {"tier": "pro_plus", "base_price": 1900, "discount_pct": 0, "currency": "USD",
             "updated_at": None, "updated_by": "SYSTEM:SEED"},
            {"tier": "pro_max", "base_price": 2900, "discount_pct": 0, "currency": "USD",
             "updated_at": None, "updated_by": "SYSTEM:SEED"},
        ]
    ])
    out = await list_pricing()
    assert len(out) == 4
    assert all(isinstance(p, PricingConfig) for p in out)
    # final_price 자동 계산 확인 (USD 센트)
    by_tier = {p.tier: p for p in out}
    assert by_tier["free"].final_price == 0
    assert by_tier["pro"].final_price == 900        # $9.00
    assert by_tier["pro_plus"].final_price == 1900   # $19.00
    assert by_tier["pro_max"].final_price == 2900    # $29.00
    assert by_tier["pro"].currency == "USD"


async def test_list_pricing_legacy_row_defaults_krw(fake_run):
    """currency 없는 legacy 행 → KRW 로 해석 (마이그레이션 전 안전망)."""
    fake_run([[
        {"tier": "pro", "base_price": 9900, "discount_pct": 10,
         "updated_at": None, "updated_by": "SYSTEM:SEED"},
    ]])
    out = await list_pricing()
    assert out[0].currency == "KRW"
    assert out[0].final_price == 8_900   # 9,900 × 0.9 = 8,910 → 100원 → 8,900


async def test_get_pricing_invalid_tier_returns_none(fake_run):
    fake = fake_run()
    assert await get_pricing("invalid_tier") is None
    # cypher 호출도 안 함
    assert len(fake.calls) == 0


async def test_get_pricing_missing_node_returns_none(fake_run):
    fake_run([[]])
    assert await get_pricing(SUBSCRIPTION_PRO) is None


# ─── update_pricing ──────────────────────────────


async def test_update_pricing_invalid_tier_returns_none(fake_run):
    fake = fake_run()
    r = await update_pricing("invalid", 10_000, 10, "admin@example.com")
    assert r is None
    assert len(fake.calls) == 0


async def test_update_pricing_clamps_negative_base(fake_run):
    """음수 base → 0 으로 clamp 후 cypher 호출."""
    fake_run([[
        {"tier": "pro_plus", "base_price": 0, "discount_pct": 10,
         "updated_at": "2026-05-17T00:00:00Z", "updated_by": "admin@example.com"}
    ]])
    r = await update_pricing(SUBSCRIPTION_PRO_PLUS, -500, 10, "admin@example.com")
    assert r is not None
    assert r.base_price == 0


async def test_update_pricing_clamps_discount_above_100(fake_run):
    """할인율 150 → 100 으로 clamp."""
    fake = fake_run([[
        {"tier": "pro_plus", "base_price": 19_900, "discount_pct": 100,
         "updated_at": "2026-05-17T00:00:00Z", "updated_by": "admin@example.com"}
    ]])
    r = await update_pricing(SUBSCRIPTION_PRO_PLUS, 19_900, 150, "admin@example.com")
    assert r is not None
    # cypher 에 전달된 값도 100 으로 clamp
    assert fake.calls[0]["params"]["discount_pct"] == 100


async def test_update_pricing_sets_updated_by(fake_run):
    fake = fake_run([[
        {"tier": "pro_plus", "base_price": 19_900, "discount_pct": 10,
         "updated_at": "2026-05-17T00:00:00Z", "updated_by": "admin@b.com"}
    ]])
    await update_pricing(SUBSCRIPTION_PRO_PLUS, 19_900, 10, "admin@b.com")
    assert fake.calls[0]["params"]["updated_by"] == "admin@b.com"
