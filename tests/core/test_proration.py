"""
Proration 단위 테스트 — 등급 업그레이드 차액 일할 계산.

[검증]
- 정상 일할 계산 (잔여 일수 × 일할 차액)
- 잔여 0일 이하 → 0원
- 100원 미만 → 0원 (토스 최소 결제액)
- 100원 단위 반올림
- current_period_end 없음 / 비정상 입력 안전 처리
- tz-aware / tz-naive 혼합 안전성
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.proration import compute_upgrade_proration


# Pro 9,900 / Pro+ 17,900 / Pro Max 29,900 (한국 운영 가격)
PRO = 9_900
PRO_PLUS = 17_900
PRO_MAX = 29_900


def _period(start_offset_days: int, end_offset_days: int):
    """now 기준 start/end datetime + 같은 now 를 함께 반환.

    [중요] datetime.now() 두 번 호출 시 미세 시간 차이로 (end - now2).days 가
    1 작아지는 floor 문제 발생. compute_upgrade_proration 에 같은 now 를 명시
    주입해서 결정적으로 만든다.
    """
    now = datetime.now(timezone.utc)
    return (
        now + timedelta(days=start_offset_days),
        now + timedelta(days=end_offset_days),
        now,
    )


# ============================================================================
# 정상 케이스
# ============================================================================


def test_proration_pro_to_pro_plus_half_remaining():
    """Pro 15일 사용 + Pro+ 업그레이드 = 잔여 15일 × 일할 차액."""
    start, end, now = _period(-15, 15)
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_PLUS,
        current_period_start=start,
        current_period_end=end,
        now=now,
    )
    assert out.days_remaining == 15
    assert out.days_in_period == 30
    # 일할 차액 = (17900 - 9900) / 30 × 15 = 8000 / 30 × 15 = 4000
    # 100원 단위 반올림 → 4000
    assert out.charge_amount == 4000


def test_proration_full_period_remaining():
    """주기 시작 직후 업그레이드 = 거의 full 차액."""
    start, end, now = _period(0, 30)
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_PLUS,
        current_period_start=start,
        current_period_end=end,
        now=now,
    )
    assert out.days_remaining == 30
    # (17900 - 9900) × 30 / 30 = 8000
    assert out.charge_amount == 8000


def test_proration_pro_to_pro_max():
    """Pro → Pro Max (10일 잔여)."""
    start, end, now = _period(-20, 10)
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_MAX,
        current_period_start=start,
        current_period_end=end,
        now=now,
    )
    # (29900 - 9900) × 10 / 30 = 20000 / 30 × 10 = 6666.67 → 100원 단위 반올림 6700
    assert out.charge_amount == 6700


def test_proration_pro_plus_to_pro_max():
    """Pro+ → Pro Max (5일 잔여)."""
    start, end, now = _period(-25, 5)
    out = compute_upgrade_proration(
        current_plan_price=PRO_PLUS,
        new_plan_price=PRO_MAX,
        current_period_start=start,
        current_period_end=end,
        now=now,
    )
    # (29900 - 17900) × 5 / 30 = 12000 × 5 / 30 = 2000
    assert out.charge_amount == 2000


# ============================================================================
# 엣지 — 잔여 0일 이하
# ============================================================================


def test_proration_zero_days_remaining():
    """주기 끝 시점 = 차액 0원."""
    start, end, now = _period(-30, 0)
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_PLUS,
        current_period_start=start,
        current_period_end=end,
        now=now,
    )
    assert out.days_remaining == 0
    assert out.charge_amount == 0


def test_proration_past_period_end():
    """end 가 이미 과거 → days_remaining 0 으로 clip."""
    start, end, now = _period(-40, -10)
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_PLUS,
        current_period_start=start,
        current_period_end=end,
        now=now,
    )
    assert out.days_remaining == 0
    assert out.charge_amount == 0


def test_proration_missing_period_end():
    """current_period_end=None → 보수적으로 0 결제 (DB 누락 방어)."""
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_PLUS,
        current_period_start=None,
        current_period_end=None,
    )
    assert out.charge_amount == 0


# ============================================================================
# 엣지 — 100원 미만 / 100원 단위 반올림
# ============================================================================


def test_proration_under_100_won_skipped():
    """차액이 100원 미만이면 0 처리 (토스 최소 결제액 가드)."""
    start, end, now = _period(-29, 1)  # 잔여 1일
    out = compute_upgrade_proration(
        current_plan_price=PRO,         # 9900
        new_plan_price=PRO + 1000,      # 10900 (예: 가격 인상 시)
        current_period_start=start,
        current_period_end=end,
        now=now,
    )
    # (10900 - 9900) × 1 / 30 = 33.33원 → 100원 단위 반올림 0 → < 100 → 0
    assert out.charge_amount == 0


def test_proration_round_to_100():
    """100원 단위 반올림 확인."""
    # (8000원 차액) × 11일 / 30일 = 2933.33 → 반올림 2900
    start, end, now = _period(-19, 11)
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_PLUS,
        current_period_start=start,
        current_period_end=end,
        now=now,
    )
    assert out.charge_amount == 2900


# ============================================================================
# tz / 입력 형식
# ============================================================================


def test_proration_accepts_iso_string():
    """current_period_end 가 ISO string 으로 와도 datetime 으로 파싱."""
    now = datetime.now(timezone.utc)
    end_iso = (now + timedelta(days=15)).isoformat()
    start_iso = (now - timedelta(days=15)).isoformat()
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_PLUS,
        current_period_start=start_iso,
        current_period_end=end_iso,
    )
    assert out.charge_amount > 0  # 잔여 15일 차액 발생


def test_proration_tz_naive_safe():
    """tz-naive datetime 입력도 안전 (Neo4j 응답이 tz 누락 케이스 대비)."""
    now = datetime.utcnow()  # tz-naive
    start = now - timedelta(days=15)
    end = now + timedelta(days=15)
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_PLUS,
        current_period_start=start,
        current_period_end=end,
    )
    # 예외 없이 정상 계산
    assert out.charge_amount > 0


def test_proration_explicit_now_argument():
    """now 주입 — 결정적 테스트 가능 (시간 흐름 무관)."""
    base = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    start = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 31, 0, 0, 0, tzinfo=timezone.utc)
    # base 기준 잔여: 5/15 12:00 → 5/31 00:00 = 약 15일
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_PLUS,
        current_period_start=start,
        current_period_end=end,
        now=base,
    )
    assert out.days_in_period == 30
    assert out.days_remaining == 15
    assert out.charge_amount == 4000


# ============================================================================
# rationale 메시지 — admin/사용자 노출용
# ============================================================================


def test_proration_rationale_includes_calculation():
    start, end, now = _period(-15, 15)
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_PLUS,
        current_period_start=start,
        current_period_end=end,
        now=now,
    )
    # 사람이 읽을 수 있는 계산식 노출
    assert "잔여" in out.rationale
    assert "주기" in out.rationale
    assert "4,000" in out.rationale or "4000" in out.rationale


def test_proration_to_dict_includes_all_fields():
    out = compute_upgrade_proration(
        current_plan_price=PRO,
        new_plan_price=PRO_PLUS,
        current_period_start=None,
        current_period_end=None,
    )
    d = out.to_dict()
    for k in [
        "days_remaining", "days_in_period",
        "current_plan_daily", "new_plan_daily",
        "charge_amount", "rationale",
    ]:
        assert k in d
