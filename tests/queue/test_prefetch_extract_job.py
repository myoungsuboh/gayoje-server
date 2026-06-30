"""
prefetch_extract_job — batch 파이프라이닝: 다음 버전 extract 를 미리 계산해 캐시.

핵심:
  - extract(cps_agent + prd_extract + prd_graph) 를 계산해 Redis 캐시에 저장.
  - 이미 캐시돼 있으면 재계산 skip (status=already_cached, LLM 0회).
  - single-flight 락이 잡혀 있으면 skip (status=locked) — 본 job 과 토큰 이중과금 방지.
  - best-effort: 어떤 결과든 Neo4j 그래프엔 쓰지 않음 (extract 순수 LLM).
"""
from __future__ import annotations

import json

import pytest

from app.queue.extract_cache import (
    extract_cache_key,
    get_cached_extract,
    set_cached_extract,
    try_acquire_extract_lock,
)
from app.queue.jobs import prefetch_extract_job
from tests.conftest import FakeGemini, FakeNeo4j, FakeRedis, make_arq_ctx

pytestmark = pytest.mark.asyncio


_CPS_RESPONSE = json.dumps({
    "nodes": [
        {"id": "doc_cps_food_v2", "label": "CPS_Document",
         "properties": {"full_markdown": "# CPS\n## Problem\n- 느림"}},
        {"id": "prb_01", "label": "Problem", "properties": {"summary": "느림"}},
        {"id": "res_01", "label": "Solution", "properties": {"summary": "캐시"}},
    ],
    "relationships": [
        {"source": "res_01", "type": "SOLVES", "target": "prb_01"},
    ],
})
_PRD_EXTRACT_TEXT = "# PRD v2\n## Epic\n- 인증"
_PRD_GRAPH_JSON = json.dumps({
    "nodes": [
        {"id": "doc_prd_food_v2", "label": "PRD_Document", "properties": {}},
        {"id": "epic_01", "label": "Epic", "properties": {"summary": "인증"}},
    ],
    "relationships": [],
})
_MEETING = "다음 버전 회의 내용 — 50자 이상으로 작성하여 충분히 길게 만든 미팅 로그 본문."


async def test_prefetch_computes_and_caches_extract():
    gemini = FakeGemini(responses=[_CPS_RESPONSE, _PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON])
    neo4j = FakeNeo4j()
    redis = FakeRedis()
    ctx = make_arq_ctx(job_id="prefetch-1", gemini=gemini, neo4j=neo4j, redis=redis)

    result = await prefetch_extract_job(
        ctx, project_name="food", version="v2", meeting_content=_MEETING,
        previous_cps_id="doc_cps_food_v2", user_email=None,
    )

    assert result["status"] == "cached"
    # extract 는 순수 LLM — Neo4j 그래프 쓰기 0건 (데이터 안전).
    assert neo4j.executed == []
    # 캐시에 결과 저장됨
    key = extract_cache_key("food", "v2", _MEETING)
    cached = await get_cached_extract(redis, key)
    assert cached is not None
    assert "nodes" in cached["cps_graph"]
    assert cached["prd_markdown"] == _PRD_EXTRACT_TEXT
    assert "nodes" in cached["prd_graph"]


async def test_prefetch_skips_when_already_cached():
    redis = FakeRedis()
    key = extract_cache_key("food", "v2", _MEETING)
    await set_cached_extract(redis, key, {"cps_graph": {}, "prd_markdown": "x", "prd_graph": {}})
    gemini = FakeGemini(responses=[_CPS_RESPONSE, _PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON])
    ctx = make_arq_ctx(job_id="prefetch-2", gemini=gemini, neo4j=FakeNeo4j(), redis=redis)

    result = await prefetch_extract_job(
        ctx, project_name="food", version="v2", meeting_content=_MEETING, user_email=None,
    )

    assert result["status"] == "already_cached"
    assert len(gemini.calls) == 0   # 재계산 안 함 — LLM 0회


async def test_prefetch_skips_when_lock_held():
    redis = FakeRedis()
    key = extract_cache_key("food", "v2", _MEETING)
    await try_acquire_extract_lock(redis, key)   # 다른 워커가 이미 점유
    gemini = FakeGemini(responses=[_CPS_RESPONSE, _PRD_EXTRACT_TEXT, _PRD_GRAPH_JSON])
    ctx = make_arq_ctx(job_id="prefetch-3", gemini=gemini, neo4j=FakeNeo4j(), redis=redis)

    result = await prefetch_extract_job(
        ctx, project_name="food", version="v2", meeting_content=_MEETING, user_email=None,
    )

    assert result["status"] == "locked"
    assert len(gemini.calls) == 0   # 락 점유 중 — 계산 안 함
