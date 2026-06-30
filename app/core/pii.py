"""PII 마스킹 — 로그/응답에서 이메일·전화번호 노출 방지 (BE-E01-T06).

- mask_pii(text): 자유 텍스트(로그 메시지 등) 내 이메일/전화 전체 마스킹.
- mask_email_partial(email): 상관(correlation)용 — 앞 1자+도메인 보존, 나머지 마스킹.
"""
from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 한국 휴대폰(01x-xxxx-xxxx, 구분자 -/공백/. 허용).
_PHONE_RE = re.compile(r"01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}")


def mask_email_partial(email: str) -> str:
    """a***@domain — local-part 앞 1자만 남기고 마스킹(도메인 보존)."""
    local, sep, domain = email.partition("@")
    if not sep:
        return email
    head = local[:1] if local else ""
    return f"{head}***@{domain}"


def mask_pii(text: str) -> str:
    """텍스트 내 이메일/전화번호 마스킹."""
    if not text:
        return text
    text = _EMAIL_RE.sub("***@***", text)
    text = _PHONE_RE.sub("***-****-****", text)
    return text
