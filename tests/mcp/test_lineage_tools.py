"""
MCP lineage tools 단위 테스트.

검증:
- find_spec_for_file: 파일 경로 → spec 항목 매칭 (정확/suffix/basename + 없는 경우)
- trace_upstream: design 노드 → Story → Epic 체인
- list_design_nodes: 전체 / kind 필터 / 잘못된 kind
- get_story: story id 정규화 (zero-pad / Story-XX.Y 형식 흡수)
- search_spec: kinds 필터 + limit 클램프 + 빈 query 가드

repository / neo4j_client 는 monkeypatch 로 fake 응답 주입.
인증 가드 (require_mcp_user_and_assert_owns) 는 no-op 으로 모킹 — auth 자체는
test_mcp_auth.py 가 검증.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest


# ─── 공용 fixture ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _bypass_auth(monkeypatch):
    """모든 테스트에서 owner 가드 우회 — tool 본문 로직만 검증."""
    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(
        "app.mcp.lineage_tools.require_mcp_user_and_assert_owns", _noop
    )


def _fake_lineage(stories=None, aggregates=None, apis=None, services=None):
    """lineage_repository.get_last_lineage 가 돌려주는 LineageResult 모킹용 객체.

    실제 LineageResult / LineageArtifact / LineageImpl 그대로 사용 — 타입 안정성.
    """
    from app.service.lineage_repository import (
        LineageArtifact,
        LineageImpl,
        LineageResult,
        LineageResultData,
        LineageStats,
    )

    def _art(items: List[Dict[str, Any]]) -> List[LineageArtifact]:
        out = []
        for it in items or []:
            impls = [LineageImpl(**impl) for impl in it.pop("implementations", [])]
            out.append(LineageArtifact(implementations=impls, **it))
        return out

    return LineageResult(
        id="lineage-test-1",
        project="proj1",
        summary="test",
        data=LineageResultData(
            stories=_art(stories or []),
            aggregates=_art(aggregates or []),
            apis=_art(apis or []),
            services=_art(services or []),
            stats=LineageStats(),
        ),
        saved_at=1700000000000,
    )


# ─── find_spec_for_file ──────────────────────────────────────


@pytest.mark.asyncio
async def test_find_spec_for_file_no_analysis(monkeypatch):
    """analyzeLineage 실행 전 — 명시적 reason 으로 안내."""
    from app.mcp import lineage_tools

    async def _none(_project):
        return None
    monkeypatch.setattr(
        "app.service.lineage_repository.get_last_lineage", _none
    )

    out = await lineage_tools.find_spec_for_file.fn("proj1", "src/x.py")
    assert out["matches"] == []
    assert out["reason"] == "no_lineage_analysis"
    assert "analyzeLineage" in out["hint"]


@pytest.mark.asyncio
async def test_find_spec_for_file_exact_match(monkeypatch):
    """정확히 같은 경로 매칭 + upstream Story bulk fetch."""
    from app.mcp import lineage_tools

    result = _fake_lineage(
        aggregates=[{
            "id": "Order",
            "name": "Order",
            "description": "주문 집합",
            "implementations": [{
                "repoUrl": "https://github.com/o/r",
                "filePath": "src/order/OrderService.py",
                "confidence": "high",
                "reason": "name match",
                "verified": True,
            }],
        }],
    )
    async def _last(_p):
        return result
    monkeypatch.setattr(
        "app.service.lineage_repository.get_last_lineage", _last
    )

    # upstream bulk fetch — aggregate "Order" 의 DERIVED_FROM Story 응답 모킹
    bulk_calls: List[Dict[str, Any]] = []
    async def _bulk(cypher, params):
        bulk_calls.append(params)
        # _BULK_UPSTREAM_CYPHER 패턴 식별
        assert "DERIVED_FROM" in cypher
        return [{
            "node_id": "Order",
            "stories": [{
                "story_id": "story_01_1",
                "story_name": "주문 처리",
                "confidence": "direct",
                "quote": "사용자가 주문...",
            }],
        }]
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _bulk
    )

    out = await lineage_tools.find_spec_for_file.fn(
        "proj1", "src/order/OrderService.py"
    )
    assert out["reason"] == "ok"
    assert len(out["matches"]) == 1
    m = out["matches"][0]
    assert m["kind"] == "aggregate"
    assert m["id"] == "Order"
    assert m["matched_impl"]["confidence"] == "high"
    # upstream Story 자동 포함
    assert m["stories"][0]["story_id"] == "story_01_1"
    assert m["stories"][0]["confidence"] == "direct"
    assert out["lineage_id"] == "lineage-test-1"
    # bulk fetch 정확히 1회 — design kind 매치만 (Story kind 는 자기 자신 → 제외)
    assert len(bulk_calls) == 1
    assert bulk_calls[0]["node_ids"] == ["Order"]


@pytest.mark.asyncio
async def test_find_spec_for_file_upstream_fetch_failure_graceful(monkeypatch):
    """upstream bulk fetch 실패해도 기본 매치는 반환 (graceful)."""
    from app.mcp import lineage_tools

    result = _fake_lineage(
        aggregates=[{
            "id": "Order", "name": "Order",
            "implementations": [{
                "repoUrl": "https://github.com/o/r",
                "filePath": "src/order/Service.py",
                "confidence": "high", "verified": True,
            }],
        }],
    )
    async def _last(_p):
        return result
    monkeypatch.setattr(
        "app.service.lineage_repository.get_last_lineage", _last
    )

    async def _bulk_fail(*_a, **_kw):
        raise RuntimeError("Neo4j down")
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _bulk_fail
    )

    out = await lineage_tools.find_spec_for_file.fn(
        "proj1", "src/order/Service.py"
    )
    # 기본 매치는 반환됨
    assert out["reason"] == "ok"
    assert len(out["matches"]) == 1
    assert out["matches"][0]["id"] == "Order"
    # upstream 은 없음 — graceful degradation
    assert "stories" not in out["matches"][0]


@pytest.mark.asyncio
async def test_find_spec_for_file_story_match_no_upstream_call(monkeypatch):
    """Story 만 매치된 경우 — bulk upstream cypher 호출 안 함 (Story 는 자기 자신)."""
    from app.mcp import lineage_tools

    result = _fake_lineage(
        stories=[{
            "id": "story_01_1", "name": "주문",
            "implementations": [{
                "repoUrl": "https://github.com/o/r",
                "filePath": "src/order/Service.py",
                "confidence": "low", "verified": False,
            }],
        }],
    )
    async def _last(_p):
        return result
    monkeypatch.setattr(
        "app.service.lineage_repository.get_last_lineage", _last
    )

    bulk_called = False
    async def _bulk(*_a, **_kw):
        nonlocal bulk_called
        bulk_called = True
        return []
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _bulk
    )

    out = await lineage_tools.find_spec_for_file.fn(
        "proj1", "src/order/Service.py"
    )
    assert out["reason"] == "ok"
    assert out["matches"][0]["kind"] == "story"
    # design kind 매치 없음 → bulk cypher 호출 안 함 (불필요한 round-trip 제거)
    assert bulk_called is False


@pytest.mark.asyncio
async def test_find_spec_for_file_suffix_match(monkeypatch):
    """사용자가 절대 경로 보냄 — impl 의 상대 경로와 suffix 매칭."""
    from app.mcp import lineage_tools

    result = _fake_lineage(
        services=[{
            "id": "OrderApi",
            "name": "Order API",
            "type": "backend",
            "tech_stack": "fastapi",
            "implementations": [{
                "repoUrl": "https://github.com/o/r",
                "filePath": "src/api/order_routes.py",
                "confidence": "medium",
                "verified": True,
            }],
        }],
    )
    async def _last(_p):
        return result
    monkeypatch.setattr(
        "app.service.lineage_repository.get_last_lineage", _last
    )

    out = await lineage_tools.find_spec_for_file.fn(
        "proj1", "/Users/dev/repo/src/api/order_routes.py"
    )
    assert out["reason"] == "ok"
    assert len(out["matches"]) == 1
    assert out["matches"][0]["kind"] == "service"
    assert out["matches"][0]["tech_stack"] == "fastapi"


@pytest.mark.asyncio
async def test_find_spec_for_file_windows_path(monkeypatch):
    """Windows 백슬래시 경로 흡수."""
    from app.mcp import lineage_tools

    result = _fake_lineage(
        stories=[{
            "id": "story_01_1",
            "name": "주문 생성",
            "implementations": [{
                "repoUrl": "https://github.com/o/r",
                "filePath": "src/order/Service.py",
                "confidence": "low",
                "verified": False,
            }],
        }],
    )
    async def _last(_p):
        return result
    monkeypatch.setattr(
        "app.service.lineage_repository.get_last_lineage", _last
    )

    out = await lineage_tools.find_spec_for_file.fn(
        "proj1", r"C:\dev\repo\src\order\Service.py"
    )
    assert out["reason"] == "ok"
    assert out["matches"][0]["id"] == "story_01_1"


@pytest.mark.asyncio
async def test_find_spec_for_file_no_match(monkeypatch):
    """매칭 없음 — 'no_match' reason + hint."""
    from app.mcp import lineage_tools

    result = _fake_lineage(
        aggregates=[{
            "id": "Order",
            "name": "Order",
            "implementations": [{
                "repoUrl": "https://github.com/o/r",
                "filePath": "src/order/Service.py",
                "confidence": "high",
                "verified": True,
            }],
        }],
    )
    async def _last(_p):
        return result
    monkeypatch.setattr(
        "app.service.lineage_repository.get_last_lineage", _last
    )

    out = await lineage_tools.find_spec_for_file.fn(
        "proj1", "src/totally/different.py"
    )
    assert out["matches"] == []
    assert out["reason"] == "no_match"


@pytest.mark.asyncio
async def test_find_spec_for_file_multiple_kinds(monkeypatch):
    """같은 파일에 Story / Aggregate / API 동시 매칭."""
    from app.mcp import lineage_tools

    impl = {
        "repoUrl": "https://github.com/o/r",
        "filePath": "src/order/OrderService.py",
        "confidence": "high",
        "verified": True,
    }
    result = _fake_lineage(
        stories=[{"id": "story_01_1", "name": "주문",
                  "implementations": [dict(impl)]}],
        aggregates=[{"id": "Order", "name": "Order",
                     "implementations": [dict(impl)]}],
        apis=[{"id": "POST /orders", "name": "POST /orders",
               "endpoint": "/orders", "method": "POST",
               "implementations": [dict(impl)]}],
    )
    async def _last(_p):
        return result
    monkeypatch.setattr(
        "app.service.lineage_repository.get_last_lineage", _last
    )

    out = await lineage_tools.find_spec_for_file.fn(
        "proj1", "src/order/OrderService.py"
    )
    kinds = {m["kind"] for m in out["matches"]}
    assert kinds == {"story", "aggregate", "api"}
    api_match = next(m for m in out["matches"] if m["kind"] == "api")
    assert api_match["endpoint"] == "/orders"
    assert api_match["method"] == "POST"


# ─── trace_upstream ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_upstream_design_node(monkeypatch):
    from app.mcp import lineage_tools

    fake_record = {
        "node": {"id": "Order", "name": "Order",
                 "description": "주문 집합", "label": "Aggregate"},
        "stories": [{"id": "story_01_1", "name": "주문 생성",
                     "description": "..."}],
        "lineage_edges": [{"story_id": "story_01_1",
                           "confidence": "direct", "quote": "사용자가 주문..."}],
        "epics": [{"id": "epic_01", "name": "주문 도메인",
                   "description": "..."}],
        "screens": [{"id": "screen_01", "name": "주문 화면"}],
    }

    async def _run(_cypher, params):
        assert params["node_id"] == "Order"
        return [fake_record]
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    out = await lineage_tools.trace_upstream.fn("proj1", "Order")
    assert out["node"]["id"] == "Order"
    assert out["stories"][0]["id"] == "story_01_1"
    assert out["epics"][0]["id"] == "epic_01"
    assert out["lineage_edges"][0]["confidence"] == "direct"


@pytest.mark.asyncio
async def test_trace_upstream_story_id_retry(monkeypatch):
    """raw story id ('Story-1.1') 로 들어와도 후보 id 로 retry 해서 매칭."""
    from app.mcp import lineage_tools

    seen_ids = []
    fake_record = {
        "node": {"id": "story_01_1", "name": "주문",
                 "description": "", "label": "Story"},
        "stories": [], "lineage_edges": [], "epics": [], "screens": [],
    }

    async def _run(_cypher, params):
        seen_ids.append(params["node_id"])
        # 1차 (raw "Story-1.1") miss, 후보 중 'story_01_01' 매치 가정
        if params["node_id"] == "story_01_01":
            return [fake_record]
        return []
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    out = await lineage_tools.trace_upstream.fn("proj1", "Story-1.1")
    assert out is not None
    assert out["node"]["id"] == "story_01_1"
    # 후보 4개 중 일부 시도됐어야 함
    assert "Story-1.1" in seen_ids
    assert any(s.startswith("story_") for s in seen_ids)


@pytest.mark.asyncio
async def test_trace_upstream_node_not_found(monkeypatch):
    from app.mcp import lineage_tools

    async def _run(_c, _p):
        return []
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    out = await lineage_tools.trace_upstream.fn("proj1", "DoesNotExist")
    assert out is None


# ─── list_design_nodes ───────────────────────────────────────


@pytest.mark.asyncio
async def test_list_design_nodes_all(monkeypatch):
    """전체 종류 + total/has_more pagination 응답 검증."""
    from app.mcp import lineage_tools

    fake_items = [
        {"id": "Order", "name": "Order", "description": "",
         "kind": "Aggregate", "stories": [
             {"id": "story_01_1", "name": "주문", "confidence": "direct"}]},
        {"id": "OrderRoutes", "name": "Order Routes",
         "description": "", "kind": "ArchService", "stories": []},
    ]
    captured: Dict[str, Any] = {}
    async def _run(_c, params):
        captured.update(params)
        return [{"items": fake_items, "total": 487}]
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    out = await lineage_tools.list_design_nodes.fn("proj1")
    assert isinstance(out, dict)
    assert len(out["items"]) == 2
    assert out["total"] == 487
    assert out["offset"] == 0
    assert out["limit"] == 100
    assert out["has_more"] is True   # 0 + 100 < 487
    assert captured["kind_label"] is None
    assert captured["offset"] == 0
    assert captured["limit"] == 100
    assert out["items"][0]["stories"][0]["id"] == "story_01_1"


@pytest.mark.asyncio
async def test_list_design_nodes_pagination(monkeypatch):
    """offset/limit clamp + has_more 계산."""
    from app.mcp import lineage_tools

    captured: Dict[str, Any] = {}
    async def _run(_c, params):
        captured.update(params)
        return [{"items": [], "total": 200}]
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    # 음수 offset → 0 으로 clamp, 초과 limit → 500 으로 clamp
    out = await lineage_tools.list_design_nodes.fn(
        "proj1", offset=-10, limit=10000
    )
    assert captured["offset"] == 0
    assert captured["limit"] == 500
    assert out["offset"] == 0
    assert out["limit"] == 500
    # 200 total, offset 0 + limit 500 → 모두 소진 → has_more False
    assert out["has_more"] is False

    # 중간 페이지 — has_more True
    captured.clear()
    out = await lineage_tools.list_design_nodes.fn(
        "proj1", offset=50, limit=20
    )
    assert captured["offset"] == 50
    assert captured["limit"] == 20
    # 50 + 20 < 200 → has_more True
    assert out["has_more"] is True


@pytest.mark.asyncio
async def test_list_design_nodes_kind_filter(monkeypatch):
    from app.mcp import lineage_tools

    captured: Dict[str, Any] = {}
    async def _run(_c, params):
        captured.update(params)
        return [{"items": [], "total": 0}]
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    await lineage_tools.list_design_nodes.fn("proj1", kind="aggregate")
    assert captured["kind_label"] == "Aggregate"

    await lineage_tools.list_design_nodes.fn("proj1", kind="DB")
    assert captured["kind_label"] == "ArchDatabase"


@pytest.mark.asyncio
async def test_list_design_nodes_invalid_kind(monkeypatch):
    """잘못된 kind 는 빈 결과 (cypher 호출 자체 안 함) — 사용자 의도 보존."""
    from app.mcp import lineage_tools

    called = False
    async def _run(*_a, **_kw):
        nonlocal called
        called = True
        return []
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    out = await lineage_tools.list_design_nodes.fn("proj1", kind="banana")
    assert out["items"] == []
    assert out["total"] == 0
    assert out["has_more"] is False
    assert called is False


# ─── get_story ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_story_normalized(monkeypatch):
    """다양한 입력 형식 → 후보 IDs 로 매칭."""
    from app.mcp import lineage_tools

    fake_record = {
        "story": {"id": "story_01_1", "name": "주문 생성",
                  "description": "", "acceptance_criteria": ""},
        "epic": {"id": "epic_01", "name": "주문", "description": ""},
        "screens": [{"id": "screen_01", "name": "주문 화면"}],
        "derived_nodes": [
            {"id": "Order", "name": "Order", "kind": "Aggregate",
             "confidence": "direct", "quote": "..."}
        ],
    }
    captured: Dict[str, Any] = {}
    async def _run(_c, params):
        captured.update(params)
        return [fake_record]
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    out = await lineage_tools.get_story.fn("proj1", "Story-1.1")
    assert out["story"]["id"] == "story_01_1"
    assert out["epic"]["id"] == "epic_01"
    assert out["derived_nodes"][0]["kind"] == "Aggregate"
    # 후보 4개 (zero-pad 변형) 모두 포함되어야
    cands = set(captured["candidates"])
    assert "story_1_1" in cands and "story_01_1" in cands
    assert "story_1_01" in cands and "story_01_01" in cands


@pytest.mark.asyncio
async def test_get_story_not_found(monkeypatch):
    from app.mcp import lineage_tools

    async def _run(_c, _p):
        return []
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    out = await lineage_tools.get_story.fn("proj1", "Story-99.99")
    assert out is None


@pytest.mark.asyncio
async def test_get_story_invalid_id_format(monkeypatch):
    """story_id 패턴 매칭 안 되면 raw 그대로 후보로 사용 (정확 id 직접 전달 케이스)."""
    from app.mcp import lineage_tools

    captured: Dict[str, Any] = {}
    async def _run(_c, params):
        captured.update(params)
        return []
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    await lineage_tools.get_story.fn("proj1", "custom_id_123")
    assert captured["candidates"] == ["custom_id_123"]


# ─── search_spec ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_spec_fulltext_path(monkeypatch):
    """fulltext 인덱스 정상 동작 시 — Lucene 결과 + search_method='fulltext'."""
    from app.mcp import lineage_tools

    captured: Dict[str, Any] = {}
    async def _run(cypher, params):
        captured.update(params)
        # fulltext cypher 호출 식별
        assert "db.index.fulltext.queryNodes" in cypher
        return [{
            "id": "Order", "name": "Order",
            "description": "주문 집합", "kind": "Aggregate",
            "score": 2.5,
        }]
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    out = await lineage_tools.search_spec.fn("proj1", "주문")
    assert len(out) == 1
    assert out[0]["search_method"] == "fulltext"
    assert out[0]["score"] == 2.5
    # default 는 모든 종류 검색
    assert captured["wants_story"] is True
    assert captured["wants_aggregate"] is True
    # Lucene escape 결과 — 단어 wildcard 래핑
    assert captured["lucene_q"] == "*주문*"


@pytest.mark.asyncio
async def test_search_spec_fallback_on_fulltext_failure(monkeypatch):
    """fulltext 인덱스 없거나 Lucene parse 실패 시 — CONTAINS 폴백."""
    from app.mcp import lineage_tools

    cypher_calls: List[str] = []
    async def _run(cypher, params):
        cypher_calls.append(cypher)
        if "db.index.fulltext.queryNodes" in cypher:
            raise RuntimeError("Unable to find fulltext index")
        # CONTAINS 폴백 호출
        assert "CONTAINS" in cypher
        assert params.get("q") == "주문"
        return [{
            "id": "Order", "name": "Order",
            "description": "주문", "kind": "Aggregate",
        }]
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    out = await lineage_tools.search_spec.fn("proj1", "주문")
    # 두 cypher 모두 시도됨
    assert any("fulltext" in c for c in cypher_calls)
    assert any("CONTAINS" in c for c in cypher_calls)
    assert len(out) == 1
    assert out[0]["search_method"] == "contains"
    assert "score" not in out[0]


@pytest.mark.asyncio
async def test_search_spec_multi_word_lucene_escape(monkeypatch):
    """공백 분리 multi-word + 특수문자 escape."""
    from app.mcp import lineage_tools

    captured: Dict[str, Any] = {}
    async def _run(cypher, params):
        captured.update(params)
        return []
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    await lineage_tools.search_spec.fn("proj1", "주문 처리")
    assert captured["lucene_q"] == "*주문* AND *처리*"

    # Lucene 특수문자 escape — `:` `(` `*` `?` `\` 등
    captured.clear()
    await lineage_tools.search_spec.fn("proj1", "Order:id(test)")
    assert captured["lucene_q"] == r"*Order\:id\(test\)*"


@pytest.mark.asyncio
async def test_search_spec_kinds_filter(monkeypatch):
    from app.mcp import lineage_tools

    captured: Dict[str, Any] = {}
    async def _run(_c, params):
        captured.update(params)
        return []
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    await lineage_tools.search_spec.fn("proj1", "ord", kinds=["aggregate", "api"])
    assert captured["wants_aggregate"] is True
    assert captured["wants_api"] is True
    assert captured["wants_story"] is False
    assert captured["wants_entity"] is False


@pytest.mark.asyncio
async def test_search_spec_empty_query(monkeypatch):
    """빈 query → 빈 결과 (Neo4j 호출 자체 안 함)."""
    from app.mcp import lineage_tools

    called = False
    async def _run(*_a, **_kw):
        nonlocal called
        called = True
        return []
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    out = await lineage_tools.search_spec.fn("proj1", "   ")
    assert out == []
    assert called is False


@pytest.mark.asyncio
async def test_search_spec_limit_clamp(monkeypatch):
    """limit 음수/초과 값 → 1~200 범위로 clamp."""
    from app.mcp import lineage_tools

    captured: Dict[str, Any] = {}
    async def _run(_c, params):
        captured.update(params)
        return []
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", _run
    )

    await lineage_tools.search_spec.fn("proj1", "x", limit=10000)
    assert captured["limit"] == 200

    captured.clear()
    await lineage_tools.search_spec.fn("proj1", "x", limit=-5)
    assert captured["limit"] == 1


# ─── helpers (내부 유틸 회귀) ─────────────────────────────


def test_path_matching_helpers():
    from app.mcp.lineage_tools import _normalize_file_path, _path_matches

    assert _normalize_file_path(r"C:\dev\Src\X.py") == "c:/dev/src/x.py"
    assert _normalize_file_path("./src/x.py") == "src/x.py"
    assert _normalize_file_path('"src/x.py"') == "src/x.py"

    # 정확 매칭
    assert _path_matches("src/x.py", "src/x.py") is True
    # suffix 매칭 (디렉토리 경계)
    assert _path_matches("order/Service.py",
                         "src/order/Service.py") is True
    # 반대 방향 suffix (사용자가 절대 경로 보낸 케이스)
    assert _path_matches("/Users/me/repo/src/x.py",
                         "src/x.py") is True
    # basename 안전망 — needle 이 순수 파일명일 때만
    assert _path_matches("file.py", "x/y/file.py") is True
    # partial suffix (디렉토리 경계 없음) → 차단
    assert _path_matches("der/Service.py",
                         "src/order/Service.py") is False
    # needle 에 부분 경로 있으면 basename fallback 차단 (false positive 방지)
    assert _path_matches("a/b/c/file.py", "x/y/file.py") is False
    # 빈 값
    assert _path_matches("", "src/x.py") is False


def test_story_id_candidates():
    from app.mcp.lineage_tools import _story_id_candidates_from_any

    cands = _story_id_candidates_from_any("Story-1.1")
    assert set(cands) == {
        "story_1_1", "story_01_1", "story_1_01", "story_01_01"
    }
    cands = _story_id_candidates_from_any("[Story 02.10]")
    assert "story_02_10" in cands
    assert "story_2_10" in cands
    # 매칭 안 되는 경우
    assert _story_id_candidates_from_any("not-a-story") == []
    assert _story_id_candidates_from_any("") == []
