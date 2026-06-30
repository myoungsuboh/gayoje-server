"""
query_repository 단위 테스트 — 7개 조회 함수.

각 함수가 Cypher 호출 + 정규화 + Pydantic 변환 정상 동작 검증.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import query_repository as q
from app.service.query_repository import (
    ArchitectureGraph,
    CpsMaster,
    DddGraph,
    GraphRel,
    MeetingLog,
    MeetingVersion,
    PrdMaster,
    SpackGraph,
)


pytestmark = pytest.mark.asyncio


class _Fake:
    def __init__(self, responses=None):
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
        fake = _Fake(responses=responses)
        monkeypatch.setattr(
            "app.service.query_repository.neo4j_client.run_cypher", fake
        )
        return fake
    return _setup


# ─── getCPS ─────────────────────────────────────────────────────


async def test_get_master_cps_returns_cps_master(fake_run):
    fake = fake_run(
        [
            [
                {
                    "master_id": "doc_cps_master_x",
                    "version": "Final",
                    "content": "## CPS\n- body",
                    "last_updated": 1700000000000,
                    "absorbed_cps_ids": ["doc_cps_x_v1_1", "doc_cps_x_v1_2"],
                }
            ]
        ]
    )
    out = await q.get_master_cps("x")
    assert isinstance(out, CpsMaster)
    assert out.master_id == "doc_cps_master_x"
    assert out.version == "Final"
    assert out.last_updated == 1700000000000
    assert "doc_cps_x_v1_1" in out.absorbed_cps_ids
    # parameter binding
    assert fake.calls[0]["params"] == {"project": "x"}


async def test_get_master_cps_returns_none_when_not_found(fake_run):
    fake_run([[]])
    assert await q.get_master_cps("missing") is None


async def test_get_master_cps_filters_null_absorbed_ids(fake_run):
    fake_run(
        [
            [
                {
                    "master_id": "doc_cps_master_x",
                    "version": "Final",
                    "content": "x",
                    "last_updated": 1,
                    "absorbed_cps_ids": ["id-1", None, "id-2"],  # null 끼어있음
                }
            ]
        ]
    )
    out = await q.get_master_cps("x")
    assert out is not None
    assert out.absorbed_cps_ids == ["id-1", "id-2"]


# ─── getPRD ─────────────────────────────────────────────────────


async def test_get_master_prd_returns_with_cps_link(fake_run):
    fake_run(
        [
            [
                {
                    "master_prd_id": "doc_prd_master_x",
                    "prd_content": "## PRD\n",
                    "last_updated": 1700000001000,
                    "related_master_cps_id": "doc_cps_master_x",
                    "absorbed_prd_ids": [],
                }
            ]
        ]
    )
    out = await q.get_master_prd("x")
    assert isinstance(out, PrdMaster)
    assert out.related_master_cps_id == "doc_cps_master_x"


async def test_get_master_prd_returns_none(fake_run):
    fake_run([[]])
    assert await q.get_master_prd("x") is None


# ─── getDDD ─────────────────────────────────────────────────────


async def test_get_ddd_graph_full(fake_run):
    fake_run(
        [
            [
                {
                    "contexts": [{"id": "CTX-01", "name": "Tickets"}],
                    "aggregates": [{"id": "AGG-01", "name": "Ticket"}],
                    "domain_entities": [{"id": "DENT-01", "name": "TT"}],
                    "domain_events": [{"id": "EVT-01", "name": "TicketIssued"}],
                    "internal_rels": [
                        {
                            "source_id": "AGG-01",
                            "target_id": "CTX-01",
                            "type": "BELONGS_TO",
                        }
                    ],
                    "trigger_rels": [
                        {
                            "source_id": "Story-01.1",
                            "target_id": "EVT-01",
                            "type": "TRIGGERS",
                        }
                    ],
                }
            ]
        ]
    )
    out = await q.get_ddd_graph("x")
    assert isinstance(out, DddGraph)
    assert out.aggregates[0]["name"] == "Ticket"
    assert len(out.internal_rels) == 1
    assert out.internal_rels[0].type == "BELONGS_TO"
    assert out.trigger_rels[0].source_id == "Story-01.1"


async def test_get_ddd_graph_empty_returns_empty_object(fake_run):
    """createDesign 미실행 프로젝트도 404 가 아닌 빈 그래프 반환."""
    fake_run([[]])
    out = await q.get_ddd_graph("empty")
    assert isinstance(out, DddGraph)
    assert out.contexts == []
    assert out.internal_rels == []


async def test_get_ddd_filters_invalid_rels(fake_run):
    """source_id/target_id/type 중 하나라도 누락된 rel 은 skip."""
    fake_run(
        [
            [
                {
                    "contexts": [],
                    "aggregates": [],
                    "domain_entities": [],
                    "domain_events": [],
                    "internal_rels": [
                        {"source_id": "a", "target_id": "b", "type": "X"},  # OK
                        {"source_id": "a", "target_id": "b"},  # type 누락 → skip
                        {"source_id": None, "target_id": "b", "type": "X"},  # null
                    ],
                    "trigger_rels": [],
                }
            ]
        ]
    )
    out = await q.get_ddd_graph("x")
    assert len(out.internal_rels) == 1


# ─── getSpack ───────────────────────────────────────────────────


async def test_get_spack_graph_full(fake_run):
    fake_run(
        [
            [
                {
                    "apis": [{"id": "API-01", "method": "POST", "endpoint": "/x"}],
                    "entities": [{"id": "ENT-01", "name": "Ticket"}],
                    "policies": [{"id": "POL-01", "category": "Security"}],
                    "internal_rels": [
                        {
                            "source_id": "POL-01",
                            "target_id": "ENT-01",
                            "type": "GOVERNS",
                        }
                    ],
                    "implement_rels": [
                        {
                            "source_id": "API-01",
                            "target_id": "Story-01.1",
                            "type": "IMPLEMENTS",
                        }
                    ],
                }
            ]
        ]
    )
    out = await q.get_spack_graph("x")
    assert isinstance(out, SpackGraph)
    assert len(out.apis) == 1
    assert out.implement_rels[0].target_id == "Story-01.1"


async def test_get_spack_graph_preserves_linkage_for_scorer(fake_run):
    """[2026-06 연결 점수 fix] 읽기 쿼리가 복원한 API related_story_id / entity
    lineage 가 decode → model_dump 까지 보존돼, 스코어러 tier3(연결)가 0% 가
    아니게 된다.

    [회귀] 이전엔 related_story_id 가 노드 속성에 없어(IMPLEMENTS 엣지로만 저장)
    스코어러가 항상 0 → 연결 0% 고착. 읽기 쿼리에서 엣지로부터 복원하도록 수정.
    """
    from evals.scorer import score_spack

    fake_run([[{
        # 수정된 _GET_SPACK_CYPHER 가 내놓는 모양: API 에 related_story_id,
        # Entity 에 lineage 객체가 노드 맵으로 복원돼 들어온다.
        "apis": [{
            "id": "API-01", "method": "POST", "endpoint": "/x",
            "related_story_id": "Story-01.1",
        }],
        "entities": [{
            "id": "ENT-01", "name": "Ticket",
            "lineage": {"confidence": "direct",
                        "related_stories": [{"story_id": "Story-01.1"}]},
        }],
        "policies": [],
    }]])
    out = await q.get_spack_graph("x")

    # 1) decode/모델 통과 후에도 연결 정보 보존
    assert out.apis[0]["related_story_id"] == "Story-01.1"
    assert out.entities[0]["lineage"]["confidence"] == "direct"

    # 2) 스코어러 tier3(연결)가 0 이 아니어야 함 (핵심 회귀 가드)
    report = score_spack(out.model_dump())
    assert report.tier3.score > 0
    assert report.tier3.sub_metrics.get("api_story_mapped_ratio") == 1.0


async def test_get_spack_graph_unlinked_apis_score_zero_linkage(fake_run):
    """대조군 — related_story_id 가 없으면(연결 진짜 없음) tier3 가 0 인 게 정상."""
    from evals.scorer import score_spack

    fake_run([[{
        "apis": [{"id": "API-01", "method": "POST", "endpoint": "/x"}],
        "entities": [],
        "policies": [],
    }]])
    out = await q.get_spack_graph("x")
    report = score_spack(out.model_dump())
    assert report.tier3.sub_metrics.get("api_story_mapped_ratio") == 0.0


# ─── getArchitecture ────────────────────────────────────────────


async def test_get_architecture_with_protocol_in_connections(fake_run):
    fake_run(
        [
            [
                {
                    "services": [
                        {
                            "id": "SVC-01",
                            "name": "Backend",
                            "tech_stack": "Spring Boot",
                        }
                    ],
                    "databases": [
                        {"id": "DB-01", "name": "Primary", "tech_stack": "PostgreSQL"}
                    ],
                    "connections": [
                        {
                            "source_id": "SVC-01",
                            "target_id": "DB-01",
                            "type": "CONNECTS_TO",
                            "protocol": "JDBC",
                            "description": "DB 연결",
                        }
                    ],
                }
            ]
        ]
    )
    out = await q.get_architecture_graph("x")
    assert isinstance(out, ArchitectureGraph)
    assert out.connections[0].protocol == "JDBC"
    assert out.connections[0].description == "DB 연결"


# ─── getMeetingLogs / Versions ──────────────────────────────────


async def test_get_meeting_log_returns_one(fake_run):
    fake = fake_run(
        [
            [
                {
                    "version": "v1.0",
                    "date": "2026-05-13",
                    "meeting_content": "회의 내용",
                    "created_at": 1700000000000,
                }
            ]
        ]
    )
    out = await q.get_meeting_log("x", "v1.0")
    assert isinstance(out, MeetingLog)
    assert out.version == "v1.0"
    assert out.meeting_content == "회의 내용"
    # parameter binding (version 도 $ 로 전달)
    assert fake.calls[0]["params"] == {"project": "x", "version": "v1.0"}


async def test_get_meeting_log_returns_none_when_missing(fake_run):
    fake_run([[]])
    assert await q.get_meeting_log("x", "v99") is None


async def test_get_meeting_versions_returns_list(fake_run):
    fake_run(
        [
            [
                {"log_id": "log_x_v1_0", "version": "v1.0", "date": "2026-05-10"},
                {"log_id": "log_x_v1_1", "version": "v1.1", "date": "2026-05-12"},
            ]
        ]
    )
    out = await q.get_meeting_versions("x")
    assert len(out) == 2
    assert isinstance(out[0], MeetingVersion)
    assert out[0].version == "v1.0"
    assert out[1].version == "v1.1"


async def test_get_meeting_versions_empty(fake_run):
    fake_run([[]])
    assert await q.get_meeting_versions("x") == []


async def test_get_meeting_versions_skips_null_version_rows(fake_run):
    fake_run(
        [
            [
                {"log_id": "id-1", "version": "v1.0", "date": "x"},
                {"log_id": "id-2", "version": None, "date": "x"},  # skip
            ]
        ]
    )
    out = await q.get_meeting_versions("x")
    assert len(out) == 1


# ─── Cypher injection regression ────────────────────────────────


async def test_project_name_with_special_chars_is_parameterized(fake_run):
    """모든 7개 query 가 $param 으로 바인딩되는지 회귀 검증."""
    dangerous = "x' OR true OR '"
    fake_run([[]])
    await q.get_master_cps(dangerous)
    fake_run([[]])
    await q.get_master_prd(dangerous)
    fake_run([[]])
    await q.get_ddd_graph(dangerous)
    fake_run([[]])
    await q.get_spack_graph(dangerous)
    fake_run([[]])
    await q.get_architecture_graph(dangerous)
    fake_run([[]])
    await q.get_meeting_log(dangerous, "v1.0")
    fake_run([[]])
    await q.get_meeting_versions(dangerous)
    # 모든 호출이 $project parameter 로만 전달됐는지 — 각 fake 의 마지막 call
    # 더 강한 검증: query_repository 의 cypher 본문에 $project / $version 만 있고 보간 없음
    from app.service.query_repository import (
        _GET_CPS_CYPHER,
        _GET_PRD_CYPHER,
        _GET_DDD_CYPHER,
        _GET_SPACK_CYPHER,
        _GET_ARCHITECTURE_CYPHER,
        _GET_MEETING_LOG_CYPHER,
        _GET_MEETING_VERSIONS_CYPHER,
    )
    for cypher in [
        _GET_CPS_CYPHER,
        _GET_PRD_CYPHER,
        _GET_DDD_CYPHER,
        _GET_SPACK_CYPHER,
        _GET_ARCHITECTURE_CYPHER,
        _GET_MEETING_LOG_CYPHER,
        _GET_MEETING_VERSIONS_CYPHER,
    ]:
        assert "$project" in cypher
        assert dangerous not in cypher  # 입력값이 본문에 포함되지 않음


# ─── Timeline ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_project_timeline_merges_sources_sorted_desc(fake_run):
    """미팅 + CPS/PRD + Lint + Lineage + Repo 이벤트를 합쳐 최신 순 정렬."""
    fake_run(
        responses=[
            # _TIMELINE_MEETINGS_CYPHER
            [{"version": "v1.2", "date": "2025-05-01", "ts": 1_700_000_000_000}],
            # _TIMELINE_CPS_PRD_CYPHER
            [
                {"label": "CPS_Document", "version": "v1.2", "ts": 1_700_000_300_000},
                {"label": "PRD_Document", "version": "v1.2", "ts": 1_700_000_400_000},
            ],
            # _TIMELINE_LINT_CYPHER
            [{"score": 85, "files": 12, "ts": 1_700_000_500_000}],
            # _TIMELINE_LINEAGE_CYPHER
            [{"total_impls": 30, "missing": 5, "drift": 3, "ts": 1_700_000_600_000}],
            # _TIMELINE_REPO_CYPHER
            [
                {
                    "url": "https://github.com/o/r",
                    "role": "frontend",
                    "ts": 1_700_000_700_000,
                }
            ],
        ]
    )
    from app.service import query_repository as q
    out = await q.get_project_timeline("proj1", since_ms=1_700_000_000_000, limit=10)
    assert out.project == "proj1"
    assert len(out.events) == 6
    # 최신 순 (repo_add 가 ts 가 가장 큼)
    assert out.events[0].kind == "repo_add"
    assert out.events[-1].kind == "meeting"
    # 카운트 정확
    assert out.counts["meeting"] == 1
    assert out.counts["lint"] == 1
    assert out.counts["lineage"] == 1


@pytest.mark.asyncio
async def test_get_project_timeline_respects_limit(fake_run):
    """limit 적용. 카운트는 limit 적용된 events 기준."""
    fake_run(
        responses=[
            [
                {"version": f"v1.{i}", "date": "", "ts": 1_700_000_000_000 + i}
                for i in range(20)
            ],
            [], [], [], [],
        ]
    )
    from app.service import query_repository as q
    out = await q.get_project_timeline("p", since_ms=1_700_000_000_000, limit=5)
    assert len(out.events) == 5
    # limit slice 후 카운트라 5
    assert out.counts.get("meeting") == 5


@pytest.mark.asyncio
async def test_get_project_timeline_handles_missing_ts(fake_run):
    """ts 가 None 이면 해당 이벤트 skip (정렬 키 부재 시 안전)."""
    fake_run(
        responses=[
            [{"version": "v1", "date": "", "ts": None}],
            [], [], [], [],
        ]
    )
    from app.service import query_repository as q
    out = await q.get_project_timeline("p", since_ms=0)
    assert out.events == []


# ─── getProjectGraph (BE graph proxy) ───────────────────────────


@pytest.mark.asyncio
async def test_get_project_graph_returns_nodes_and_edges(fake_run):
    fake = fake_run(
        [
            [
                {
                    "nodes": [
                        {"id": "doc_cps_x", "label": "CPS_Document", "properties": {"project": "x", "version": "v1"}},
                        {"id": "prb_01", "label": "Problem", "properties": {"summary": "결제 실패"}},
                    ],
                    "edges": [
                        {"source_id": "prb_01", "target_id": "doc_cps_x", "type": "EXTRACTED_FROM"},
                    ],
                }
            ]
        ]
    )
    out = await q.get_project_graph("x")
    assert out.project == "x"
    assert len(out.nodes) == 2
    assert {n.id for n in out.nodes} == {"doc_cps_x", "prb_01"}
    assert len(out.edges) == 1
    assert out.edges[0].source_id == "prb_01"
    assert out.edges[0].type == "EXTRACTED_FROM"
    # parameter binding 검증
    assert fake.calls[0]["params"] == {"project": "x"}


@pytest.mark.asyncio
async def test_get_project_graph_strips_heavy_properties(fake_run):
    """embedding / full_markdown / raw_content 가 응답에서 제거되어야 함."""
    fake_run(
        [
            [
                {
                    "nodes": [
                        {
                            "id": "doc_cps_x",
                            "label": "CPS_Document",
                            "properties": {
                                "project": "x",
                                "full_markdown": "X" * 10000,
                                "embedding": [0.1] * 768,
                                "raw_content": "very large",
                            },
                        },
                    ],
                    "edges": [],
                }
            ]
        ]
    )
    out = await q.get_project_graph("x")
    assert len(out.nodes) == 1
    props = out.nodes[0].properties
    assert "embedding" not in props
    assert "full_markdown" not in props
    assert "raw_content" not in props
    assert props.get("project") == "x"


@pytest.mark.asyncio
async def test_get_project_graph_drops_dangling_edges(fake_run):
    """노드 cap 으로 잘려서 한쪽 노드가 사라지면 그 엣지는 drop."""
    fake_run(
        [
            [
                {
                    "nodes": [
                        {"id": "a", "label": "Story", "properties": {}},
                    ],
                    "edges": [
                        {"source_id": "a", "target_id": "missing_b", "type": "TRIGGERS"},
                        {"source_id": "ghost", "target_id": "a", "type": "FOO"},
                    ],
                }
            ]
        ]
    )
    out = await q.get_project_graph("x")
    assert len(out.nodes) == 1
    assert out.edges == []  # 양쪽 다 valid 아니면 drop


@pytest.mark.asyncio
async def test_get_project_graph_falls_back_when_primary_fails(monkeypatch):
    """APOC 미설치 등으로 primary 쿼리 실패하면 fallback 으로 재시도."""
    calls: List[str] = []

    async def fake_cypher(cypher, params=None, database=None):
        calls.append(cypher)
        if len(calls) == 1:
            # 1번째 = primary (apoc 사용) → 실패
            raise RuntimeError("apoc.map.removeKeys not found")
        # 2번째 = fallback → 성공
        return [{
            "nodes": [{"id": "a", "label": "Story", "properties": {"full_markdown": "X"}}],
            "edges": [],
        }]

    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake_cypher
    )
    out = await q.get_project_graph("x")
    assert len(calls) == 2
    assert "apoc" in calls[0]
    assert "apoc" not in calls[1]
    # fallback 경로에서도 헤비 속성은 _strip_heavy 로 제거됨
    assert "full_markdown" not in out.nodes[0].properties


@pytest.mark.asyncio
async def test_get_project_graph_empty_result(fake_run):
    fake_run([[]])
    out = await q.get_project_graph("empty")
    assert out.project == "empty"
    assert out.nodes == []
    assert out.edges == []


@pytest.mark.asyncio
async def test_get_project_graph_skips_invalid_node_shapes(fake_run):
    fake_run(
        [
            [
                {
                    "nodes": [
                        {"id": "good", "label": "Story", "properties": {}},
                        {"id": "", "label": "Empty", "properties": {}},   # empty id → skip
                        {"id": "no_label", "label": "", "properties": {}},  # empty label → skip
                        "not_a_dict",
                    ],
                    "edges": [
                        {"source_id": "good", "target_id": "good", "type": "SELF"},
                        {"source_id": "good", "target_id": "good", "type": ""},  # empty type → skip
                        "not_a_dict",
                    ],
                }
            ]
        ]
    )
    out = await q.get_project_graph("x")
    assert len(out.nodes) == 1
    assert out.nodes[0].id == "good"
    assert len(out.edges) == 1
    assert out.edges[0].type == "SELF"


# ─── getScreenSubgraph (BE 화면별 PRD 서브그래프) ─────────────────


@pytest.mark.asyncio
async def test_get_screen_subgraph_returns_screen_story_epic(fake_run):
    """기본 케이스: Screen + 연결된 Story + 포함 Epic 반환."""
    fake = fake_run(
        [
            [
                {
                    "nodes": [
                        {"id": "screen:메인 화면", "label": "Screen", "properties": {"name": "메인 화면", "project": "food"}},
                        {"id": "S-1.1", "label": "Story", "properties": {"summary": "점심 추천"}},
                        {"id": "E-01", "label": "Epic", "properties": {"summary": "점심 추천 도메인"}},
                    ],
                    "edges": [
                        {"source_id": "S-1.1", "target_id": "screen:메인 화면", "type": "IMPLEMENTED_ON"},
                        {"source_id": "E-01", "target_id": "S-1.1", "type": "CONTAINS"},
                    ],
                }
            ]
        ]
    )
    out = await q.get_screen_subgraph("food", "메인 화면")
    assert out.project == "food"
    assert {n.id for n in out.nodes} == {"screen:메인 화면", "S-1.1", "E-01"}
    assert {n.label for n in out.nodes} == {"Screen", "Story", "Epic"}
    assert len(out.edges) == 2
    # 파라미터 바인딩 — screen_name 까지 잘 전달됐는지
    assert fake.calls[0]["params"] == {"project": "food", "screen_name": "메인 화면"}


@pytest.mark.asyncio
async def test_get_screen_subgraph_screen_without_stories(fake_run):
    """Screen 만 있고 연결 Story 없으면 Screen 노드 1개만 반환 (엣지는 0)."""
    fake_run(
        [
            [
                {
                    "nodes": [
                        {"id": "screen:고립", "label": "Screen", "properties": {"name": "고립", "project": "p"}},
                    ],
                    "edges": [],
                }
            ]
        ]
    )
    out = await q.get_screen_subgraph("p", "고립")
    assert len(out.nodes) == 1
    assert out.nodes[0].label == "Screen"
    assert out.edges == []


@pytest.mark.asyncio
async def test_get_screen_subgraph_unknown_screen(fake_run):
    """존재하지 않는 Screen → nodes=[] / edges=[]."""
    fake_run([[]])
    out = await q.get_screen_subgraph("p", "없는 화면")
    assert out.nodes == []
    assert out.edges == []


@pytest.mark.asyncio
async def test_get_screen_subgraph_strips_heavy_properties(fake_run):
    """fallback 경로에서도 embedding/full_markdown 등은 응답에서 제거되어야 함."""
    fake_run(
        [
            [
                {
                    "nodes": [
                        {
                            "id": "S-1.1",
                            "label": "Story",
                            "properties": {
                                "summary": "ok",
                                "embedding": [0.1] * 768,
                                "full_markdown": "X" * 5000,
                            },
                        },
                    ],
                    "edges": [],
                }
            ]
        ]
    )
    out = await q.get_screen_subgraph("p", "any")
    props = out.nodes[0].properties
    assert "embedding" not in props
    assert "full_markdown" not in props
    assert props.get("summary") == "ok"


@pytest.mark.asyncio
async def test_get_screen_subgraph_falls_back_when_primary_fails(monkeypatch):
    """APOC 미설치 등으로 primary 실패 시 fallback Cypher 재시도."""
    calls: List[str] = []

    async def fake_cypher(cypher, params=None, database=None):
        calls.append(cypher)
        if len(calls) == 1:
            raise RuntimeError("apoc.map.removeKeys not found")
        return [{
            "nodes": [{"id": "screen:x", "label": "Screen", "properties": {}}],
            "edges": [],
        }]

    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake_cypher
    )
    out = await q.get_screen_subgraph("p", "x")
    assert len(calls) == 2
    assert "apoc" in calls[0]
    assert "apoc" not in calls[1]
    assert len(out.nodes) == 1


@pytest.mark.asyncio
async def test_get_screen_subgraph_drops_dangling_edges(fake_run):
    """node cap 후 dangling 된 엣지는 drop — 기존 get_project_graph 와 동일 정책."""
    fake_run(
        [
            [
                {
                    "nodes": [
                        {"id": "screen:s", "label": "Screen", "properties": {}},
                    ],
                    "edges": [
                        {"source_id": "screen:s", "target_id": "ghost_story", "type": "IMPLEMENTED_ON"},
                    ],
                }
            ]
        ]
    )
    out = await q.get_screen_subgraph("p", "s")
    assert len(out.nodes) == 1
    assert out.edges == []


@pytest.mark.asyncio
async def test_get_screen_subgraph_falls_back_to_markdown(monkeypatch):
    """Cypher 결과가 비면 PRD markdown 의 Screen→Story 매핑으로 보강 조회."""
    calls: List[Dict[str, Any]] = []

    async def fake_cypher(cypher, params=None, database=None):
        calls.append({"cypher": cypher, "params": params or {}})
        # 1차 traverse → 빈 결과
        if "IMPLEMENTED_ON" in cypher and "story_ids" not in cypher:
            return [{"nodes": [], "edges": []}]
        # Phase 2 fuzzy 매칭: 프로젝트 전체 Story id 조회 (RETURN s.id AS id)
        if "RETURN s.id AS id" in cypher:
            return [
                {"id": "story_01_1"},
                {"id": "story_03_2"},
                {"id": "story_03_1"},
            ]
        # 2차 by-ids → Story + Epic 반환
        if "story_ids" in cypher:
            return [{
                "nodes": [
                    {"id": "story_01_1", "label": "Story", "properties": {"summary": "추천 카드"}},
                    {"id": "story_03_2", "label": "Story", "properties": {"summary": "길찾기"}},
                    {"id": "epic_01", "label": "Epic", "properties": {"summary": "메인 화면 도메인"}},
                ],
                "edges": [
                    {"source_id": "epic_01", "target_id": "story_01_1", "type": "CONTAINS"},
                    {"source_id": "epic_01", "target_id": "story_03_2", "type": "CONTAINS"},
                ],
            }]
        return []

    async def fake_master_prd(project):
        from app.service.query_repository import PrdMaster
        # PRD 마크다운에 Screen Architecture 섹션 포함 — Story 1.1, 3.2 가 메인 화면에 연결.
        md = """
### 3. Screen Architecture (화면별 구현 명세)
#### 🖥️ [Screen: 메인 화면]
- 포함된 기능:
  - [Story 1.1] 직장인이 추천 카드를 본다.
  - [Story 3.2] 카카오맵으로 길찾기.
#### 🖥️ [Screen: 설정 화면]
- 포함된 기능:
  - [Story 3.1] 회사 위치 등록.
"""
        return PrdMaster(master_prd_id="doc_prd_master", prd_content=md)

    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", fake_cypher
    )
    monkeypatch.setattr(
        "app.service.query_repository.get_master_prd", fake_master_prd
    )

    out = await q.get_screen_subgraph("food", "메인 화면")
    # Story 2개 + Epic 1개 + 합성 Screen 1개 = 4 노드
    assert len(out.nodes) == 4
    labels = {n.label for n in out.nodes}
    assert labels == {"Story", "Epic", "Screen"}
    # 합성 Screen ID 'screen:메인 화면' 가 존재
    assert any(n.id == "screen:메인 화면" for n in out.nodes)
    # IMPLEMENTED_ON 합성 엣지 2개 (각 Story → Screen) + CONTAINS 2개
    assert len([e for e in out.edges if e.type == "IMPLEMENTED_ON"]) == 2
    assert len([e for e in out.edges if e.type == "CONTAINS"]) == 2


@pytest.mark.asyncio
async def test_get_screen_subgraph_markdown_fallback_no_screen(monkeypatch):
    """Cypher 비고 markdown 에서도 screen 섹션 못 찾으면 끝까지 빈 결과."""
    async def fake_cypher(cypher, params=None, database=None):
        return [{"nodes": [], "edges": []}]

    async def fake_master_prd(project):
        from app.service.query_repository import PrdMaster
        return PrdMaster(master_prd_id="x", prd_content="### 3. Screen Architecture\n#### 🖥️ [Screen: 다른 화면]\n- [Story 1.1] x")

    monkeypatch.setattr("app.service.query_repository.neo4j_client.run_cypher", fake_cypher)
    monkeypatch.setattr("app.service.query_repository.get_master_prd", fake_master_prd)

    out = await q.get_screen_subgraph("p", "찾을 수 없는 화면")
    assert out.nodes == []
    assert out.edges == []


def test_extract_screen_story_ids_basic():
    """다양한 Story 표기 정규화 — Story 1.1 / Story-01.1 / `[Story-01.1]` 모두 흡수."""
    from app.service.query_repository import _extract_screen_story_ids_from_markdown
    md = """
### 3. Screen Architecture
#### 🖥️ [Screen: 메인 화면]
- [Story 1.1] 한 자리 major
- `[Story-02.3]` 백틱 + dash
- [Story-10.5] 두 자리 major
#### 🖥️ [Screen: 설정 화면]
- [Story 9.9] 이건 다른 화면이라 무시되어야 함
"""
    ids = _extract_screen_story_ids_from_markdown(md, "메인 화면")
    # [2026-05] zero-pad 변형 후보 모두 생성 — 다양한 LLM 출력 형식 흡수.
    # zero-pad 형태가 모두 포함됐는지만 검증.
    assert "story_01_1" in ids
    assert "story_02_3" in ids
    assert "story_10_5" in ids


def test_extract_screen_story_ids_unknown_screen():
    from app.service.query_repository import _extract_screen_story_ids_from_markdown
    assert _extract_screen_story_ids_from_markdown("### 3. Screen\n#### [Screen: A]\n- [Story 1.1]", "B") == []
    assert _extract_screen_story_ids_from_markdown("", "any") == []
    assert _extract_screen_story_ids_from_markdown(None, "any") == []


# ─── [2026-05-28] Phase 2 fuzzy 매칭 — Story id 형식 변형 흡수 ──────────────────

def test_parse_story_major_minor_variants():
    """다양한 LLM 출력 형식의 Story id 에서 (major, minor) 페어 추출."""
    from app.service.query_repository import _parse_story_major_minor
    # zero-pad 변형
    assert _parse_story_major_minor("story_1_1") == (1, 1)
    assert _parse_story_major_minor("story_01_1") == (1, 1)
    assert _parse_story_major_minor("story_001_001") == (1, 1)
    assert _parse_story_major_minor("story_10_5") == (10, 5)
    # separator 변형 (`-`, `.`, 공백)
    assert _parse_story_major_minor("story-1-1") == (1, 1)
    assert _parse_story_major_minor("story.1.1") == (1, 1)
    assert _parse_story_major_minor("Story 2.3") == (2, 3)
    # 다른 prefix
    assert _parse_story_major_minor("s_1_1") == (1, 1)
    assert _parse_story_major_minor("US-2-1") == (2, 1)
    # 매칭 실패 — 정수가 1개 또는 0개
    assert _parse_story_major_minor("story_1") is None
    assert _parse_story_major_minor("") is None
    assert _parse_story_major_minor(None) is None
    # Epic 패턴은 결과적으로 1개 정수만 있어 None
    assert _parse_story_major_minor("epic_01") is None


def test_extract_screen_story_pairs_basic():
    """markdown 에서 (major, minor) 페어 추출 — 옛 ID 후보 함수와 같은 입력, 정수 페어 출력."""
    from app.service.query_repository import _extract_screen_story_pairs_from_markdown
    md = """
### 3. Screen Architecture
#### 🖥️ [Screen: 메인 화면]
- [Story 1.1] a
- [Story-02.3] b
- [Story 10.5] c
#### 🖥️ [Screen: 설정 화면]
- [Story 9.9] 무시
"""
    pairs = _extract_screen_story_pairs_from_markdown(md, "메인 화면")
    assert (1, 1) in pairs
    assert (2, 3) in pairs
    assert (10, 5) in pairs
    assert (9, 9) not in pairs


def test_extract_screen_story_pairs_backtick_format():
    """[2026-06] 현 prd_extract 포맷 — '포함된 기능' 목록의 Story 참조가 백틱('`Story 1.1`').
    대괄호만 보던 기존 regex 가 0건 매칭 → 'no_implemented_on' 빈 그래프 버그 회귀 방지."""
    from app.service.query_repository import _extract_screen_story_pairs_from_markdown
    md = """
### 3. Screen Architecture
#### 🖥️ [Screen: 데이터 소스 관리]
- **포함된 기능**:
  - `Story 1.1` 데이터 소스 연결 설정 (from Epic 1)
- **화면 흐름**: '메인 메뉴' → '데이터 소스 관리' 화면 진입 → ...
#### 🖥️ [Screen: 통합 대시보드]
- **포함된 기능**:
  - `Story 2.1` 통합 대시보드 조회 (from Epic 2)
"""
    assert _extract_screen_story_pairs_from_markdown(md, "데이터 소스 관리") == [(1, 1)]
    assert _extract_screen_story_pairs_from_markdown(md, "통합 대시보드") == [(2, 1)]


def test_extract_screen_story_pairs_quoted_screen_in_user_flow():
    """[2026-06] Screen Architecture 섹션이 없고 화면명이 User Flow 안에 따옴표로 감싸
    ('데이터 소스 관리' 화면) 등장하는 경우도 Phase 2 inline 매칭으로 흡수."""
    from app.service.query_repository import _extract_screen_story_pairs_from_markdown
    md = """
#### 📦 Epic 1
- **[Story 1.1] 데이터 소스 연결 설정**
  - **User Flow**: 1. 관리자가 '데이터 소스 관리' 화면에서 '새 소스 추가' 버튼을 클릭한다.
- **[Story 2.1] 통합 대시보드 조회**
  - **User Flow**: 1. 관리자가 '통합 대시보드' 화면으로 이동한다.
"""
    assert _extract_screen_story_pairs_from_markdown(md, "데이터 소스 관리") == [(1, 1)]
    assert _extract_screen_story_pairs_from_markdown(md, "통합 대시보드") == [(2, 1)]


@pytest.mark.asyncio
async def test_get_screen_subgraph_fuzzy_matches_padded_story_ids(monkeypatch):
    """[크리티컬 버그픽스] markdown [Story 1.1] 이 Neo4j 의 'story_001_001' (3-digit pad) 와
    매칭되도록 (major, minor) 페어 fuzzy 매칭. 이전 4-variant 매칭으로는 흡수 안 되던 케이스."""

    async def fake_cypher(cypher, params=None, database=None):
        # 1차 traverse → 빈 결과 (LLM 이 IMPLEMENTED_ON 엣지 누락)
        if "IMPLEMENTED_ON" in cypher and "story_ids" not in cypher:
            return [{"nodes": [], "edges": []}]
        # Phase 2: 프로젝트 전체 Story id 조회 — LLM 이 3-digit pad 로 저장한 상황
        if "RETURN s.id AS id" in cypher:
            return [
                {"id": "story_001_001"},  # major.minor = 1.1 — 매칭되어야 함
                {"id": "story_002_005"},  # 2.5 — 매칭 X
            ]
        # by-ids cypher → 매칭된 ID 로 Story+Epic 반환
        if "story_ids" in cypher:
            return [{
                "nodes": [
                    {"id": "story_001_001", "label": "Story", "properties": {"summary": "추천"}},
                    {"id": "epic_01", "label": "Epic", "properties": {"summary": "도메인"}},
                ],
                "edges": [
                    {"source_id": "epic_01", "target_id": "story_001_001", "type": "CONTAINS"},
                ],
            }]
        return []

    async def fake_master_prd(project):
        from app.service.query_repository import PrdMaster
        md = """
### 3. Screen Architecture
#### 🖥️ [Screen: 테스트 케이스 관리]
- [Story 1.1] 케이스 등록
"""
        return PrdMaster(master_prd_id="doc_prd_master", prd_content=md)

    monkeypatch.setattr("app.service.query_repository.neo4j_client.run_cypher", fake_cypher)
    monkeypatch.setattr("app.service.query_repository.get_master_prd", fake_master_prd)

    out = await q.get_screen_subgraph("p", "테스트 케이스 관리")
    assert out.reason == "ok"
    # 합성 Screen + Story + Epic
    assert len(out.nodes) == 3
    assert any(n.id == "screen:테스트 케이스 관리" for n in out.nodes)
    assert any(n.id == "story_001_001" for n in out.nodes)
    # IMPLEMENTED_ON 합성 엣지
    assert any(e.type == "IMPLEMENTED_ON" for e in out.edges)


@pytest.mark.asyncio
async def test_get_screen_subgraph_returns_stories_match_no_data_with_debug(monkeypatch):
    """페어 매칭 0건이면 stories_match_no_data 와 함께 디버깅 정보 (attempted/existing) 반환."""

    async def fake_cypher(cypher, params=None, database=None):
        if "IMPLEMENTED_ON" in cypher and "story_ids" not in cypher:
            return [{"nodes": [], "edges": []}]
        if "RETURN s.id AS id" in cypher:
            # markdown 은 (1,1) 을 원하지만 Neo4j 엔 (9,9) 만 있음 — 진짜 동기화 깨짐
            return [{"id": "story_9_9"}]
        return []

    async def fake_master_prd(project):
        from app.service.query_repository import PrdMaster
        return PrdMaster(
            master_prd_id="x",
            prd_content="### 3. Screen\n#### 🖥️ [Screen: A]\n- [Story 1.1] x",
        )

    monkeypatch.setattr("app.service.query_repository.neo4j_client.run_cypher", fake_cypher)
    monkeypatch.setattr("app.service.query_repository.get_master_prd", fake_master_prd)

    out = await q.get_screen_subgraph("p", "A")
    assert out.reason == "stories_match_no_data"
    assert out.nodes == []
    assert out.debug is not None
    assert out.debug.get("attempted_pairs") == [[1, 1]]
    assert "story_9_9" in (out.debug.get("existing_story_ids_in_neo4j") or [])


# ─── [D — 2026-05] Design Lineage Graph ──────────────────────


async def test_get_design_lineage_graph_all_returns_full_subgraph(fake_run):
    """focus_story_id=None 일 때 project 의 모든 DERIVED_FROM 엣지 반환."""
    fake = fake_run(
        [
            [
                {
                    "nodes": [
                        {"id": "ENT-01", "label": "Entity", "properties": {"name": "Ticket"}},
                        {"id": "SVC-01", "label": "ArchService", "properties": {"name": "Backend"}},
                        {"id": "story_01_1", "label": "Story", "properties": {"summary": "발행"}},
                        {"id": "epic_01", "label": "Epic", "properties": {"name": "결제"}},
                    ],
                    "edges": [
                        {
                            "source_id": "ENT-01", "target_id": "story_01_1",
                            "type": "DERIVED_FROM",
                            "properties": {"confidence": "direct", "quote": "티켓 발행"},
                        },
                        {
                            "source_id": "SVC-01", "target_id": "story_01_1",
                            "type": "DERIVED_FROM",
                            "properties": {"confidence": "inferred", "quote": "백엔드 처리"},
                        },
                        {
                            "source_id": "epic_01", "target_id": "story_01_1",
                            "type": "CONTAINS", "properties": {},
                        },
                    ],
                }
            ]
        ]
    )
    out = await q.get_design_lineage_graph("food")
    assert out.project == "food"
    assert {n.id for n in out.nodes} == {"ENT-01", "SVC-01", "story_01_1", "epic_01"}
    derived = [e for e in out.edges if e.type == "DERIVED_FROM"]
    assert len(derived) == 2
    # edge properties 보존 — D 의 핵심
    ent_edge = next(e for e in derived if e.source_id == "ENT-01")
    assert ent_edge.properties["confidence"] == "direct"
    assert ent_edge.properties["quote"] == "티켓 발행"


async def test_get_design_lineage_graph_includes_implements_api_edges(fake_run):
    """[2026-06-13] API(→Story 는 IMPLEMENTS)도 lineage 그래프에 포함 — 이전엔
    DERIVED_FROM 만 매칭해 API 노드가 '관계선 없는 고립'으로 보였다."""
    fake_run(
        [
            [
                {
                    "nodes": [
                        {"id": "API-01", "label": "API", "properties": {"name": "POST /tickets"}},
                        {"id": "story_01_1", "label": "Story", "properties": {"summary": "발행"}},
                    ],
                    "edges": [
                        {
                            "source_id": "API-01", "target_id": "story_01_1",
                            "type": "IMPLEMENTS",
                            "properties": {"confidence": "direct"},
                        },
                    ],
                }
            ]
        ]
    )
    out = await q.get_design_lineage_graph("food")
    impl = [e for e in out.edges if e.type == "IMPLEMENTS"]
    assert len(impl) == 1
    assert impl[0].source_id == "API-01" and impl[0].target_id == "story_01_1"


def test_lineage_cypher_matches_both_rel_types():
    """정적 가드 — lineage 쿼리가 DERIVED_FROM 과 IMPLEMENTS 둘 다 매칭해야.
    (DERIVED_FROM 만 두면 API lineage 가 통째로 사라지는 회귀 방지.)"""
    from app.service.query_repository import (
        _LINEAGE_GRAPH_ALL_CYPHER,
        _LINEAGE_GRAPH_BY_STORY_CYPHER,
    )
    assert "DERIVED_FROM|IMPLEMENTS" in _LINEAGE_GRAPH_ALL_CYPHER
    assert "DERIVED_FROM|IMPLEMENTS" in _LINEAGE_GRAPH_BY_STORY_CYPHER


async def test_get_design_lineage_graph_nodes_but_no_edges_sets_debug(fake_run):
    """[2026-06-13 관측성] 노드>0 + 엣지=0 이면 reason=ok 이되 debug 로 단서 노출."""
    fake_run(
        [
            [
                {
                    "nodes": [
                        {"id": "ENT-01", "label": "Entity", "properties": {"name": "Ticket"}},
                    ],
                    "edges": [],
                }
            ]
        ]
    )
    out = await q.get_design_lineage_graph("food")
    assert out.reason == "ok"
    assert out.debug and out.debug.get("edge_count") == 0


async def test_get_design_lineage_graph_focused_by_story(fake_run):
    """focus_story_id 지정 시 해당 Story 중심 서브그래프만 + params 정확 전달."""
    fake = fake_run([[{"nodes": [], "edges": []}]])
    await q.get_design_lineage_graph("food", focus_story_id="story_02_3")
    assert fake.calls[0]["params"] == {
        "project": "food", "focus_story_id": "story_02_3",
    }


async def test_get_design_lineage_graph_empty(fake_run):
    """lineage 데이터 없으면 빈 그래프 — FE 빈 상태 안내용."""
    fake_run([[{"nodes": [], "edges": []}]])
    out = await q.get_design_lineage_graph("food")
    assert out.nodes == []
    assert out.edges == []


# ─── [2026-05] Screen subgraph fallback v2 + reason ────────


async def test_extract_screen_story_ids_from_user_flow():
    """현 prd_extract 형식: Story block 안 User Flow 에 화면명 인라인."""
    from app.service.query_repository import _extract_screen_story_ids_from_markdown
    md = """
### 2. Epics & User Stories
#### 📦 Epic 1: 결제
- **[Story 1.1] 티켓 발행**
  - **User Story**: 사용자는 펀딩 미달성 시 티켓을 받는다.
  - **User Flow**: 1. 사용자가 [홈] 화면에서 종료 알림을 본다. → 2. 티켓 발행.
- **[Story 1.2] 환불 처리**
  - **User Flow**: 1. 사용자가 [마이페이지] 화면에서 환불 요청한다.
"""
    # zero-pad 형태가 후보에 포함됐는지 — 후보 multi-form 으로 변경됨
    assert "story_01_1" in _extract_screen_story_ids_from_markdown(md, "홈")
    assert "story_01_2" in _extract_screen_story_ids_from_markdown(md, "마이페이지")


async def test_extract_screen_story_ids_no_match_when_screen_absent():
    """화면명이 PRD 어디에도 없으면 빈 list."""
    from app.service.query_repository import _extract_screen_story_ids_from_markdown
    md = "- **[Story 1.1] 발행**\n  - **User Flow**: 1. 사용자가 [홈] 화면에서..."
    assert _extract_screen_story_ids_from_markdown(md, "결제완료") == []


async def test_extract_screen_story_ids_false_positive_guard():
    """'홈' 이라는 단어가 다른 문맥 (예: '홈페이지') 에 있어도 매칭 안 됨 — '화면' 단어 인접 필수."""
    from app.service.query_repository import _extract_screen_story_ids_from_markdown
    md = """
- **[Story 1.1] 홈페이지 개선**
  - **User Story**: 홈페이지 디자인 변경.
"""
    # '홈' 으로 검색해도 매칭 X — '홈 화면' 또는 '[홈] 화면' 패턴이어야
    assert _extract_screen_story_ids_from_markdown(md, "홈") == []


async def test_extract_screen_story_ids_legacy_screen_architecture_still_works():
    """옛 PRD: Screen Architecture 섹션이 있는 형식도 호환."""
    from app.service.query_repository import _extract_screen_story_ids_from_markdown
    md = """
### 3. Screen Architecture
#### 🖥️ [Screen: 메인 화면]
- 포함된 기능:
  - [Story 1.1] 발행
  - [Story-02.3] 정산
"""
    out = _extract_screen_story_ids_from_markdown(md, "메인 화면")
    # zero-pad 형태가 후보에 포함된지 검증 (post-2026-05: multi-form)
    assert "story_01_1" in out
    assert "story_02_3" in out


async def test_get_screen_subgraph_reason_ok_when_data_present(fake_run):
    """1차 cypher 성공 시 reason='ok'."""
    fake_run([
        [{
            "nodes": [{"id": "story_01_1", "label": "Story", "properties": {}}],
            "edges": [],
        }]
    ])
    out = await q.get_screen_subgraph("p", "홈")
    assert out.reason == "ok"


async def test_get_screen_subgraph_reason_no_implemented_on(fake_run, monkeypatch):
    """1차 cypher 빈 결과 + markdown fallback 도 매칭 0 → no_implemented_on."""
    # 1차 cypher = 빈 결과, get_master_prd 호출 mock
    fake_run([
        [{"nodes": [], "edges": []}],   # primary screen subgraph
    ])

    async def fake_master_prd(p):
        from app.service.query_repository import PrdMaster
        return PrdMaster(
            project=p, id="doc_prd_master_p",
            prd_content="### no screen mentions here",
            created_at_ms=0, updated_at_ms=0,
        )
    monkeypatch.setattr(q, "get_master_prd", fake_master_prd)

    out = await q.get_screen_subgraph("p", "어디에도없는화면")
    assert out.reason == "no_implemented_on"


async def test_get_design_lineage_graph_reason_no_design(fake_run):
    """lineage graph 빈 + design 노드 자체 0개 → no_design."""
    fake_run([
        [{"nodes": [], "edges": []}],     # lineage all cypher
        [{"n_design": 0}],                # design 노드 카운트
    ])
    out = await q.get_design_lineage_graph("p")
    assert out.reason == "no_design"


async def test_get_design_lineage_graph_reason_no_lineage(fake_run):
    """lineage graph 빈 + design 노드는 있음 → no_lineage (옛 design)."""
    fake_run([
        [{"nodes": [], "edges": []}],
        [{"n_design": 5}],
    ])
    out = await q.get_design_lineage_graph("p")
    assert out.reason == "no_lineage"


async def test_get_design_lineage_graph_api_only_not_no_design(fake_run):
    """[2026-06-13] API 노드만 있는 프로젝트(IMPLEMENTS 누락)는 no_design 이 아니라
    no_lineage — design_check 가 API 라벨도 세야 한다."""
    fake = fake_run([
        [{"nodes": [], "edges": []}],   # lineage 빈 결과
        [{"n_design": 3}],              # design_check 가 API 3개를 셈
    ])
    out = await q.get_design_lineage_graph("p")
    assert out.reason == "no_lineage"
    # design_check 쿼리가 API 라벨을 포함해야 (API-only 프로젝트 오분류 방지)
    assert "n:API" in fake.calls[1]["cypher"]



    """LLM 의 zero-pad 변형 차이를 흡수하기 위해 4 후보 모두 생성."""
    from app.service.query_repository import _story_id_candidates
    out = _story_id_candidates("2", "1")
    assert "story_2_1" in out      # raw
    assert "story_02_1" in out     # major pad
    assert "story_2_01" in out     # minor pad
    assert "story_02_01" in out    # both pad


def test_story_id_candidates_no_dup_when_already_padded():
    """이미 두 자리면 후보가 중복 없이 적게."""
    from app.service.query_repository import _story_id_candidates
    out = _story_id_candidates("12", "3")
    # 12 는 이미 두 자리 → major-pad 와 raw 같음
    assert "story_12_3" in out
    assert "story_12_03" in out


# ─── [2026-05-18 Phase 1 동시접속] meeting_log_exists ──────────


async def test_meeting_log_exists_returns_true_when_found(fake_run):
    """Neo4j 에 같은 (project, version) Meeting_Log 존재 → True."""
    fake_run([[{"id": "log_proj_v1_1"}]])
    assert await q.meeting_log_exists("proj", "v1.1") is True


async def test_meeting_log_exists_returns_false_when_missing(fake_run):
    """Neo4j 응답 비어있음 → False (안전하게 진행 가능)."""
    fake_run([[]])
    assert await q.meeting_log_exists("proj", "v1.2") is False


async def test_meeting_log_exists_short_circuits_on_empty_inputs(fake_run):
    """빈 project / version 은 Cypher 호출 자체 안 함 (방어적 가드)."""
    fake = fake_run([])
    assert await q.meeting_log_exists("", "v1.0") is False
    assert await q.meeting_log_exists("proj", "") is False
    assert await q.meeting_log_exists(None, None) is False
    # cypher 호출 0회 — short-circuit 검증
    assert fake.calls == []
