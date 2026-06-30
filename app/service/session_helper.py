"""
Session 등록 helper — 로그인 / refresh 라우트가 호출.

[Phase 3 — 2026-05-18]
모든 로그인 경로 (email/password / GitHub / Google / refresh) 가 토큰 발급 후
이 helper 만 호출하면 session_registry 에 등록됨. 라우트 코드 중복 ↓.

[FastAPI Request 에서 메타 추출]
- User-Agent: starlette 가 자동 파싱
- IP: X-Forwarded-For (Cloudflare/proxy 경유) → 첫번째 → fallback request.client.host

[Fail-open]
session_registry 가 fail-open 이라 여기서도 raise 안 함 — 인증 흐름 그대로.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request

from app.core import session_registry
from app.core.security import decode_token_lenient


def _client_ip(request: Optional[Request]) -> str:
    """X-Forwarded-For 우선 (proxy 뒤), 없으면 request.client.host.

    XFF 가 여러 IP 면 첫번째 (가장 멀리 있는 client).
    """
    if not request:
        return ""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return getattr(request.client, "host", "") if request.client else ""


def _user_agent(request: Optional[Request]) -> str:
    if not request:
        return ""
    return request.headers.get("user-agent") or ""


def _device_label_from_ua(ua: str) -> str:
    """UA 에서 디바이스/브라우저 추론 — FE 가 친근하게 표시할 라벨.

    완벽한 파싱 아니라 핵심 키워드만 매칭. 라이브러리 의존성 추가 없이 ms 단위.
    예: 'Chrome on macOS', 'Safari on iPhone', '브라우저 (unknown)'.
    """
    if not ua:
        return "알 수 없는 디바이스"
    ua_l = ua.lower()
    # browser
    if "edg/" in ua_l:
        browser = "Edge"
    elif "chrome" in ua_l and "edg" not in ua_l:
        browser = "Chrome"
    elif "firefox" in ua_l:
        browser = "Firefox"
    elif "safari" in ua_l and "chrome" not in ua_l:
        browser = "Safari"
    else:
        browser = "브라우저"
    # OS / 디바이스
    if "iphone" in ua_l:
        device = "iPhone"
    elif "ipad" in ua_l:
        device = "iPad"
    elif "android" in ua_l:
        device = "Android"
    elif "macintosh" in ua_l or "mac os" in ua_l:
        device = "macOS"
    elif "windows" in ua_l:
        device = "Windows"
    elif "linux" in ua_l:
        device = "Linux"
    else:
        device = "PC"
    return f"{browser} on {device}"


async def record_access_token_session(
    access_token: str,
    *,
    request: Optional[Request] = None,
) -> None:
    """access token 의 jti / email / exp 를 회수해 session_registry 등록.

    [언제 호출]
    모든 로그인 경로 (email/password / OAuth callback / refresh) 의 토큰 발급 직후.

    [Fail-open]
    - 토큰 decode 실패 → silent return (로그인 자체는 성공한 시점이라 분리 처리)
    - session_registry 가 Redis 실패 시 silent (warning log)
    """
    payload = decode_token_lenient(access_token)
    if not payload:
        return
    email = payload.get("sub") or ""
    jti = payload.get("jti") or ""
    exp = payload.get("exp")
    if not (email and jti and exp):
        return

    ua = _user_agent(request)
    ip = _client_ip(request)
    device_label = _device_label_from_ua(ua)

    await session_registry.record_session(
        email=email,
        jti=jti,
        exp_epoch=int(exp),
        user_agent=ua,
        ip=ip,
        device_label=device_label,
    )
