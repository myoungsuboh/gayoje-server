"""
post_meeting_pipeline_job 의 batch 파이프라이닝 배선:
  - 캐시 HIT → extract LLM(cps_agent/prd_extract/prd_graph) 3회 skip, merge 만 실행.
  - 캐시 MISS → extract 계산 후 캐시에 저장(다음 재사용) + 결과 정상.
  - next_meeting 주어지면 다음 버전 extract 선반입(prefetch) enqueue.

응답 shape(cps/prd 키)는 기존과 동일 — FE 계약 보존.
"""
from __future__ import annotations

import json

import pytest

from app.queue import jobs as jobs_mod
from app.queue.extract_cache import extract_cache_key, get_cached_extract, set_cached_extract
from app.queue.jobs import post_meeting_pipeline_job
from tests.conftest import FakeGemini, FakeNeo4j, FakeRedis, make_arq_ctx

pytestmark = pytest.mark.asyncio


_CPS_GRAPH = {
    "nodes": [
        {"id": "doc_cps_food_v1", "label": "CPS_Document",
         "properties": {"full_markdown": "## CPS\n### 1. Problem\n- 느림"}},
        {"id": "prb_01", "label": "Problem", "properties": {"summary": "느림"}},
        {"id": "res_01", "label": "Solution", "properties": {"summary": "캐시"}},
    ],
    "relationships": [{"source": "res_01", "type": "SOLVES", "target": "prb_01"}],
    "_extraction_mode": "strict",
}
_PRD_GRAPH = {
    "nodes": [
        {"id": "doc_prd_food_v1", "label": "PRD_Document", "properties": {}},
        {"id": "epic_01", "label": "Epic", "properties": {"summary": "인증"}},
        {"id": "story_01", "label": "Story", "properties": {"summary": "로그인"}},
    ],
    "relationships": [{"source": "epic_01", "type": "CONTAINS", "target": "story_01"}],
}
_EXTRACT = {
    "cps_graph": _CPS_GRAPH,
    "prd_markdown": "# PRD v1\n## Epic\n- 인증",
    "prd_graph": _PRD_GRAPH,
}

_CPS_IMPACT = json.dumps({"affected_sections": [], "removed_prb_ids": [], "removed_res_ids": [], "analysis": ""})
_CPS_MERGE_TEXT = "### 1. Problem\n- 느림\n### 2. Solution\n- 캐시"
_PRD_IMPACT = json.dumps({"affected_sections": [], "removed_epic_ids": [], "removed_story_ids": [], "analysis": ""})
_PRD_MERGE_TEXT = "### Epic & Story Map\n#### 📦 [Epic-01] 인증\n- **[Story-01.1] 로그인**"

_CONTENT = "오늘 회의 — 50자 이상으로 작성하여 impact fallback 트리거를 피하고 충분히 길게 만든 본문."

# merge 단계 LLM 응답 순서: cps_impact, cps_merge, prd_impact, prd_merge
_MERGE_RESPONSES = [_CPS_IMPACT, _CPS_MERGE_TEXT, _PRD_IMPACT, _PRD_MERGE_TEXT]


@pytest.fixture(autouse=True)
def _no_auto_cleanup(monkeypatch):
    """_maybe_trigger_auto_cleanup 은 실제 neo4j_client(get_master_prd)를 쳐서 단위
    테스트와 무관 — no-op 로 격리 (best-effort 라 기능엔 영향 없음)."""
    async def _noop(**kwargs):
        return None
    monkeypatch.setattr(jobs_mod, "_maybe_trigger_auto_cleanup", _noop)


async def test_cache_hit_skips_extract_llm():
    """캐시에 extract 가 있으면 cps_agent/prd_extract/prd_graph 3회 skip — merge LLM 4회만."""
    redis = FakeRedis()
    await set_cached_extract(redis, extract_cache_key("food", "v1", _CONTENT), _EXTRACT)
    gemini = FakeGemini(responses=list(_MERGE_RESPONSES))
    neo4j = FakeNeo4j(responses=[[]] * 7)  # cps: save_log/save_cps/fetch/merge + prd: fetch/save/merge
    ctx = make_arq_ctx(job_id="pm-hit", gemini=gemini, neo4j=neo4j, redis=redis)

    result = await post_meeting_pipeline_job(
        ctx, project_name="food", version="v1", date="d",
        meeting_content=_CONTENT, user_email=None,
    )

    assert len(gemini.calls) == 4         # ← extract 3회 skip, merge 4회만
    assert result["cps"]["master_cps_id"] == "doc_cps_master_food"
    assert result["cps"]["mode"] == "first_run"
    assert result["prd"]["master_prd_id"] == "doc_prd_master_food"
    assert result["cps"]["extraction_mode"] == "strict"


async def test_cache_miss_computes_and_persists_extract():
    """캐시 미스면 extract 계산(LLM 3회) + merge(4회) = 7회, 그리고 결과를 캐시에 저장."""
    redis = FakeRedis()
    extract_responses = [
        json.dumps(_CPS_GRAPH),                       # cps_agent
        _EXTRACT["prd_markdown"],                     # prd_extract
        json.dumps(_PRD_GRAPH),                       # prd_graph
    ]
    gemini = FakeGemini(responses=extract_responses + list(_MERGE_RESPONSES))
    neo4j = FakeNeo4j(responses=[[]] * 7)
    ctx = make_arq_ctx(job_id="pm-miss", gemini=gemini, neo4j=neo4j, redis=redis)

    result = await post_meeting_pipeline_job(
        ctx, project_name="food", version="v1", date="d",
        meeting_content=_CONTENT, user_email=None,
    )

    assert len(gemini.calls) == 7         # extract 3 + merge 4
    assert result["prd"]["master_prd_id"] == "doc_prd_master_food"
    # 다음 재사용을 위해 캐시에 저장됨
    cached = await get_cached_extract(redis, extract_cache_key("food", "v1", _CONTENT))
    assert cached is not None
    assert "nodes" in cached["cps_graph"]


async def test_overlap_writes_cps_master_before_prd_master():
    """[2026-06-04 perf 오버랩] CPS 병합과 PRD compute 를 동시 실행해도 **CPS master 쓰기가
    PRD master 쓰기보다 먼저** 일어나야 한다 — PRD master 의 BASED_ON 이 master CPS 를
    찾으려면 CPS master 가 먼저 존재해야 하기 때문(데이터/결과 동일성 보장)."""
    redis = FakeRedis()
    await set_cached_extract(redis, extract_cache_key("food", "v1", _CONTENT), _EXTRACT)
    gemini = FakeGemini(responses=list(_MERGE_RESPONSES))
    neo4j = FakeNeo4j(responses=[[]] * 7)
    ctx = make_arq_ctx(job_id="pm-order", gemini=gemini, neo4j=neo4j, redis=redis)

    await post_meeting_pipeline_job(
        ctx, project_name="food", version="v1", date="d",
        meeting_content=_CONTENT, user_email=None,
    )

    cyphers = [e["cypher"] for e in neo4j.executed]
    cps_master_idx = next(i for i, c in enumerate(cyphers) if "MERGE (master:CPS_Document" in c)
    prd_master_idx = next(i for i, c in enumerate(cyphers) if "MERGE (master:PRD_Document" in c)
    assert cps_master_idx < prd_master_idx, (
        "CPS master 쓰기가 PRD master 쓰기보다 먼저여야 BASED_ON 무결성 보장"
    )
    # PRD master merge 에 BASED_ON 포함 (master CPS 연결).
    assert "BASED_ON" in cyphers[prd_master_idx]


async def test_malformed_cache_entry_treated_as_miss_and_recomputed():
    """손상/구버전 캐시(cps_graph 누락)는 hit 으로 오인하지 않고 miss 처리 → 재계산.

    cache HIT 경로의 extract['cps_graph'] 가 KeyError 로 job 을 깨뜨리지 않도록 방어
    (포맷 드리프트/손상 대비). 재계산 결과로 캐시도 정상 형태로 self-heal.
    """
    redis = FakeRedis()
    # cps_graph 누락된 손상 캐시 엔트리
    await set_cached_extract(
        redis, extract_cache_key("food", "v1", _CONTENT), {"prd_markdown": "stale"}
    )
    extract_responses = [json.dumps(_CPS_GRAPH), _EXTRACT["prd_markdown"], json.dumps(_PRD_GRAPH)]
    gemini = FakeGemini(responses=extract_responses + list(_MERGE_RESPONSES))
    neo4j = FakeNeo4j(responses=[[]] * 7)
    ctx = make_arq_ctx(job_id="pm-malformed", gemini=gemini, neo4j=neo4j, redis=redis)

    result = await post_meeting_pipeline_job(
        ctx, project_name="food", version="v1", date="d",
        meeting_content=_CONTENT, user_email=None,
    )

    assert len(gemini.calls) == 7      # 손상 캐시 무시 → extract 3 재계산 + merge 4
    assert result["prd"]["master_prd_id"] == "doc_prd_master_food"
    # 캐시가 정상 extract 로 치유됨
    healed = await get_cached_extract(redis, extract_cache_key("food", "v1", _CONTENT))
    assert isinstance(healed.get("cps_graph"), dict)
    assert "nodes" in healed["cps_graph"]


def test_is_valid_extract_rejects_empty_prd_markdown():
    """[2026-06-04] prd_markdown 이 빈/공백이면 invalid → 빈/환각 extract 가 캐시 HIT 으로
    재사용돼 master PRD 가 엉뚱하게 굳는 사고 방지. 정상 본문은 valid."""
    from app.queue.jobs import _is_valid_extract

    ok = {"cps_graph": {"nodes": []}, "prd_graph": {"nodes": []}, "prd_markdown": "# PRD"}
    assert _is_valid_extract(ok) is True

    # 빈 / 공백 prd_markdown → invalid (miss 로 재계산)
    assert _is_valid_extract({**ok, "prd_markdown": ""}) is False
    assert _is_valid_extract({**ok, "prd_markdown": "   \n  "}) is False
    # prd_markdown 키 자체 누락 / 비-문자열 → invalid
    assert _is_valid_extract({"cps_graph": {}, "prd_graph": {}}) is False
    assert _is_valid_extract({**ok, "prd_markdown": None}) is False
    # 형태 자체가 깨진 경우
    assert _is_valid_extract({"prd_markdown": "x"}) is False
    assert _is_valid_extract(None) is False


async def test_empty_prd_markdown_cache_entry_is_recomputed():
    """빈 prd_markdown 캐시 엔트리는 hit 으로 오인하지 않고 재계산 (self-heal)."""
    redis = FakeRedis()
    await set_cached_extract(
        redis, extract_cache_key("food", "v1", _CONTENT),
        {"cps_graph": _CPS_GRAPH, "prd_graph": _PRD_GRAPH, "prd_markdown": ""},
    )
    extract_responses = [json.dumps(_CPS_GRAPH), _EXTRACT["prd_markdown"], json.dumps(_PRD_GRAPH)]
    gemini = FakeGemini(responses=extract_responses + list(_MERGE_RESPONSES))
    neo4j = FakeNeo4j(responses=[[]] * 7)
    ctx = make_arq_ctx(job_id="pm-empty-md", gemini=gemini, neo4j=neo4j, redis=redis)

    result = await post_meeting_pipeline_job(
        ctx, project_name="food", version="v1", date="d",
        meeting_content=_CONTENT, user_email=None,
    )

    assert len(gemini.calls) == 7      # 빈 prd_markdown 무시 → extract 3 재계산 + merge 4
    assert result["prd"]["master_prd_id"] == "doc_prd_master_food"
    healed = await get_cached_extract(redis, extract_cache_key("food", "v1", _CONTENT))
    assert healed["prd_markdown"].strip()    # 정상 본문으로 치유됨


async def test_next_meeting_enqueues_prefetch(monkeypatch):
    """next_meeting 주어지면 다음 버전 extract 선반입 enqueue (content/version 전달)."""
    enqueued = []

    async def _fake_enqueue_prefetch(**kwargs):
        enqueued.append(kwargs)
        return "prefetch-task"

    monkeypatch.setattr(
        "app.queue.client.enqueue_prefetch_extract", _fake_enqueue_prefetch
    )

    redis = FakeRedis()
    await set_cached_extract(redis, extract_cache_key("food", "v1", _CONTENT), _EXTRACT)
    gemini = FakeGemini(responses=list(_MERGE_RESPONSES))
    neo4j = FakeNeo4j(responses=[[]] * 7)
    ctx = make_arq_ctx(job_id="pm-next", gemini=gemini, neo4j=neo4j, redis=redis)

    await post_meeting_pipeline_job(
        ctx, project_name="food", version="v1", date="d",
        meeting_content=_CONTENT, user_email=None,
        next_meeting={"content": "다음 회의 본문 내용", "version": "v2",
                      "previous_cps_id": "doc_cps_food_v2"},
    )

    assert len(enqueued) == 1
    assert enqueued[0]["version"] == "v2"
    assert enqueued[0]["meeting_content"] == "다음 회의 본문 내용"
    assert enqueued[0]["project_name"] == "food"
