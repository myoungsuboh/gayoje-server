"""
Pricing 라우트 — 공개 가격 조회 + admin 가격 수정.

[엔드포인트]
- GET    /api/pricing                 → 공개. 모든 등급 가격 (FE 부팅 시 fetch).
- GET    /api/admin/pricing           → admin 전체 조회 (변경 이력 별도 audit-logs).
- PUT    /api/admin/pricing/{tier}    → admin 가격 수정.

[설계 의도]
가격은 FE 의 부팅 시점에 1회 fetch → 모든 컴포넌트가 동일 값 참조. 캐시는 BE
응답 자체. 가격 변경 시 사용자 새로고침 또는 다음 부팅에 반영 (실시간 push 없음
— OK 정책).

[보안]
공개 라우트 (/pricing): 인증 불필요. 누구나 볼 수 있는 정보.
admin 라우트: get_admin_user 의존성 + slowapi rate limit.
가격 변경은 audit_repository 에 ACTION_PRICING_UPDATE 로 영구 기록.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.limiter import limiter
from app.core.security import get_admin_user
from app.core.subscription import SUBSCRIPTION_TYPES
from app.service import audit_repository, pricing_repository
from app.service.audit_repository import ACTION_PRICING_UPDATE
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

# 공개 라우트와 admin 라우트가 다른 prefix 라 별도 router.
public_router = APIRouter(prefix="/api", tags=["Pricing"])
admin_router = APIRouter(prefix="/api/admin", tags=["Admin", "Pricing"])


# ===== Response DTOs =====


class PricingItem(BaseModel):
    tier: str = Field(..., description="'free' | 'pro' | 'pro_plus' | 'pro_max'")
    base_price: int = Field(..., description="정가 (최소 단위: USD 센트 / KRW 원)")
    discount_pct: int = Field(..., description="할인율 (0-100)")
    final_price: int = Field(..., description="할인 적용 후 최종가 (자동 계산, 통화별 반올림)")
    currency: str = Field("USD", description="'USD' | 'KRW' (legacy)")
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class PricingListResponse(BaseModel):
    pricing: List[PricingItem]


# ===== Request DTOs =====


class UpdatePricingRequest(BaseModel):
    base_price: int = Field(
        ..., ge=0, le=10_000_000,
        description="정가 (최소 단위: USD 센트). 0 ~ 10,000,000(=$100,000)",
    )
    discount_pct: int = Field(..., ge=0, le=100, description="할인율 %. 0-100")


# ===== 공개 라우트 =====


@public_router.get("/pricing", response_model=PricingListResponse)
@limiter.limit("60/minute")
async def get_public_pricing(request: Request) -> PricingListResponse:
    """
    공개 가격 조회 — FE 부팅 시 1회 호출.

    인증 불필요. 모든 등급의 정가/할인율/최종가 반환.
    """
    rows = await pricing_repository.list_pricing()
    return PricingListResponse(
        pricing=[PricingItem(**r.to_dict()) for r in rows]
    )


# ===== admin 라우트 =====


@admin_router.get("/pricing", response_model=PricingListResponse)
@limiter.limit("60/minute")
async def list_pricing_admin_route(
    request: Request,
    _admin: UserPublic = Depends(get_admin_user),
) -> PricingListResponse:
    """admin 전체 가격 조회. 공개 라우트와 동일 데이터 (변경 이력은 별도 audit-logs)."""
    rows = await pricing_repository.list_pricing()
    return PricingListResponse(
        pricing=[PricingItem(**r.to_dict()) for r in rows]
    )


@admin_router.put("/pricing/{tier}", response_model=PricingItem)
@limiter.limit("30/minute")
async def update_pricing_route(
    request: Request,
    tier: str,
    payload: UpdatePricingRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> PricingItem:
    """
    가격 수정 — base_price(최소 단위: USD 센트) + discount_pct.

    final_price 는 자동 계산: 통화별 반올림 (USD 센트 / KRW 100원). currency 는 변경 안 함.

    변경은 audit_repository 에 ACTION_PRICING_UPDATE 로 영구 기록 (분쟁 추적용).
    """
    if tier not in SUBSCRIPTION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tier 는 {SUBSCRIPTION_TYPES} 중 하나여야 합니다.",
        )

    # 변경 전 값 (감사로그 payload 에 from/to 기록)
    before = await pricing_repository.get_pricing(tier)
    if before is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"등급 '{tier}' 의 가격 설정을 찾을 수 없습니다.",
        )

    after = await pricing_repository.update_pricing(
        tier=tier,
        base_price=payload.base_price,
        discount_pct=payload.discount_pct,
        updated_by=admin.email,
    )
    if after is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="가격 수정에 실패했습니다.",
        )

    # 감사 로그 — best-effort (기록 실패가 본 액션을 막지 않음)
    try:
        await audit_repository.write(
            actor_email=admin.email,
            action=ACTION_PRICING_UPDATE,
            target_email="",
            payload={
                "tier": tier,
                "from": {
                    "base_price": before.base_price,
                    "discount_pct": before.discount_pct,
                    "final_price": before.final_price,
                },
                "to": {
                    "base_price": after.base_price,
                    "discount_pct": after.discount_pct,
                    "final_price": after.final_price,
                },
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("pricing audit log 실패 (tier=%s): %s", tier, e)

    return PricingItem(**after.to_dict())
