"""Festivals 도메인 라우터 — 공개 브라우즈(목록/상세) 읽기 API.

GET /api/v1/festivals        — 가요제 목록(요약: 제목·일정·장소·지역·주최)
GET /api/v1/festivals/{id}   — 가요제 상세

레이어 규약: router → service. 공개 읽기라 인증 불요.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.festivals.schema import (
    FestivalDetail,
    FestivalListItem,
    FestivalListResponse,
)
from app.api.v1.festivals.service import get_festival, list_festivals
from app.common.errors import NotFoundError
from app.infra.db import get_session

router = APIRouter(prefix="/festivals", tags=["Festivals"])


@router.get("", response_model=FestivalListResponse)
async def list_festival_events(
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    region: str | None = Query(None, description="지역명 부분일치(예: 서울)"),
    session: AsyncSession = Depends(get_session),
) -> FestivalListResponse:
    rows, total = await list_festivals(session, limit=limit, offset=offset, region=region)
    return FestivalListResponse(
        items=[FestivalListItem.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{festival_id}", response_model=FestivalDetail)
async def get_festival_event(
    festival_id: int,
    session: AsyncSession = Depends(get_session),
) -> FestivalDetail:
    ev = await get_festival(session, festival_id)
    if ev is None:
        raise NotFoundError(f"가요제를 찾을 수 없습니다: {festival_id}")
    return FestivalDetail.model_validate(ev)
