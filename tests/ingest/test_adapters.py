"""INGEST-E1-T2 / E2 회귀 — 어댑터 레지스트리 + 각 어댑터 normalize(샘플 기반)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.v1.ingestion import registry
from app.api.v1.ingestion.adapters.base import extract_records, is_gayoje

_FIX = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    return extract_records(json.loads((_FIX / name).read_text(encoding="utf-8")))


def test_registry_has_adapters():
    keys = set(registry.list_adapter_keys())
    assert {"standard_performance", "cultural_festival", "tour_api"} <= keys
    assert registry.get_adapter("tour_api").SOURCE_KEY == "tour_api"
    with pytest.raises(KeyError):
        registry.get_adapter("does-not-exist")


def test_is_gayoje_filter():
    assert is_gayoje("제30회 강변가요제")
    assert is_gayoje("전국 트로트 노래대회")
    assert not is_gayoje("시립교향악단 정기연주회")
    assert not is_gayoje(None)


@pytest.mark.parametrize(
    "key,fixture,expect_min",
    [
        ("standard_performance", "standard_performance_sample.json", 2),
        ("cultural_festival", "cultural_festival_sample.json", 1),
        ("tour_api", "tour_api_sample.json", 1),
    ],
)
def test_adapter_normalize_selects_gayoje(key, fixture, expect_min):
    adapter = registry.get_adapter(key)
    records = _load(fixture)
    selected = [n for n in (adapter.normalize(r) for r in records) if n is not None]

    assert len(selected) >= expect_min
    assert len(selected) <= len(records)  # 비가요제는 필터됨
    for ev in selected:
        assert ev.source_system == adapter.SOURCE_SYSTEM
        assert ev.title and is_gayoje(ev.title)
        assert ev.source_record_id and len(ev.payload_hash) == 64
        assert ev.raw_payload  # 원본 보존


def test_tour_api_yyyymmdd_date_parsed():
    adapter = registry.get_adapter("tour_api")
    evs = [adapter.normalize(r) for r in _load("tour_api_sample.json")]
    evs = [e for e in evs if e]
    assert evs and all(e.start_date and e.start_date.year >= 2026 for e in evs)
    # 지역(addr1) 매핑
    assert all(e.region_name for e in evs)
