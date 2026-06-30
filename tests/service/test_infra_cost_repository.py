"""
infra_cost_repository 단위 테스트.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from app.service import infra_cost_repository
from app.service.infra_cost_repository import (
    InfraCost,
    current_year_month,
    default_infra_cost_for_month,
    get_infra_cost,
    list_infra_cost_by_year,
    upsert_infra_cost,
)


pytestmark = pytest.mark.asyncio


class _FakeRunCypher:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher, params=None, database=None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses=None):
        fake = _FakeRunCypher(responses)
        monkeypatch.setattr(
            "app.service.infra_cost_repository.neo4j_client.run_cypher", fake
        )
        return fake
    return _setup


# ─── get_infra_cost ──────────────────────


async def test_get_infra_cost_returns_normalized(fake_run):
    fake_run([[
        {"year": 2026, "month": 5, "amount_krw": 76000,
         "note": "Neo4j 50K + Vercel 26K",
         "updated_at": "2026-05-17T00:00:00Z",
         "updated_by": "admin@example.com"}
    ]])
    out = await get_infra_cost(2026, 5)
    assert isinstance(out, InfraCost)
    assert out.year == 2026
    assert out.month == 5
    assert out.amount_krw == 76000
    assert out.note == "Neo4j 50K + Vercel 26K"


async def test_get_infra_cost_missing_returns_none(fake_run):
    fake_run([[]])
    assert await get_infra_cost(2026, 5) is None


async def test_get_infra_cost_invalid_month(fake_run):
    """month 가 1-12 밖이면 cypher 호출도 안 함."""
    fake = fake_run()
    assert await get_infra_cost(2026, 0) is None
    assert await get_infra_cost(2026, 13) is None
    assert len(fake.calls) == 0


# ─── upsert_infra_cost ───────────────────


async def test_upsert_infra_cost_clamps_negative_amount(fake_run):
    fake = fake_run([[
        {"year": 2026, "month": 5, "amount_krw": 0, "note": "",
         "updated_at": "2026-05-17T00:00:00Z", "updated_by": "admin@b.com"}
    ]])
    r = await upsert_infra_cost(
        year=2026, month=5, amount_krw=-1000, note="", updated_by="admin@b.com"
    )
    assert r is not None
    # cypher 에 0 으로 clamp 되어 전달
    assert fake.calls[0]["params"]["amount_krw"] == 0


async def test_upsert_infra_cost_truncates_note(fake_run):
    """note 500자 이상 — 500자로 자름."""
    fake = fake_run([[
        {"year": 2026, "month": 5, "amount_krw": 1000, "note": "a" * 500,
         "updated_at": "2026-05-17T00:00:00Z", "updated_by": "admin@b.com"}
    ]])
    long_note = "a" * 1000
    await upsert_infra_cost(
        year=2026, month=5, amount_krw=1000, note=long_note, updated_by="admin@b.com"
    )
    assert len(fake.calls[0]["params"]["note"]) <= 500


async def test_upsert_infra_cost_invalid_year(fake_run):
    fake = fake_run()
    assert await upsert_infra_cost(year=1900, month=5, amount_krw=0, note="", updated_by="a") is None
    assert await upsert_infra_cost(year=2200, month=5, amount_krw=0, note="", updated_by="a") is None
    assert len(fake.calls) == 0


async def test_upsert_infra_cost_invalid_month(fake_run):
    fake = fake_run()
    assert await upsert_infra_cost(year=2026, month=0, amount_krw=0, note="", updated_by="a") is None
    assert await upsert_infra_cost(year=2026, month=13, amount_krw=0, note="", updated_by="a") is None
    assert len(fake.calls) == 0


# ─── items + fixed (고정비) ──────────────


async def test_upsert_infra_cost_with_items_forces_sum_and_persists_fixed(fake_run):
    """items 가 있으면 amount_krw 는 항목 합계로 강제 + fixed 플래그 직렬화 보존."""
    fake = fake_run([[
        {"year": 2026, "month": 6, "amount_krw": 80000, "note": "",
         "items_json": "[]", "updated_at": None, "updated_by": "admin@b.com"}
    ]])
    r = await upsert_infra_cost(
        year=2026, month=6, amount_krw=0, note="", updated_by="admin@b.com",
        items=[
            {"category": "서버 운영비", "amount_krw": 50000, "note": "", "fixed": True},
            {"category": "기타", "amount_krw": 30000, "note": "", "fixed": False},
        ],
    )
    assert r is not None
    params = fake.calls[0]["params"]
    # amount_krw 는 항목 합계(80000)로 강제
    assert params["amount_krw"] == 80000
    stored = json.loads(params["items_json"])
    assert stored[0]["fixed"] is True
    assert stored[1]["fixed"] is False
    assert stored[0]["category"] == "서버 운영비"


async def test_upsert_infra_cost_drops_empty_items(fake_run):
    """빈 항목(카테고리·금액·메모 모두 없음)은 직렬화에서 제외."""
    fake = fake_run([[
        {"year": 2026, "month": 6, "amount_krw": 50000, "note": "",
         "items_json": "[]", "updated_at": None, "updated_by": "a"}
    ]])
    await upsert_infra_cost(
        year=2026, month=6, amount_krw=0, note="", updated_by="a",
        items=[
            {"category": "서버 운영비", "amount_krw": 50000, "note": "", "fixed": True},
            {"category": "", "amount_krw": 0, "note": "", "fixed": False},  # 빈 항목 → 드롭
        ],
    )
    stored = json.loads(fake.calls[0]["params"]["items_json"])
    assert len(stored) == 1
    assert stored[0]["fixed"] is True


async def test_get_infra_cost_parses_items_with_fixed(fake_run):
    """items_json 파싱 — fixed 플래그 복원."""
    fake_run([[
        {"year": 2026, "month": 6, "amount_krw": 50000, "note": "",
         "items_json": '[{"category": "서버 운영비", "amount_krw": 50000, "note": "", "fixed": true}]',
         "updated_at": None, "updated_by": "a"}
    ]])
    out = await get_infra_cost(2026, 6)
    assert out is not None
    assert len(out.items) == 1
    assert out.items[0]["fixed"] is True
    assert out.items[0]["category"] == "서버 운영비"


# ─── list_infra_cost_by_year ─────────────


async def test_list_infra_cost_by_year_returns_ordered(fake_run):
    fake_run([[
        {"year": 2026, "month": 3, "amount_krw": 75000, "note": "", "updated_at": None, "updated_by": "a"},
        {"year": 2026, "month": 5, "amount_krw": 80000, "note": "", "updated_at": None, "updated_by": "a"},
    ]])
    rows = await list_infra_cost_by_year(2026)
    assert len(rows) == 2
    assert rows[0].month == 3
    assert rows[1].month == 5


# ─── 헬퍼 ────────────────────────────────


def test_default_infra_cost_is_positive():
    """default 값이 양수 — 운영 추정치 변경 시 회귀 가드."""
    assert default_infra_cost_for_month() > 0


def test_current_year_month_returns_valid_range():
    year, month = current_year_month()
    assert 2020 <= year <= 2100
    assert 1 <= month <= 12
