"""전역 예외 핸들러 — 모든 에러를 표준 envelope(traceId 포함)로 (BE-E01-T05).

- AppError(도메인 예외) → status_code/code 매핑.
- RequestValidationError → 422 + 필드별 fields.
- StarletteHTTPException(라우트 404/명시 HTTPException) → envelope(detail 보존).
- 그 외 미처리 Exception → 500(내부 메시지 비노출, traceId 로 로그 상관).
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.common.errors import AppError, ErrorDetail, ErrorResponse
from app.core.request_context import current_request_id

logger = logging.getLogger("gayoje.error")

# HTTP status → 안정적 에러 코드(라우트 HTTPException 매핑용).
_STATUS_CODE: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


def _trace_id() -> str | None:
    rid = current_request_id()
    return rid if rid and rid != "-" else None


def _envelope(status_code, code, message, *, detail=None, fields=None) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(
            code=code, message=message, detail=detail,
            trace_id=_trace_id(), fields=fields,
        )
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(by_alias=True))


async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return _envelope(
        exc.status_code, exc.code, exc.message, detail=exc.detail, fields=exc.fields
    )


async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    fields = [
        {"field": ".".join(str(p) for p in e.get("loc", [])), "message": e.get("msg", "")}
        for e in exc.errors()
    ]
    return _envelope(
        422, "validation_error", "요청 검증에 실패했습니다.", fields=fields
    )


async def _http_exception_handler(
    _: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = _STATUS_CODE.get(exc.status_code, "error")
    if isinstance(exc.detail, str):
        message, detail = exc.detail, None
    else:
        # dict/구조화 detail(예: health_deep checks)은 detail 에 보존.
        message, detail = code, exc.detail
    return _envelope(exc.status_code, code, message, detail=detail)


async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
    # 내부 메시지는 응답에 노출하지 않고 로그로만(traceId 로 상관).
    logger.exception("unhandled exception: %s", type(exc).__name__)
    return _envelope(500, "internal_error", "서버 오류가 발생했습니다.")


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
