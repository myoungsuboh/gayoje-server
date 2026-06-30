"""festivals 도메인 ORM — 정규화된 가요제/행사 (INGEST PoC 최소 스키마, BE-E01-T04).

출처(provenance) 필드는 NOT NULL 로 강제 — 수집 데이터의 1차 출처 추적 불변식
(INGEST-E1-T1). 원본(raw_payload)도 보존해 재처리 가능. (source_system, source_record_id)
UNIQUE 로 멱등 upsert.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import JSON, Date, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.base import Base, TimestampMixin


class FestivalEvent(Base, TimestampMixin):
    __tablename__ = "festival_event"
    __table_args__ = (
        UniqueConstraint(
            "source_system", "source_record_id", name="uq_festival_event_source"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ===== provenance (출처 보존 — NOT NULL 강제) =====
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # 변경판별
    # 원본 보존(재처리). 운영 PG 에선 JSONB 로 최적화 가능(현재는 portable JSON).
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    # ===== normalized =====
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    host_org: Mapped[str | None] = mapped_column(String(512), nullable=True)
    region_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(512), nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
