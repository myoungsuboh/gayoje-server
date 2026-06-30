"""
autofix needs_input 영속화 (2026-06) — 새로고침/다른 기기에서 '인터뷰로 채우기' 복원.

[배경]
'/prd/autofix' 의 needs_input(AI 가 근거 부족으로 못 채운 항목)이 FE in-memory
store 에만 있어 새로고침 시 증발 → 사용자가 보완하기를 다시 눌러 같은 진단에
토큰을 재지출. master PRD 노드에 JSON 으로 저장하고 getPRD 가 동봉한다.

[수명 검증]
회의록 merge / delete rebuild 시 자동 소멸 (cypher 가 null 처리) — "새 정보가
PRD 에 반영되기 전까지 유지"가 의미론. 수동 편집(PATCH)은 유지.
"""
from __future__ import annotations

import json

import pytest

from app.service import query_repository as q

pytestmark = pytest.mark.asyncio


# ─── 저장/해제 repo 함수 ─────────────────────────────────────────


async def test_set_needs_serializes_json_and_scopes_team(monkeypatch):
    seen = {}

    async def fake_cypher(cypher, params=None, **kw):
        seen["cypher"] = cypher
        seen["params"] = params
        return [{"master_id": "doc_prd_master_p"}]
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake_cypher
    )

    ok = await q.set_prd_autofix_needs_input(
        "projX",
        [{"topic": "모델 제휴", "question": "어떤 파라미터가 필요한가요?"}],
        team_id="team-7",
    )
    assert ok is True
    assert "autofix_needs_input = $needs_json" in seen["cypher"]
    assert "team-7" in seen["params"]["project"]  # scoped key
    items = json.loads(seen["params"]["needs_json"])
    assert items == [{"topic": "모델 제휴", "question": "어떤 파라미터가 필요한가요?"}]


async def test_set_needs_empty_list_clears(monkeypatch):
    """빈 진단 → 이전 잔존 값 해제 (저장 아님)."""
    seen = {}

    async def fake_cypher(cypher, params=None, **kw):
        seen["cypher"] = cypher
        return [{"master_id": "m"}]
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake_cypher
    )

    await q.set_prd_autofix_needs_input("projX", [])
    assert "autofix_needs_input = null" in seen["cypher"]


async def test_clear_needs_returns_false_when_no_master(monkeypatch):
    async def fake_cypher(cypher, params=None, **kw):
        return []
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake_cypher
    )
    assert await q.clear_prd_autofix_needs_input("projX") is False


# ─── getPRD 동봉 + 파싱 ──────────────────────────────────────────


def _prd_row(**over):
    row = {
        "master_prd_id": "doc_prd_master_p",
        "prd_content": "# PRD",
        "last_updated": 1765000000000,
        "markdown_stale": False,
        "related_master_cps_id": None,
        "absorbed_prd_ids": [],
        "autofix_needs_input": None,
    }
    row.update(over)
    return row


async def test_get_master_prd_includes_parsed_needs(monkeypatch):
    raw = json.dumps([{"topic": "t1", "question": "q1"}], ensure_ascii=False)

    async def fake_cypher(cypher, params=None, **kw):
        assert "autofix_needs_input" in cypher  # RETURN 에 포함
        return [_prd_row(autofix_needs_input=raw)]
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake_cypher
    )

    out = await q.get_master_prd("projX")
    assert out.autofix_needs_input == [{"topic": "t1", "question": "q1"}]


async def test_get_master_prd_corrupt_needs_yields_empty(monkeypatch):
    """손상된 JSON 이 getPRD 자체를 막으면 안 됨 — [] 로 강등."""
    async def fake_cypher(cypher, params=None, **kw):
        return [_prd_row(autofix_needs_input="{not json")]
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake_cypher
    )
    out = await q.get_master_prd("projX")
    assert out.autofix_needs_input == []


def test_parse_helper_filters_malformed_items():
    raw = json.dumps([
        {"topic": "ok", "question": "q"},
        {"topic": "", "question": ""},   # 둘 다 빈 항목 — 제외
        "not-a-dict",                    # 형식 불일치 — 제외
    ])
    assert q._parse_autofix_needs(raw) == [{"topic": "ok", "question": "q"}]
    assert q._parse_autofix_needs(None) == []
    assert q._parse_autofix_needs(123) == []


# ─── 수명: merge / delete rebuild 가 소멸시키는지 ────────────────


def test_prd_merge_cypher_clears_needs():
    """회의록 merge = 새 정보 반영 → 진단 무효. 같은 노드 SET 이라 명시적 null 필요."""
    from app.pipelines.prd_pipeline import build_merge_master_prd_query

    cypher, _params = build_merge_master_prd_query(
        project_name="projX", merged_content="# PRD", latest_delta_id=None,
        cleanup_at_version_count=None,
    )
    assert "master.autofix_needs_input = null" in cypher


def test_delete_rebuild_cypher_clears_needs():
    from app.pipelines import delete_pipeline

    assert "master.autofix_needs_input = null" in delete_pipeline._SAVE_REBUILT_PRD_CYPHER
