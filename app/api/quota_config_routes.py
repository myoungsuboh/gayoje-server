"""
QuotaConfig 라우트 — admin 한도 조회/수정 + 공개 조회.

[엔드포인트]
- GET    /api/quota-config              → 공개. 모든 등급의 한도 (FE pricing 카드 표시).
- GET    /api/admin/quota-config        → admin 전체 조회.
- PUT    /api/admin/quota-config/{tier} → admin 한도 수정.

[설계 의도]
한도 조정 후 재배포 없이 즉시 사용자에게 반영. admin update 시 BE 메모리 상의
`quota._LIMITS_OVERRIDE` 도 함께 갱신해서 다음 가드 호출부터 새 값 사용.

[보안]
공개 라우트: 인증 불필요 (Pricing 카드 표시용 정보 — 가격과 동일 수준).
admin 라우트: get_admin_user + slowapi rate limit.
한도 변경은 audit_repository 에 ACTION_QUOTA_CONFIG_UPDATE 로 영구 기록.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core import quota
from app.core.limiter import limiter
from app.core.security import get_admin_user
from app.core.subscription import SUBSCRIPTION_TYPES
from app.service import audit_repository, quota_config_repository
from app.service.audit_repository import ACTION_QUOTA_CONFIG_UPDATE
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)


public_router = APIRouter(prefix="/api", tags=["QuotaConfig"])
admin_router = APIRouter(prefix="/api/admin", tags=["Admin", "QuotaConfig"])


# ===== Response DTOs =====


class QuotaConfigItem(BaseModel):
    tier: str = Field(..., description="'free' | 'pro' | 'pro_plus' | 'pro_max'")
    meeting_logs: int = Field(..., description="월간 미팅 로그 등록 한도")
    summary_chars: int = Field(..., description="회의록 1회 입력 글자수 상한")
    total_tokens: int = Field(..., description="월간 LLM 누적 토큰 (메인/Flash)")
    library_skills: int = Field(..., description="스킬 라이브러리 저장 수")
    max_projects: int = Field(..., description="동시 보유 가능한 프로젝트 수")
    lite_daily_cap: int = Field(0, description="메인 소진 후 Lite 오버플로우 주간 캡 (롤링 7일, 0=하드월 — 필드명은 호환 유지)")
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class QuotaConfigListResponse(BaseModel):
    quota: List[QuotaConfigItem]


# ===== Request DTOs =====


class UpdateQuotaConfigRequest(BaseModel):
    # 상한은 보수적으로 잡음 — 실수로 큰 값 박는 사고 방지.
    meeting_logs: int = Field(..., ge=0, le=100_000)
    summary_chars: int = Field(..., ge=0, le=10_000_000)
    total_tokens: int = Field(..., ge=0, le=10_000_000_000)
    library_skills: int = Field(..., ge=0, le=1_000_000)
    max_projects: int = Field(..., ge=0, le=10_000)
    # [2026-06-11 주간 전환] Lite 주간 캡. 0=오버플로우 없음(하드월). 필드명/optional 은 호환 유지.
    lite_daily_cap: int = Field(0, ge=0, le=10_000_000_000)


# ===== 공개 라우트 =====


@public_router.get("/quota-config", response_model=QuotaConfigListResponse)
@limiter.limit("60/minute")
async def get_public_quota_config(request: Request) -> QuotaConfigListResponse:
    """공개 한도 조회 — pricing 카드 표시용. 인증 불필요."""
    rows = await quota_config_repository.list_quota_config()
    return QuotaConfigListResponse(
        quota=[QuotaConfigItem(**r.to_dict()) for r in rows]
    )


# ===== admin 라우트 =====


@admin_router.get("/quota-config", response_model=QuotaConfigListResponse)
@limiter.limit("60/minute")
async def list_quota_config_admin_route(
    request: Request,
    _admin: UserPublic = Depends(get_admin_user),
) -> QuotaConfigListResponse:
    """admin 전체 한도 조회."""
    rows = await quota_config_repository.list_quota_config()
    return QuotaConfigListResponse(
        quota=[QuotaConfigItem(**r.to_dict()) for r in rows]
    )


@admin_router.put("/quota-config/{tier}", response_model=QuotaConfigItem)
@limiter.limit("30/minute")
async def update_quota_config_route(
    request: Request,
    tier: str,
    payload: UpdateQuotaConfigRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> QuotaConfigItem:
    """한도 수정 — 5개 필드 한 번에 갱신. in-memory override 즉시 반영."""
    if tier not in SUBSCRIPTION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tier 는 {SUBSCRIPTION_TYPES} 중 하나여야 합니다.",
        )

    before = await quota_config_repository.get_quota_config(tier)
    if before is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"등급 '{tier}' 의 한도 설정을 찾을 수 없습니다.",
        )

    after = await quota_config_repository.update_quota_config(
        tier=tier,
        meeting_logs=payload.meeting_logs,
        summary_chars=payload.summary_chars,
        total_tokens=payload.total_tokens,
        library_skills=payload.library_skills,
        max_projects=payload.max_projects,
        lite_daily_cap=payload.lite_daily_cap,
        updated_by=admin.email,
    )
    if after is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="한도 수정에 실패했습니다.",
        )

    # in-memory override 즉시 반영 — 다음 가드 호출부터 새 값.
    quota.apply_limits_override(
        tier,
        {
            "meeting_logs": after.meeting_logs,
            "summary_chars": after.summary_chars,
            "total_tokens": after.total_tokens,
            "library_skills": after.library_skills,
            "max_projects": after.max_projects,
            "lite_daily_cap": after.lite_daily_cap,
        },
    )

    # 감사 로그 — best-effort.
    try:
        await audit_repository.write(
            actor_email=admin.email,
            action=ACTION_QUOTA_CONFIG_UPDATE,
            target_email="",
            payload={
                "tier": tier,
                "from": before.to_dict(),
                "to": after.to_dict(),
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("quota_config audit log 실패 (tier=%s): %s", tier, e)

    return QuotaConfigItem(**after.to_dict())


# ===== 부팅 헬퍼 =====


async def load_quota_overrides_into_memory() -> None:
    """부팅 시 호출 — DB 의 QuotaConfig 를 quota._LIMITS_OVERRIDE 에 load.

    이후엔 quota.get_limits / get_max_projects 가 이 값을 우선 사용.
    """
    try:
        rows = await quota_config_repository.list_quota_config()
        for row in rows:
            quota.apply_limits_override(
                row.tier,
                {
                    "meeting_logs": row.meeting_logs,
                    "summary_chars": row.summary_chars,
                    "total_tokens": row.total_tokens,
                    "library_skills": row.library_skills,
                    "max_projects": row.max_projects,
                    # [2026-06] _row_to_config 가 기존 노드엔 코드 기본값 fallback —
                    # 0 이 박혀 라이브 오버플로우가 하드월로 깨지는 사고 방지.
                    "lite_daily_cap": row.lite_daily_cap,
                },
            )
        logger.info(
            "quota_config: %d개 등급 한도를 in-memory override 에 load 완료",
            len(rows),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("quota_config: in-memory load 실패, 코드 상수 fallback (%s)", e)
