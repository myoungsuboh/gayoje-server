"""
quota_config_repository — lite_daily_cap 필드 + 기존 노드 fallback (2026-06).

[핵심 안전장치]
기존 운영 QuotaConfig 노드엔 lite_daily_cap 속성이 없다(NULL). 그대로 0 으로
override 에 박으면 라이브 Lite 오버플로우가 전 등급 하드월로 깨진다. _row_to_config
가 NULL 일 때 _DEFAULT_QUOTA 코드 기본값으로 fallback 하는지 회귀 가드.
"""
from __future__ import annotations

import pytest

from app.service import quota_config_repository as qcr
from app.service.quota_config_repository import (
    _DEFAULT_QUOTA,
    _row_to_config,
    update_quota_config,
)
from app.core.subscription import SUBSCRIPTION_PRO, SUBSCRIPTION_FREE

pytestmark = pytest.mark.asyncio


def test_row_fallback_uses_code_default_when_lite_cap_missing():
    """기존 노드(lite_daily_cap 속성 없음) → 코드 기본값 fallback (0 박힘 방지)."""
    row = {
        "tier": SUBSCRIPTION_PRO,
        "meeting_logs": 3_000_000 and 50,
        "summary_chars": 50_000,
        "total_tokens": 3_000_000,
        "library_skills": 1_000,
        "max_projects": 3,
        # lite_daily_cap 없음 (기존 운영 노드)
    }
    cfg = _row_to_config(row)
    assert cfg.lite_daily_cap == _DEFAULT_QUOTA[SUBSCRIPTION_PRO]["lite_daily_cap"]
    assert cfg.lite_daily_cap == 1_500_000   # [2026-06-11] Pro 기본 주간캡


def test_row_uses_db_value_when_present():
    """admin 이 설정한 값이 있으면 그대로 사용."""
    row = {
        "tier": SUBSCRIPTION_PRO, "meeting_logs": 50, "summary_chars": 50_000,
        "total_tokens": 3_000_000, "library_skills": 1_000, "max_projects": 3,
        "lite_daily_cap": 777_000,
    }
    assert _row_to_config(row).lite_daily_cap == 777_000


def test_free_default_lite_cap_is_zero():
    """Free 는 오버플로우 없음(하드월) — 기본 캡 0."""
    row = {"tier": SUBSCRIPTION_FREE, "meeting_logs": 5, "summary_chars": 10_000,
           "total_tokens": 1_000_000, "library_skills": 100, "max_projects": 1}
    assert _row_to_config(row).lite_daily_cap == 0


def test_to_dict_includes_lite_daily_cap():
    row = {"tier": SUBSCRIPTION_PRO, "meeting_logs": 50, "summary_chars": 50_000,
           "total_tokens": 3_000_000, "library_skills": 1_000, "max_projects": 3,
           "lite_daily_cap": 500_000}
    assert _row_to_config(row).to_dict()["lite_daily_cap"] == 500_000


async def test_update_passes_lite_daily_cap(monkeypatch):
    """update_quota_config 가 cypher 에 lite_daily_cap 을 음수 방어 후 전달."""
    captured = {}

    async def fake_run(cypher, params=None, database=None):
        captured["params"] = params or {}
        return [{
            "tier": SUBSCRIPTION_PRO, "meeting_logs": 50, "summary_chars": 50_000,
            "total_tokens": 3_000_000, "library_skills": 1_000, "max_projects": 3,
            "lite_daily_cap": 600_000, "updated_at": "t", "updated_by": "a@b.com",
        }]

    monkeypatch.setattr(qcr.neo4j_client, "run_cypher", fake_run)
    out = await update_quota_config(
        tier=SUBSCRIPTION_PRO, meeting_logs=50, summary_chars=50_000,
        total_tokens=3_000_000, library_skills=1_000, max_projects=3,
        lite_daily_cap=-5,  # 음수 → 0 으로 방어
        updated_by="a@b.com",
    )
    assert captured["params"]["lite_daily_cap"] == 0
    assert out.lite_daily_cap == 600_000
