"""
Email 발송 — Resend API.

[배경 — 2026-05]
비밀번호 찾기 reset 링크 발송용. 다른 트랜잭션 이메일도 향후 이 모듈 활용.

[Resend 선택 이유]
- 개발자 친화적 REST API (간단, 키 1개)
- 무료 100통/월 (운영 초기 충분)
- SPF/DKIM 자동 처리 (도메인 검증만)
- httpx 로 직접 호출 (SDK 의존 X)

[설계]
- email_enabled (RESEND_API_KEY 설정 여부) 미체크 시 silent skip + warning log
- HTML + plain text 둘 다 전송 (이메일 클라이언트 호환성)
- 운영 미설정 환경에서도 BE 부팅 가능 — 라우트가 503 응답
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


RESEND_API_URL = "https://api.resend.com/emails"


class EmailDisabled(Exception):
    """RESEND_API_KEY 미설정."""


class EmailSendError(Exception):
    """발송 실패 (API 호출 실패 또는 거부)."""


async def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    kind: Optional[str] = None,
    log_context: Optional[dict] = None,
) -> str:
    """
    이메일 1통 발송. Resend API.

    Args:
        to: 수신자 이메일
        subject: 제목
        html: HTML 본문
        text: plain text 본문 (없으면 html 에서 태그 제거 fallback)
        kind: NotificationLog 기록용 분류 (notification_log_repository.KIND_*).
              미지정 시 'other'. 결제/환불 등 분쟁 가능 메일은 반드시 지정.
        log_context: NotificationLog.context 에 저장할 추가 메타 (선택)

    Returns:
        Resend 가 반환한 message id (audit log 용).

    Raises:
        EmailDisabled: RESEND_API_KEY 미설정
        EmailSendError: API 호출 실패 또는 거부
    """
    # 지연 import — 순환 의존 회피 (notification_log_repository 가 future-proof).
    from app.service import notification_log_repository as _nlog

    if not settings.email_enabled:
        # 발송은 안 했지만 '발송 시도가 있었음' 은 기록 — 운영 misconfig 추적
        try:
            await _nlog.record(
                user_email=to,
                kind=kind or _nlog.KIND_OTHER,
                subject=subject,
                status="disabled",
                error_message="RESEND_API_KEY 미설정",
                context=log_context,
            )
        except Exception:  # noqa: BLE001
            pass
        raise EmailDisabled("이메일 발송이 구성되지 않았습니다 (RESEND_API_KEY 미설정).")

    body = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        body["text"] = text

    headers = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(RESEND_API_URL, json=body, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("Resend API 호출 실패: %s", e)
            # [2026-05-18] 발송 실패 기록
            try:
                await _nlog.record(
                    user_email=to, kind=kind or _nlog.KIND_OTHER,
                    subject=subject, status="failed",
                    error_message=f"network: {e}", context=log_context,
                )
            except Exception:  # noqa: BLE001
                pass
            raise EmailSendError(f"이메일 발송 실패: {e}") from e

        if resp.status_code >= 400:
            # Resend 의 에러 응답 — 도메인 미검증, 한도 초과, 잘못된 API key 등
            try:
                err_body = resp.json()
            except Exception:  # noqa: BLE001
                err_body = {"raw": resp.text}
            logger.warning(
                "Resend 거부 (status=%d): %s", resp.status_code, err_body
            )
            err_msg = (
                f"status={resp.status_code}: "
                f"{err_body.get('message') or err_body.get('error') or err_body}"
            )
            try:
                await _nlog.record(
                    user_email=to, kind=kind or _nlog.KIND_OTHER,
                    subject=subject, status="failed",
                    error_message=err_msg, context=log_context,
                )
            except Exception:  # noqa: BLE001
                pass
            raise EmailSendError(
                f"이메일 발송 거부 (status={resp.status_code}): "
                f"{err_body.get('message') or err_body.get('error') or err_body}"
            )

    data = resp.json() if resp.content else {}
    message_id = str(data.get("id") or "")
    logger.info("Email sent — to=%s, id=%s, subject=%s", to, message_id, subject)
    # [2026-05-18] 발송 성공 기록 — best-effort
    try:
        await _nlog.record(
            user_email=to, kind=kind or _nlog.KIND_OTHER,
            subject=subject, status="sent",
            provider_message_id=message_id, context=log_context,
        )
    except Exception:  # noqa: BLE001
        pass
    return message_id


# ===== 템플릿 — 문의 답변 알림 (2026-05) =====


def render_inquiry_reply_email(
    *,
    recipient_name: str,
    subject_original: str,
    admin_reply: str,
    inquiry_url: str,
) -> tuple[str, str, str]:
    """관리자가 문의에 답변 시 사용자에게 발송하는 알림 이메일."""
    subject = f"[Harness] 문의에 답변이 도착했습니다 — {subject_original}"
    # 답변 본문이 너무 길면 일부만 미리보기 (300자)
    preview = admin_reply.strip()
    if len(preview) > 300:
        preview = preview[:300] + "..."
    text = (
        f"안녕하세요 {recipient_name}님,\n\n"
        f"문의하신 내용 '{subject_original}' 에 관리자가 답변을 작성했습니다.\n\n"
        f"답변 미리보기:\n{preview}\n\n"
        f"전체 답변은 아래 링크에서 확인할 수 있습니다:\n{inquiry_url}\n\n"
        f"— Harness Team"
    )
    # HTML 의 admin_reply 는 안전을 위해 escape 처리 (사용자 측 XSS 방어)
    import html as _html
    safe_preview = _html.escape(preview).replace("\n", "<br/>")
    safe_subject = _html.escape(subject_original)
    safe_name = _html.escape(recipient_name)
    html = f"""\
<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8" /><title>{subject}</title></head>
<body style="margin:0; padding:0; background:#F7F5EB; font-family:'Pretendard',sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#F7F5EB; padding:40px 0;">
    <tr><td align="center">
      <table role="presentation" width="560" cellspacing="0" cellpadding="0" style="background:#ffffff; border-radius:14px; padding:36px; box-shadow:0 4px 16px rgba(0,0,0,0.06);">
        <tr><td>
          <h1 style="margin:0 0 8px; font-size:22px; color:#2A2421; letter-spacing:-0.02em;">Harness</h1>
          <p style="margin:0 0 24px; font-size:12px; color:#8A817C; letter-spacing:0.06em; text-transform:uppercase;">문의 답변 도착</p>

          <p style="margin:0 0 12px; font-size:15px; color:#2A2421; line-height:1.6;">
            안녕하세요 <strong>{safe_name}</strong>님,
          </p>
          <p style="margin:0 0 20px; font-size:14px; color:#2A2421; line-height:1.7;">
            문의하신 내용 "<strong>{safe_subject}</strong>" 에<br/>
            관리자가 답변을 작성했습니다.
          </p>

          <div style="background:#F7F5EB; border-left:3px solid #8C6239; padding:14px 16px; border-radius:8px; margin:0 0 24px;">
            <p style="margin:0 0 4px; font-size:11px; color:#8A817C; font-weight:700; text-transform:uppercase; letter-spacing:0.04em;">답변 미리보기</p>
            <p style="margin:0; font-size:13.5px; color:#2A2421; line-height:1.7;">{safe_preview}</p>
          </div>

          <table role="presentation" cellspacing="0" cellpadding="0" style="margin:0 0 28px;">
            <tr><td>
              <a href="{inquiry_url}" style="display:inline-block; padding:12px 28px; background:#8C6239; color:#ffffff; text-decoration:none; border-radius:10px; font-weight:700; font-size:14px;">
                전체 답변 보기
              </a>
            </td></tr>
          </table>

          <hr style="border:none; border-top:1px solid rgba(140,98,57,0.15); margin:0 0 18px;" />
          <p style="margin:0; font-size:11px; color:#8A817C; line-height:1.6;">
            로그인 후 "내 문의" 페이지에서 답변 전문과 이전 문의 내역을 모두 확인할 수 있습니다.<br/>
            — Harness Team
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return subject, html, text


# ===== 템플릿 — 결제 알림 (2026-05-18) =====


def _common_email_html(*, title: str, body_html: str, cta_url: Optional[str] = None,
                       cta_label: Optional[str] = None) -> str:
    """결제 알림 공용 wrapper — 일관된 헤더/푸터 + 본문 슬롯."""
    cta_block = ""
    if cta_url and cta_label:
        cta_block = f"""\
          <table role="presentation" cellspacing="0" cellpadding="0" style="margin:0 0 28px;">
            <tr><td>
              <a href="{cta_url}" style="display:inline-block; padding:12px 28px; background:#8C6239; color:#ffffff; text-decoration:none; border-radius:10px; font-weight:700; font-size:14px;">
                {cta_label}
              </a>
            </td></tr>
          </table>"""
    return f"""\
<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8" /><title>{title}</title></head>
<body style="margin:0; padding:0; background:#F7F5EB; font-family:'Pretendard',sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#F7F5EB; padding:40px 0;">
    <tr><td align="center">
      <table role="presentation" width="560" cellspacing="0" cellpadding="0" style="background:#ffffff; border-radius:14px; padding:36px; box-shadow:0 4px 16px rgba(0,0,0,0.06);">
        <tr><td>
          <h1 style="margin:0 0 8px; font-size:22px; color:#2A2421; letter-spacing:-0.02em;">Harness</h1>
          <p style="margin:0 0 24px; font-size:12px; color:#8A817C; letter-spacing:0.06em; text-transform:uppercase;">{title}</p>
          {body_html}
          {cta_block}
          <hr style="border:none; border-top:1px solid rgba(140,98,57,0.15); margin:0 0 18px;" />
          <p style="margin:0; font-size:11px; color:#8A817C; line-height:1.6;">
            문의: <a href="mailto:support@gayoje.example" style="color:#8C6239;">support@gayoje.example</a><br/>
            — Harness Team
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def render_subscription_expiring_soon_email(
    *,
    recipient_name: str,
    plan_label: str,
    grace_until: str,
    hours_left: int,
    pricing_url: str,
) -> tuple[str, str, str]:
    """강등 임박 알림 (grace 만료 24시간 전 정도). 마지막 기회."""
    subject = f"[Harness] 무료 등급 강등까지 {hours_left}시간 — 마지막 안내"
    text = (
        f"안녕하세요 {recipient_name}님,\n\n"
        f"{plan_label} 등급이 {hours_left}시간 후 ({grace_until}) 무료 등급으로 강등됩니다.\n"
        f"카드 정보를 업데이트하면 자동 재결제가 시도됩니다.\n\n"
        f"카드 변경 / 재결제:\n{pricing_url}\n\n"
        f"— Harness Team"
    )
    body_html = f"""\
          <p style="margin:0 0 12px; font-size:15px; color:#2A2421; line-height:1.6;">
            안녕하세요 <strong>{recipient_name}</strong>님,
          </p>
          <div style="background:#FEF3C7; border-left:3px solid #B45309; padding:14px 16px; border-radius:8px; margin:0 0 20px;">
            <p style="margin:0; font-size:14px; color:#78350F; line-height:1.7;">
              <strong>{plan_label}</strong> 등급이 <strong>{hours_left}시간 후</strong> ({grace_until})<br/>
              무료 등급으로 강등됩니다.
            </p>
          </div>
          <p style="margin:0 0 20px; font-size:14px; color:#2A2421; line-height:1.7;">
            카드 정보를 업데이트하면 자동 재결제가 시도되어 등급이 유지됩니다.<br/>
            지금이 마지막 안내입니다.
          </p>"""
    html = _common_email_html(
        title=f"강등까지 {hours_left}시간",
        body_html=body_html,
        cta_url=pricing_url,
        cta_label="카드 변경 / 재결제",
    )
    return subject, html, text


def render_admin_alert_email(
    *,
    severity: str,      # 'critical' | 'warning'
    title: str,         # 짧은 제목 (이메일 subject 에 사용)
    message: str,       # 상세 설명 (plain text)
    context: Optional[dict] = None,  # key/value 추가 정보 (사용자/결제 ID 등)
    action_required: Optional[str] = None,  # admin 이 취할 액션
) -> tuple[str, str, str]:
    """운영자 (admin) 에게 발송하는 사고 알림 — 자동 환불 실패 / align 실패 / webhook retry 폭주 등."""
    severity_label = "🚨 CRITICAL" if severity == "critical" else "⚠️ WARNING"
    subject = f"[Harness {severity_label}] {title}"

    import html as _html

    ctx_lines_text = ""
    ctx_html = ""
    if context:
        ctx_lines_text = "\n".join(f"  - {k}: {v}" for k, v in context.items())
        ctx_html = "".join(
            f"""<tr>
              <td style="padding:4px 12px 4px 0; font-size:12px; color:#8A817C; vertical-align:top; white-space:nowrap;">{_html.escape(str(k))}</td>
              <td style="padding:4px 0; font-size:12.5px; color:#2A2421; word-break:break-all;">{_html.escape(str(v))}</td>
            </tr>"""
            for k, v in context.items()
        )

    action_line_text = f"\n\n[액션 필요]\n{action_required}" if action_required else ""
    action_block_html = ""
    if action_required:
        action_block_html = f"""\
          <div style="background:#FEE2E2; border-left:3px solid #DC2626; padding:12px 14px; border-radius:8px; margin:0 0 18px;">
            <p style="margin:0 0 4px; font-size:11px; color:#7F1D1D; font-weight:700; text-transform:uppercase;">액션 필요</p>
            <p style="margin:0; font-size:13px; color:#7F1D1D; line-height:1.6;">{_html.escape(action_required).replace(chr(10), '<br/>')}</p>
          </div>"""

    text = (
        f"{severity_label} — {title}\n\n"
        f"{message}\n\n"
        f"{('컨텍스트:' + chr(10) + ctx_lines_text) if ctx_lines_text else ''}"
        f"{action_line_text}\n\n"
        f"— Harness 운영 알림\n"
    )

    safe_msg = _html.escape(message).replace("\n", "<br/>")
    body_html = f"""\
          <div style="background:{'#FEE2E2' if severity == 'critical' else '#FEF3C7'};
                      border-radius:8px; padding:10px 14px; margin:0 0 18px;
                      font-size:12px; font-weight:700;
                      color:{'#7F1D1D' if severity == 'critical' else '#78350F'};">
            {severity_label}
          </div>
          <p style="margin:0 0 18px; font-size:14.5px; color:#2A2421; line-height:1.7;">
            {safe_msg}
          </p>
          {f'<table role="presentation" cellspacing="0" cellpadding="0" width="100%" style="background:#F7F5EB; border-radius:10px; padding:10px 14px; margin:0 0 18px;">{ctx_html}</table>' if ctx_html else ''}
          {action_block_html}
          <p style="margin:0; font-size:11px; color:#8A817C; line-height:1.6;">
            이 알림은 운영자 (ADMIN_EMAILS) 에게 자동 발송됩니다.
          </p>"""

    html = _common_email_html(title=title, body_html=body_html)
    return subject, html, text


def render_refund_notification_email(
    *,
    recipient_name: str,
    plan_label: str,
    refund_amount_krw: int,
    original_amount_krw: int,
    reason: str,                # admin 이 입력한 환불 사유
    downgraded_to_free: bool,   # 환불과 동시에 강등됐는지
    receipt_url: Optional[str],
    pricing_url: str,
) -> tuple[str, str, str]:
    """환불 처리 안내. 사용자가 환불 사실을 메일로 인지 → 분쟁 감소."""
    is_partial = refund_amount_krw < original_amount_krw
    refund_type = "부분 환불" if is_partial else "전액 환불"
    subject = f"[Harness] {refund_amount_krw:,}원 {refund_type} 처리 완료"

    downgrade_line = ""
    if downgraded_to_free:
        downgrade_line = (
            "\n등급이 무료로 변경되었습니다. 프로젝트와 데이터는 그대로 보존됩니다."
        )
    receipt_line = f"\n토스 영수증 (환불 내역 포함): {receipt_url}" if receipt_url else ""

    text = (
        f"안녕하세요 {recipient_name}님,\n\n"
        f"{plan_label} 등급 결제 건에 대한 {refund_type} ({refund_amount_krw:,}원) 가 처리되었습니다.\n\n"
        f"- 환불 금액: {refund_amount_krw:,}원\n"
        f"- 원 결제 금액: {original_amount_krw:,}원\n"
        f"- 사유: {reason}{downgrade_line}{receipt_line}\n\n"
        f"카드사 정책에 따라 영업일 기준 3~7일 이내 환불 반영됩니다.\n\n"
        f"문의: support@gayoje.example\n\n"
        f"— Harness Team"
    )

    import html as _html
    safe_reason = _html.escape(reason).replace("\n", "<br/>")

    downgrade_block = ""
    if downgraded_to_free:
        downgrade_block = """\
          <div style="background:#FEF3C7; border-left:3px solid #B45309; padding:12px 14px; border-radius:8px; margin:0 0 18px;">
            <p style="margin:0; font-size:13px; color:#78350F; line-height:1.6;">
              ⚠️ 환불 처리와 함께 등급이 <strong>무료</strong>로 변경되었습니다.<br/>
              프로젝트 / 미팅 로그 / 설정 데이터는 그대로 보존됩니다.
            </p>
          </div>"""

    receipt_btn = ""
    if receipt_url:
        receipt_btn = f"""\
          <table role="presentation" cellspacing="0" cellpadding="0" style="margin:0 0 12px;">
            <tr><td>
              <a href="{receipt_url}" style="display:inline-block; padding:9px 20px; background:transparent; color:#8C6239; text-decoration:none; border-radius:8px; font-weight:700; font-size:13px; border:1px solid #8C6239;">
                토스 영수증 (환불 내역) 보기
              </a>
            </td></tr>
          </table>"""

    body_html = f"""\
          <p style="margin:0 0 12px; font-size:15px; color:#2A2421; line-height:1.6;">
            안녕하세요 <strong>{recipient_name}</strong>님,
          </p>
          <p style="margin:0 0 18px; font-size:14px; color:#2A2421; line-height:1.7;">
            <strong>{plan_label}</strong> 등급 결제 건에 대한
            <strong>{refund_type} ({refund_amount_krw:,}원)</strong> 가 처리되었습니다.
          </p>

          <table role="presentation" cellspacing="0" cellpadding="0" width="100%"
                 style="background:#F7F5EB; border-radius:10px; padding:14px 18px; margin:0 0 18px;">
            <tr>
              <td style="padding:4px 0; font-size:12px; color:#8A817C; width:120px;">환불 금액</td>
              <td style="padding:4px 0; font-size:14px; color:#2A2421; font-weight:700;">{refund_amount_krw:,}원</td>
            </tr>
            <tr>
              <td style="padding:4px 0; font-size:12px; color:#8A817C;">원 결제 금액</td>
              <td style="padding:4px 0; font-size:13.5px; color:#2A2421;">{original_amount_krw:,}원</td>
            </tr>
            <tr>
              <td style="padding:4px 0; font-size:12px; color:#8A817C; vertical-align:top;">사유</td>
              <td style="padding:4px 0; font-size:13.5px; color:#2A2421; line-height:1.6;">{safe_reason}</td>
            </tr>
          </table>

          {downgrade_block}
          {receipt_btn}

          <p style="margin:12px 0 0; font-size:12px; color:#6F665F; line-height:1.6;">
            ※ 카드사 정책에 따라 영업일 기준 <strong>3~7일</strong> 이내 환불이 반영됩니다.<br/>
            ※ 환불에 대한 문의는 <a href="mailto:support@gayoje.example" style="color:#8C6239;">support@gayoje.example</a> 으로 연락 주세요.
          </p>"""

    html = _common_email_html(
        title=f"{refund_type} 처리 완료",
        body_html=body_html,
        cta_url=pricing_url,
        cta_label="구독 / 결제 이력 확인",
    )
    return subject, html, text


def render_payment_success_email(
    *,
    recipient_name: str,
    plan_label: str,
    amount_krw: int,
    purpose_label: str,    # "첫 결제" / "정기결제" / "업그레이드 차액"
    paid_at: str,          # "YYYY-MM-DD HH:MM"
    next_billing_at: Optional[str],   # "YYYY-MM-DD" 또는 None (단건 결제 등)
    receipt_url: Optional[str],
    pricing_url: str,
) -> tuple[str, str, str]:
    """결제 성공 영수증 + 다음 결제일 안내."""
    from app.core.billing_tax import vat_breakdown

    # 표시가는 VAT 포함 → 공급가액/부가세 분리 표기(영수증 신뢰도 ↑).
    supply_krw, vat_krw = vat_breakdown(amount_krw)

    subject = f"[Harness] {plan_label} 결제 완료 — {amount_krw:,}원"
    next_line = f"다음 결제일: {next_billing_at}" if next_billing_at else "단건 결제 (정기결제 아님)"
    receipt_line = f"\n토스 영수증: {receipt_url}" if receipt_url else ""

    text = (
        f"안녕하세요 {recipient_name}님,\n\n"
        f"{plan_label} 등급의 결제가 정상 처리되었습니다.\n\n"
        f"- 구분: {purpose_label}\n"
        f"- 공급가액: {supply_krw:,}원\n"
        f"- 부가세(10%): {vat_krw:,}원\n"
        f"- 합계: {amount_krw:,}원 (VAT 포함)\n"
        f"- 결제 일시: {paid_at}\n"
        f"- {next_line}{receipt_line}\n\n"
        f"구독 / 결제 이력 확인:\n{pricing_url}\n\n"
        f"— Harness Team"
    )

    receipt_btn = ""
    if receipt_url:
        receipt_btn = f"""\
          <table role="presentation" cellspacing="0" cellpadding="0" style="margin:0 0 12px;">
            <tr><td>
              <a href="{receipt_url}" style="display:inline-block; padding:9px 20px; background:transparent; color:#8C6239; text-decoration:none; border-radius:8px; font-weight:700; font-size:13px; border:1px solid #8C6239;">
                토스 영수증 보기
              </a>
            </td></tr>
          </table>"""

    body_html = f"""\
          <p style="margin:0 0 12px; font-size:15px; color:#2A2421; line-height:1.6;">
            안녕하세요 <strong>{recipient_name}</strong>님,
          </p>
          <p style="margin:0 0 20px; font-size:14px; color:#2A2421; line-height:1.7;">
            <strong>{plan_label}</strong> 등급의 결제가 정상 처리되었습니다.
          </p>

          <table role="presentation" cellspacing="0" cellpadding="0" width="100%"
                 style="background:#F7F5EB; border-radius:10px; padding:14px 18px; margin:0 0 20px;">
            <tr>
              <td style="padding:4px 0; font-size:12px; color:#8A817C; width:90px;">구분</td>
              <td style="padding:4px 0; font-size:13.5px; color:#2A2421;">{purpose_label}</td>
            </tr>
            <tr>
              <td style="padding:4px 0; font-size:12px; color:#8A817C;">공급가액</td>
              <td style="padding:4px 0; font-size:13.5px; color:#2A2421;">{supply_krw:,}원</td>
            </tr>
            <tr>
              <td style="padding:4px 0; font-size:12px; color:#8A817C;">부가세 (10%)</td>
              <td style="padding:4px 0; font-size:13.5px; color:#2A2421;">{vat_krw:,}원</td>
            </tr>
            <tr>
              <td style="padding:4px 0; font-size:12px; color:#8A817C;">합계</td>
              <td style="padding:4px 0; font-size:14px; color:#2A2421; font-weight:700;">{amount_krw:,}원 <span style="font-size:11px; color:#8A817C; font-weight:400;">(VAT 포함)</span></td>
            </tr>
            <tr>
              <td style="padding:4px 0; font-size:12px; color:#8A817C;">결제 일시</td>
              <td style="padding:4px 0; font-size:13.5px; color:#2A2421;">{paid_at}</td>
            </tr>
            <tr>
              <td style="padding:4px 0; font-size:12px; color:#8A817C;">{'다음 결제' if next_billing_at else '구분'}</td>
              <td style="padding:4px 0; font-size:13.5px; color:#2A2421;">{next_billing_at or '단건 결제 (정기결제 아님)'}</td>
            </tr>
          </table>

          {receipt_btn}"""

    html = _common_email_html(
        title="결제 완료",
        body_html=body_html,
        cta_url=pricing_url,
        cta_label="구독 / 결제 이력 확인",
    )
    return subject, html, text


def render_subscription_canceled_email(
    *,
    recipient_name: str,
    plan_label: str,
    reason: str,  # 'payment_failed' | 'user_canceled' | 'admin_terminated'
    pricing_url: str,
) -> tuple[str, str, str]:
    """강등 완료 안내. 데이터 보존 강조 + 재구독 유도."""
    reason_label = {
        "payment_failed": "결제 실패 (유예 기간 종료)",
        "user_canceled": "사용자 해지 요청 (주기 만료)",
        "admin_terminated": "관리자 종료",
    }.get(reason, "구독 종료")
    subject = f"[Harness] {plan_label} 구독이 종료되었습니다"
    text = (
        f"안녕하세요 {recipient_name}님,\n\n"
        f"{plan_label} 등급 구독이 종료되어 무료 등급으로 전환되었습니다.\n"
        f"사유: {reason_label}\n\n"
        f"프로젝트 / 미팅 로그 / 설정은 모두 그대로 보존되어 있습니다.\n"
        f"언제든 다시 결제하시면 등급이 즉시 복원됩니다.\n\n"
        f"재구독:\n{pricing_url}\n\n"
        f"— Harness Team"
    )
    body_html = f"""\
          <p style="margin:0 0 12px; font-size:15px; color:#2A2421; line-height:1.6;">
            안녕하세요 <strong>{recipient_name}</strong>님,
          </p>
          <p style="margin:0 0 20px; font-size:14px; color:#2A2421; line-height:1.7;">
            <strong>{plan_label}</strong> 등급 구독이 종료되어 <strong>무료 등급</strong>으로 전환되었습니다.
          </p>
          <div style="background:#F7F5EB; border-left:3px solid #8C6239; padding:14px 16px; border-radius:8px; margin:0 0 20px;">
            <p style="margin:0 0 4px; font-size:11px; color:#8A817C; font-weight:700; text-transform:uppercase; letter-spacing:0.04em;">사유</p>
            <p style="margin:0; font-size:13.5px; color:#2A2421;">{reason_label}</p>
          </div>
          <p style="margin:0 0 20px; font-size:13.5px; color:#2A2421; line-height:1.7;">
            ✓ 프로젝트 / 미팅 로그 / 설정은 모두 그대로 <strong>보존</strong>됩니다.<br/>
            ✓ 다시 결제하시면 등급이 즉시 복원되고 한도도 회복됩니다.
          </p>"""
    html = _common_email_html(
        title="구독 종료 안내",
        body_html=body_html,
        cta_url=pricing_url,
        cta_label="다시 구독하기",
    )
    return subject, html, text


# ===== 템플릿 — 비밀번호 reset =====


def render_password_reset_email(*, recipient_name: str, reset_link: str, expire_minutes: int) -> tuple[str, str, str]:
    """
    비밀번호 reset 이메일 (HTML + text + subject) 생성.

    Args:
        recipient_name: 수신자 표시 이름
        reset_link: FE 의 reset 페이지 + 토큰 (full URL)
        expire_minutes: 만료 시간 안내

    Returns:
        (subject, html, text)
    """
    subject = "Harness 비밀번호 재설정 안내"
    text = (
        f"안녕하세요 {recipient_name}님,\n\n"
        f"Harness 계정의 비밀번호 재설정을 요청하셨습니다.\n"
        f"아래 링크를 클릭해 새 비밀번호를 설정해주세요 ({expire_minutes}분 후 만료):\n\n"
        f"{reset_link}\n\n"
        f"본인이 요청하지 않았다면 이 이메일을 무시해주세요. "
        f"비밀번호는 변경되지 않습니다.\n\n"
        f"— Harness Team"
    )
    html = f"""\
<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8" /><title>{subject}</title></head>
<body style="margin:0; padding:0; background:#F7F5EB; font-family:'Pretendard',sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#F7F5EB; padding:40px 0;">
    <tr><td align="center">
      <table role="presentation" width="520" cellspacing="0" cellpadding="0" style="background:#ffffff; border-radius:14px; padding:36px; box-shadow:0 4px 16px rgba(0,0,0,0.06);">
        <tr><td>
          <h1 style="margin:0 0 8px; font-size:22px; color:#2A2421; letter-spacing:-0.02em;">Harness</h1>
          <p style="margin:0 0 24px; font-size:12px; color:#8A817C; letter-spacing:0.06em; text-transform:uppercase;">비밀번호 재설정 안내</p>

          <p style="margin:0 0 12px; font-size:15px; color:#2A2421; line-height:1.6;">
            안녕하세요 <strong>{recipient_name}</strong>님,
          </p>
          <p style="margin:0 0 24px; font-size:14px; color:#2A2421; line-height:1.7;">
            Harness 계정의 비밀번호 재설정을 요청하셨습니다.<br/>
            아래 버튼을 클릭해 새 비밀번호를 설정해주세요. 링크는 <strong>{expire_minutes}분 후</strong> 만료됩니다.
          </p>

          <table role="presentation" cellspacing="0" cellpadding="0" style="margin:0 0 28px;">
            <tr><td>
              <a href="{reset_link}" style="display:inline-block; padding:12px 28px; background:#8C6239; color:#ffffff; text-decoration:none; border-radius:10px; font-weight:700; font-size:14px;">
                비밀번호 재설정
              </a>
            </td></tr>
          </table>

          <p style="margin:0 0 8px; font-size:12px; color:#6F665F; line-height:1.6;">
            버튼이 작동하지 않으면 아래 링크를 브라우저에 직접 붙여넣으세요:
          </p>
          <p style="margin:0 0 28px; font-size:12px; color:#8C6239; word-break:break-all; line-height:1.5;">
            <a href="{reset_link}" style="color:#8C6239; text-decoration:underline;">{reset_link}</a>
          </p>

          <hr style="border:none; border-top:1px solid rgba(140,98,57,0.15); margin:0 0 18px;" />
          <p style="margin:0; font-size:11px; color:#8A817C; line-height:1.6;">
            본인이 요청하지 않았다면 이 이메일을 무시해주세요. 비밀번호는 변경되지 않습니다.<br/>
            — Harness Team
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    return subject, html, text
