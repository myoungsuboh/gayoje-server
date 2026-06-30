"""
Admin Billing 라우트 — 환불 / 강제 구독 종료 / 결제 조회.

[정책 — 사용자 응답 2026-05-18]
환불 정책: **Admin 재량 환불 (동적)** — 자동 계산 없이 admin 이 건별 판단 후 금액 입력.

[엔드포인트]
- GET    /api/admin/billing/users/{email}           : 특정 사용자 구독 + 결제 이력
- GET    /api/admin/billing/payments/{payment_id}   : 결제 상세 (raw_response 포함)
- POST   /api/admin/billing/refund                  : 환불 (전체/부분)
- POST   /api/admin/billing/terminate               : 구독 강제 종료 + 등급 즉시 강등

[보안]
- get_admin_user 의존성 + slowapi rate limit
- 모든 환불은 audit_repository 영구 기록 (분쟁 evidence)
- payment.raw_response 는 admin 만 볼 수 있음 (PG 응답 PII 포함 가능성)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.limiter import limiter
from app.core.security import get_admin_user
from app.core.subscription import SUBSCRIPTION_FREE
from app.service import (
    admin_repository,
    audit_repository,
    payment_repository,
    subscription_repository,
)
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/admin/billing", tags=["Admin", "Billing"])


SYSTEM_BILLING_ACTOR = "SYSTEM:BILLING"


# ============================================================================
# Pydantic
# ============================================================================


class RefundRecordItem(BaseModel):
    id: str
    payment_id: str
    user_email: str
    amount: int
    reason: Optional[str] = None
    created_at: Optional[str] = None


class RefundRequest(BaseModel):
    payment_id: str = Field(..., description="환불할 Payment 노드 id")
    refund_amount: int = Field(..., gt=0, description="환불 금액 (KRW). 잔여 금액 이하.")
    reason: str = Field(..., min_length=1, max_length=500, description="환불 사유 — audit log")
    downgrade_to_free: bool = Field(
        False,
        description="환불 후 사용자를 즉시 free 로 강등 + Subscription terminate",
    )


class TerminateRequest(BaseModel):
    email: str = Field(..., description="대상 사용자 이메일")
    reason: str = Field(..., min_length=1, max_length=500)


class AdminPaymentView(BaseModel):
    id: str
    subscription_id: str
    user_email: str
    amount: int
    currency: str
    status: str
    purpose: str
    method: Optional[str] = None
    pg_payment_key: Optional[str] = None
    pg_order_id: Optional[str] = None
    paid_at: Optional[str] = None
    failed_at: Optional[str] = None
    fail_reason: Optional[str] = None
    refunded_at: Optional[str] = None
    refund_amount: int = 0
    refund_reason: Optional[str] = None
    raw_response: Optional[str] = None
    created_at: Optional[str] = None


class UserBillingSummary(BaseModel):
    subscription: Optional[Dict[str, Any]] = None
    payments: List[AdminPaymentView] = []


# ============================================================================
# 라우트
# ============================================================================


@router.get(
    "/users/{email}",
    response_model=UserBillingSummary,
    summary="특정 사용자의 구독 + 결제 이력 (admin)",
)
@limiter.limit("60/minute")
async def get_user_billing_route(
    request: Request,
    email: str,
    _admin: UserPublic = Depends(get_admin_user),
) -> UserBillingSummary:
    sub = await subscription_repository.get_latest_subscription(email)
    payments = await payment_repository.list_payments_by_user(email, limit=200)
    return UserBillingSummary(
        subscription=sub.to_dict() if sub else None,
        payments=[
            AdminPaymentView(
                id=p.id,
                subscription_id=p.subscription_id,
                user_email=p.user_email,
                amount=p.amount,
                currency=p.currency,
                status=p.status,
                purpose=p.purpose,
                method=p.method,
                pg_payment_key=p.pg_payment_key,
                pg_order_id=p.pg_order_id,
                paid_at=p.paid_at,
                failed_at=p.failed_at,
                fail_reason=p.fail_reason,
                refunded_at=p.refunded_at,
                refund_amount=p.refund_amount,
                refund_reason=p.refund_reason,
                raw_response=p.raw_response,
                created_at=p.created_at,
            )
            for p in payments
        ],
    )


@router.get(
    "/payments/{payment_id}",
    response_model=AdminPaymentView,
    summary="결제 상세 (raw_response 포함, admin)",
)
@limiter.limit("60/minute")
async def get_payment_detail_route(
    request: Request,
    payment_id: str,
    _admin: UserPublic = Depends(get_admin_user),
) -> AdminPaymentView:
    p = await payment_repository.get_payment_by_id(payment_id)
    if not p:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment 를 찾을 수 없습니다."
        )
    return AdminPaymentView(
        id=p.id, subscription_id=p.subscription_id, user_email=p.user_email,
        amount=p.amount, currency=p.currency, status=p.status, purpose=p.purpose,
        method=p.method, pg_payment_key=p.pg_payment_key, pg_order_id=p.pg_order_id,
        paid_at=p.paid_at, failed_at=p.failed_at, fail_reason=p.fail_reason,
        refunded_at=p.refunded_at, refund_amount=p.refund_amount,
        refund_reason=p.refund_reason, raw_response=p.raw_response,
        created_at=p.created_at,
    )


@router.get(
    "/payments/{payment_id}/refunds",
    response_model=List[RefundRecordItem],
    summary="결제의 환불 이력 (RefundRecord 노드 list, admin)",
)
@limiter.limit("60/minute")
async def list_payment_refunds_route(
    request: Request,
    payment_id: str,
    _admin: UserPublic = Depends(get_admin_user),
) -> List[RefundRecordItem]:
    """부분 환불 N건의 정확한 이력. audit log 의 refund_id 로 추적된 노드들."""
    records = await payment_repository.list_refund_records_for_payment(payment_id)
    return [
        RefundRecordItem(
            id=r.get("id") or "",
            payment_id=r.get("payment_id") or "",
            user_email=r.get("user_email") or "",
            amount=int(r.get("amount") or 0),
            reason=r.get("reason"),
            created_at=r.get("created_at"),
        )
        for r in records if r.get("id")
    ]


@router.post(
    "/refund",
    summary="환불 (admin) — Paddle 전환으로 비활성 (501)",
)
@limiter.limit("20/minute")
async def refund_route(
    request: Request,
    payload: RefundRequest,
    _admin: UserPublic = Depends(get_admin_user),
) -> None:
    """[2026-06 Paddle MoR 전환] Toss cancel API 기반 환불 라우트 제거.

    Paddle 은 MoR 라 환불·분쟁을 Paddle 대시보드에서 처리하고, 환불 시
    webhook(subscription.*)으로 구독 상태가 동기화된다. FE admin 화면이
    Paddle 안내로 개편될 때까지 라우트는 남겨 명확한 501 로 응답한다
    (RefundRecord/Payment 조회 라우트들은 과거 이력 열람용으로 보존).
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="환불은 Paddle 대시보드에서 처리하세요 — Toss 환불 라우트는 제거되었습니다.",
    )

@router.post(
    "/terminate",
    summary="구독 강제 종료 + 등급 free 강등 (admin)",
)
@limiter.limit("20/minute")
async def terminate_subscription_route(
    request: Request,
    payload: TerminateRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> Dict[str, Any]:
    sub = await subscription_repository.get_latest_subscription(payload.email)
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="구독을 찾을 수 없습니다."
        )

    ok = await subscription_repository.terminate(sub.id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="구독 종료에 실패했습니다.",
        )

    # 등급 free 강등
    await admin_repository.change_subscription(
        target_email=payload.email,
        to_type=SUBSCRIPTION_FREE,
        reason=f"admin_terminate:{payload.reason[:100]}",
        changed_by_email=admin.email,
    )

    try:
        await audit_repository.write(
            actor_email=admin.email,
            action=audit_repository.ACTION_SUBSCRIPTION_TERMINATE,
            target_email=payload.email,
            payload={
                "subscription_id": sub.id,
                "reason": payload.reason,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("audit terminate 실패: %s", e)

    return {"subscription_id": sub.id, "status": "canceled"}
