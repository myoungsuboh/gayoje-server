"""
RevenueRepository — 수익 / 원가 / 순이익 집계 (admin 대시보드용).

[배경]
영업 분석 기반 — admin 이 일/월/년 단위로 매출/원가/순이익 추적.

[데이터 출처]
- 매출 (revenue): 등급별 사용자 수 × 현재 가격 (PricingConfig)
                   = 결제 시스템 미반영 상태 → "예상 매출"
- LLM 원가: 등급별 total_tokens 합산 × token_unit_price (1.25원/1K tokens)
- 인프라 원가: admin 입력 (InfraCost)
- 순이익 = 매출 - LLM원가 - 인프라원가

[현재 한계]
- 실제 결제 데이터 없음 (mailto 기반) — 매출은 "활성 구독자 × 현재 가격" 추정
- 일별 사용자 분포 추적 안 함 → 일별 매출은 "오늘 시점 구독자" 만 가능
- 추후 결제 시스템 도입 시 (:Subscription) 노드 추가하여 정확한 일별 매출

[현재 가능한 집계]
1. summary: 현재 시점 MRR + 등급 분포 + 이번달 토큰 사용 (실측)
2. monthly: 월별 토큰 사용 + 인프라 비용 (입력 시) — 매출은 현재 분포 기준 추정
3. yearly: 연간 토큰 + 연간 인프라

[token 단가]
quota.py 주석 기준 — flash 모델 평균 1.25원/1K tokens (input 70 : output 30).
admin 이 .env 또는 settings 로 조정 가능 (향후 확장).
"""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import List, Optional

from app.clients import neo4j_client
from app.core.subscription import SUBSCRIPTION_FREE

logger = logging.getLogger(__name__)


# ===== 상수 =====


# 토큰 단가 — 1M tokens 당 KRW.
# Gemini 2.5 Flash 평균 (input 70:output 30): input $0.30 + output $2.50/M
#   = (0.30 × 0.7 + 2.50 × 0.3) × 1300 KRW = 0.96 × 1300 ≈ 1,250원/M
TOKEN_UNIT_PRICE_PER_M_KRW = 1_250


# ===== 도메인 모델 =====


@dataclass(frozen=True)
class TierBreakdown:
    """등급별 사용자 수 + 토큰 사용 + (등급 단위) 매출/원가/기여 마진.

    [revenue_krw]
    구독자 × 현재 가격 (Free=0). pricing 이 필요하므로 get_tier_breakdown 단계에선
    0 으로 두고 compute_summary 에서 채운다.

    [llm_cost_krw / profit_krw — 파생]
    llm_cost_krw = token_cost_krw(total_tokens). FE 가 단가(1,250원/M)를 하드코딩해
    재계산하던 것을 BE 단일 출처로 통일(단가 변경 시 드리프트 방지).
    profit_krw = revenue_krw - llm_cost_krw 인 '등급 기여 마진'. 인프라(월 단일값)는
    등급별로 배분하지 않으므로 제외 — 어느 등급이 토큰 원가 대비 남는지 보기 위함.
    """

    tier: str
    subscribers: int
    total_tokens: int
    revenue_krw: int = 0

    @property
    def llm_cost_krw(self) -> int:
        return token_cost_krw(self.total_tokens)

    @property
    def profit_krw(self) -> int:
        return self.revenue_krw - self.llm_cost_krw

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "subscribers": self.subscribers,
            "total_tokens": self.total_tokens,
            "revenue_krw": self.revenue_krw,
            "llm_cost_krw": self.llm_cost_krw,
            "profit_krw": self.profit_krw,
        }


@dataclass(frozen=True)
class RevenueSummary:
    """현재 시점 요약 (MRR + 사용 현황)."""

    breakdown: List[TierBreakdown]
    # 매출/원가 — 이번달 기준 예상.
    mrr_krw: int                   # Monthly Recurring Revenue (구독자 × 현재가격, 추정)
    llm_cost_krw: int              # 이번달 토큰 원가 추정
    infra_cost_krw: int            # 이번달 인프라 비용 (admin 입력)
    profit_krw: int                # mrr - llm_cost - infra_cost
    total_subscribers: int         # 유료 구독자 합계 (Pro/Pro+/Pro Max)
    total_users: int               # 전체 사용자 (Free 포함)
    arpu_krw: int                  # Average Revenue Per User (paid 기준)
    # [2026-05-18] 실 결제 데이터 (Payment 노드 기반) — default 있는 필드는 반드시 맨 끝에
    actual_revenue_krw: int = 0    # 이번달 실 매출 (paid - 환불)
    actual_refund_krw: int = 0     # 이번달 누적 환불
    payment_count: int = 0         # 이번달 결제 건수

    def to_dict(self) -> dict:
        return {
            "breakdown": [b.to_dict() for b in self.breakdown],
            "mrr_krw": self.mrr_krw,
            "llm_cost_krw": self.llm_cost_krw,
            "infra_cost_krw": self.infra_cost_krw,
            "profit_krw": self.profit_krw,
            "total_subscribers": self.total_subscribers,
            "total_users": self.total_users,
            "arpu_krw": self.arpu_krw,
            "actual_revenue_krw": self.actual_revenue_krw,
            "actual_refund_krw": self.actual_refund_krw,
            "payment_count": self.payment_count,
        }


@dataclass(frozen=True)
class MonthlyRevenue:
    """월별 매출/원가/순이익.

    [2026-05]
    llm_cost_tracked: 현재 달만 토큰 사용량 정확 추적. 과거 달은 월간 reset 으로
                       데이터 손실 → FE 가 "—" 또는 "N/A" 표시.
    """

    year: int
    month: int
    mrr_krw: int
    llm_cost_krw: int
    infra_cost_krw: int
    profit_krw: int
    llm_cost_tracked: bool = False
    # [2026-05-18] 실 결제 — Payment 노드 기반
    actual_revenue_krw: int = 0    # 그 달 실 매출 (paid - 환불)
    actual_refund_krw: int = 0     # 그 달 누적 환불
    payment_count: int = 0         # 그 달 결제 건수

    def to_dict(self) -> dict:
        return {
            "year": self.year,
            "month": self.month,
            "mrr_krw": self.mrr_krw,
            "llm_cost_krw": self.llm_cost_krw,
            "infra_cost_krw": self.infra_cost_krw,
            "profit_krw": self.profit_krw,
            "llm_cost_tracked": self.llm_cost_tracked,
            "actual_revenue_krw": self.actual_revenue_krw,
            "actual_refund_krw": self.actual_refund_krw,
            "payment_count": self.payment_count,
        }


# ===== Cypher =====


# [2026-05-18] 일별 실 매출 — Payment 노드 paid_at 기준 그룹화.
# 환불 시 RefundRecord 가 생성됐다면 그 시점을 환불일로 별도 집계.
_DAILY_REVENUE_CYPHER = """\
// gross_paid: Payment.paid_at 기준 그 날 결제된 금액 합
// total_refunded: RefundRecord.created_at 기준 그 날 환불된 금액 합
// 두 시점이 같은 날이면 같은 row, 다르면 별도 row.
CALL {
  MATCH (p:Payment)
  WHERE p.status IN ['paid', 'partial_refund', 'refunded']
    AND COALESCE(p.paid_at, p.created_at) >= datetime($start_iso)
    AND COALESCE(p.paid_at, p.created_at) <  datetime($end_iso)
  WITH date(COALESCE(p.paid_at, p.created_at)) AS day,
       sum(p.amount) AS amount,
       count(p) AS pay_count
  RETURN day, amount AS gross, 0 AS refunded, pay_count
  UNION ALL
  MATCH (r:RefundRecord)
  WHERE r.created_at >= datetime($start_iso)
    AND r.created_at <  datetime($end_iso)
  WITH date(r.created_at) AS day, sum(r.amount) AS refund_sum
  RETURN day, 0 AS gross, refund_sum AS refunded, 0 AS pay_count
}
WITH day, sum(gross) AS gross, sum(refunded) AS refunded, sum(pay_count) AS pay_count
RETURN toString(day) AS date,
       gross AS gross_paid,
       refunded AS total_refunded,
       gross - refunded AS net_revenue,
       pay_count
ORDER BY date ASC
"""


# [2026-05-18] Payment 노드 기반 실 매출 — 특정 기간 paid 합 - 환불.
# 매출 인식 시점: paid_at 우선, NULL 이면 created_at fallback (회계상 결제일 기준).
_ACTUAL_REVENUE_BY_PERIOD_CYPHER = """\
MATCH (p:Payment)
WITH p,
     COALESCE(p.paid_at, p.created_at) AS recognized_at
WHERE recognized_at >= datetime($start_iso)
  AND recognized_at <  datetime($end_iso)
  AND p.status IN ['paid', 'partial_refund', 'refunded']
WITH
  sum(p.amount) AS gross_paid,
  sum(COALESCE(p.refund_amount, 0)) AS total_refunded,
  count(p) AS pay_count
RETURN
  COALESCE(gross_paid, 0) AS gross_paid,
  COALESCE(total_refunded, 0) AS total_refunded,
  COALESCE(pay_count, 0) AS pay_count
"""


# 현재 시점 등급별 사용자 수 + 토큰 누적 (이번 주기).
_BREAKDOWN_BY_TIER_CYPHER = """\
MATCH (u:User)
WITH COALESCE(u.subscription_type, 'free') AS tier,
     COALESCE(u.usage_total_tokens, 0) AS tokens
RETURN tier,
       count(*) AS subscribers,
       sum(tokens) AS total_tokens
ORDER BY
  CASE tier
    WHEN 'free' THEN 0
    WHEN 'pro' THEN 1
    WHEN 'pro_plus' THEN 2
    WHEN 'pro_max' THEN 3
    ELSE 99
  END
"""


# [2026-05] 특정 시점의 활성 구독자 등급 분포 — SubscriptionChange 이력 기반.
#
# 흐름:
#  1. 그 시점 이전 가입한 user 만 (WHERE u.created_at <= $at).
#  2. user 의 마지막 SubscriptionChange (changed_at <= $at) 의 to_type 적용.
#  3. 변경 이력 없는 user → 'free' (가입 default).
#
# collect(sc)[0] = ORDER BY changed_at DESC 의 첫 번째 = 가장 최근. Neo4j 관용구.
_BREAKDOWN_AT_TIMESTAMP_CYPHER = """\
MATCH (u:User)
WHERE u.created_at <= datetime($at)
OPTIONAL MATCH (u)-[:SUBSCRIPTION_HISTORY]->(sc:SubscriptionChange)
WHERE sc.changed_at <= datetime($at)
WITH u, sc
ORDER BY sc.changed_at DESC
WITH u, collect(sc)[0] AS last_sc
WITH COALESCE(last_sc.to_type, 'free') AS tier
RETURN tier, count(*) AS subscribers
ORDER BY
  CASE tier
    WHEN 'free' THEN 0
    WHEN 'pro' THEN 1
    WHEN 'pro_plus' THEN 2
    WHEN 'pro_max' THEN 3
    ELSE 99
  END
"""


# ===== 헬퍼 =====


def token_cost_krw(total_tokens: int) -> int:
    """토큰 → 원가 (KRW). 1M tokens 당 TOKEN_UNIT_PRICE."""
    if total_tokens <= 0:
        return 0
    return int(total_tokens * TOKEN_UNIT_PRICE_PER_M_KRW / 1_000_000)


def end_of_month_iso(year: int, month: int) -> str:
    """(year, month) → 그 달 마지막 날 23:59:59 UTC ISO datetime.

    매출 계산용 — "그 달 어느 시점이라도 유료 등급이었던 사용자"의 마지막 등급
    스냅샷 시점.
    """
    last_day = calendar.monthrange(year, month)[1]
    dt = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return dt.isoformat()


def is_future_month(year: int, month: int) -> bool:
    """주어진 (year, month) 가 현재보다 미래?"""
    now = datetime.now(timezone.utc)
    if year > now.year:
        return True
    if year == now.year and month > now.month:
        return True
    return False


def is_current_month(year: int, month: int) -> bool:
    now = datetime.now(timezone.utc)
    return year == now.year and month == now.month


# ===== 함수 =====


async def get_daily_revenue(
    start_iso: str, end_iso: str,
) -> List[dict]:
    """
    [2026-05-18] 일별 매출 (Payment + RefundRecord 기준).

    Returns: [{date, gross_paid, total_refunded, net_revenue, pay_count}, ...]

    date 는 'YYYY-MM-DD'. 결제/환불 둘 다 없는 날은 row 없음 (FE 에서 빈 날 채움).
    """
    rows = await neo4j_client.run_cypher(
        _DAILY_REVENUE_CYPHER,
        {"start_iso": start_iso, "end_iso": end_iso},
    )
    return [
        {
            "date": r.get("date") or "",
            "gross_paid": int(r.get("gross_paid") or 0),
            "total_refunded": int(r.get("total_refunded") or 0),
            "net_revenue": int(r.get("net_revenue") or 0),
            "pay_count": int(r.get("pay_count") or 0),
        }
        for r in rows if r.get("date")
    ]


async def get_actual_revenue_for_period(
    start_iso: str, end_iso: str,
) -> dict:
    """
    [2026-05-18] 지정 기간의 실 매출 (Payment 노드 기반).

    Returns:
        {
            'gross_paid': int,    # 총 결제 금액 (환불 전)
            'total_refunded': int, # 총 환불 금액
            'net_revenue': int,    # gross_paid - total_refunded
            'pay_count': int       # 결제 건수
        }
    """
    rows = await neo4j_client.run_cypher(
        _ACTUAL_REVENUE_BY_PERIOD_CYPHER,
        {"start_iso": start_iso, "end_iso": end_iso},
    )
    if not rows:
        return {"gross_paid": 0, "total_refunded": 0, "net_revenue": 0, "pay_count": 0}
    r = rows[0]
    gross = int(r.get("gross_paid") or 0)
    refunded = int(r.get("total_refunded") or 0)
    return {
        "gross_paid": gross,
        "total_refunded": refunded,
        "net_revenue": max(0, gross - refunded),
        "pay_count": int(r.get("pay_count") or 0),
    }


def _month_period_iso(year: int, month: int) -> tuple[str, str]:
    """그 달 start/end ISO (start inclusive, end exclusive — 다음달 1일)."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start.isoformat(), end.isoformat()


async def get_tier_breakdown() -> List[TierBreakdown]:
    """등급별 사용자 수 + 토큰 사용 합계 (현재 시점)."""
    rows = await neo4j_client.run_cypher(_BREAKDOWN_BY_TIER_CYPHER)
    return [
        TierBreakdown(
            tier=r.get("tier") or SUBSCRIPTION_FREE,
            subscribers=int(r.get("subscribers") or 0),
            total_tokens=int(r.get("total_tokens") or 0),
        )
        for r in rows
    ]


async def get_active_subscribers_at(at_iso: str) -> dict[str, int]:
    """
    특정 시점의 등급별 활성 구독자 수 — SubscriptionChange 이력 기반.

    각 user 의 그 시점 이전 마지막 SubscriptionChange.to_type 사용.
    이력 없으면 'free' (가입 default).

    Args:
        at_iso: 기준 시점 ISO datetime (e.g. "2026-05-31T23:59:59+00:00").

    Returns:
        {'free': 900, 'pro': 70, 'pro_plus': 20, 'pro_max': 10}.
        모든 등급 키가 있지는 않음 — 호출자가 .get(tier, 0) 으로 안전 조회.
    """
    rows = await neo4j_client.run_cypher(
        _BREAKDOWN_AT_TIMESTAMP_CYPHER, {"at": at_iso}
    )
    return {
        (r.get("tier") or SUBSCRIPTION_FREE): int(r.get("subscribers") or 0)
        for r in rows
    }


async def compute_summary(
    *,
    pricing_map: dict[str, int],
    infra_cost_krw: int,
) -> RevenueSummary:
    """현재 시점 요약 — MRR + 사용 현황 + 순이익.

    Args:
        pricing_map: tier → final_price (PricingConfig).
                     {'free': 0, 'pro': 9900, 'pro_plus': 17900, 'pro_max': 29900}
        infra_cost_krw: 이번달 인프라 비용 (admin 입력 또는 default).

    Returns:
        RevenueSummary.
    """
    raw_breakdown = await get_tier_breakdown()

    breakdown: List[TierBreakdown] = []
    mrr = 0
    total_tokens = 0
    total_subs = 0
    total_users = 0

    for b in raw_breakdown:
        total_users += b.subscribers
        total_tokens += b.total_tokens
        tier_revenue = 0
        if b.tier != SUBSCRIPTION_FREE:
            total_subs += b.subscribers
            tier_revenue = b.subscribers * pricing_map.get(b.tier, 0)
            mrr += tier_revenue
        # 등급별 매출을 채워 llm_cost_krw/profit_krw(파생)가 정확히 계산되게 한다.
        breakdown.append(replace(b, revenue_krw=tier_revenue))

    llm_cost = token_cost_krw(total_tokens)
    profit = mrr - llm_cost - infra_cost_krw
    arpu = int(mrr / total_subs) if total_subs > 0 else 0

    # [2026-05-18] 이번달 실 결제 매출 추가
    now = datetime.now(timezone.utc)
    start_iso, end_iso = _month_period_iso(now.year, now.month)
    actual = await get_actual_revenue_for_period(start_iso, end_iso)

    return RevenueSummary(
        breakdown=breakdown,
        mrr_krw=mrr,
        llm_cost_krw=llm_cost,
        infra_cost_krw=infra_cost_krw,
        profit_krw=profit,
        total_subscribers=total_subs,
        total_users=total_users,
        arpu_krw=arpu,
        actual_revenue_krw=actual["net_revenue"],
        actual_refund_krw=actual["total_refunded"],
        payment_count=actual["pay_count"],
    )


async def compute_monthly(
    *,
    year: int,
    month: int,
    pricing_map: dict[str, int],
    infra_cost_krw: int,
) -> MonthlyRevenue:
    """월별 매출/원가/순이익 — SubscriptionChange 이력 기반 (2026-05 갱신).

    [매출 계산]
    - 미래 달: 0 (아직 발생 안 함)
    - 과거 / 현재 달: 그 달 말일 기준 활성 구독자 (SubscriptionChange 이력)
                      × 현재 가격 (PricingConfig). 그 시점 가격이 다를 수 있으나
                      가격 변경 빈도 낮아 현재 가격 적용 (P1 정확화 가능).

    [LLM 원가]
    - 현재 달: 모든 user 의 usage_total_tokens 합 × 토큰 단가 (실측)
    - 과거 / 미래 달: 0 — 월간 reset 정책상 과거 데이터 없음.
                      FE 에서 "—" 또는 "N/A" 로 표시.

    [인프라 비용]
    - admin 입력값 그대로 (미래 달도 admin 이 계획값 입력 가능).

    [순이익]
    = mrr - llm_cost - infra_cost. 과거 달은 llm_cost = 0 이라 매출 - 인프라.
    """
    # 미래 달 — 모든 값 0 (인프라만 admin 입력 시 표시).
    if is_future_month(year, month):
        return MonthlyRevenue(
            year=year, month=month,
            mrr_krw=0, llm_cost_krw=0,
            infra_cost_krw=infra_cost_krw,
            profit_krw=-infra_cost_krw,
            llm_cost_tracked=False,
        )

    # 그 달 말일 기준 활성 구독자 (이력 기반)
    at_iso = end_of_month_iso(year, month)
    subscribers_by_tier = await get_active_subscribers_at(at_iso)

    # 매출 — 유료 등급만 (Free 제외) × 현재 가격
    mrr = sum(
        subscribers * pricing_map.get(tier, 0)
        for tier, subscribers in subscribers_by_tier.items()
        if tier != SUBSCRIPTION_FREE
    )

    # LLM 원가 — 현재 달만 정확 (월간 reset 정책상 과거는 추적 불가)
    current_month = is_current_month(year, month)
    if current_month:
        breakdown = await get_tier_breakdown()
        total_tokens = sum(b.total_tokens for b in breakdown)
        llm_cost = token_cost_krw(total_tokens)
    else:
        llm_cost = 0  # 과거 달은 추적 불가

    profit = mrr - llm_cost - infra_cost_krw

    # [2026-05-18] 그 달 실 결제 매출 추가
    start_iso, end_iso = _month_period_iso(year, month)
    actual = await get_actual_revenue_for_period(start_iso, end_iso)

    return MonthlyRevenue(
        year=year,
        month=month,
        mrr_krw=mrr,
        llm_cost_krw=llm_cost,
        infra_cost_krw=infra_cost_krw,
        profit_krw=profit,
        llm_cost_tracked=current_month,
        actual_revenue_krw=actual["net_revenue"],
        actual_refund_krw=actual["total_refunded"],
        payment_count=actual["pay_count"],
    )
