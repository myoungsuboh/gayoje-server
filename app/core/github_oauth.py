"""
GitHub OAuth helper — 인증 코드 흐름 (Authorization Code) 구현.

[Flow]
1. FE 가 BE 의 /auth/github/login 으로 이동
2. BE 가 state 토큰 발급 + GitHub authorize URL 로 redirect
3. 사용자가 GitHub 에서 승인
4. GitHub → BE /auth/github/callback?code=...&state=...
5. BE 가 code → access_token 교환, state 검증
6. BE 가 GitHub API 로 사용자 정보 조회
7. BE 가 우리 User 노드와 연결 (기존이면 link, 신규면 create) + 우리 JWT 발급
8. BE → FE callback URL 로 token 동봉 redirect

[State 토큰]
CSRF 방어용. 짧은 수명(10분) JWT — settings.JWT_SECRET_KEY 로 서명.
mode("login" | "link") + 선택적 email(link 모드 시 누가 연결하는지) 포함.

[보안 노트]
GitHub access_token 은 우리 DB (Neo4j User 노드) 에 저장된다 — private repo
다루려면 필요. `app/core/token_encryption.py` 의 Fernet (AES-128-CBC +
HMAC-SHA256) 으로 컬럼 암호화 후 저장. 복호화는 호출 시점에 `try_decrypt`.
TOKEN_ENCRYPTION_KEY env 미설정 시 평문 fallback (개발 편의) — 운영에서는
반드시 키 설정. user_repository.py 의 link_github / create_user_from_github
가 encrypt() 호출 지점.
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


GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_USER_EMAILS_URL = "https://api.github.com/user/emails"

STATE_EXPIRE_MINUTES = 10
STATE_TYPE = "github_oauth_state"


# ===== 예외 =====


class GitHubOAuthDisabled(Exception):
    """필수 환경 변수 미설정."""


class GitHubOAuthError(Exception):
    """OAuth flow 실패 (GitHub 호출 실패, 토큰 교환 실패 등)."""


# ===== State 토큰 =====


def create_state_token(mode: str, email: Optional[str] = None) -> str:
    """
    CSRF 방어용 state 토큰 생성.

    Args:
      mode: "login" (신규/기존 로그인) | "link" (이미 로그인한 사용자가 GitHub 연결)
      email: link 모드일 때 현재 사용자 email — callback 에서 누가 연결할지 식별
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
    # [2026-05 H2] state 서명은 OAUTH_STATE_SECRET_KEY (없으면 JWT_SECRET fallback).
    # 키 분리로 JWT_SECRET 누설 시 OAuth state 위조 표면 축소.
    return jwt.encode(
        payload, settings.oauth_state_secret, algorithm=settings.JWT_ALGORITHM
    )


def verify_state_token(token: str) -> Dict[str, Any]:
    """state 검증 → payload 반환. 실패 시 GitHubOAuthError."""
    try:
        # [2026-05 H2] OAUTH_STATE_SECRET_KEY 우선 (없으면 JWT_SECRET fallback).
        payload = jwt.decode(
            token, settings.oauth_state_secret, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError as e:
        raise GitHubOAuthError("state 토큰이 만료되었습니다. 다시 시도해주세요.") from e
    except jwt.InvalidTokenError as e:
        raise GitHubOAuthError("state 토큰이 유효하지 않습니다.") from e
    if payload.get("type") != STATE_TYPE:
        raise GitHubOAuthError("state 토큰 형식이 올바르지 않습니다.")
    if payload.get("mode") not in ("login", "link"):
        raise GitHubOAuthError("state 토큰의 mode 가 올바르지 않습니다.")
    return payload


# ===== Authorize URL =====


def build_authorize_url(state: str) -> str:
    """
    GitHub authorize URL 생성.
    호출 전에 `settings.github_oauth_enabled` 확인 필수.
    """
    if not settings.github_oauth_enabled:
        raise GitHubOAuthDisabled("GitHub OAuth 가 구성되지 않았습니다.")
    params = {
        "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
        "scope": " ".join(settings.github_oauth_scopes_list),
        "state": state,
        # PKCE 는 GitHub 가 일부 환경에서만 지원하므로 state 만 사용.
        # allow_signup=true (default) — 신규 GitHub 계정 가입도 허용
    }
    return f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"


# ===== Code ↔ Token 교환 =====


async def exchange_code_for_token(code: str) -> str:
    """
    Authorization code → access_token.
    Raises: GitHubOAuthError, GitHubOAuthDisabled
    """
    if not settings.github_oauth_enabled:
        raise GitHubOAuthDisabled("GitHub OAuth 가 구성되지 않았습니다.")

    data = {
        "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
        "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
        "code": code,
        "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
    }
    headers = {"Accept": "application/json"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(GITHUB_TOKEN_URL, data=data, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("GitHub token exchange failed: %s", e)
            raise GitHubOAuthError("GitHub 토큰 교환 실패.") from e

    body = resp.json() if resp.content else {}
    token = body.get("access_token")
    if not token:
        # GitHub 가 error 필드로 실패 사유를 줌
        err = body.get("error_description") or body.get("error") or "unknown"
        raise GitHubOAuthError(f"GitHub 토큰을 받지 못했습니다: {err}")
    return token


# ===== GitHub API: 사용자 정보 =====


async def fetch_github_user(access_token: str) -> Dict[str, Any]:
    """
    /user 와 /user/emails 를 조회해 정규화된 사용자 정보 반환.

    Returns:
      {
        "github_id": int (필수),
        "login": str (GitHub username, 필수),
        "name": str (없으면 login),
        "email": str (primary, verified email — 없으면 ValueError),
        "avatar_url": str,
      }

    Raises: GitHubOAuthError — API 호출 실패 또는 verified email 없음.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            user_resp = await client.get(GITHUB_USER_URL, headers=headers)
            user_resp.raise_for_status()
            user = user_resp.json()
        except httpx.HTTPError as e:
            raise GitHubOAuthError("GitHub /user 조회 실패.") from e

        # /user 의 email 은 사용자가 public 으로 노출하지 않으면 null.
        # /user/emails 로 primary + verified 이메일 명시 조회.
        email = user.get("email")
        if not email:
            try:
                emails_resp = await client.get(GITHUB_USER_EMAILS_URL, headers=headers)
                emails_resp.raise_for_status()
                emails = emails_resp.json() or []
            except httpx.HTTPError as e:
                raise GitHubOAuthError("GitHub /user/emails 조회 실패.") from e
            primary = next(
                (e for e in emails if e.get("primary") and e.get("verified")),
                None,
            )
            email = primary.get("email") if primary else None

    if not email:
        raise GitHubOAuthError(
            "GitHub 에서 verified 이메일을 받지 못했습니다. "
            "GitHub 계정의 이메일 인증 후 다시 시도해주세요."
        )
    github_id = user.get("id")
    login = user.get("login")
    if not github_id or not login:
        raise GitHubOAuthError("GitHub 사용자 정보가 불완전합니다.")

    return {
        "github_id": int(github_id),
        "login": str(login),
        "name": str(user.get("name") or login),
        "email": str(email).lower(),
        "avatar_url": str(user.get("avatar_url") or ""),
    }


def assert_oauth_configured() -> None:
    """라우트 진입 시 OAuth 미구성이면 503."""
    if not settings.github_oauth_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "GitHub OAuth 가 구성되지 않았습니다. "
                "관리자가 GITHUB_OAUTH_CLIENT_ID / SECRET / REDIRECT_URI / "
                "FRONTEND_OAUTH_CALLBACK_URL 환경 변수를 설정해야 합니다."
            ),
        )
