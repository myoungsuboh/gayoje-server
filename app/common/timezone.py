"""시간대 유틸 — 저장은 UTC, 표기는 KST 일관 (BE-E01-T01).

규약:
- DB/내부 저장: timezone-aware UTC (now_utc()).
- API 표기/로그: KST (to_kst / now_kst_iso).
- naive datetime 은 UTC 로 간주(방어). 외부 입력은 가능한 빨리 aware UTC 로 정규화.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), name="KST")


def now_utc() -> datetime:
    """현재 시각 (timezone-aware UTC)."""
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime) -> datetime:
    """naive 면 UTC 로 간주해 aware 로 만든다(방어)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def to_utc(dt: datetime) -> datetime:
    """aware/naive 입력을 UTC aware 로 정규화."""
    return _ensure_aware(dt).astimezone(timezone.utc)


def to_kst(dt: datetime) -> datetime:
    """입력을 KST aware 로 변환."""
    return _ensure_aware(dt).astimezone(KST)


def now_kst_iso() -> str:
    """현재 KST 시각 ISO8601 문자열 (+09:00)."""
    return to_kst(now_utc()).isoformat()
