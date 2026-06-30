"""
결제 알림 발송 공용 helper — admin 결제 처리(강제 종료 등)와 Paddle 흐름에서 사용.
(Toss 시절 billing_routes / internal_billing_routes 는 Paddle MoR 전환으로 제거됨.)

[패턴]
- 모든 발송은 best-effort (실패 시 logger.warning + 결제 흐름 진행)
- RESEND 미설정 시 silent skip
- pricing_url 은 FRONTEND_OAUTH_CALLBACK_URL 의 origin 기준 자동 도출
"""
from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import urlparse

from app.core import email as email_lib
from app.core.config import settings
from app.service import notification_log_repository as _nlog

logger = logging.getLogger(__name__)


# [2026-05-18] admin alert throttle — 같은 키(throttle_key)가 24시간 안에 또
# 호출되면 발송 skip. webhook 반복 도착 / 같은 사용자 반복 실패 시 admin spam 차단.
#
# [2026-05 Phase 4] Redis 기반으로 승격 (SET NX EX 원자적 락).
# 다중 instance / 재시작에도 throttle 유지. Redis 미가용 시 in-memory 폴백 —
# 단일 instance 환경에서도 동작 보장 (best-effort 알림이라 폴백이 안전한 선택).
_THROTTLE_WINDOW_SECONDS = 24 * 3600
_THROTTLE_KEY_PREFIX = "admin_alert_throttle:"
_recent_alerts: dict[str, float] = {}


def _purge_expired_throttle_keys() -> None:
    """메모리 누수 방지 — TTL 만료 키 정리 (in-memory 폴백 경로용)."""
    now = time.time()
    expired = [k for k, t in _recent_alerts.items() if now - t > _THROTTLE_WINDOW_SECONDS]
    for k in expired:
        _recent_alerts.pop(k, None)


def _acquire_throttle_in_memory(throttle_key: str) -> bool:
    """in-memory 폴백 throttle. True=발송 허용, False=throttle 됨."""
    _purge_expired_throttle_keys()
    now = time.time()
    last = _recent_alerts.get(throttle_key)
    if last and (now - last) < _THROTTLE_WINDOW_SECONDS:
        return False
    _recent_alerts[throttle_key] = now
    return True


async def _acquire_throttle(throttle_key: str) -> bool:
    """발송 허용 여부 판정 + 락 획득 (원자적). True=발송, False=throttle.

    Redis SET NX EX 로 multi-instance 안전. Redis 오류 시 in-memory 폴백.
    """
    redis_key = _THROTTLE_KEY_PREFIX + throttle_key
    try:
        from app.queue import client as _queue_client
        pool = await _queue_client.get_pool()
        # nx=True → 키가 없을 때만 set. 성공(획득) 시 truthy, 이미 있으면 None.
        acquired = await pool.set(redis_key, "1", ex=_THROTTLE_WINDOW_SECONDS, nx=True)
        return bool(acquired)
    except Exception as e:  # noqa: BLE001 — Redis 장애가 알림을 완전히 막지 않게 폴백.
        logger.warning("admin alert throttle Redis 실패 — in-memory 폴백 (key=%s err=%s)", throttle_key, e)
        return _acquire_throttle_in_memory(throttle_key)


PLAN_LABEL = {
    "pro": "Pro",
    "pro_plus": "Pro+",
    "pro_max": "Pro Max",
}


PURPOSE_LABEL = {
    "initial": "첫 결제",
    "renewal": "정기결제",
    "upgrade_proration": "업그레이드 차액",
    "manual": "관리자 결제",
}


def pricing_url() -> str:
    """FE 의 /pricing 절대 URL — FRONTEND_OAUTH_CALLBACK_URL 의 origin 기준."""
    base = (settings.FRONTEND_OAUTH_CALLBACK_URL or "").strip()
    if not base:
        return "/pricing"
    try:
        u = urlparse(base)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}/pricing"
    except Exception:  # noqa: BLE001
        pass
    return "/pricing"


async def send_admin_alert(
    *,
    severity: str,                       # 'critical' | 'warning'
    title: str,
    message: str,
    context: Optional[dict] = None,
    action_required: Optional[str] = None,
    throttle_key: Optional[str] = None,  # 동일 키 24h 안 1회만 발송 (spam 차단)
) -> None:
    """
    운영자 (settings.admin_emails_list) 에게 사고 알림. best-effort.

    [발송 시점]
    - 자동 환불 호출 실패 (사용자 돈 안 회수됨 — admin 수동 처리 필요)
    - upgrade align 실패 (등급 변경 누락 — admin 보정 필요)
    - webhook retry 5회+ 실패 (PG 통합 사고 가능성)
    - rollback 실패 (sub.plan 잘못된 상태)

    [throttle]
    같은 사고가 반복 발생할 때 admin 메일함 spam 방지. throttle_key 명시 시 24h
    안 같은 키 발송 skip. 키 패턴 예: f"auto_refund_failed:{user_email}",
    f"webhook_align:{user_email}:{plan}".
    """
    # throttle 체크 — 미설정 환경에서도 동작 (email disabled 보다 먼저).
    # Redis SET NX EX 원자적 락 (multi-instance 안전), 실패 시 in-memory 폴백.
    if throttle_key:
        allowed = await _acquire_throttle(throttle_key)
        if not allowed:
            logger.info(
                "admin alert throttled — key=%s window=%ds title=%s",
                throttle_key, _THROTTLE_WINDOW_SECONDS, title,
            )
            return

    if not settings.email_enabled:
        return
    admins = settings.admin_emails_list
    if not admins:
        return
    try:
        subject, html, text = email_lib.render_admin_alert_email(
            severity=severity, title=title, message=message,
            context=context, action_required=action_required,
        )
        for email in admins:
            try:
                await email_lib.send_email(
                    to=email, subject=subject, html=html, text=text,
                    kind=_nlog.KIND_ADMIN_ALERT,
                    log_context={"severity": severity, "title": title},
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("admin alert 발송 실패 admin=%s err=%s", email, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("admin alert 렌더 실패 title=%s err=%s", title, e)


async def send_refund_notification(
    *,
    user_email: str,
    user_name: Optional[str],
    plan: str,
    refund_amount_krw: int,
    original_amount_krw: int,
    reason: str,
    downgraded_to_free: bool,
    receipt_url: Optional[str],
) -> None:
    """환불 처리 후 사용자에게 안내. best-effort."""
    if not settings.email_enabled:
        return
    if not user_email:
        return
    try:
        name = user_name or user_email.split("@")[0] or "고객"
        plan_label = PLAN_LABEL.get(plan, plan or "Pro")
        subject, html, text = email_lib.render_refund_notification_email(
            recipient_name=name,
            plan_label=plan_label,
            refund_amount_krw=refund_amount_krw,
            original_amount_krw=original_amount_krw,
            reason=reason,
            downgraded_to_free=downgraded_to_free,
            receipt_url=receipt_url or None,
            pricing_url=pricing_url(),
        )
        await email_lib.send_email(
            to=user_email, subject=subject, html=html, text=text,
            kind=_nlog.KIND_REFUND,
            log_context={
                "plan": plan, "refund_amount": refund_amount_krw,
                "downgraded_to_free": downgraded_to_free,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("환불 알림 메일 발송 실패 user=%s err=%s", user_email, e)


async def send_payment_success(
    *,
    user_email: str,
    user_name: Optional[str],
    plan: str,
    amount_krw: int,
    purpose: str,         # 'initial' | 'renewal' | 'upgrade_proration' | 'manual'
    paid_at_iso: Optional[str],
    next_billing_at_iso: Optional[str],
    receipt_url: Optional[str],
) -> None:
    """결제 성공 직후 영수증 + 다음 결제일 이메일. 호출자가 await."""
    if not settings.email_enabled:
        return
    if not user_email:
        return
    try:
        name = user_name or user_email.split("@")[0] or "고객"
        plan_label = PLAN_LABEL.get(plan, plan or "Pro")
        purpose_label = PURPOSE_LABEL.get(purpose, "결제")

        # paid_at / next_billing 포맷 — None 안전
        def _fmt_dt(iso: Optional[str], with_time: bool = True) -> str:
            if not iso:
                return ""
            try:
                # Neo4j datetime() → ISO8601 with offset
                from datetime import datetime
                dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")
            except Exception:  # noqa: BLE001
                # 형식 알 수 없으면 앞 16자 (또는 10자) 사용
                s = str(iso)
                return s[:16] if with_time else s[:10]

        paid_str = _fmt_dt(paid_at_iso, with_time=True)
        next_str = _fmt_dt(next_billing_at_iso, with_time=False) if next_billing_at_iso else None

        subject, html, text = email_lib.render_payment_success_email(
            recipient_name=name,
            plan_label=plan_label,
            amount_krw=amount_krw,
            purpose_label=purpose_label,
            paid_at=paid_str or "—",
            next_billing_at=next_str,
            receipt_url=receipt_url or None,
            pricing_url=pricing_url(),
        )
        await email_lib.send_email(
            to=user_email, subject=subject, html=html, text=text,
            kind=_nlog.KIND_PAYMENT_SUCCESS,
            log_context={"plan": plan, "amount": amount_krw, "purpose": purpose},
        )
    except (email_lib.EmailDisabled, email_lib.EmailSendError) as e:
        logger.warning("결제 성공 이메일 발송 실패 user=%s err=%s", user_email, e)
    except Exception as e:  # noqa: BLE001
        # 다른 예외 (LookupError 등) — 결제 흐름은 절대 막지 않음
        logger.warning("결제 성공 이메일 예외 user=%s err=%s", user_email, e)
