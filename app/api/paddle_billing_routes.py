"""
Paddle 부속 라우트 — FE 구독현황 + 고객포털 진입.

체크아웃은 FE(Paddle.js 오버레이), 결제 확정은 웹훅(paddle_webhook_routes).
여기는 웹훅이 영속화한 스냅샷 조회와, 구독관리(해지/재개/결제수단/영수증)를
위임할 Paddle 고객포털 세션 생성만 담당한다.

[포털 세션 — Paddle API]
POST {paddle_api_base}/customers/{customer_id}/portal-sessions
→ data.urls.general.overview 가 포털 진입 URL (단기 유효 — 매번 새로 생성).
docs: https://developer.paddle.com/api-reference/customer-portals/sessions/create-customer-portal-session
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.security import get_current_user
from app.core.subscription import (
    PAID_SUBSCRIPTIONS,
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    SUBSCRIPTION_PRO_MAX,
    SUBSCRIPTION_PRO_PLUS,
)
from app.service import paddle_subscription_repository
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paddle", tags=["Paddle", "Billing"])

# 등급 변경(PATCH)이 가능한 Paddle 구독 상태 — 살아있는 구독만.
# [2026-06 감사] past_due(미납 처리 중) 제거 — dunning 중 등급변경은 정산 충돌/과금 모호.
# canceled·paused·past_due 는 제외 → 신규 결제(체크아웃) 경로로 보낸다(409).
_CHANGEABLE_STATUSES = {"active", "trialing"}

# 등급 서열 — 업/다운그레이드 판별용(proration 모드 결정).
_TIER_RANK = {
    SUBSCRIPTION_FREE: 0,
    SUBSCRIPTION_PRO: 1,
    SUBSCRIPTION_PRO_PLUS: 2,
    SUBSCRIPTION_PRO_MAX: 3,
}
_VALID_CYCLES = {"monthly", "yearly"}


class ChangeSubscriptionRequest(BaseModel):
    """기존 구독자의 등급 변경 요청 — 신규 결제(체크아웃)와 구분된다."""

    tier: str = Field(..., description="대상 등급 (pro|pro_plus|pro_max)")
    cycle: str = Field("monthly", description="결제 주기 (monthly|yearly)")


@router.get("/subscription", summary="현재 사용자 Paddle 구독 스냅샷")
async def get_paddle_subscription_route(
    current_user: UserPublic = Depends(get_current_user),
) -> Dict[str, Any]:
    """없으면 subscription=null — FE 는 '구독 없음' 상태로 표시."""
    sub = await paddle_subscription_repository.get_by_email(current_user.email)
    return {"subscription": sub}


def _portal_url_from_body(body: Any) -> str:
    """Paddle 포털 세션 응답 → 진입 URL (없으면 '')."""
    if not isinstance(body, dict):
        return ""
    return str((((body.get("data") or {}).get("urls") or {}).get("general") or {}).get("overview") or "")


async def _post_portal_session(client: "httpx.AsyncClient", customer_id: str) -> "httpx.Response":
    """POST /customers/{id}/portal-sessions. subscription_ids 미전달 → 해당 customer 의 모든
    구독을 포털에 표시(명시 시 sub_id↔customer 불일치로 403 위험)."""
    return await client.post(
        f"{settings.paddle_api_base}/customers/{customer_id}/portal-sessions",
        headers={"Authorization": f"Bearer {settings.PADDLE_API_KEY}"},
        json={},
    )


async def _resolve_customer_id_by_email(client: "httpx.AsyncClient", email: str) -> str:
    """이메일로 Paddle 운영 customer_id 즉석 조회 (self-heal). 저장된 id 가 stale/환경
    불일치(샌드박스↔운영)일 때, 현재 환경(PADDLE_ENV)의 실제 customer 를 찾는다.
    실패/없음이면 '' — 호출부가 폴백 처리. active 우선, 없으면 첫 행."""
    try:
        resp = await client.get(
            f"{settings.paddle_api_base}/customers",
            headers={"Authorization": f"Bearer {settings.PADDLE_API_KEY}"},
            params={"email": email},
        )
    except httpx.HTTPError as e:
        logger.warning("paddle customers 조회 실패(env=%s): %s", settings.PADDLE_ENV, e)
        return ""
    if resp.status_code >= 400:
        logger.warning(
            "paddle customers 조회 거부 %s (env=%s): %s",
            resp.status_code, settings.PADDLE_ENV, getattr(resp, "text", "")[:300],
        )
        return ""
    rows = (resp.json() or {}).get("data") or []
    for r in rows:
        if isinstance(r, dict) and r.get("status") == "active" and r.get("id"):
            return str(r["id"])
    for r in rows:
        if isinstance(r, dict) and r.get("id"):
            return str(r["id"])
    return ""


@router.post("/portal-session", summary="Paddle 고객포털 세션 URL 생성")
async def create_portal_session_route(
    current_user: UserPublic = Depends(get_current_user),
) -> Dict[str, Any]:
    if not settings.PADDLE_API_KEY:
        # API key 미설정 = 포털 기능 비활성 (웹훅 secret 과 별개 게이트).
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="paddle_api_not_configured")

    sub = await paddle_subscription_repository.get_by_email(current_user.email)
    stored_id = str((sub or {}).get("customer_id") or "")

    resp = None
    async with httpx.AsyncClient(timeout=15) as client:
        # 1) 저장된 customer_id 로 먼저 시도 (정상 경로 — 추가 조회 없음).
        if stored_id:
            try:
                resp = await _post_portal_session(client, stored_id)
            except httpx.HTTPError as e:
                # 네트워크/타임아웃 — 클린 502 로 변환(CORS 헤더 보존, 브라우저 CORS 오인 방지).
                logger.warning(
                    "paddle portal-session 호출 실패(env=%s, base=%s): %s",
                    settings.PADDLE_ENV, settings.paddle_api_base, e,
                )
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="paddle_api_unreachable")
            if resp.status_code < 400:
                url = _portal_url_from_body(resp.json())
                if url:
                    return {"url": url}

        # 2) [self-heal] 저장값이 없거나 Paddle 이 거부(403/404 — stale/환경 불일치)했으면,
        #    이메일로 현재 환경의 운영 customer_id 를 즉석 조회해 재시도 + DB 보정.
        #    (사례: 샌드박스 테스트로 저장된 customer_id 가 운영 키로는 접근 불가 → 403)
        resolved = await _resolve_customer_id_by_email(client, current_user.email)
        if resolved and resolved != stored_id:
            try:
                await paddle_subscription_repository.set_customer_id(current_user.email, resolved)
            except Exception:  # noqa: BLE001 — 캐시 보정 실패는 포털 발급을 막지 않는다
                logger.exception("paddle portal-session: customer_id DB 보정 실패 (무시)")
            try:
                resp = await _post_portal_session(client, resolved)
            except httpx.HTTPError as e:
                logger.warning("paddle portal-session 재시도 실패(env=%s): %s", settings.PADDLE_ENV, e)
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="paddle_api_unreachable")
            if resp.status_code < 400:
                url = _portal_url_from_body(resp.json())
                if url:
                    return {"url": url}

    # 3) 모두 실패 — 진단 후 502/404.
    if resp is None:
        # 저장된 구독도 없고 이메일로도 Paddle 고객을 못 찾음.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no_paddle_subscription")
    if resp.status_code >= 400:
        # [진단] 사유 특정 위해 customer_id + Paddle 에러 본문 기록 (본문엔 비밀값 없음 — 에러 설명).
        #   403 = 키 권한/환경 불일치 또는 customer 미소유, 404 = customer 부재. error.code 가 결정적.
        logger.warning(
            "paddle portal-session failed %s (env=%s stored_id=%s): %s",
            resp.status_code, settings.PADDLE_ENV, stored_id or "(none)", getattr(resp, "text", "")[:500],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"paddle_api_error:{resp.status_code}",
        )
    logger.warning("paddle portal-session 예상 외 응답 shape: %s", str(resp.json())[:300])
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="paddle_api_unexpected_response")


@router.post("/change-subscription", summary="기존 구독 등급 변경 (proration 즉시청구)")
async def change_subscription_route(
    payload: ChangeSubscriptionRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> Dict[str, Any]:
    """기존 구독자의 등급 변경 — 기존 subscription 의 price 를 교체(PATCH)한다.

    [왜 체크아웃이 아니라 PATCH 인가]
    Paddle 체크아웃은 완료 1회당 '새 구독' 을 만든다. 기존 구독자가 업그레이드를
    체크아웃으로 다시 결제하면 옛 구독이 살아있는 채 새 구독이 또 생겨 '이중청구' 가
    난다. 기존 구독자는 PATCH /subscriptions/{id} 로 같은 구독의 items(price)만
    바꿔야 한다(proration 즉시청구). 등급 반영은 그 결과로 오는 subscription.updated
    웹훅이 확정한다(진실원천 동일).

    [신규 vs 기존]
    활성 구독이 없으면 409 — FE 는 이 경우 체크아웃 오버레이로 신규 결제해야 한다.
    """
    if not settings.PADDLE_API_KEY:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="paddle_api_not_configured")

    if payload.tier not in PAID_SUBSCRIPTIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid_tier:{payload.tier}")

    # [2026-06 감사] cycle 검증 — 오타('yearli' 등)가 조용히 monthly 로 떨어져 의도와 다른
    # 가격으로 청구되는 silent-fail 방지. 결제는 모호하면 400 으로 크게 실패시킨다.
    if payload.cycle not in _VALID_CYCLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid_cycle:{payload.cycle}")

    new_price = settings.paddle_price_for_tier(payload.tier, payload.cycle)
    if not new_price:
        # 가격 미설정 — env(PADDLE_PRICE_*) 누락. 신규 결제도 불가한 상태.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"price_not_configured:{payload.tier}/{payload.cycle}",
        )

    sub = await paddle_subscription_repository.get_by_email(current_user.email)
    subscription_id = (sub or {}).get("subscription_id")
    sub_status = (sub or {}).get("status")
    # 변경 가능한(=살아있는) 상태만 PATCH 대상. canceled/paused 처럼 끝났거나 멈춘 구독은
    # 스냅샷에 subscription_id 가 남아있어도 '활성 구독 없음' 으로 본다 → FE 가 409 를 받고
    # 신규 결제(체크아웃)로 폴백한다. (죽은 구독을 PATCH 해 혼란스러운 502 를 주지 않도록.)
    if not subscription_id or sub_status not in _CHANGEABLE_STATUSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no_active_subscription")

    if (sub or {}).get("price_id") == new_price:
        # 이미 같은 price — 불필요한 PATCH(=불필요한 proration 청구) 방지.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already_on_target_tier")

    # [2026-06 감사] 업/다운그레이드 방향 판별 → proration 모드 분기.
    # 업그레이드(상위 등급)는 즉시 차액 청구(prorated_immediately)가 맞지만, 다운그레이드는
    # 즉시청구하면 남은 선결제분에 추가 과금 위험 → 다음 결제주기에 정산(크레딧 보존).
    # 현재 등급은 스냅샷 price_id 로 역추적(없으면 user.subscription_type).
    cur_tier = settings.paddle_price_to_tier.get((sub or {}).get("price_id")) or current_user.subscription_type
    is_downgrade = _TIER_RANK.get(payload.tier, 0) < _TIER_RANK.get(cur_tier, 0)
    proration_mode = "prorated_next_billing_period" if is_downgrade else "prorated_immediately"

    url = f"{settings.paddle_api_base}/subscriptions/{subscription_id}"
    patch_body = {
        # 구독 수량은 항상 1 — 다중 좌석 모델 아님.
        "items": [{"price_id": new_price, "quantity": 1}],
        "proration_billing_mode": proration_mode,
        # [2026-06] 보류 중인 예약 변경(특히 '기말 해지') 해제 — 등급을 바꾸는 행위 자체가
        # "구독을 계속 쓰겠다"는 의사다. 해지 예약을 둔 채 업그레이드하면 "결제했는데 곧
        # 취소됨" 모순이 남는다(실관측). null 은 예약이 없으면 무해한 no-op.
        # Paddle Update Subscription: scheduled_change=null → 예약 변경 제거.
        "scheduled_change": None,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.patch(
                url,
                headers={
                    "Authorization": f"Bearer {settings.PADDLE_API_KEY}",
                    # [2026-06 감사] 멱등성 키 — (구독, 목표 price) 기준 결정적 키.
                    # FE 재시도/더블클릭이 같은 변경이면 동일 키 → Paddle 이 캐시된 첫 응답을
                    # 반환해 중복 proration(이중청구)을 막는다. (uuid 면 매번 달라 dedup 무의미.)
                    "Idempotency-Key": f"chgsub:{subscription_id}:{new_price}",
                },
                json=patch_body,
            )
    except httpx.HTTPError as e:
        logger.warning("paddle change-subscription 호출 실패(env=%s): %s", settings.PADDLE_ENV, e)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="paddle_api_unreachable")

    if resp.status_code >= 400:
        # [2026-06 감사] 로그에 sub_id·Paddle 응답 전문(민감) 미기록 — 상태코드/env 만.
        logger.warning(
            "paddle change-subscription failed %s (env=%s)",
            resp.status_code, settings.PADDLE_ENV,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"paddle_api_error:{resp.status_code}",
        )

    # 성공 — 등급 반영은 subscription.updated 웹훅이 확정. FE 는 폴링으로 감지.
    # [2026-06 감사] email·sub_id(민감) 미기록 — tier/cycle 만.
    logger.info(
        "paddle change-subscription ok → %s/%s",
        payload.tier, payload.cycle,
    )
    return {"status": "ok", "subscription_id": subscription_id, "tier": payload.tier}
