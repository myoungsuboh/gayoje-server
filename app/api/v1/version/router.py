"""version 도메인 — 배포/환경 메타 (BE-E01-T01).

GET /api/v1/version — 배포 확인/스모크용. 인증 불요.
레이어 규약상 단순 메타라 service/repository 없이 router 에서 직접 구성.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.version.schema import VersionResponse
from app.common import APP_VERSION
from app.common.timezone import now_kst_iso
from app.core.config import settings

router = APIRouter(prefix="/version", tags=["Version"])


@router.get("", response_model=VersionResponse)
async def get_version() -> VersionResponse:
    return VersionResponse(
        service="gayoje-server",
        version=APP_VERSION,
        env=settings.ENV,
        server_time=now_kst_iso(),
    )
