"""
[2026-06-04 보안] 일회용(throwaway) 이메일 도메인 차단 — 계정 대량생성 어뷰징 방어.

per-account quota 는 계정 단위라, 일회용 메일로 무한히 계정을 찍으면 무료 quota 를
계속 새로 받는 어뷰징이 가능하다. 진짜 메일 소유를 요구하면 계정당 비용이 올라가
대량생성을 억제한다. (정식 이메일 인증 도입 전의 1차 방어.)

[오탐 0 원칙]
명백한 throwaway 서비스 도메인만 보수적으로 등록한다. 일반 사용자가 쓰는 도메인
(gmail/naver/daum/outlook/회사 도메인 등)은 절대 포함하지 않는다. 목록은 추가만 하고
제거는 신중히 — 잘못 넣으면 정상 가입이 막힌다.
"""
from __future__ import annotations

# 잘 알려진 일회용 메일 제공자 (보수적 큐레이션). 소문자 도메인.
_DISPOSABLE_DOMAINS: frozenset[str] = frozenset({
    "mailinator.com",
    "guerrillamail.com",
    "guerrillamail.net",
    "guerrillamail.org",
    "sharklasers.com",
    "grr.la",
    "10minutemail.com",
    "10minutemail.net",
    "temp-mail.org",
    "tempmail.com",
    "tempmailo.com",
    "throwawaymail.com",
    "yopmail.com",
    "yopmail.net",
    "getnada.com",
    "nada.email",
    "maildrop.cc",
    "dispostable.com",
    "fakeinbox.com",
    "trashmail.com",
    "trashmail.de",
    "mailnesia.com",
    "mohmal.com",
    "emailondeck.com",
    "tempr.email",
    "mailcatch.com",
    "spamgourmet.com",
    "mintemail.com",
    "moakt.com",
    "tmail.ws",
    "tmpmail.org",
    "burnermail.io",
    "33mail.com",
    "anonaddy.com",
    "guerrillamailblock.com",
    "spam4.me",
    "mailto.plus",
    "fakemail.net",
    "discard.email",
})


def email_domain(email: str) -> str:
    """이메일에서 도메인부(소문자)만 추출. 형식 이상 시 ""."""
    if not email or "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].strip().lower()


def is_disposable_email(email: str) -> bool:
    """일회용 메일 도메인이면 True. (서브도메인도 차단: x.mailinator.com 등.)"""
    domain = email_domain(email)
    if not domain:
        return False
    if domain in _DISPOSABLE_DOMAINS:
        return True
    # 서브도메인 매칭 — sub.mailinator.com 도 차단.
    return any(domain.endswith("." + d) for d in _DISPOSABLE_DOMAINS)
