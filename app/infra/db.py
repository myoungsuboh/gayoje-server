"""PostgreSQL 비동기 연결 — SQLAlchemy async engine/session (BE-E01-T03).

DATABASE_URL 로 엔진을 lazy 싱글톤 생성. PG(asyncpg) / sqlite(aiosqlite) 모두 지원.
- get_engine(): 프로세스 단일 AsyncEngine (풀 포함).
- get_session(): FastAPI 의존성 — 요청 스코프 AsyncSession (예외 시 rollback, 항상 close).
- session_scope(): 비-요청(잡/스크립트)용 컨텍스트 매니저 — commit/rollback 자동.
- check_db(): 헬스용 SELECT 1.
- dispose_engine(): lifespan 종료 시 풀 정리.

PG/SQLite 분기: sqlite 는 서버 커넥션 풀 개념이 없어 pool_size 등 옵션 미적용.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def get_engine() -> AsyncEngine:
    """프로세스 단일 AsyncEngine. 최초 호출 시 DATABASE_URL 로 생성."""
    global _engine, _sessionmaker
    if _engine is None:
        url = settings.DATABASE_URL
        kwargs: dict = {"echo": False, "pool_pre_ping": True}
        if not _is_sqlite(url):
            kwargs.update(
                pool_size=settings.DB_POOL_SIZE,
                max_overflow=settings.DB_MAX_OVERFLOW,
                pool_recycle=settings.DB_POOL_RECYCLE_SEC,
                pool_timeout=settings.DB_POOL_TIMEOUT_SEC,
            )
        _engine = create_async_engine(url, **kwargs)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 의존성 — 요청 스코프 세션. 예외 시 rollback, 항상 close.

    commit 은 호출자(라우터/서비스) 책임 — 명시적 트랜잭션 경계를 위해.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """비-요청(잡/스크립트)용 세션 — 정상 종료 시 commit, 예외 시 rollback."""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_db() -> bool:
    """헬스용 — SELECT 1 성공 여부."""
    eng = get_engine()
    async with eng.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        return result.scalar() == 1


async def dispose_engine() -> None:
    """엔진/풀 정리 — lifespan 종료 시."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
