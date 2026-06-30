"""version 도메인 스키마 (BE-E01-T01)."""
from __future__ import annotations

from app.common.schemas import CamelModel


class VersionResponse(CamelModel):
    """GET /api/v1/version 응답. server_time 은 응답에서 serverTime 으로 직렬화."""

    service: str
    version: str
    env: str
    server_time: str  # KST ISO8601
