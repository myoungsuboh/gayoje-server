"""표준 에러 스키마 + 도메인 예외 계층 (BE-E01-T05).

모든 에러 응답은 ErrorResponse(camelCase) 단일 형태로 — code/message/detail/traceId/
fields. 도메인 코드는 AppError 하위 예외를 raise 하면 핸들러가 HTTP 로 매핑한다.
"""
from __future__ import annotations

from typing import Any, List, Optional

from app.common.schemas import CamelModel


class ErrorDetail(CamelModel):
    code: str
    message: str
    detail: Optional[Any] = None
    trace_id: Optional[str] = None
    # 필드 검증 실패 목록 — [{field, message}]
    fields: Optional[List[dict]] = None


class ErrorResponse(CamelModel):
    error: ErrorDetail


# ===== 도메인 예외 계층 → HTTP 매핑 =====
class AppError(Exception):
    """도메인 예외 베이스. status_code/code 를 하위에서 지정."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        detail: Any = None,
        fields: Optional[List[dict]] = None,
    ) -> None:
        self.message = message or self.code
        self.detail = detail
        self.fields = fields
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
