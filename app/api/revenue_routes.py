"""
Revenue 라우트 — admin 수익 대시보드 (매출/원가/순이익 + 인프라 비용 관리).

[엔드포인트]
- GET    /api/admin/revenue/summary            → 현재 시점 MRR + 분포
- GET    /api/admin/revenue/monthly?year=&month= → 월별 (단일)
- GET    /api/admin/revenue/yearly?year=       → 연간 (12개월)
- GET    /api/admin/infra-cost?year=&month=    → 월별 인프라 비용 조회
- PUT    /api/admin/infra-cost                 → 월별 인프라 비용 upsert

[설계]
모두 admin 전용 — get_admin_user 의존성 + slowapi rate limit.
revenue 계산은 PricingConfig (현재 가격) + 토큰 사용 (실측) + InfraCost (admin 입력)
조합. 결제 시스템 미반영 상태라 모두 "예상치" — FE 에 안내.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.core.limiter import limiter
from app.core.security import get_admin_user
from app.service import (
    audit_repository,
    infra_cost_repository,
    pricing_repository,
    revenue_repository,
)
from app.service.audit_repository import ACTION_INFRA_COST_UPDATE
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["Admin", "Revenue"])


# ===== Response DTOs =====


class TierBreakdownItem(BaseModel):
    tier: str
    subscribers: int
    total_tokens: int
    # 등급 단위 매출/원가/기여 마진 (인프라 제외). FE 가 단가 하드코딩 없이 그대로 표시.
    revenue_krw: int = Field(default=0, description="등급 매출 (구독자 × 현재가격, Free=0)")
    llm_cost_krw: int = Field(default=0, description="등급 토큰 원가 (1,250원/M)")
    profit_krw: int = Field(default=0, description="등급 기여 마진 = 매출 - 토큰원가 (인프라 제외)")


class RevenueSummaryResponse(BaseModel):
    breakdown: List[TierBreakdownItem]
    mrr_krw: int = Field(..., description="Monthly Recurring Revenue — 활성 구독자 × 현재 가격 (추정)")
    llm_cost_krw: int = Field(..., description="이번달 토큰 원가 추정 (1,250원/M tokens)")
    infra_cost_krw: int = Field(..., description="이번달 인프라 비용 (admin 입력 또는 default)")
    profit_krw: int = Field(..., description="mrr - llm_cost - infra_cost")
    total_subscribers: int = Field(..., description="유료 구독자 합 (Pro/Pro+/Pro Max)")
    total_users: int
    arpu_krw: int = Field(..., description="Average Revenue Per (paid) User")
    # [2026-05-18] Payment 노드 기반 실 매출
    actual_revenue_krw: int = Field(default=0, description="이번달 실 매출 (paid - 환불)")
    actual_refund_krw: int = Field(default=0, description="이번달 누적 환불")
    payment_count: int = Field(default=0, description="이번달 결제 건수")


class MonthlyRevenueItem(BaseModel):
    year: int
    month: int
    mrr_krw: int
    llm_cost_krw: int
    infra_cost_krw: int
    profit_krw: int
    # [2026-05] llm_cost_tracked=False 면 과거/미래 달 — FE 가 "—" 표시.
    # 월간 reset 정책상 과거 토큰 사용량 추적 불가.
    llm_cost_tracked: bool = False
    # [2026-05-18] Payment 노드 기반 실 매출
    actual_revenue_krw: int = 0
    actual_refund_krw: int = 0
    payment_count: int = 0


class YearlyRevenueResponse(BaseModel):
    year: int
    months: List[MonthlyRevenueItem]
    total_mrr_krw: int
    total_llm_cost_krw: int
    total_infra_cost_krw: int
    total_profit_krw: int


# [2026-05-18] 일별 매출 — Payment 노드 기반 실 매출
class DailyRevenueItem(BaseModel):
    date: str = Field(..., description="YYYY-MM-DD")
    gross_paid: int = Field(..., description="그 날 결제된 총액")
    total_refunded: int = Field(..., description="그 날 환불된 총액 (RefundRecord)")
    net_revenue: int = Field(..., description="gross_paid - total_refunded")
    pay_count: int = Field(..., description="그 날 결제 건수")


class DailyRevenueResponse(BaseModel):
    start_date: str
    end_date: str
    days: List[DailyRevenueItem]
    total_gross_paid: int
    total_refunded: int
    total_net_revenue: int
    total_pay_count: int


class InfraCostLineItem(BaseModel):
    """비용 항목 한 줄 — 서버 운영비 / LLM API / 지적재산 등록비 등."""
    category: str = Field(default="기타", max_length=60)
    amount_krw: int = Field(default=0, ge=0, le=100_000_000)
    note: str = Field(default="", max_length=200)
    # 매월 반복되는 고정비(서버 운영비·AI 구독 등) 표식 — '고정비 일괄 적용' 대상.
    fixed: bool = False


class InfraCostItem(BaseModel):
    year: int
    month: int
    amount_krw: int
    note: str
    items: List[InfraCostLineItem] = Field(default_factory=list)
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class UpsertInfraCostRequest(BaseModel):
    year: int = Field(..., ge=2020, le=2100)
    month: int = Field(..., ge=1, le=12)
    # 항목(items) 이 있으면 amount_krw 는 서버가 합계로 강제. 없으면 이 lump 사용(하위호환).
    amount_krw: int = Field(default=0, ge=0, le=100_000_000)
    note: str = Field(default="", max_length=500)
    items: List[InfraCostLineItem] = Field(default_factory=list)


# ===== 헬퍼 =====


async def _get_pricing_map() -> dict[str, int]:
    """현재 가격 dict — tier → final_price. revenue 계산에서 사용."""
    rows = await pricing_repository.list_pricing()
    return {p.tier: p.final_price for p in rows}


# ===== 라우트 =====


@router.get("/revenue/summary", response_model=RevenueSummaryResponse)
@limiter.limit("60/minute")
async def get_revenue_summary(
    request: Request,
    _admin: UserPublic = Depends(get_admin_user),
) -> RevenueSummaryResponse:
    """
    현재 시점 수익 요약 (대시보드 메인 카드).

    매출 = 활성 구독자 × 현재 가격 (PricingConfig)
    LLM 원가 = total_tokens × 1,250원/M (Gemini 2.5 Flash 평균)
    인프라 = 이번달 InfraCost (없으면 default 80,000원)
    순이익 = 매출 - LLM원가 - 인프라
    """
    pricing_map = await _get_pricing_map()
    year, month = infra_cost_repository.current_year_month()
    infra = await infra_cost_repository.get_infra_cost(year, month)
    infra_amount = (
        infra.amount_krw if infra else infra_cost_repository.default_infra_cost_for_month()
    )
    summary = await revenue_repository.compute_summary(
        pricing_map=pricing_map,
        infra_cost_krw=infra_amount,
    )
    return RevenueSummaryResponse(**summary.to_dict())


@router.get("/revenue/monthly", response_model=MonthlyRevenueItem)
@limiter.limit("60/minute")
async def get_revenue_monthly(
    request: Request,
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    _admin: UserPublic = Depends(get_admin_user),
) -> MonthlyRevenueItem:
    """특정 월의 매출/원가/순이익."""
    pricing_map = await _get_pricing_map()
    infra = await infra_cost_repository.get_infra_cost(year, month)
    infra_amount = (
        infra.amount_krw if infra else infra_cost_repository.default_infra_cost_for_month()
    )
    m = await revenue_repository.compute_monthly(
        year=year,
        month=month,
        pricing_map=pricing_map,
        infra_cost_krw=infra_amount,
    )
    return MonthlyRevenueItem(**m.to_dict())


@router.get("/revenue/yearly", response_model=YearlyRevenueResponse)
@limiter.limit("60/minute")
async def get_revenue_yearly(
    request: Request,
    year: int = Query(...),
    _admin: UserPublic = Depends(get_admin_user),
) -> YearlyRevenueResponse:
    """연간 12개월 매출/원가/순이익 + 연간 총합.

    [2026-05 갱신 — 매출은 이력 기반 정확]
    각 월 매출 = 그 달 말일 기준 활성 구독자 (SubscriptionChange 이력) × 현재 가격.
    과거 달도 정확히 계산.

    [한계 — 토큰 원가]
    월간 reset 정책으로 과거 달의 토큰 사용량 데이터 없음 → 과거 달 LLM 원가는 0
    + llm_cost_tracked=False. FE 가 "—" 또는 "추적 시작 후 부터" 안내.
    """
    pricing_map = await _get_pricing_map()
    # 연간 인프라 비용 조회 (set 된 월만)
    infra_list = await infra_cost_repository.list_infra_cost_by_year(year)
    infra_map = {c.month: c.amount_krw for c in infra_list}

    months: List[MonthlyRevenueItem] = []
    total_mrr = 0
    total_llm = 0
    total_infra = 0
    total_profit = 0

    for month in range(1, 13):
        infra_amount = infra_map.get(
            month, infra_cost_repository.default_infra_cost_for_month()
        )
        m = await revenue_repository.compute_monthly(
            year=year,
            month=month,
            pricing_map=pricing_map,
            infra_cost_krw=infra_amount,
        )
        item = MonthlyRevenueItem(**m.to_dict())
        months.append(item)
        total_mrr += item.mrr_krw
        total_llm += item.llm_cost_krw
        total_infra += item.infra_cost_krw
        total_profit += item.profit_krw

    return YearlyRevenueResponse(
        year=year,
        months=months,
        total_mrr_krw=total_mrr,
        total_llm_cost_krw=total_llm,
        total_infra_cost_krw=total_infra,
        total_profit_krw=total_profit,
    )


@router.get("/revenue/daily", response_model=DailyRevenueResponse)
@limiter.limit("60/minute")
async def get_revenue_daily(
    request: Request,
    days: int = Query(30, ge=1, le=365, description="조회 기간 (일수, 1~365)"),
    _admin: UserPublic = Depends(get_admin_user),
) -> DailyRevenueResponse:
    """
    [2026-05-18] 최근 N일 일별 매출 — Payment 노드 paid_at 기준 + RefundRecord 환불.

    빈 날 (결제/환불 0건) 도 row 로 포함 → FE 차트가 일별 0 표시 가능.
    """
    from datetime import datetime, timedelta, timezone, date as date_cls

    now = datetime.now(timezone.utc)
    start_dt = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    rows = await revenue_repository.get_daily_revenue(
        start_iso=start_dt.isoformat(),
        end_iso=end_dt.isoformat(),
    )

    # 빈 날 채우기 — 차트가 일별로 연속해서 표시되도록.
    by_date = {r["date"]: r for r in rows}
    items: List[DailyRevenueItem] = []
    total_gross = 0
    total_refund = 0
    total_net = 0
    total_count = 0
    cur = start_dt.date()
    end_date = (end_dt - timedelta(days=1)).date()
    while cur <= end_date:
        key = cur.isoformat()
        r = by_date.get(key) or {
            "date": key, "gross_paid": 0, "total_refunded": 0,
            "net_revenue": 0, "pay_count": 0,
        }
        items.append(DailyRevenueItem(**r))
        total_gross += r["gross_paid"]
        total_refund += r["total_refunded"]
        total_net += r["net_revenue"]
        total_count += r["pay_count"]
        cur += timedelta(days=1)

    return DailyRevenueResponse(
        start_date=start_dt.date().isoformat(),
        end_date=end_date.isoformat(),
        days=items,
        total_gross_paid=total_gross,
        total_refunded=total_refund,
        total_net_revenue=total_net,
        total_pay_count=total_count,
    )


@router.get("/infra-cost", response_model=Optional[InfraCostItem])
@limiter.limit("60/minute")
async def get_infra_cost_route(
    request: Request,
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    _admin: UserPublic = Depends(get_admin_user),
):
    """월별 인프라 비용 조회. 미설정이면 null + default."""
    cost = await infra_cost_repository.get_infra_cost(year, month)
    if cost is None:
        return None
    return InfraCostItem(**cost.to_dict())


@router.put("/infra-cost", response_model=InfraCostItem)
@limiter.limit("30/minute")
async def upsert_infra_cost_route(
    request: Request,
    payload: UpsertInfraCostRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> InfraCostItem:
    """월별 인프라 비용 upsert + audit log."""
    before = await infra_cost_repository.get_infra_cost(payload.year, payload.month)
    cost = await infra_cost_repository.upsert_infra_cost(
        year=payload.year,
        month=payload.month,
        amount_krw=payload.amount_krw,
        note=payload.note,
        updated_by=admin.email,
        items=[i.model_dump() for i in payload.items],
    )
    if cost is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="인프라 비용 저장에 실패했습니다.",
        )
    try:
        await audit_repository.write(
            actor_email=admin.email,
            action=ACTION_INFRA_COST_UPDATE,
            target_email="",
            payload={
                "year": payload.year,
                "month": payload.month,
                "from": before.to_dict() if before else None,
                "to": cost.to_dict(),
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("infra_cost audit log 실패: %s", e)

    return InfraCostItem(**cost.to_dict())
