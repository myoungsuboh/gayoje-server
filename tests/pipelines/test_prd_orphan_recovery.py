"""
orphan(master 빈 + prd_total>1) 가드의 자기증식 트랩 해소 (P0-6 / C).

[기존 버그]
_commit_orphan 이 save_prd(delta)를 **쓴 뒤** raise → prd_total 증가 → 다음 회의도
orphan 재진입 → 매 회의 delta 만 쌓이고 master 는 영영 미생성 (자기증식 갇힘).

[수정 후 계약]
- 실질 prd_markdown 이면: 빈 master 를 그 문서로 부트스트랩(빈→채움 — 잃을 master 없음)
  + delta 정상 저장 + mode='first_run' + diagnostic.orphan_recovered (과거 delta 는
  '마스터 재구성' 안내). → 갇힘 자동 탈출.
- 비실질이면: **delta 저장 없이** raise (증식 차단). R1(#175)이 prd.mode='error' 로
  강등해 FE 가 재생성 안내.
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.prd_pipeline import PrdInput, run_prd_merge
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio

_IMPACT_EMPTY = json.dumps({
    "affected_sections": [], "removed_epic_ids": [], "removed_story_ids": [], "analysis": "",
})

_SUBSTANTIVE_MD = (
    "## PRD: 재고관리\n\n### 2. 기능 계층\n"
    "- 에픽: 바코드 입출고 — 스토리: 사용자는 스캔으로 입고를 등록하고 시스템이 재고를 갱신한다.\n"
    "- 에픽: 재고 대시보드 — 스토리: 사용자는 품목별 현재고와 안전재고를 비교 조회한다.\n"
    "### 3. 화면\n- 입고 화면, 출고 화면, 대시보드 화면을 제공한다.\n"
    "### 4. 비기능\n- 응답 2초 이내, 동시 스캔 50건 처리, 감사 로그 보존.\n"
)

# spec_count>0 (main path 진입 → orphan 가드 도달) graph.
_SPEC_GRAPH = {
    "nodes": [
        {"id": "doc_prd_x", "label": "PRD_Document", "properties": {}},
        {"id": "epic_01", "label": "Epic", "properties": {"summary": "바코드 입출고"}},
        {"id": "story_01_1", "label": "Story", "properties": {"summary": "스캔 입고 등록"}},
    ],
    "relationships": [],
}
_PARSED = {"pure_markdown": "## CPS\n- 문제", "problems": "- [prb_01] 수기 재고 오류"}

# orphan 상태 fetch 응답: master 빈 + prd_total>1.
_ORPHAN_FETCH = [{
    "master_id": None, "master_content": "",
    "master_prd_details": [], "latest_id": "doc_prd_x_v4", "latest_content": "직전 delta",
    "latest_prd_details": [], "project_name": "x", "prd_total": 4,
    "cleanup_at_version_count": 0,
}]


async def test_orphan_with_substantive_markdown_recovers_master():
    """orphan + 실질 문서 → 빈 master 부트스트랩(자동 탈출), raise 없음."""
    extract = {"parsed": _PARSED, "prd_markdown": _SUBSTANTIVE_MD, "prd_graph": _SPEC_GRAPH}
    gemini = FakeGemini(responses=[_IMPACT_EMPTY])
    neo4j = FakeNeo4j(responses=[_ORPHAN_FETCH])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="orphan-recover")

    result = await run_prd_merge(
        ctx, PrdInput(project_name="x", version="V5", cps_graph={"nodes": [], "relationships": []}), extract
    )

    assert result.mode == "first_run"
    assert result.diagnostic.get("orphan_recovered") is True
    cyphers = [e["cypher"] for e in neo4j.executed]
    assert any("MERGE (master:PRD_Document" in c for c in cyphers)   # master 채워짐
    save = [e for e in neo4j.executed if "MERGE (master:PRD_Document" in e["cypher"]][0]
    assert save["params"]["merged_content"] == _SUBSTANTIVE_MD


async def test_orphan_without_substantive_markdown_raises_without_delta_write():
    """orphan + 비실질 문서 → raise 유지하되 **delta 저장 없음** (자기증식 차단)."""
    extract = {
        "parsed": _PARSED,
        "prd_markdown": "# 짧음",          # 비실질 (<150 실내용)
        "prd_graph": _SPEC_GRAPH,
    }
    gemini = FakeGemini(responses=[_IMPACT_EMPTY])
    neo4j = FakeNeo4j(responses=[_ORPHAN_FETCH])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="orphan-block")

    with pytest.raises(RuntimeError, match="마스터"):
        await run_prd_merge(
            ctx, PrdInput(project_name="x", version="V5", cps_graph={"nodes": [], "relationships": []}), extract
        )

    # fetch(읽기) 외 쓰기 0건 — 특히 delta save(UNWIND) 가 없어야 prd_total 증식이 멈춘다.
    writes = [e for e in neo4j.executed if "UNWIND" in e["cypher"] or "MERGE (master" in e["cypher"]]
    assert writes == [], f"orphan 차단 분기에서 쓰기 발생: {[w['cypher'][:60] for w in writes]}"
