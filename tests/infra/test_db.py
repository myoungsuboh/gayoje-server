"""BE-E01-T03 회귀 — DB(SQLAlchemy async) 엔진/세션/헬스.

sqlite 인메모리로 인프라 동작만 검증(PG 불필요). PG 전용 풀 옵션은 분기 확인.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.infra import db

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _sqlite_memory(monkeypatch):
    """각 테스트를 격리된 sqlite 인메모리로 — 엔진 싱글톤 리셋."""
    await db.dispose_engine()
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    yield
    await db.dispose_engine()


async def test_check_db_ok():
    assert await db.check_db() is True


async def test_get_engine_is_singleton():
    assert db.get_engine() is db.get_engine()


async def test_get_session_yields_async_session():
    agen = db.get_session()
    session = await agen.__anext__()
    try:
        assert isinstance(session, AsyncSession)
        result = await session.execute(text("SELECT 1"))
        assert result.scalar() == 1
    finally:
        await agen.aclose()


async def test_session_scope_commits():
    async with db.session_scope() as session:
        result = await session.execute(text("SELECT 42"))
        assert result.scalar() == 42


async def test_sqlite_branch_no_pool_args():
    # sqlite URL 이면 pool_size 등 미적용 — 엔진 생성이 예외 없이 성공.
    eng = db.get_engine()
    assert eng.url.get_backend_name() == "sqlite"
