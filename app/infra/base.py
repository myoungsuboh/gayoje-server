"""SQLAlchemy 선언적 베이스 + 공통 mixin (BE-E01-T04).

모든 ORM 모델은 Base 를 상속한다. create_all/Alembic 이 Base.metadata 로 스키마를 만든다.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """생성/수정 시각 (timezone-aware, UTC 저장)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
