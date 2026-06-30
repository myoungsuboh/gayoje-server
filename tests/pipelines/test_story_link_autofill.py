"""story_link_autofill — PRD 연결(스토리 추적) AI 매칭 보완 파이프라인 테스트.

핵심 가드:
- 대상 선별: 미연결 API/Entity/Policy 만 (이미 연결된 노드 무손상)
- 환각 차단: LLM 이 낸 story_id 는 실제 Story id whitelist 에 있을 때만 적용
- 강등: LLM 실패/스토리 없음 → 연결 0건 meta (예외 미전파 — error/auth 결과 보호)
- 저장 라벨 whitelist: API/Entity/Policy 외 라벨은 쿼리 자체를 실행하지 않음
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.story_link_autofill import (
    LinkItem,
    collect_link_targets,
    parse_links,
    run_story_link_autofill,
)
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio


def _ctx(gemini) -> PipelineContext:
    return PipelineContext(
        gemini=gemini, neo4j=FakeNeo4j(responses=[]), idempotency_key="link-test"
    )


_SPACK = {
    "apis": [
        {"id": "API-01", "name": "작업 생성", "method": "post", "endpoint": "/tasks",
         "description": "에이전트 작업 생성"},                       # 미연결 → 대상
        {"id": "API-02", "name": "작업 조회", "related_story_id": "story_01_1"},  # 연결됨 → 제외
        {"name": "id 없음"},                                          # id 없음 → 제외
    ],
    "entities": [
        {"id": "ENT-01", "name": "AgentTask", "lineage": {"confidence": "direct", "related_stories": []}},  # 대상
        {"id": "ENT-02", "name": "AiAccount",
         "lineage": {"confidence": "direct", "related_stories": [{"story_id": "story_01_1"}]}},  # 연결됨 → 제외
    ],
    "policies": [
        {"id": "POL-01", "category": "PERFORMANCE", "description": "rate limit 50/min"},  # 대상
        {"id": "POL-02", "category": "SECURITY", "related_story_id": "story_02_1"},        # 연결됨 → 제외
    ],
}


def test_collect_link_targets_picks_only_unlinked():
    items = collect_link_targets(_SPACK)
    ids = {(it.kind, it.id) for it in items}
    assert ids == {("api", "API-01"), ("entity", "ENT-01"), ("policy", "POL-01")}
    # API 설명에 method/endpoint 가 합쳐져 매칭 근거로 쓰임
    api = next(it for it in items if it.kind == "api")
    assert "POST" in api.description and "/tasks" in api.description


def test_parse_links_whitelist_and_dedup():
    items = [
        LinkItem(id="API-01", kind="api"),
        LinkItem(id="ENT-01", kind="entity"),
        LinkItem(id="POL-01", kind="policy"),
    ]
    story_ids = {"story_01_1", "story_02_1"}
    parsed = {"links": [
        {"item_id": "API-01", "story_id": "story_01_1"},      # 정상
        {"item_id": "API-01", "story_id": "story_02_1"},      # 중복 item → 첫 번째만
        {"item_id": "ENT-01", "story_id": "story_99_9"},      # 환각 story id → 버림
        {"item_id": "POL-01", "story_id": ""},                # 근거 없음(빈 값) → 버림
        {"item_id": "GHOST", "story_id": "story_01_1"},       # 미지 item → 버림
        "garbage",                                              # 형식 오류 → 버림
    ]}
    fills = parse_links(parsed, items, story_ids)
    assert len(fills) == 1
    assert fills[0].item_id == "API-01" and fills[0].story_id == "story_01_1"
    assert fills[0].kind == "api"


def test_parse_links_null_string_dropped():
    items = [LinkItem(id="API-01", kind="api")]
    parsed = {"links": [{"item_id": "API-01", "story_id": "null"}]}
    assert parse_links(parsed, items, {"story_01_1"}) == []


async def test_run_happy_path_saves_links(monkeypatch):
    """LLM 매칭 → whitelist 통과분만 저장, meta 집계."""
    from app.service import query_repository as qr

    monkeypatch.setattr(qr, "list_prd_nodes", AsyncMock(return_value=[
        {"id": "story_01_1", "label": "Story", "summary": "직원이 AI 계정을 발급받는다"},
        {"id": "epic_01", "label": "Epic", "summary": "계정 관리"},   # Story 만 사용
    ]))
    save_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(qr, "update_node_story_link", save_spy)

    gemini = FakeGemini(responses=[
        '{"links": [{"item_id": "API-01", "story_id": "story_01_1"},'
        ' {"item_id": "ENT-01", "story_id": "story_01_1"},'
        ' {"item_id": "POL-01", "story_id": ""}]}',
    ])
    meta = await run_story_link_autofill(_ctx(gemini), "proj", _SPACK)

    assert meta == {"linkTargets": 3, "linkedCount": 2, "linkSavedCount": 2}
    # 저장 호출: (project, label, node_id, story_id)
    calls = {(c.args[1], c.args[2], c.args[3]) for c in save_spy.call_args_list}
    assert calls == {("API", "API-01", "story_01_1"), ("Entity", "ENT-01", "story_01_1")}
    # 프롬프트에 스토리와 항목이 렌더됐는지
    prompt = gemini.calls[0]["prompt"]
    assert "story_01_1" in prompt and "API-01" in prompt and "AgentTask" in prompt
    assert "epic_01" not in prompt   # Epic 은 매칭 대상 아님


async def test_run_no_stories_skips_llm(monkeypatch):
    from app.service import query_repository as qr

    monkeypatch.setattr(qr, "list_prd_nodes", AsyncMock(return_value=[]))
    gemini = FakeGemini(responses=[])
    meta = await run_story_link_autofill(_ctx(gemini), "proj", _SPACK)
    assert meta["linkedCount"] == 0 and meta["linkTargets"] == 3
    assert gemini.calls == []        # LLM 호출 자체가 없음


async def test_run_no_targets_skips_everything(monkeypatch):
    from app.service import query_repository as qr

    spy = AsyncMock(return_value=[])
    monkeypatch.setattr(qr, "list_prd_nodes", spy)
    meta = await run_story_link_autofill(
        _ctx(FakeGemini(responses=[])), "proj",
        {"apis": [{"id": "A", "related_story_id": "story_01_1"}], "entities": [], "policies": []},
    )
    assert meta == {"linkTargets": 0, "linkedCount": 0, "linkSavedCount": 0}
    spy.assert_not_called()


async def test_run_llm_failure_degrades(monkeypatch):
    """LLM 예외 + 폴백 없음 → 연결 0건 meta (예외 미전파)."""
    from app.service import query_repository as qr

    monkeypatch.setattr(qr, "list_prd_nodes", AsyncMock(return_value=[
        {"id": "story_01_1", "label": "Story", "summary": "s"},
    ]))

    class _BoomGemini:
        async def generate(self, *a, **k):
            raise RuntimeError("down")

    meta = await run_story_link_autofill(_ctx(_BoomGemini()), "proj", _SPACK)
    assert meta["linkedCount"] == 0 and meta["linkSavedCount"] == 0


async def test_run_save_failure_isolated(monkeypatch):
    """한 노드 저장 실패는 그 노드만 — 나머지 saved 집계 유지."""
    from app.service import query_repository as qr

    monkeypatch.setattr(qr, "list_prd_nodes", AsyncMock(return_value=[
        {"id": "story_01_1", "label": "Story", "summary": "s"},
    ]))
    save = AsyncMock(side_effect=[True, RuntimeError("save down")])
    monkeypatch.setattr(qr, "update_node_story_link", save)

    gemini = FakeGemini(responses=[
        '{"links": [{"item_id": "API-01", "story_id": "story_01_1"},'
        ' {"item_id": "ENT-01", "story_id": "story_01_1"}]}',
    ])
    meta = await run_story_link_autofill(_ctx(gemini), "proj", _SPACK)
    assert meta["linkedCount"] == 2
    assert meta["linkSavedCount"] == 1


async def test_update_node_story_link_label_whitelist():
    """미지 라벨/빈 인자는 쿼리 실행 없이 False (Cypher 라벨 주입 차단)."""
    from app.service.query_repository import update_node_story_link

    assert await update_node_story_link("p", "Story", "x", "story_01_1") is False
    assert await update_node_story_link("p", "API; DETACH DELETE n", "x", "s") is False
    assert await update_node_story_link("p", "API", "", "story_01_1") is False
    assert await update_node_story_link("p", "API", "x", "") is False
