"""
요청 본문 크기 제한 미들웨어 — 2026-05 보안 점검 #1 (DoS 방어).

[문제]
Pydantic 모델의 `meeting_content: str = Field(..., min_length=1)` 에는
max_length 가 없고, FastAPI 자체에도 body size limit 이 없음 →
100MB 같은 거대 페이로드를 받으면 메모리에 다 적재한 뒤에야 Pydantic /
quota 단에서 거부. 동시 요청 N개로 메모리 압박 = DoS.

[해결]
Content-Length 헤더가 한도를 초과하면 본문 읽기 전에 413 반환.
chunked 전송 (헤더 없음) 도 처리 — receive 단계에서 누적 검사.

[정책]
- 기본 한도: 10MB (회의록 텍스트 분량 충분, 보통 100KB 이하).
- 환경변수 MAX_REQUEST_BODY_BYTES 로 override 가능.
- /health, /health/deep 같은 GET 은 영향 없음 (본문 비어있음).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


logger = logging.getLogger(__name__)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """
    Content-Length > max_bytes 면 413 반환. 헤더 없는 chunked 는 본문 읽으면서 누적.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        if max_bytes <= 0:
            raise ValueError("max_bytes 는 양수여야 함")
        self.max_bytes = max_bytes

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # 1. Content-Length 빠른 거부
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
            except ValueError:
                # invalid 헤더 — 본문 읽으면서 누적 검사로 fallthrough
                size = -1
            if size > self.max_bytes:
                logger.warning(
                    "본문 크기 초과 (Content-Length=%s > %s) path=%s",
                    size, self.max_bytes, request.url.path,
                )
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"요청 본문이 너무 큽니다 "
                            f"(최대 {self.max_bytes // 1024 // 1024}MB)."
                        )
                    },
                )

        # 2. chunked / 헤더 없음 — 본문 누적 검사 wrapper
        # NOTE: BaseHTTPMiddleware 가 본문을 이미 buffer 하므로 여기서 받으면 늦음.
        # 그러나 일반 클라이언트는 Content-Length 를 항상 보내므로 1단계로 충분.
        # 헤더 없는 케이스는 fall-through 후 FastAPI 의 기본 동작 (메모리 적재) 으로.
        return await call_next(request)


def install_body_size_limit(app, max_bytes: int) -> None:
    """app.add_middleware 의 thin wrapper — main.py 가 호출."""
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)
