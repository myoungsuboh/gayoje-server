"""festivals 도메인 서비스 — 공개 목록/상세 조회 (읽기 전용).

레이어 규약: router → service → (여기서는 직접 ORM 조회). 정렬은 시작일 최신 우선
(NULL 은 뒤로), 동일 시 id 역순. 지역(region) 부분일치 필터 옵션.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.festivals.models import FestivalEvent


async def list_festivals(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    region: str | None = None,
) -> tuple[list[FestivalEvent], int]:
    """가요제 목록 + 총건수. region 은 region_name 부분일치."""
    where = []
    if region:
        where.append(FestivalEvent.region_name.ilike(f"%{region}%"))

    count_stmt = select(func.count()).select_from(FestivalEvent)
    for c in where:
        count_stmt = count_stmt.where(c)
    total = int(await session.scalar(count_stmt) or 0)

    stmt = select(FestivalEvent)
    for c in where:
        stmt = stmt.where(c)
    # 시작일 최신 우선(NULL 뒤로), 동률은 id 역순.
    stmt = stmt.order_by(
        FestivalEvent.start_date.is_(None),
        FestivalEvent.start_date.desc(),
        FestivalEvent.id.desc(),
    ).limit(limit).offset(offset)
    rows = list((await session.scalars(stmt)).all())
    return rows, total


async def get_festival(session: AsyncSession, festival_id: int) -> FestivalEvent | None:
    return await session.get(FestivalEvent, festival_id)
