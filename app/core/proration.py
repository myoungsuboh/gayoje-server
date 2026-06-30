"""
Proration — 등급 변경 (업그레이드) 시 일할 차액 계산.

[정책 — 2026-05-18]
사용자가 Pro 결제 후 N일 사용 → Pro+ 업그레이드 시:
    잔여일수 = (current_period_end - now).days
    현 plan 일할 가치   = (현재 plan 가격 / 주기 일수) × 잔여일수
    새 plan 일할 가치   = (새 plan 가격   / 주기 일수) × 잔여일수
    추가 결제액       = 새 일할 가치 − 현 일할 가치  (소수 버림 → 100원 단위 반올림)

다운그레이드는 즉시 적용 안 함 — `next_billing_at` 부터 새 plan + 새 가격.
(SaaS 표준; 사용자가 이미 결제한 만큼은 보장.)

[엣지]
- 남은 일수 0/음수 → 0 결제 (이미 만료된 주기)
- 새 plan 가격 ≤ 현 plan 가격 (사실상 업그레이드 아님) → 0 (호출자가 막아야 함)
- 100원 미만은 round-up 으로 0 처리 (토스 최소 결제액 100원)

[주기]
가입일 기준 매월 — DB 에 current_period_start/end 가 정확한 일자 기록.
계산 시 그 값 그대로 사용 (월별 일수 차이 자연 반영).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class ProrationResult:
    """업그레이드 차액 계산 결과."""

    days_remaining: int          # 잔여 일수 (음수 가능 → 0 으로 clip)
    days_in_period: int          # 현재 주기 총 일수
    current_plan_daily: int      # 현 plan 의 일할 가치 (KRW)
    new_plan_daily: int          # 새 plan 의 일할 가치 (KRW)
    charge_amount: int           # 실제 추가 결제 금액 (KRW, 100원 단위)
    rationale: str               # 사용자/admin 노출용 계산 근거 문구

    def to_dict(self) -> dict:
        return {
            "days_remaining": self.days_remaining,
            "days_in_period": self.days_in_period,
            "current_plan_daily": self.current_plan_daily,
            "new_plan_daily": self.new_plan_daily,
            "charge_amount": self.charge_amount,
            "rationale": self.rationale,
        }


def _to_dt(value) -> Optional[datetime]:
    """str(ISO) → datetime. None / 비정상 입력은 None."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        # Neo4j 의 toString(datetime()) 은 ISO8601 with offset.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _round_to_100(n: float) -> int:
    """100원 단위 반올림 (토스 결제 최소 단위)."""
    if n <= 0:
        return 0
    return int(round(n / 100.0) * 100)


def compute_upgrade_proration(
    *,
    current_plan_price: int,
    new_plan_price: int,
    current_period_start,
    current_period_end,
    now: Optional[datetime] = None,
) -> ProrationResult:
    """
    업그레이드 차액 계산. 호출자는 가격을 PricingConfig.final_price 로 넘김 (할인 적용 후).

    Args:
        current_plan_price: 현재 plan 의 월 결제액 (KRW, 정수)
        new_plan_price:     업그레이드할 plan 의 월 결제액 (KRW, 정수)
        current_period_start / end: Subscription 의 현재 주기
        now: 테스트 주입용; 기본은 UTC now

    Returns:
        ProrationResult — charge_amount 가 0 이면 결제 skip.
    """
    now_dt = now or datetime.now(timezone.utc)
    start_dt = _to_dt(current_period_start) or now_dt
    end_dt = _to_dt(current_period_end)

    # end_dt 없으면 안전하게 30일 가정 (방어적; 정상 데이터에선 발생 안 함).
    if end_dt is None:
        days_in_period = 30
        days_remaining = 0
        rationale = "현재 주기 정보 누락 — 추가 결제 없이 plan 만 변경."
    else:
        days_in_period = max(1, (end_dt - start_dt).days)
        # tz-naive vs aware 안전 — 둘 다 aware 화
        if now_dt.tzinfo is None and end_dt.tzinfo is not None:
            now_dt = now_dt.replace(tzinfo=end_dt.tzinfo)
        elif now_dt.tzinfo is not None and end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=now_dt.tzinfo)
        days_remaining = max(0, (end_dt - now_dt).days)
        rationale = (
            f"잔여 {days_remaining}일 / 주기 {days_in_period}일. "
            f"새 일할 ({new_plan_price:,}원/{days_in_period}일 × {days_remaining}일) − "
            f"기존 일할 ({current_plan_price:,}원/{days_in_period}일 × {days_remaining}일)"
        )

    if days_remaining <= 0 or days_in_period <= 0:
        return ProrationResult(
            days_remaining=days_remaining,
            days_in_period=days_in_period,
            current_plan_daily=0,
            new_plan_daily=0,
            charge_amount=0,
            rationale=rationale + " → 잔여 0일, 결제 없음.",
        )

    current_daily = current_plan_price / days_in_period
    new_daily = new_plan_price / days_in_period
    raw_charge = (new_daily - current_daily) * days_remaining
    charge = _round_to_100(raw_charge)

    # 100원 미만 — 토스 최소 결제액. skip.
    if charge < 100:
        return ProrationResult(
            days_remaining=days_remaining,
            days_in_period=days_in_period,
            current_plan_daily=int(round(current_daily)),
            new_plan_daily=int(round(new_daily)),
            charge_amount=0,
            rationale=rationale + f" = {int(raw_charge):,}원 → 100원 미만, 결제 없음.",
        )

    return ProrationResult(
        days_remaining=days_remaining,
        days_in_period=days_in_period,
        current_plan_daily=int(round(current_daily)),
        new_plan_daily=int(round(new_daily)),
        charge_amount=charge,
        rationale=rationale + f" = {charge:,}원.",
    )
