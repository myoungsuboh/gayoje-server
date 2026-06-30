"""
요청 컨텍스트 — request_id / user_email 을 로그에 자동 첨부.

[배경 — 2026-05 보안 점검 #2]
이전: stdlib `logging.basicConfig` 만 + format `%(asctime)s [%(levelname)s] ...`.
운영 디버깅 시 어느 사용자/어느 요청이 어떤 에러를 냈는지 grep 으로 못 잇음.

[구조]
1. RequestIdMiddleware:
   - 들어오는 X-Request-ID 헤더가 있으면 사용, 없으면 새 UUID 생성.
   - request.state.request_id 설정 + contextvar 에 저장.
   - 응답 헤더에도 같은 X-Request-ID 추가 (FE/load balancer 추적).
   - 가능하면 JWT 에서 user_email 도 best-effort 추출.

2. ContextFilter (logging.Filter):
   - logger 가 record 만들 때 contextvar 에서 request_id / user_email 을 record 에 첨부.
   - format string `... req=%(request_id)s user=%(user_email)s` 로 자동 출력.

[디자인 선택]
JSON 로그는 별도 PR — 단계적 도입. 우선 text 포맷에 컨텍스트 만 추가하는 게
운영자가 grep 으로 바로 쓰기 좋음. JSON 은 추후 LOG_FORMAT=json env 로 토글.
"""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Awaitable, Callable, Optional

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import settings


# ===== Context vars =====
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
_user_email_var: ContextVar[str] = ContextVar("user_email", default="-")


def current_request_id() -> str:
    return _request_id_var.get()


def current_user_email() -> str:
    return _user_email_var.get()


# ===== 미들웨어 =====


_INCOMING_HEADER = "x-request-id"
_OUTGOING_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    들어오는 X-Request-ID 우선, 없으면 새 UUID 발급. 응답 헤더에 같은 ID echo.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        rid = request.headers.get(_INCOMING_HEADER) or uuid.uuid4().hex
        # 외부에서 임의 길이/형식 받지 않게 truncate. UUID hex 32자 + 여유.
        rid = rid[:64]

        email = _try_extract_email(request)

        rid_token = _request_id_var.set(rid)
        email_token = _user_email_var.set(email or "-")
        try:
            request.state.request_id = rid
            response = await call_next(request)
        finally:
            _request_id_var.reset(rid_token)
            _user_email_var.reset(email_token)

        # 응답 헤더 — FE / load balancer 가 추적할 수 있게.
        response.headers[_OUTGOING_HEADER] = rid
        return response


def _try_extract_email(request: Request) -> Optional[str]:
    """
    JWT Authorization 헤더에서 email best-effort 추출. 디코드 실패 시 None.
    rate_limit_key 와 동일 패턴 — 본격 인증은 get_current_user 가 따로 강제.
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    try:
        payload = jwt.decode(
            parts[1],
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.PyJWTError:
        return None
    return payload.get("sub") or payload.get("email")


# ===== 로깅 필터 =====


class _ContextFilter(logging.Filter):
    """logger record 에 request_id / user_email 첨부 — format string 에서 사용."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        record.user_email = current_user_email()
        return True


def install_request_context_logging(root_logger: Optional[logging.Logger] = None) -> None:
    """
    root logger 의 모든 handler 에 _ContextFilter 부착.
    main.py 의 logging.basicConfig 이후에 한 번 호출.
    """
    log = root_logger or logging.getLogger()
    flt = _ContextFilter()
    for h in log.handlers:
        # 중복 부착 방지 — 같은 클래스 필터가 이미 있으면 skip.
        if any(isinstance(f, _ContextFilter) for f in h.filters):
            continue
        h.addFilter(flt)
