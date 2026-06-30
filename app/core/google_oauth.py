"""
Google OAuth helper — 인증 코드 흐름 (Authorization Code) 구현.

[Flow]
1. FE 가 BE 의 /auth/google/login 으로 이동
2. BE 가 state 토큰 발급 + Google authorize URL 로 redirect
3. 사용자가 Google 에서 승인
4. Google → BE /auth/google/callback?code=...&state=...
5. BE 가 code → access_token 교환, state 검증
6. BE 가 Google API 로 사용자 정보 조회 (userinfo endpoint)
7. BE 가 우리 User 노드와 연결 (기존이면 link, 신규면 create) + 우리 JWT 발급
8. BE → FE callback URL 로 token 동봉 redirect

[State 토큰]
GitHub 와 동일 패턴 — JWT (10분 만료) + mode("login" | "link") + email.
같은 STATE_TYPE 으로 GitHub 와 동일 검증 로직 가능하지만, 두 OAuth 가 섞이지
않도록 별도 type ("google_oauth_state") 사용.

[보안 노트]
Google access_token 은 우리 DB 에 저장 안 함 — 단발성으로 userinfo 만 조회 후
폐기. (private repo 등 후속 API 호출 시나리오 없음.) 향후 Google Drive 등
연동 필요 시 token_encryption 적용.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)


GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

STATE_EXPIRE_MINUTES = 10
STATE_TYPE = "google_oauth_state"

# 최소 권한 — userinfo 만 필요. profile + email scope 표준.
DEFAULT_SCOPES = ["openid", "email", "profile"]


# ===== 예외 =====


class GoogleOAuthDisabled(Exception):
    """필수 환경 변수 미설정."""


class GoogleOAuthError(Exception):
    """OAuth flow 실패."""


# ===== State 토큰 =====


def create_state_token(mode: str, email: Optional[str] = None) -> str:
    """
    CSRF 방어용 state 토큰 생성.

    Args:
      mode: "login" (신규/기존 로그인) | "link" (이미 로그인한 사용자가 Google 연결)
      email: link 모드일 때 현재 사용자 email
    """
    if mode not in ("login", "link"):
        raise ValueError(f"invalid mode: {mode}")
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "type": STATE_TYPE,
        "mode": mode,
        "iat": now,
        "exp": now + timedelta(minutes=STATE_EXPIRE_MINUTES),
    }
    if email:
        payload["email"] = email
    return jwt.encode(
        payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )


def verify_state_token(token: str) -> Dict[str, Any]:
    """state 검증 → payload 반환."""
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError as e:
        raise GoogleOAuthError("state 토큰이 만료되었습니다. 다시 시도해주세요.") from e
    except jwt.InvalidTokenError as e:
        raise GoogleOAuthError("state 토큰이 유효하지 않습니다.") from e
    if payload.get("type") != STATE_TYPE:
        raise GoogleOAuthError("state 토큰 형식이 올바르지 않습니다.")
    if payload.get("mode") not in ("login", "link"):
        raise GoogleOAuthError("state 토큰의 mode 가 올바르지 않습니다.")
    return payload


# ===== Authorize URL =====


def build_authorize_url(state: str) -> str:
    """Google authorize URL 생성."""
    if not settings.google_oauth_enabled:
        raise GoogleOAuthDisabled("Google OAuth 가 구성되지 않았습니다.")
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "scope": " ".join(settings.google_oauth_scopes_list),
        "response_type": "code",
        "state": state,
        # access_type=online — refresh_token 불필요 (단발성 userinfo)
        # prompt=select_account — 매번 계정 선택 화면 (UX 명확)
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


# ===== Code ↔ Token 교환 =====


async def exchange_code_for_token(code: str) -> str:
    """Authorization code → access_token."""
    if not settings.google_oauth_enabled:
        raise GoogleOAuthDisabled("Google OAuth 가 구성되지 않았습니다.")

    data = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
        "code": code,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    headers = {"Accept": "application/json"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(GOOGLE_TOKEN_URL, data=data, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Google token exchange failed: %s", e)
            raise GoogleOAuthError("Google 토큰 교환 실패.") from e

    body = resp.json() if resp.content else {}
    token = body.get("access_token")
    if not token:
        err = body.get("error_description") or body.get("error") or "unknown"
        raise GoogleOAuthError(f"Google 토큰을 받지 못했습니다: {err}")
    return token


# ===== Google API: 사용자 정보 =====


async def fetch_google_user(access_token: str) -> Dict[str, Any]:
    """
    /userinfo 조회 → 정규화된 사용자 정보 반환.

    Returns:
      {
        "google_id": str (subject "sub", 필수),
        "email": str (verified, 필수),
        "name": str,
        "picture": str (avatar URL),
      }

    Raises: GoogleOAuthError — API 실패 또는 unverified email.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(GOOGLE_USERINFO_URL, headers=headers)
            resp.raise_for_status()
            user = resp.json()
        except httpx.HTTPError as e:
            raise GoogleOAuthError("Google /userinfo 조회 실패.") from e

    google_id = user.get("sub")
    email = user.get("email")
    email_verified = user.get("email_verified")

    if not google_id:
        raise GoogleOAuthError("Google 사용자 ID(sub) 를 받지 못했습니다.")
    if not email:
        raise GoogleOAuthError("Google 에서 이메일을 받지 못했습니다.")
    if not email_verified:
        raise GoogleOAuthError(
            "Google 계정의 이메일이 verified 상태가 아닙니다. "
            "Gmail 인증 후 다시 시도해주세요."
        )

    return {
        "google_id": str(google_id),
        "email": str(email).lower(),
        "name": str(user.get("name") or email.split("@")[0]),
        "picture": str(user.get("picture") or ""),
    }


def assert_oauth_configured() -> None:
    """라우트 진입 시 OAuth 미구성이면 503."""
    if not settings.google_oauth_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Google OAuth 가 구성되지 않았습니다. "
                "관리자가 GOOGLE_OAUTH_CLIENT_ID / SECRET / REDIRECT_URI / "
                "FRONTEND_OAUTH_CALLBACK_URL 환경 변수를 설정해야 합니다."
            ),
        )
