"""
Paddle (MoR) 웹훅 — 결제/구독 상태 변화를 수신해 사용자 entitlement(subscription_type) 갱신.

[진실원천]
결제 성공의 진실원천은 이 웹훅이다. FE 는 체크아웃을 '열기만' 하고, 등급 반영은
Paddle 이 push 하는 subscription.* / transaction.completed 웹훅으로 확정한다.

[보안 — 서명 검증]
Paddle-Signature 헤더: `ts=<unix>;h1=<hmac_sha256_hex>`.
서명 대상 = `f"{ts}:{raw_body}"`, 키 = PADDLE_WEBHOOK_SECRET (Notifications 등록 시 발급).
검증 실패 → 401 (위조 차단). secret 미설정(paddle_enabled=False) → 503 (오설정 방어).

[멱등성]
webhook_event_repository.try_insert(event_id) 로 중복 이벤트 skip (토스 웹훅과 동일 패턴).

[사용자 식별]
FE openCheckout 가 customData={user_email} 로 넘기므로 data.custom_data.user_email 사용.
없으면 처리 skip(로그) — 우리 유저와 매핑 불가한 결제는 무시(보수적).

[등급 매핑]
data.items[].price.id → settings.paddle_price_to_tier → change_subscription(to_type).
subscription.canceled → free 강등.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.core.config import settings
from app.core.subscription import SUBSCRIPTION_FREE
from app.service import (
    admin_repository,
    audit_repository,
    paddle_subscription_repository,
    webhook_event_repository,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paddle", tags=["Paddle", "Webhook"])

# 등급 부여 이벤트 — 활성 구독.
_GRANT_EVENTS = {
    "subscription.created",
    "subscription.activated",
    "subscription.updated",
    "subscription.resumed",
}
# 등급 회수 이벤트 — free 강등.
_REVOKE_EVENTS = {
    "subscription.canceled",
    "subscription.paused",
}


def verify_paddle_signature(raw_body: bytes, signature_header: Optional[str], secret: str) -> bool:
    """Paddle-Signature(`ts=..;h1=..`) HMAC-SHA256 검증. 형식 불량/불일치 시 False."""
    if not signature_header or not secret:
        return False
    parts: Dict[str, str] = {}
    for seg in signature_header.split(";"):
        if ":" in seg:  # 일부 구현은 ':' 구분 — 방어적 처리
            k, _, v = seg.partition(":")
        else:
            k, _, v = seg.partition("=")
        parts[k.strip()] = v.strip()
    ts, h1 = parts.get("ts"), parts.get("h1")
    if not ts or not h1:
        return False
    signed = f"{ts}:".encode() + raw_body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, h1)


def _tier_from_items(items: Any) -> Optional[str]:
    """subscription.items[].price.id 중 매핑되는 첫 등급."""
    mapping = settings.paddle_price_to_tier
    for it in items or []:
        price = (it or {}).get("price") or {}
        pid = price.get("id") or (it or {}).get("price_id")
        if pid and pid in mapping:
            return mapping[pid]
    return None


def _user_email_from_data(data: Dict[str, Any]) -> Optional[str]:
    """custom_data.user_email 우선, 없으면 None (매핑 불가 결제는 무시)."""
    custom = data.get("custom_data") or {}
    email = custom.get("user_email") or custom.get("email")
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


@router.post("/webhook", summary="Paddle webhook — 서명검증 + 멱등 + entitlement 갱신")
async def paddle_webhook_route(
    request: Request,
    paddle_signature: Optional[str] = Header(default=None, alias="Paddle-Signature"),
) -> Dict[str, Any]:
    if not settings.paddle_enabled:
        # secret 미설정 = 오설정. 위조 검증 불가하니 처리 거부.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="paddle_not_configured")

    raw_body = await request.body()
    if not verify_paddle_signature(raw_body, paddle_signature, settings.PADDLE_WEBHOOK_SECRET or ""):
        logger.warning("paddle webhook: signature 검증 실패")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_signature")

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.warning("paddle webhook: invalid json (%s)", e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_json")

    event_id = str(event.get("event_id") or "")
    event_type = str(event.get("event_type") or "")
    if not event_id or not event_type:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing_event_fields")

    # 멱등 — 중복 이벤트 skip.
    _status, is_new = await webhook_event_repository.try_insert(
        pg_event_id=event_id, event_type=event_type, payload=event
    )
    if not is_new:
        logger.info("paddle webhook: 중복 이벤트 skip event_id=%s type=%s", event_id, event_type)
        return {"status": "duplicate"}

    try:
        await audit_repository.write(
            actor_email="SYSTEM:PADDLE",
            action=audit_repository.ACTION_WEBHOOK_RECEIVED,
            target_email="",
            payload={"event_type": event_type, "event_id": event_id[:24]},
        )
    except Exception:  # noqa: BLE001 — best-effort
        pass

    try:
        handled = await _handle_event(
            event_type, event.get("data") or {}, occurred_at=str(event.get("occurred_at") or "")
        )
        await webhook_event_repository.mark_processed(event_id)
        return {"status": "ok" if handled else "ignored"}
    except Exception as e:  # noqa: BLE001
        logger.exception("paddle webhook 처리 실패 event=%s err=%s", event_type, e)
        await webhook_event_repository.mark_failed(event_id, str(e))
        # Paddle 은 non-2xx 시 재시도 → 500 반환해 재시도 유도.
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="processing_failed")


# 종료 상태 — grant 이벤트(subscription.updated 등)에 실려와도 등급을 부여하면 안 되는 상태.
# (canceled 직후 updated 가 늦게 도착하는 순서 꼬임에서 해지자가 등급을 재취득하는 것 방어.)
_TERMINAL_STATUSES = {"canceled", "paused"}


async def _handle_event(event_type: str, data: Dict[str, Any], occurred_at: str = "") -> bool:
    """이벤트 → entitlement 갱신. 처리했으면 True, 무시면 False."""
    if event_type not in _GRANT_EVENTS and event_type not in _REVOKE_EVENTS:
        logger.info("paddle webhook: 처리 대상 아님 (type=%s)", event_type)
        return False

    email = _user_email_from_data(data)
    if not email:
        logger.warning("paddle webhook: custom_data.user_email 없음 — skip (type=%s)", event_type)
        return False

    # 구독 스냅샷 영속화 — 고객포털 세션(customer_id)·FE 구독현황의 소스.
    # 실패해도 핵심(entitlement 갱신)은 진행 — 스냅샷은 다음 subscription.* 웹훅에서 재시도된다.
    # 단, stale(이미 더 최신 occurred_at 반영됨) 판정은 entitlement 도 막는다 —
    # 늦게 재전달된 옛 active 이벤트가 해지자 등급을 되살리는 순서 꼬임 방지.
    snapshot_applied = True
    sub_id = str(data.get("id") or "")
    customer_id = str(data.get("customer_id") or "")

    # [수량 가드] 구독은 수량 1 이 정상 — Paddle 상품에서 수량 고정=1 권장.
    # 2 이상이면 상품 설정 오류/체크아웃 수량조정 오남용 신호(=초과청구) → 경고만 남기고
    # 등급 부여는 진행(사용자 차단보다 가시성 우선). 운영에선 Paddle 상품 수량을 잠글 것.
    for _it in data.get("items") or []:
        _q = (_it or {}).get("quantity")
        if isinstance(_q, int) and _q != 1:
            logger.warning(
                "paddle webhook: 비정상 구독 수량 quantity=%s (email=%s sub=%s) — Paddle 상품 수량 고정=1 확인 필요",
                _q, email, sub_id,
            )
            break

    if sub_id and customer_id:
        period = data.get("current_billing_period") or {}
        price_id = None
        for it in data.get("items") or []:
            pid = ((it or {}).get("price") or {}).get("id")
            if pid:
                price_id = pid
                break
        try:
            snap, applied = await paddle_subscription_repository.upsert(
                email=email,
                subscription_id=sub_id,
                customer_id=customer_id,
                status=str(data.get("status") or ""),
                price_id=price_id,
                current_period_end=period.get("ends_at"),
                occurred_at=occurred_at,
            )
            # snap=None 은 User 부재 — stale 아님 (entitlement 경로가 동일 판단을 내림).
            if snap is not None and not applied:
                snapshot_applied = False
        except Exception:  # noqa: BLE001 — 스냅샷 "실패"는 entitlement 를 막지 않는다 (stale 판정과 다름)
            logger.exception("paddle webhook: 구독 스냅샷 영속화 실패 (email=%s sub=%s)", email, sub_id)

    if not snapshot_applied:
        logger.warning(
            "paddle webhook: stale 이벤트 skip — 더 최신 상태가 이미 반영됨 (email=%s type=%s occurred_at=%s)",
            email, event_type, occurred_at,
        )
        return False

    sub_status = str(data.get("status") or "")
    if event_type in _REVOKE_EVENTS or sub_status in _TERMINAL_STATUSES:
        to_type = SUBSCRIPTION_FREE
    else:
        to_type = _tier_from_items(data.get("items"))
        if not to_type:
            logger.warning("paddle webhook: items 에서 등급 매핑 실패 — skip (email=%s)", email)
            return False

    result = await admin_repository.change_subscription(
        target_email=email,
        to_type=to_type,
        reason=f"paddle:{event_type}",
        changed_by_email="SYSTEM:PADDLE",
    )
    if result is None:
        logger.warning("paddle webhook: 사용자 없음 email=%s (type=%s)", email, event_type)
        return False
    logger.info("paddle webhook: entitlement 갱신 email=%s → %s (type=%s)", email, to_type, event_type)
    return True
