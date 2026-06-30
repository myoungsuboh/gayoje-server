"""
slowapi rate limiter — 보안 점검 (2026-05).

[변경 — 2026-05]
이전: `Limiter(key_func=get_remote_address)` 단일.
문제: reverse proxy 뒤에 BE 가 있을 때 `request.client.host` 가 proxy IP 만 반환.
      → 모든 사용자가 같은 버킷 공유 → rate limit 실질 무력화 / DoS 표면.

해법: JWT Authorization 헤더가 있으면 email 기반 키, 없거나 invalid 면 IP.
- email 기반: 인증된 endpoint (대다수) — 사용자 단위 정확한 제한.
- IP fallback: /auth/login, /auth/signup 등 익명 endpoint.
- IP 추출: X-Forwarded-For 첫 IP > X-Real-IP > request.client.host 순서.

JWT 디코드 실패 / 만료 / 토큰 없음 → IP fallback (전부 익명 취급).
"""
from __future__ import annotations

from typing import Optional

import jwt
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.core.config import settings


def _extract_email_from_jwt(request: Request) -> Optional[str]:
    """
    Authorization: Bearer <token> 에서 email 추출.

    rate limit 키 결정용이라 best-effort — 예외는 모두 None 으로.
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1]
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.PyJWTError:
        return None
    return payload.get("sub") or payload.get("email")


def _extract_client_ip(request: Request) -> str:
    """
    실제 클라이언트 IP 추출 — proxy 헤더 우선.

    X-Forwarded-For 는 클라이언트가 spoofing 가능 — 신뢰 proxy 뒤에서만 안전.
    rate limit 용이라 보안 결정엔 사용 안 함 (인증/감사 로그는 별도).
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return get_remote_address(request)


def rate_limit_key(request: Request) -> str:
    """
    JWT user email > 클라이언트 IP 우선순위로 rate limit 키 결정.

    - 인증된 요청: "user:<email>" — 같은 사용자의 여러 IP 합산.
    - 익명 요청: "ip:<client_ip>" — login/signup 등 공격 표면 보호.
    """
    email = _extract_email_from_jwt(request)
    if email:
        return f"user:{email}"
    return f"ip:{_extract_client_ip(request)}"


limiter = Limiter(
    key_func=rate_limit_key,
    # [2026-06-04 보안] 전역 기본 한도 — 데코레이터(@limiter.limit) 없는 라우트도
    # 라우트별·사용자별(email>IP)로 자동 보호. 명시 데코가 있으면 그 값이 우선(더 빡빡).
    # per-route 카운트라 폴링(한 라우트)·페이지 fan-out(여러 라우트)이 합산되지 않음 →
    # 정상 사용자(디자인 업데이트 수십 콜, getJobStatus 3초 폴링 등)는 절대 안 닿고,
    # 한 엔드포인트를 분당 300+ 때리는 폭주 스크립트만 차단. 오픈 전 느슨값(300/분).
    # 모니터링/헬스/문서 라우트는 main.py 에서 @limiter.exempt 로 면제.
    default_limits=["300/minute"],
)
