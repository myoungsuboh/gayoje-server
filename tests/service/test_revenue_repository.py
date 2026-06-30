"""
revenue_repository 단위 테스트 — 토큰 원가 + 등급별 집계.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import revenue_repository
from app.service.revenue_repository import (
    TOKEN_UNIT_PRICE_PER_M_KRW,
    compute_monthly,
    compute_summary,
    end_of_month_iso,
    get_active_subscribers_at,
    get_tier_breakdown,
    is_current_month,
    is_future_month,
    token_cost_krw,
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
    def _setup(responses):
        fake = _FakeRunCypher(responses)
        monkeypatch.setattr(
            "app.service.revenue_repository.neo4j_client.run_cypher", fake
        )
        return fake
    return _setup


# ─── token_cost_krw ──────────────────────────────


def test_token_cost_krw_zero():
    assert token_cost_krw(0) == 0
    assert token_cost_krw(-100) == 0


def test_token_cost_krw_1m_tokens():
    """1M tokens → TOKEN_UNIT_PRICE."""
    assert token_cost_krw(1_000_000) == TOKEN_UNIT_PRICE_PER_M_KRW


def test_token_cost_krw_5m_tokens_pro_limit():
    """Pro 한도 5M → 5 × 1,250 = 6,250원."""
    assert token_cost_krw(5_000_000) == 6_250


def test_token_cost_krw_20m_tokens_pro_max_limit():
    """Pro Max 한도 20M → 25,000원."""
    assert token_cost_krw(20_000_000) == 25_000


# ─── get_tier_breakdown ──────────────────────────


async def test_get_tier_breakdown_returns_all_tiers(fake_run):
    fake_run([
        [
            {"tier": "free", "subscribers": 900, "total_tokens": 5_000_000},
            {"tier": "pro", "subscribers": 70, "total_tokens": 50_000_000},
            {"tier": "pro_plus", "subscribers": 20, "total_tokens": 30_000_000},
            {"tier": "pro_max", "subscribers": 10, "total_tokens": 40_000_000},
        ]
    ])
    rows = await get_tier_breakdown()
    assert len(rows) == 4
    by_tier = {r.tier: r for r in rows}
    assert by_tier["pro"].subscribers == 70
    assert by_tier["pro_max"].total_tokens == 40_000_000


# ─── compute_summary ─────────────────────────────


async def test_compute_summary_mrr_arpu_profit(fake_run):
    """매출/원가/순이익 + ARPU 계산 회귀 검증."""
    fake_run([
        [
            {"tier": "free", "subscribers": 900, "total_tokens": 5_000_000},
            {"tier": "pro", "subscribers": 70, "total_tokens": 50_000_000},
            {"tier": "pro_plus", "subscribers": 20, "total_tokens": 30_000_000},
            {"tier": "pro_max", "subscribers": 10, "total_tokens": 40_000_000},
        ]
    ])
    pricing = {"free": 0, "pro": 9_900, "pro_plus": 17_900, "pro_max": 29_900}
    s = await compute_summary(pricing_map=pricing, infra_cost_krw=80_000)

    # 매출 = 70*9900 + 20*17900 + 10*29900 = 693,000 + 358,000 + 299,000 = 1,350,000
    assert s.mrr_krw == 1_350_000
    # 토큰 합 = 125M, 원가 = 125 × 1250 = 156,250원
    assert s.llm_cost_krw == 156_250
    # 순이익 = 1,350,000 - 156,250 - 80,000 = 1,113,750
    assert s.profit_krw == 1_113_750
    # 활성 구독자 = 100, ARPU = 13,500
    assert s.total_subscribers == 100
    assert s.arpu_krw == 13_500
    assert s.total_users == 1_000

    # ── 등급별(per-tier) 매출/원가/기여 마진 ──
    by_tier = {b.tier: b for b in s.breakdown}
    # Free: 매출 0, 토큰 5M → 원가 6,250, 마진 = -6,250 (적자 — 무료 등급 LLM 비용)
    assert by_tier["free"].revenue_krw == 0
    assert by_tier["free"].llm_cost_krw == 6_250
    assert by_tier["free"].profit_krw == -6_250
    # Pro: 매출 70×9,900=693,000, 토큰 50M → 원가 62,500, 마진 630,500
    assert by_tier["pro"].revenue_krw == 693_000
    assert by_tier["pro"].llm_cost_krw == 62_500
    assert by_tier["pro"].profit_krw == 630_500
    # Pro Max: 매출 10×29,900=299,000, 토큰 40M → 원가 50,000, 마진 249,000
    assert by_tier["pro_max"].revenue_krw == 299_000
    assert by_tier["pro_max"].profit_krw == 249_000
    # 등급별 원가 합 = 전체 llm_cost (집계 일관성 회귀 가드)
    assert sum(b.llm_cost_krw for b in s.breakdown) == s.llm_cost_krw
    # to_dict 직렬화에도 새 필드 포함 (route DTO 매핑 회귀 가드)
    d = by_tier["pro"].to_dict()
    assert d["revenue_krw"] == 693_000 and d["profit_krw"] == 630_500


async def test_compute_summary_zero_subscribers_arpu_zero(fake_run):
    """모두 Free 인 경우 ARPU = 0 (ZeroDivision 회피)."""
    fake_run([[{"tier": "free", "subscribers": 50, "total_tokens": 100_000}]])
    pricing = {"free": 0, "pro": 9_900}
    s = await compute_summary(pricing_map=pricing, infra_cost_krw=10_000)
    assert s.mrr_krw == 0
    assert s.total_subscribers == 0
    assert s.arpu_krw == 0


# ─── 2026-05 이력 기반 월별 매출 ───────────────────


def test_end_of_month_iso_handles_february_leap():
    """윤년 처리 — 2024-02 마지막 날 29일."""
    # 2024 는 윤년
    iso = end_of_month_iso(2024, 2)
    assert "2024-02-29" in iso
    # 2025 는 평년
    iso = end_of_month_iso(2025, 2)
    assert "2025-02-28" in iso


def test_end_of_month_iso_31day_months():
    """31일 달 (1/3/5/7/8/10/12) — 마지막 날 31."""
    for month in (1, 3, 5, 7, 8, 10, 12):
        iso = end_of_month_iso(2026, month)
        assert f"-{month:02d}-31" in iso


def test_is_future_month():
    """미래 (year, month) 판별."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    # 다음 해는 미래
    assert is_future_month(now.year + 1, 1) is True
    # 작년은 과거
    assert is_future_month(now.year - 1, 12) is False


def test_is_current_month():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    assert is_current_month(now.year, now.month) is True
    assert is_current_month(now.year - 1, now.month) is False


async def test_get_active_subscribers_at_returns_dict(fake_run):
    """특정 시점 활성 구독자 cypher 응답 매핑."""
    fake = fake_run([
        [
            {"tier": "free", "subscribers": 900},
            {"tier": "pro", "subscribers": 70},
            {"tier": "pro_plus", "subscribers": 20},
            {"tier": "pro_max", "subscribers": 10},
        ]
    ])
    result = await get_active_subscribers_at("2026-05-31T23:59:59+00:00")
    assert result == {"free": 900, "pro": 70, "pro_plus": 20, "pro_max": 10}
    # cypher 에 SubscriptionChange + changed_at <= 조건 포함 — 회귀 가드
    cypher = fake.calls[0]["cypher"]
    assert "SubscriptionChange" in cypher
    assert "changed_at <= datetime" in cypher
    # at 파라미터 바인딩
    assert fake.calls[0]["params"]["at"] == "2026-05-31T23:59:59+00:00"


async def test_compute_monthly_past_returns_history_based_mrr(fake_run):
    """과거 달 — 이력 기반 구독자 × 현재 가격. LLM 원가 = 0 (추적 불가)."""
    # 과거 달의 활성 구독자 cypher 응답 (이력 기반)
    fake_run([
        [
            {"tier": "free", "subscribers": 500},
            {"tier": "pro", "subscribers": 50},
            {"tier": "pro_plus", "subscribers": 15},
            {"tier": "pro_max", "subscribers": 5},
        ]
    ])
    pricing = {"free": 0, "pro": 9_900, "pro_plus": 17_900, "pro_max": 29_900}
    # 작년 같은 달 (확실히 과거)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    past_year = now.year - 1
    m = await compute_monthly(
        year=past_year, month=now.month,
        pricing_map=pricing, infra_cost_krw=80_000,
    )
    # MRR = 50*9900 + 15*17900 + 5*29900 = 495,000 + 268,500 + 149,500 = 913,000
    assert m.mrr_krw == 913_000
    # 과거 달 — LLM 원가 추적 불가
    assert m.llm_cost_krw == 0
    assert m.llm_cost_tracked is False
    # 순이익 = 매출 - 0 - 80,000 = 833,000
    assert m.profit_krw == 833_000


async def test_compute_monthly_future_returns_zeros(fake_run):
    """미래 달 — 매출/원가 0, 인프라만 (admin 입력 시), 순이익 = -인프라."""
    fake = fake_run([])  # cypher 호출 안 함 (early return)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    pricing = {"pro": 9_900}
    m = await compute_monthly(
        year=now.year + 1, month=1,
        pricing_map=pricing, infra_cost_krw=80_000,
    )
    assert m.mrr_krw == 0
    assert m.llm_cost_krw == 0
    assert m.infra_cost_krw == 80_000
    assert m.profit_krw == -80_000  # 인프라만 빠짐
    assert m.llm_cost_tracked is False
    # 미래 달은 DB 조회 안 함 (early return)
    assert len(fake.calls) == 0


async def test_compute_monthly_current_returns_tracked_llm_cost(fake_run):
    """현재 달 — 이력 기반 매출 + 실측 토큰 원가."""
    fake_run([
        # 1. get_active_subscribers_at — 현재 시점 활성 구독자
        [
            {"tier": "free", "subscribers": 100},
            {"tier": "pro", "subscribers": 10},
        ],
        # 2. get_tier_breakdown — 토큰 사용량 합산용
        [
            {"tier": "free", "subscribers": 100, "total_tokens": 1_000_000},
            {"tier": "pro", "subscribers": 10, "total_tokens": 5_000_000},
        ],
    ])
    pricing = {"free": 0, "pro": 9_900}
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    m = await compute_monthly(
        year=now.year, month=now.month,
        pricing_map=pricing, infra_cost_krw=80_000,
    )
    assert m.mrr_krw == 99_000  # 10 × 9900
    # LLM 원가 = 6M tokens × 1250원/M = 7,500
    assert m.llm_cost_krw == 7_500
    assert m.llm_cost_tracked is True  # 현재 달은 추적 가능
