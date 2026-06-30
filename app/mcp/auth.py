"""
MCP 인증 미들웨어.

[목적]
FastMCP `/mcp/*` 경로로 들어오는 모든 요청에 JWT Bearer 인증 강제.
인증된 사용자 정보(UserPublic) 를 ContextVar 로 노출 →
tool 함수는 `current_mcp_user()` 로 호출자를 식별해 `assert_owns()` 에 넘김.

[흐름]
1. ASGI HTTP request 진입.
2. Authorization: Bearer <jwt> 헤더 추출. 없으면 401.
3. JWT 디코드 + access 토큰 강제 + jti 블랙리스트 검사.
4. Neo4j 에서 user 조회 (탈퇴 시 즉시 차단).
5. ContextVar 설정 → call_next (FastMCP tool 실행).
6. 응답 후 ContextVar 해제.

[설계 메모]
- 같은 인증 정책을 `get_current_user` 와 byte-equivalent 로 적용 →
  REST 라우트와 MCP 가 권한 모델 일치.
- streamable HTTP (SSE) 도 한 번의 HTTP request 안에서 처리되므로
  ContextVar 가 tool invoke 까지 유지됨 (asyncio task copy_context).
- 인증 실패 시 SSE handshake 전에 401 응답 → 클라이언트는 표준 HTTP 에러 처리.
"""
from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from typing import Optional

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core import token_blacklist
from app.core.security import decode_token

logger = logging.getLogger(__name__)


# 미들웨어 → tool 함수로 user 전달용. 인증 안 된 컨텍스트에선 None.
_current_mcp_user: ContextVar[Optional["UserPublic"]] = ContextVar(  # noqa: F821
    "harness_mcp_current_user", default=None
)

# fire-and-forget Task 참조 보존 — GC 가 in-flight task 를 죽이지 않도록.
_background_tasks: set[asyncio.Task] = set()


def current_mcp_user():
    """현재 MCP tool 호출의 인증된 사용자. 미들웨어 미통과면 None.

    Returns:
        UserPublic | None
    """
    return _current_mcp_user.get()


class MCPAuthMiddleware:
    """
    ASGI middleware — `/mcp/*` 의 모든 요청에 JWT Bearer 강제.

    REST `get_current_user` 와 동일 정책:
      - Bearer 헤더 필수
      - access 토큰만 허용 (refresh 로는 접근 불가)
      - jti 블랙리스트 (Redis) 검사 — 로그아웃된 토큰 차단
      - Neo4j 에서 user 재조회 — 탈퇴/삭제 사용자 즉시 차단

    실패 시 401 JSON 응답으로 즉시 종료.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # HTTP 외 (websocket/lifespan) 는 통과
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        try:
            user, payload = await self._authenticate(request)
        except HTTPException as e:
            response = JSONResponse(
                {"detail": e.detail}, status_code=e.status_code
            )
            await response(scope, receive, send)
            return
        except Exception:  # noqa: BLE001
            logger.exception("mcp auth middleware unexpected error")
            response = JSONResponse(
                {"detail": "internal auth error"}, status_code=500
            )
            await response(scope, receive, send)
            return

        token_ctx = _current_mcp_user.set(user)

        # last_used_at 업데이트 — fire-and-forget. Task 참조는 set 에 보존
        # 후 done callback 으로 제거 (GC 안전 + warning 회피).
        jti = payload.get("jti")
        if jti:
            from app.service import mcp_token_repository
            task = asyncio.create_task(mcp_token_repository.touch_last_used(jti))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        try:
            await self.app(scope, receive, send)
        finally:
            _current_mcp_user.reset(token_ctx)

    @staticmethod
    async def _authenticate(request: Request):
        """get_current_user 와 동일 정책. HTTPException 으로 실패 시그널."""
        # 지연 import — 순환 의존 회피 (core ↔ service ↔ middleware).
        from app.service import user_repository as users

        auth = request.headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401,
                detail="MCP 접근에는 Bearer 토큰이 필요합니다.",
            )
        token = auth.split(" ", 1)[1].strip()
        if not token:
            raise HTTPException(status_code=401, detail="빈 토큰입니다.")

        payload = decode_token(token)
        if payload.get("type") != "mcp":
            raise HTTPException(
                status_code=401,
                detail="MCP 전용 토큰이 필요합니다. 프로필에서 발급하세요.",
            )

        jti = payload.get("jti")
        if jti and await token_blacklist.is_revoked(jti):
            raise HTTPException(status_code=401, detail="로그아웃된 토큰입니다.")

        # [2026-06 hardening] Redis 블랙리스트는 fail-open (미가용/evict 시 통과) 인데
        # MCP 토큰은 90일 장수명이라 회수 우회 창이 크다. Neo4j McpToken.revoked 를
        # durable backstop 으로 추가 검사 — 인증은 어차피 직후 user 조회로 Neo4j 를
        # 치므로 추가 가용성 의존성은 없다. (조회 실패는 best-effort False → user 조회가 gate.)
        if jti:
            from app.service import mcp_token_repository
            if await mcp_token_repository.is_durably_revoked(jti):
                raise HTTPException(status_code=401, detail="회수된 토큰입니다.")

        email = payload.get("sub")
        if not email:
            raise HTTPException(
                status_code=401, detail="토큰에 사용자 정보가 없습니다."
            )

        user_db = await users.get_user_by_email(email)
        if not user_db:
            raise HTTPException(
                status_code=401, detail="사용자를 찾을 수 없습니다."
            )
        return users.UserPublic.from_db(user_db), payload


async def require_mcp_user_and_assert_owns(project_name: str):
    """
    Tool 함수용 공용 가드.

    - 인증된 사용자가 없으면 401 의미의 PermissionError.
    - 프로젝트 소유권이 없으면 403 의미의 PermissionError.

    MCP tool 함수는 raise 한 예외를 그대로 응답으로 시리얼라이즈해주므로,
    클라이언트 (AI Agent) 는 자연스러운 에러 메시지를 받게 된다.
    """
    from app.service import ownership_repository

    user = current_mcp_user()
    if user is None:
        # 미들웨어가 통과시킨 경우에만 여기까지 옴 — 방어적 안전망.
        raise PermissionError("인증이 필요합니다 (MCP).")

    try:
        await ownership_repository.assert_owns(user.email, project_name)
    except HTTPException as e:
        # 403 (남의 프로젝트) — tool 호출자에게 동일 의미로 전달.
        raise PermissionError(e.detail) from e
