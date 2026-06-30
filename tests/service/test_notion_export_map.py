"""Notion export 페이지 매핑 저장/조회 (user_repository) 테스트."""
import pytest

from app.service import user_repository as ur

pytestmark = pytest.mark.asyncio


def _patch_store(monkeypatch):
    """in-memory JSON 저장 흉내 — read/write cypher 분기."""
    state = {"json": None}

    async def fake_run(cy, params):
        if "SET u.notion_export_map" in cy:
            state["json"] = params["map_json"]
            return [{"email": params["email"]}]
        return [{"map_json": state["json"]}]

    monkeypatch.setattr(ur.neo4j_client, "run_cypher", fake_run)
    return state


async def test_save_then_get_roundtrip(monkeypatch):
    _patch_store(monkeypatch)
    await ur.save_notion_export_map("u@e.com", "proj", hub_page_id="H", cps_page_id="C")
    got = await ur.get_notion_export_map("u@e.com", "proj")
    assert got["hub_page_id"] == "H"
    assert got["cps_page_id"] == "C"
    assert "synced_at" in got


async def test_save_coalesces_existing(monkeypatch):
    _patch_store(monkeypatch)
    await ur.save_notion_export_map("u@e.com", "proj", hub_page_id="H", cps_page_id="C")
    await ur.save_notion_export_map("u@e.com", "proj", prd_page_id="P")
    got = await ur.get_notion_export_map("u@e.com", "proj")
    assert got["hub_page_id"] == "H"
    assert got["cps_page_id"] == "C"  # 유지
    assert got["prd_page_id"] == "P"  # 추가


async def test_get_missing_returns_none(monkeypatch):
    _patch_store(monkeypatch)
    assert await ur.get_notion_export_map("u@e.com", "nope") is None


async def test_separate_projects_isolated(monkeypatch):
    _patch_store(monkeypatch)
    await ur.save_notion_export_map("u@e.com", "p1", hub_page_id="H1")
    await ur.save_notion_export_map("u@e.com", "p2", hub_page_id="H2")
    assert (await ur.get_notion_export_map("u@e.com", "p1"))["hub_page_id"] == "H1"
    assert (await ur.get_notion_export_map("u@e.com", "p2"))["hub_page_id"] == "H2"
