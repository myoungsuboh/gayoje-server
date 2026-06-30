"""
Coupon 라우트 — 결제 전 검증 (user) + 발급 / 조회 / 회수 (admin).

[엔드포인트]
- POST /api/coupons/validate          → 사용자, 결제 전 코드 유효성 확인
- POST /api/admin/coupons             → admin, 쿠폰 1장 발급 (code 자동 생성 가능)
- POST /api/admin/coupons/bulk        → admin, N장 일괄 발급 (회사 단위 배포용)
- GET  /api/admin/coupons             → admin, 발급 이력 조회
- DELETE /api/admin/coupons/{code}    → admin, 쿠폰 회수 (active=false)

[redemption 위치]
실제 사용은 billing_routes.subscribe_route 안에서 `coupon_repository.redeem_coupon`
호출. 이 라우트는 검증/관리만 담당.

[보안]
- /validate: 인증 필요 (로그인 사용자만, 자기 자신만 검증) — rate limit 강함
- admin/*: get_admin_user 의존성 + audit_repository 기록
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.limiter import limiter
from app.core.security import get_admin_user, get_current_user
from app.core.subscription import PAID_SUBSCRIPTIONS
from app.service import audit_repository, coupon_repository
from app.service.coupon_repository import COUPON_TIER_ANY
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

# 사용자 / admin 라우트가 다른 보안 모델이라 별도 router.
user_router = APIRouter(prefix="/api", tags=["Coupon"])
admin_router = APIRouter(prefix="/api/admin", tags=["Admin", "Coupon"])


# ===== Response DTO =====


class CouponView(BaseModel):
    code: str
    applies_to_tier: str
    free_months: int
    max_uses: int
    used_count: int
    active: bool
    note: str
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    expires_at: Optional[str] = None
    remaining: Optional[int] = None


class CouponListResponse(BaseModel):
    coupons: List[CouponView]


class CouponValidateResponse(BaseModel):
    ok: bool
    code: str
    reason: str = ""
    free_months: int = 0
    applies_to_tier: str = ""
    # FE 가 즉시 "₩X → 무료 N개월" 형식으로 표시할 수 있도록.
    message: str = ""


class BulkCreateResponse(BaseModel):
    created: List[CouponView]
    skipped: int = 0


# ===== Request DTO =====


class ValidateCouponRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    tier: str = Field(..., description="결제하려는 등급 ('pro' | 'pro_plus' | 'pro_max')")


class CreateCouponRequest(BaseModel):
    code: Optional[str] = Field(
        None, max_length=64,
        description="고정 코드 (없으면 자동 생성 BETA-XXXXXX 형식)",
    )
    applies_to_tier: str = Field(
        COUPON_TIER_ANY,
        description="'any' = 모든 유료 등급 / 또는 'pro' | 'pro_plus' | 'pro_max'",
    )
    free_months: int = Field(1, ge=1, le=12, description="무료 제공 개월수 (1-12)")
    max_uses: int = Field(1, ge=0, le=10_000, description="최대 사용 횟수 (0 = 무제한)")
    expires_at: Optional[datetime] = Field(
        None, description="발급 만료. 미지정 시 무기한.",
    )
    note: str = Field("", max_length=500, description="admin 메모 (배포 채널, 대상 등)")


class BulkCreateCouponRequest(BaseModel):
    count: int = Field(..., ge=1, le=100, description="한 번에 생성할 쿠폰 갯수 (1-100)")
    applies_to_tier: str = Field(COUPON_TIER_ANY)
    free_months: int = Field(1, ge=1, le=12)
    max_uses: int = Field(1, ge=1, le=10_000, description="각 코드의 max_uses")
    expires_at: Optional[datetime] = None
    note: str = Field("", max_length=500)


# ===== 사용자 라우트 =====


# reason → 사용자 친화 메시지.
_REASON_MESSAGE = {
    "not_found": "유효하지 않은 쿠폰 코드입니다.",
    "inactive": "회수된 쿠폰입니다.",
    "expired": "만료된 쿠폰입니다.",
    "exhausted": "이 쿠폰의 모든 사용 횟수가 소진되었습니다.",
    "tier_mismatch": "이 쿠폰은 선택하신 등급에 적용할 수 없습니다.",
    "already_redeemed": "이미 사용한 쿠폰입니다.",
}


@user_router.post(
    "/coupons/validate",
    response_model=CouponValidateResponse,
    summary="쿠폰 코드 유효성 확인 (결제 전)",
)
@limiter.limit("20/minute")
async def validate_coupon_route(
    request: Request,
    payload: ValidateCouponRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> CouponValidateResponse:
    """쿠폰 코드 + 결제 등급 → 적용 가능 여부.

    redeem 은 별도 (subscribe 흐름 내부 atomic). FE 는 이 응답으로
    "₩X → 무료 N 개월" 미리보기를 즉시 표시.
    """
    result = await coupon_repository.validate_coupon(
        code=payload.code, user_email=current_user.email, tier=payload.tier,
    )
    if not result.ok:
        msg = _REASON_MESSAGE.get(result.reason, "쿠폰을 적용할 수 없습니다.")
        return CouponValidateResponse(
            ok=False,
            code=result.code,
            reason=result.reason,
            message=msg,
        )
    c = result.coupon
    return CouponValidateResponse(
        ok=True,
        code=result.code,
        free_months=c.free_months if c else 0,
        applies_to_tier=c.applies_to_tier if c else "",
        message=f"무료 {c.free_months if c else 0}개월 적용 가능합니다.",
    )


# ===== admin 라우트 =====


def _coupon_to_view(c: coupon_repository.Coupon) -> CouponView:
    d = c.to_dict()
    return CouponView(**d)


@admin_router.post(
    "/coupons",
    response_model=CouponView,
    summary="쿠폰 1장 발급",
)
@limiter.limit("60/minute")
async def create_coupon_route(
    request: Request,
    payload: CreateCouponRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> CouponView:
    if payload.applies_to_tier != COUPON_TIER_ANY and payload.applies_to_tier not in PAID_SUBSCRIPTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"applies_to_tier 는 'any' 또는 {PAID_SUBSCRIPTIONS} 중 하나여야 합니다.",
        )

    # expires_at 은 UTC tz-aware 로 통일.
    exp = payload.expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)

    coupon = await coupon_repository.create_coupon(
        code=payload.code,
        applies_to_tier=payload.applies_to_tier,
        free_months=payload.free_months,
        max_uses=payload.max_uses,
        expires_at=exp,
        note=payload.note,
        created_by=admin.email,
    )
    if not coupon:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="쿠폰 발급 실패 — 코드 충돌 또는 입력 오류입니다.",
        )

    # 감사 로그 (best-effort)
    try:
        await audit_repository.write(
            actor_email=admin.email,
            action=audit_repository.ACTION_COUPON_CREATE,
            target_email="",
            payload={
                "code": coupon.code,
                "applies_to_tier": coupon.applies_to_tier,
                "free_months": coupon.free_months,
                "max_uses": coupon.max_uses,
                "expires_at": coupon.expires_at,
                "note": coupon.note,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("coupon: audit 기록 실패 (%s)", e)

    return _coupon_to_view(coupon)


@admin_router.post(
    "/coupons/bulk",
    response_model=BulkCreateResponse,
    summary="쿠폰 N장 일괄 발급 (베타 신청 폼 응답 처리용)",
)
@limiter.limit("30/minute")
async def bulk_create_coupons_route(
    request: Request,
    payload: BulkCreateCouponRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> BulkCreateResponse:
    """같은 조건의 쿠폰 여러 장을 한 번에 발급. 각 코드는 자동 생성.

    회사 단위로 N명에게 코드 배포하는 케이스 (예: "한 회사 5명 무료 1개월").
    실패한 건은 skipped 로 카운트만, 성공한 건만 created 에 반환.
    """
    if payload.applies_to_tier != COUPON_TIER_ANY and payload.applies_to_tier not in PAID_SUBSCRIPTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"applies_to_tier 는 'any' 또는 {PAID_SUBSCRIPTIONS} 중 하나여야 합니다.",
        )

    exp = payload.expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)

    created: List[CouponView] = []
    skipped = 0
    for _ in range(payload.count):
        coupon = await coupon_repository.create_coupon(
            code=None,  # 자동 생성
            applies_to_tier=payload.applies_to_tier,
            free_months=payload.free_months,
            max_uses=payload.max_uses,
            expires_at=exp,
            note=payload.note,
            created_by=admin.email,
        )
        if coupon:
            created.append(_coupon_to_view(coupon))
        else:
            skipped += 1

    # 감사 로그 — bulk 는 요약만.
    if created:
        try:
            await audit_repository.write(
                actor_email=admin.email,
                action=audit_repository.ACTION_COUPON_CREATE,
                target_email="",
                payload={
                    "bulk": True,
                    "count": len(created),
                    "skipped": skipped,
                    "applies_to_tier": payload.applies_to_tier,
                    "free_months": payload.free_months,
                    "max_uses": payload.max_uses,
                    "note": payload.note,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("coupon: bulk audit 기록 실패 (%s)", e)

    return BulkCreateResponse(created=created, skipped=skipped)


@admin_router.get(
    "/coupons",
    response_model=CouponListResponse,
    summary="쿠폰 발급 이력 조회",
)
@limiter.limit("60/minute")
async def list_coupons_route(
    request: Request,
    limit: int = 200,
    _admin: UserPublic = Depends(get_admin_user),
) -> CouponListResponse:
    rows = await coupon_repository.list_coupons(limit=limit)
    return CouponListResponse(coupons=[_coupon_to_view(c) for c in rows])


@admin_router.delete(
    "/coupons/{code}",
    response_model=CouponView,
    summary="쿠폰 회수 (active=false)",
)
@limiter.limit("30/minute")
async def revoke_coupon_route(
    request: Request,
    code: str,
    admin: UserPublic = Depends(get_admin_user),
) -> CouponView:
    before = await coupon_repository.get_coupon(code)
    if not before:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="쿠폰을 찾을 수 없습니다.",
        )
    if not before.active:
        # 이미 회수된 상태 — idempotent.
        return _coupon_to_view(before)

    ok = await coupon_repository.revoke_coupon(code)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="쿠폰 회수에 실패했습니다.",
        )

    try:
        await audit_repository.write(
            actor_email=admin.email,
            action=audit_repository.ACTION_COUPON_REVOKE,
            target_email="",
            payload={"code": before.code, "used_count": before.used_count},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("coupon: revoke audit 실패 (%s)", e)

    revoked = await coupon_repository.get_coupon(code)
    return _coupon_to_view(revoked or before)
