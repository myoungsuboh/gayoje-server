"""
Notion OAuth helper — Public Integration / Authorization Code 흐름.

[Flow]
1. 로그인된 사용자가 FE 에서 "노션 연결" → BE `/auth/notion/link` (POST, Bearer)
   호출 → state JWT + authorize URL 반환.
2. FE 가 받은 URL 로 navigate → 사용자가 노션에서 워크스페이스 승인.
3. 노션 → BE `/auth/notion/callback?code=...&state=...`
4. BE 가 code → access_token 교환 + state 검증
5. BE 가 user_repository.link_notion 으로 토큰 저장 → FE callback URL 로 mode=notion_link
   redirect (이미 BE JWT 가 있는 사용자라 새 access_token 발급 안 함)

[GitHub OAuth 와 차이점]
- login 모드 없음 — 노션은 로그인 수단으로 안 씀. link 만 지원.
- 응답 body 에 workspace_id / workspace_name / bot_id 추가 — 노션 API 호출 시 필요.
- token endpoint 가 Basic Auth (client_id:client_secret base64) 필요.

[보안]
state 토큰: JWT (10분 만료, settings.JWT_SECRET_KEY 서명) + email 포함 →
callback 시 누구의 연결인지 인증된 값으로 식별. FE 가 보낸 값은 절대 신뢰 X.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
import jwt

from app.core.config import settings

logger = logging.getLogger(__name__)


NOTION_AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token"

STATE_EXPIRE_MINUTES = 10
STATE_TYPE = "notion_oauth_state"


# ===== 예외 =====


class NotionOAuthDisabled(Exception):
    """필수 환경 변수 미설정."""


class NotionOAuthError(Exception):
    """OAuth flow 실패 (Notion 호출 실패, 토큰 교환 실패 등)."""


# ===== 가드 =====


def assert_oauth_configured() -> None:
    if not settings.notion_oauth_enabled:
        raise NotionOAuthDisabled(
            "Notion OAuth 미설정 — NOTION_OAUTH_CLIENT_ID / CLIENT_SECRET / "
            "REDIRECT_URI 환경변수를 설정해주세요."
        )


# ===== State 토큰 =====


def create_state_token(email: str) -> str:
    """CSRF 방어용 state — 누가 연결 중인지 (Bearer 인증된 email) 포함."""
    if not email:
        raise ValueError("email is required for notion link state")
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "type": STATE_TYPE,
        "mode": "link",
        "email": email,
        "iat": now,
        "exp": now + timedelta(minutes=STATE_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm="HS256")


def verify_state_token(state: str) -> Dict[str, Any]:
    """state 검증 후 payload 반환. 만료/변조면 예외."""
    try:
        payload = jwt.decode(state, settings.JWT_SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as e:
        raise NotionOAuthError("state_expired") from e
    except jwt.InvalidTokenError as e:
        raise NotionOAuthError(f"invalid_state:{e}") from e
    if payload.get("type") != STATE_TYPE:
        raise NotionOAuthError("state_type_mismatch")
    if not payload.get("email"):
        raise NotionOAuthError("state_email_missing")
    return payload


# ===== Authorize URL =====


def build_authorize_url(state: str) -> str:
    """노션 OAuth authorize URL — owner=user 로 사용자 권한 받기."""
    assert_oauth_configured()
    params = {
        "client_id": settings.NOTION_OAUTH_CLIENT_ID,
        "redirect_uri": settings.NOTION_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "owner": "user",
        "state": state,
    }
    return f"{NOTION_AUTHORIZE_URL}?{urlencode(params)}"


# ===== Code → token 교환 =====


async def exchange_code_for_token(code: str) -> Dict[str, Any]:
    """
    노션 token endpoint 호출 — Basic Auth (client_id:secret) 필수.

    응답 예:
        {
            "access_token": "secret_...",
            "token_type": "bearer",
            "bot_id": "...",
            "workspace_id": "...",
            "workspace_name": "My Workspace",
            "workspace_icon": "...",
            "owner": { "type": "user", "user": { ... } },
            "duplicated_template_id": null
        }
    """
    assert_oauth_configured()
    creds = f"{settings.NOTION_OAUTH_CLIENT_ID}:{settings.NOTION_OAUTH_CLIENT_SECRET}"
    basic = base64.b64encode(creds.encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.NOTION_OAUTH_REDIRECT_URI,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(NOTION_TOKEN_URL, headers=headers, json=body)
    except httpx.HTTPError as e:
        logger.warning("notion token exchange network error: %s", e)
        raise NotionOAuthError("notion_token_network_error") from e

    if res.status_code != 200:
        # 응답 body 가 짧을 거 — 로그에 dump
        logger.warning(
            "notion token exchange failed: status=%s body=%s",
            res.status_code,
            res.text[:500],
        )
        raise NotionOAuthError(f"notion_token_status_{res.status_code}")

    try:
        data = res.json()
    except Exception as e:  # noqa: BLE001
        raise NotionOAuthError("notion_token_json_decode_failed") from e

    if not data.get("access_token"):
        raise NotionOAuthError("notion_token_missing_access_token")
    return data
