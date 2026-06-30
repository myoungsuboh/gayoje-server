"""
Design 파이프라인 end-to-end (Gemini/Neo4j fake).

검증 포인트:
  - PRD 마스터 부재 시 ValueError
  - 3개 Agent 가 순차 실행 (Spack → DDD → Architecture)
  - DDD/Architecture 프롬프트에 upstream agent 결과가 포함됨 (의존성 보존)
  - 3개 Cypher save 가 순서대로 실행
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.design_pipeline import (
    DesignInput,
    DesignPipelineCancelled,
    run_design_pipeline,
)
from tests.conftest import FakeGemini, FakeNeo4j


pytestmark = pytest.mark.asyncio


_PRD_MASTER_MD = """\
## 🗺️ Master PRD

### 1. Product Overview (통합 제품 비전)
- **통합 비전**: 펀딩 플랫폼

### 2. Epic & User Story Map (기능 계층도)
#### 📦 [Epic-01] 티켓 관리
- `[Story-01.1]` 티켓 발행 ➡️ *(구현 화면: Home)*

### 3. Screen Architecture (화면별 구현 명세)
#### 🖥️ [Screen: Home]
- **포함된 기능**:
  - `[Story-01.1]` 티켓 발행

### 4. Global Non-Functional Requirements (공통 제약 사항)
- **공통 규칙**:
  - 응답 시간 1초 이내
"""


def _responder(spy: dict):
    """프롬프트로 단계 식별 + spy 에 upstream 참조 캡처."""

    def respond(prompt: str) -> str:
        if "수석 테크니컬 아키텍트" in prompt:
            spy["spack_prompt"] = prompt
            return json.dumps(
                {
                    "apis": [
                        {
                            "id": "API-01",
                            "name": "issue",
                            "method": "POST",
                            "endpoint": "/tickets",
                            "description": "티켓 발행",
                            "related_story_id": "Story-01.1",
                        }
                    ],
                    "entities": [
                        {
                            "id": "ENT-01",
                            "name": "Ticket",
                            "attributes": ["id"],
                            "description": "티켓",
                        }
                    ],
                    "policies": [
                        {
                            "id": "POL-01",
                            "category": "Performance",
                            "description": "1초 이내",
                            "related_entity": "Ticket",
                        }
                    ],
                }
            )
        if "수석 도메인 아키텍트" in prompt:
            spy["ddd_prompt"] = prompt
            return json.dumps(
                {
                    "contexts": [{"id": "CTX-01", "name": "Ticket Context", "description": "d"}],
                    "aggregates": [
                        {
                            "id": "AGG-01",
                            "name": "Ticket",
                            "context_id": "CTX-01",
                            "description": "d",
                            # [2026-05-27] lineage 없으면 normalize 가 confidence=none 으로
                            # 채우고 코드-입력 필터(filter_ddd_for_codegen)가 제외함.
                            # 실제 LLM 은 근거를 달아 줌 — fixture 현실화.
                            "lineage": {
                                "confidence": "direct",
                                "related_stories": [
                                    {"story_id": "Story-01.1", "quote": "티켓 발행"},
                                ],
                            },
                        }
                    ],
                    "entities": [],
                    "events": [],
                    "spack_entity_mapping": [
                        {
                            "spack_entity_id": "ENT-01",
                            "spack_name": "Ticket",
                            "ddd_location": "AGG-01",
                            "ddd_role": "aggregate_root",
                        }
                    ],
                }
            )
        if "수석 클라우드 시스템 아키텍트" in prompt:
            spy["arch_prompt"] = prompt
            return json.dumps(
                {
                    "services": [
                        {
                            "id": "SVC-01",
                            "name": "Front",
                            "type": "Frontend",
                            "tech_stack": "Vue.js",
                            "description": "d",
                        },
                        {
                            "id": "SVC-02",
                            "name": "Ticket Service",
                            "type": "Backend API",
                            "tech_stack": "Spring Boot",
                            "description": "d",
                            "owned_aggregates": ["Ticket"],
                        },
                    ],
                    "databases": [],
                    "connections": [],
                    "api_service_mapping": [
                        {"api_id": "API-01", "service_id": "SVC-02", "reason": "d"}
                    ],
                }
            )
        raise AssertionError(f"unexpected design prompt: {prompt[:120]}")

    return respond


async def test_full_design_pipeline_executes_three_agents_in_sequence():
    spy: dict = {}
    gemini = FakeGemini(_responder(spy))
    neo = FakeNeo4j(
        responses=[
            # Get PRD
            [
                {
                    "master_prd_id": "doc_prd_master_harness",
                    "prd_content": _PRD_MASTER_MD,
                    "last_updated": 0,
                    "related_master_cps_id": "doc_cps_master_harness",
                    "absorbed_prd_ids": [],
                }
            ],
            # Save Spack
            [{"Status": "Spack Sync Completed", "ProjectName": "harness"}],
            # Save DDD
            [{"Status": "DDD Sync Completed", "ProjectName": "harness"}],
            # Save Architecture
            [{"Status": "Architecture Sync Completed", "ProjectName": "harness"}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="d1")

    result = await run_design_pipeline(ctx, DesignInput(project_name="harness"))

    assert result.project_name == "harness"
    assert result.master_prd_id == "doc_prd_master_harness"
    assert result.spack["apis"][0]["id"] == "API-01"
    assert result.ddd["aggregates"][0]["name"] == "Ticket"
    assert result.architecture["services"][1]["owned_aggregates"] == ["Ticket"]

    # ★중요★ 순차 실행 + 의존성 보존 검증
    # DDD 프롬프트는 Spack Entity (Aggregate name source of truth) 를 포함해야 함.
    # [2026-05-27 perf] DDD 입력에서 APIs/policies 는 slim 으로 제외됨 — DDD 가 실제
    # 사용하는 건 entity id/name/description 뿐. spack_entity_mapping 의 spack_entity_id
    # 가 ENT-xx 라는 사실로도 API 미사용 확인됨.
    assert '"id": "ENT-01"' in spy["ddd_prompt"]
    assert "Ticket" in spy["ddd_prompt"]
    # Architecture 프롬프트는 Spack API + DDD Aggregate 둘 다 포함해야 함
    # (api_service_mapping + owned_aggregates 가 Architecture 의 출력 의존성).
    assert '"id": "API-01"' in spy["arch_prompt"]
    assert '"id": "AGG-01"' in spy["arch_prompt"]

    # Cypher 호출 순서: Get PRD → Save Spack → Save DDD → Save Architecture
    cyphers = [e["cypher"] for e in neo.executed]
    assert "MATCH (m:PRD_Document" in cyphers[0]
    assert "Spack Sync Completed" in cyphers[1]
    assert "DDD Sync Completed" in cyphers[2]
    assert "Architecture Sync Completed" in cyphers[3]

    # Gemini 호출 횟수: 정확히 3회
    assert len(gemini.calls) == 3

    # [B5 — 2026-05 lineage] inline lineage 필드가 응답에 포함되는지 확인.
    # 이 fake 응답엔 lineage 필드 없음 → normalize_lineage 가 default 채움.
    ent = result.spack["entities"][0]
    assert "lineage" in ent
    assert ent["lineage"] == {"confidence": "none", "related_stories": []}
    agg = result.ddd["aggregates"][0]
    assert "lineage" in agg
    svc = result.architecture["services"][0]
    assert "lineage" in svc


@pytest.mark.asyncio
async def test_design_pipeline_emits_stage_markers_in_order():
    """[progress] FE 진행바가 실제 단계 기반으로 차도록 spack→ddd→architecture→
    saving 순서로 stage 마커를 emit 하는지 검증."""
    spy: dict = {}
    gemini = FakeGemini(_responder(spy))
    neo = FakeNeo4j(
        responses=[
            [
                {
                    "master_prd_id": "doc_prd_master_harness",
                    "prd_content": _PRD_MASTER_MD,
                    "last_updated": 0,
                    "related_master_cps_id": "doc_cps_master_harness",
                    "absorbed_prd_ids": [],
                }
            ],
            [{"Status": "Spack Sync Completed", "ProjectName": "harness"}],
            [{"Status": "DDD Sync Completed", "ProjectName": "harness"}],
            [{"Status": "Architecture Sync Completed", "ProjectName": "harness"}],
        ]
    )
    stages: list = []

    async def _record(stage: str) -> None:
        stages.append(stage)

    ctx = PipelineContext(
        gemini=gemini, neo4j=neo, idempotency_key="d-stage", stage_callback=_record
    )
    await run_design_pipeline(ctx, DesignInput(project_name="harness"))

    assert stages == ["design:spack", "design:ddd", "design:architecture", "design:saving"]


async def test_lineage_filled_passes_through_to_response_and_creates_edges():
    """
    [B5] LLM 이 lineage 를 채워서 응답하면 inline 으로 spack/ddd/architecture 에
    그대로 통과 + Neo4j cypher 에 DERIVED_FROM 엣지 param 이 들어가는지 검증.
    """
    def respond(prompt: str) -> str:
        if "수석 테크니컬 아키텍트" in prompt:
            return json.dumps({
                "apis": [{
                    "id": "API-01", "name": "issue", "method": "POST",
                    "endpoint": "/tickets", "description": "티켓 발행",
                    "related_story_id": "Story-01.1",
                }],
                "entities": [{
                    "id": "ENT-01", "name": "Ticket",
                    "attributes": ["id"], "description": "티켓",
                    "lineage": {
                        "confidence": "direct",
                        "related_stories": [
                            {"story_id": "Story-01.1", "quote": "티켓 발행"},
                        ],
                    },
                }],
                "policies": [{
                    "id": "POL-01", "category": "Performance",
                    "description": "1초 이내", "related_entity": "Ticket",
                }],
            })
        if "수석 도메인 아키텍트" in prompt:
            return json.dumps({
                "contexts": [{"id": "CTX-01", "name": "Ticket Context",
                              "description": "d"}],
                "aggregates": [{
                    "id": "AGG-01", "name": "Ticket", "context_id": "CTX-01",
                    "description": "d",
                    "lineage": {
                        "confidence": "direct",
                        "related_stories": [
                            {"story_id": "Story-01.1", "quote": "티켓 발행"},
                        ],
                    },
                }],
                "entities": [],
                "events": [],
                "spack_entity_mapping": [{
                    "spack_entity_id": "ENT-01", "spack_name": "Ticket",
                    "ddd_location": "AGG-01", "ddd_role": "aggregate_root",
                }],
            })
        if "수석 클라우드 시스템 아키텍트" in prompt:
            return json.dumps({
                "services": [
                    {
                        "id": "SVC-01", "name": "Front", "type": "Frontend",
                        "tech_stack": "Vue.js", "description": "d",
                        "lineage": {
                            "confidence": "inferred",
                            "related_stories": [
                                {"story_id": "Story-01.1", "quote": "모바일 환경"},
                            ],
                        },
                    },
                    {
                        "id": "SVC-02", "name": "Ticket Service",
                        "type": "Backend API",
                        "tech_stack": "Spring Boot", "description": "d",
                        "owned_aggregates": ["Ticket"],
                        "lineage": {
                            "confidence": "direct",
                            "related_stories": [
                                {"story_id": "Story-01.1", "quote": "발행 처리"},
                            ],
                        },
                    },
                ],
                "databases": [],
                "connections": [],
                "api_service_mapping": [{
                    "api_id": "API-01", "service_id": "SVC-02", "reason": "d",
                }],
            })
        raise AssertionError(f"unexpected prompt: {prompt[:120]}")

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(
        responses=[
            [{"prd_content": _PRD_MASTER_MD,
              "prd_id": "doc_prd_master_harness"}],
            [],  # save spack
            [],  # save ddd
            [],  # save arch
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="dL")

    result = await run_design_pipeline(ctx, DesignInput(project_name="harness"))

    # inline lineage 응답 통과
    ent = result.spack["entities"][0]
    assert ent["lineage"]["confidence"] == "direct"
    assert ent["lineage"]["related_stories"][0]["story_id"] == "Story-01.1"
    agg = result.ddd["aggregates"][0]
    assert agg["lineage"]["confidence"] == "direct"
    # services 는 (label, id) 정렬 — Frontend 가 먼저
    services = result.architecture["services"]
    svc_by_name = {s["name"]: s for s in services}
    assert svc_by_name["Front"]["lineage"]["confidence"] == "inferred"
    assert svc_by_name["Ticket Service"]["lineage"]["confidence"] == "direct"

    # Neo4j cypher 의 lineage edges param 확인
    save_spack_call = [e for e in neo.executed if "Spack Sync" in e["cypher"]][0]
    edges = save_spack_call["params"].get("entity_lineage_edges") or []
    assert len(edges) == 1
    assert edges[0]["story_neo4j_id"] == "story_01_1"
    assert edges[0]["confidence"] == "direct"

    save_arch_call = [e for e in neo.executed if "Architecture Sync" in e["cypher"]][0]
    arch_edges = save_arch_call["params"].get("service_lineage_edges") or []
    # direct + inferred 둘 다 엣지 — 옵션 3-B
    assert len(arch_edges) == 2
    assert {e["confidence"] for e in arch_edges} == {"direct", "inferred"}


async def test_design_pipeline_cancels_before_spack_when_client_disconnects():
    """
    중지 — Spack LLM 호출 전에 check_cancel 이 True 면 DesignPipelineCancelled.
    Neo4j 트랜잭션(commit) 은 실행되지 않아 기존 데이터가 보존된다.
    """
    spy: dict = {}
    gemini = FakeGemini(_responder(spy))
    neo = FakeNeo4j(
        responses=[
            # Get PRD 만 응답 — 이후 stage 진입 전에 cancel 발생해야 함
            [
                {
                    "master_prd_id": "doc_prd_master_harness",
                    "prd_content": _PRD_MASTER_MD,
                    "last_updated": 0,
                    "related_master_cps_id": None,
                    "absorbed_prd_ids": [],
                }
            ],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cancel-spack")

    async def always_disconnected():
        return True

    with pytest.raises(DesignPipelineCancelled, match="spack_llm"):
        await run_design_pipeline(
            ctx, DesignInput(project_name="harness"),
            check_cancel=always_disconnected,
        )

    # ★ 핵심 ★ commit 트랜잭션이 실행되지 않음 → 기존 데이터 보존
    # Get PRD 의 run_cypher 만 호출됐고, save_* 트랜잭션은 없음.
    save_calls = [e for e in neo.executed if "SET" in (e.get("cypher") or "").upper()]
    assert save_calls == []
    # Gemini 도 호출되지 않음 — Spack stage 진입 전 차단.
    assert len(gemini.calls) == 0


async def test_design_pipeline_cancels_before_final_commit():
    """
    Spack/DDD/Architecture LLM 까지 다 돌고도, 최종 commit 직전에 cancel 감지되면
    트랜잭션 실행되지 않아야 함. 가장 critical 한 경계.
    """
    spy: dict = {}
    gemini = FakeGemini(_responder(spy))
    neo = FakeNeo4j(
        responses=[
            [
                {
                    "master_prd_id": "doc_prd_master_harness",
                    "prd_content": _PRD_MASTER_MD,
                    "last_updated": 0,
                    "related_master_cps_id": None,
                    "absorbed_prd_ids": [],
                }
            ],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cancel-final")

    # 마지막 stage (final_commit) 직전에만 True — LLM 3개는 모두 실행됨
    call_count = {"n": 0}

    async def cancel_at_final():
        call_count["n"] += 1
        # check_cancel 은 4번 호출됨: spack_llm, ddd_llm, architecture_llm, final_commit
        # 마지막 (4번째) 만 True 반환
        return call_count["n"] >= 4

    with pytest.raises(DesignPipelineCancelled, match="final_commit"):
        await run_design_pipeline(
            ctx, DesignInput(project_name="harness"),
            check_cancel=cancel_at_final,
        )

    # 3개 Gemini agent 모두 호출됨
    assert len(gemini.calls) == 3
    # 하지만 save 트랜잭션은 호출되지 않음 — 기존 데이터 보존
    save_calls = [e for e in neo.executed if "SET" in (e.get("cypher") or "").upper()]
    assert save_calls == []


async def test_design_pipeline_completes_when_check_cancel_returns_false():
    """check_cancel 이 항상 False → 정상 완료, 트랜잭션 실행됨."""
    spy: dict = {}
    gemini = FakeGemini(_responder(spy))
    neo = FakeNeo4j(
        responses=[
            [
                {
                    "master_prd_id": "doc_prd_master_harness",
                    "prd_content": _PRD_MASTER_MD,
                    "last_updated": 0,
                    "related_master_cps_id": None,
                    "absorbed_prd_ids": [],
                }
            ],
            [{"Status": "Spack Sync Completed"}],
            [{"Status": "DDD Sync Completed"}],
            [{"Status": "Architecture Sync Completed"}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="no-cancel")

    async def never_cancel():
        return False

    result = await run_design_pipeline(
        ctx, DesignInput(project_name="harness"),
        check_cancel=never_cancel,
    )
    assert result.project_name == "harness"
    # 트랜잭션 3건 실행됨
    save_calls = [e for e in neo.executed if "SET" in (e.get("cypher") or "").upper()]
    assert len(save_calls) >= 3


async def test_design_pipeline_raises_when_no_master_prd():
    gemini = FakeGemini(lambda p: "should not be called")
    neo = FakeNeo4j(responses=[[]])  # Get PRD 가 빈 결과
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="d2")

    with pytest.raises(ValueError, match="마스터 PRD 없음"):
        await run_design_pipeline(ctx, DesignInput(project_name="empty_project"))
    # Gemini 호출 없이 즉시 실패
    assert len(gemini.calls) == 0


# ─── auto_cleanup pre-step (2026-05-26) ─────────────────────────────


# Dirty PRD — Product Vision 6 회 (cleanup trigger).
_DIRTY_PRD_MD = (
    "## 🗺️ Master PRD\n\n"
    "### 1. Product Overview (통합 제품 비전)\n"
    "- **통합 비전**: 펀딩 플랫폼\n"
    + "- **Product Vision**: V1 자동화\n"
    + "- **Product Vision**: V2 자동화\n"
    + "- **Product Vision**: V3 자동화\n"
    + "- **Product Vision**: V4 자동화\n"
    + "- **Product Vision**: V5 자동화\n"
    + "- **Product Vision**: V6 자동화\n\n"
    "### 2. Epic & User Story Map (기능 계층도)\n"
    "#### 📦 [Epic-01] 티켓 관리\n"
    "- `[Story-01.1]` 티켓 발행\n\n"
    "### 3. Screen Architecture (화면별 구현 명세)\n"
    "#### 🖥️ [Screen: Home]\n"
    "- **포함된 기능**: `[Story-01.1]` 티켓 발행\n\n"
    "### 4. Global Non-Functional Requirements\n"
    "- 응답 시간 1초 이내\n"
)

# Cleanup LLM 응답 — dedupe 된 PRD (Product Vision 1개로 통합).
_CLEANED_PRD_MD = (
    "## 🗺️ Master PRD 조감도 (정리됨)\n\n"
    "### 1. Product Overview\n- **통합 비전**: 펀딩 플랫폼\n- **핵심 타겟**: 사용자\n\n"
    "### 2. Epic & User Story Map\n#### 📦 [Epic-01] 티켓 관리\n- `[Story-01.1]` 티켓 발행\n\n"
    "### 3. Screen Architecture\n#### 🖥️ [Screen: Home]\n- 포함된 기능: `[Story-01.1]` 티켓 발행\n\n"
    "### 4. Global Non-Functional Requirements\n- 응답 시간 1초 이내, OAuth, 401 처리\n"
)


def _design_responder_with_cleanup_path(spy: dict, cleanup_response: str):
    """
    cleanup_master_prd LLM + 3개 design agent 응답 라우팅.

    cleanup prompt 는 'PRD 정리(dedupe) 전문가' 또는 'DEDUPLICATION' 키워드로 식별.
    """

    def respond(prompt: str) -> str:
        if "DEDUPLICATION" in prompt or "PRD 정리(dedupe)" in prompt:
            spy.setdefault("cleanup_called", 0)
            spy["cleanup_called"] += 1
            return cleanup_response
        # 그 외엔 기존 design responder 라우팅.
        return _responder(spy)(prompt)

    return respond


@pytest.fixture
def fake_cleanup_repo(monkeypatch):
    """cleanup_master_prd_pipeline 의 query_repository 호출을 mock.

    get_master_prd: dirty PRD 반환.
    update_master_prd_markdown: cleaned content 저장.
    """
    from app.service.query_repository import PrdMaster

    state: Dict[str, Any] = {"current_content": _DIRTY_PRD_MD, "update_calls": []}

    async def fake_get_master_prd(project_name: str, team_id: str = ""):
        return PrdMaster(
            master_prd_id="doc_prd_master_harness",
            prd_content=state["current_content"],
            last_updated=1700000000000,
            markdown_stale=False,
            related_master_cps_id=None,
            absorbed_prd_ids=[],
        )

    async def fake_update_master_prd_markdown(
        project_name: str, content: str, *, client_updated_at=None, team_id: str = "",
        mark_design_stale: bool = True,
    ):
        state["update_calls"].append({
            "project_name": project_name, "content": content,
            "mark_design_stale": mark_design_stale,
        })
        state["current_content"] = content
        return {"master_id": "doc_prd_master_harness", "last_updated": 1700000001000}

    monkeypatch.setattr(
        "app.pipelines.cleanup_master_prd_pipeline.query_repository.get_master_prd",
        fake_get_master_prd,
    )
    monkeypatch.setattr(
        "app.pipelines.cleanup_master_prd_pipeline.query_repository.update_master_prd_markdown",
        fake_update_master_prd_markdown,
    )
    return state


async def test_auto_cleanup_triggered_on_dirty_prd(fake_cleanup_repo):
    """
    [2026-05-26 — design auto-cleanup]
    Dirty PRD (Product Vision 6 회) 에서 design 시작 → cleanup 자동 호출 →
    cleaned content 로 SPACK/DDD/Arch 진행. diagnostic 에 auto_cleanup 정보.
    """
    spy: Dict[str, Any] = {}
    gemini = FakeGemini(_design_responder_with_cleanup_path(spy, _CLEANED_PRD_MD))

    # FakeNeo4j: design pipeline 의 fetch_master_prd 는 첫 호출 (dirty PRD),
    # cleanup 이 update 후 refresh_fetch 가 두 번째 호출 (cleaned PRD).
    neo = FakeNeo4j(
        responses=[
            # 첫 Get PRD — dirty
            [{"master_prd_id": "doc_prd_master_harness",
              "prd_content": _DIRTY_PRD_MD, "last_updated": 0,
              "related_master_cps_id": None, "absorbed_prd_ids": []}],
            # cleanup 후 refresh fetch — cleaned
            [{"master_prd_id": "doc_prd_master_harness",
              "prd_content": _CLEANED_PRD_MD, "last_updated": 1,
              "related_master_cps_id": None, "absorbed_prd_ids": []}],
            # SPACK / DDD / Arch save
            [{"Status": "Spack Sync Completed"}],
            [{"Status": "DDD Sync Completed"}],
            [{"Status": "Architecture Sync Completed"}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="auto-clean-1")

    result = await run_design_pipeline(ctx, DesignInput(project_name="harness"))

    # cleanup 호출됨
    assert spy.get("cleanup_called") == 1
    # diagnostic 에 auto_cleanup 정보
    ac = result.diagnostic["auto_cleanup"]
    assert ac["attempted"] is True
    assert ac["applied"] is True
    assert ac["reduction_pct"] > 0  # 600 → 350 정도
    assert ac["failure_reason"] is None
    assert ac["dirty_diagnostic"]["product_vision_count"] >= 6
    # cleanup 후 cleaned content 가 SPACK 입력에 사용됐는지 — Product Vision 6번 누적
    # 문구가 빠졌는지 (spack_prompt 가 cleaned content 의 input)
    spack_prompt = spy["spack_prompt"]
    pv_count_in_prompt = spack_prompt.count("Product Vision")
    assert pv_count_in_prompt <= 2, f"cleaned content 가 SPACK 입력에 안 들어감 ({pv_count_in_prompt})"
    # design 결과 정상
    assert result.spack["apis"][0]["id"] == "API-01"


async def test_auto_cleanup_skipped_on_clean_prd():
    """
    정상 PRD (Product Vision 1개 + reconcile OK) → cleanup 호출 안 됨.
    diagnostic.auto_cleanup.attempted=False.
    """
    spy: Dict[str, Any] = {}
    gemini = FakeGemini(_responder(spy))
    neo = FakeNeo4j(
        responses=[
            [{"master_prd_id": "doc_prd_master_harness",
              "prd_content": _PRD_MASTER_MD, "last_updated": 0,
              "related_master_cps_id": None, "absorbed_prd_ids": []}],
            [{"Status": "Spack Sync Completed"}],
            [{"Status": "DDD Sync Completed"}],
            [{"Status": "Architecture Sync Completed"}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="no-clean")

    result = await run_design_pipeline(ctx, DesignInput(project_name="harness"))

    ac = result.diagnostic["auto_cleanup"]
    assert ac["attempted"] is False
    assert ac["applied"] is False
    # Gemini 호출 3회 (cleanup 안 부름)
    assert len(gemini.calls) == 3


async def test_auto_cleanup_failure_falls_through_to_original_prd(fake_cleanup_repo):
    """
    Dirty PRD 감지됐지만 cleanup LLM 이 빈 응답 → fall-through.
    design 은 원본 PRD 로 진행. auto_cleanup.applied=False, failure_reason 있음.
    """
    spy: Dict[str, Any] = {}
    # cleanup 응답 빈 string → cleanup pipeline 의 _MIN_OUTPUT_BYTES guard 가 raise
    gemini = FakeGemini(_design_responder_with_cleanup_path(spy, ""))
    neo = FakeNeo4j(
        responses=[
            # Get PRD (dirty)
            [{"master_prd_id": "doc_prd_master_harness",
              "prd_content": _DIRTY_PRD_MD, "last_updated": 0,
              "related_master_cps_id": None, "absorbed_prd_ids": []}],
            # save spack/ddd/arch
            [{"Status": "Spack Sync Completed"}],
            [{"Status": "DDD Sync Completed"}],
            [{"Status": "Architecture Sync Completed"}],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="clean-fail")

    result = await run_design_pipeline(ctx, DesignInput(project_name="harness"))

    # cleanup LLM 1회 호출됨 (실패) + design 3회
    assert spy.get("cleanup_called") == 1
    ac = result.diagnostic["auto_cleanup"]
    assert ac["attempted"] is True
    assert ac["applied"] is False
    assert ac["failure_reason"] is not None
    assert "짧" in ac["failure_reason"] or "너무" in ac["failure_reason"]
    # design 자체는 정상 완료 (fall-through) — 원본 PRD 사용
    assert result.spack["apis"][0]["id"] == "API-01"
    # SPACK prompt 엔 dirty 원본의 Product Vision 누적 흔적 남아있음
    pv_count_in_prompt = spy["spack_prompt"].count("Product Vision")
    assert pv_count_in_prompt >= 5, "fall-through 시 원본 PRD 사용 안 됨"
    # master PRD 는 update 안 됨 (cleanup 실패 → 원본 보존)
    assert fake_cleanup_repo["update_calls"] == []


def _empty_spack_ddd_responder(spy: dict):
    """SPACK·DDD 는 빈 결과, Architecture 만 비어있지 않은 응답.

    dirty PRD underextract 로 SPACK/DDD 가 비어버리는 실제 케이스 모사.
    """
    def respond(prompt: str) -> str:
        if "수석 테크니컬 아키텍트" in prompt:
            spy["spack_prompt"] = prompt
            return json.dumps({"apis": [], "entities": [], "policies": []})
        if "수석 도메인 아키텍트" in prompt:
            return json.dumps(
                {"contexts": [], "aggregates": [], "entities": [], "events": []}
            )
        if "수석 클라우드 시스템 아키텍트" in prompt:
            return json.dumps(
                {
                    "services": [
                        {"id": "SVC-01", "name": "Front", "type": "Frontend",
                         "tech_stack": "Vue.js", "description": "d"}
                    ],
                    "databases": [],
                    "connections": [],
                }
            )
        raise AssertionError(f"unexpected design prompt: {prompt[:120]}")

    return respond


def _executed_contains_stale_reset(neo) -> bool:
    return any(
        "design_source_stale = false" in (e.get("cypher") or "")
        for e in neo.executed
    )


def test_reset_stale_query_is_name_only_not_owner_email_gated():
    """[2026-06-05 회귀] reset 쿼리는 name-only 여야 한다.

    이전 버그: MATCH (p:Project {name, owner_email: $email}) + 호출부가 email=""
    전달 → reset 0건 매칭(no-op) → design_source_stale 영영 true → 재생성 후에도
    배너 부활. SET(PRD merge)·READ(source-stale)는 둘 다 name-only 라 reset 도
    동일해야 같은 노드를 끈다. (팀 격리는 scoped name 이 담당.)
    """
    from app.pipelines.design_pipeline.cypher import build_reset_design_stale_query

    cypher, params = build_reset_design_stale_query("proj__team_x")
    # owner_email 게이트가 있으면 안 됨 — 이게 no-op 의 원인이었다.
    assert "owner_email" not in cypher
    # name 으로 매칭하고 stale=false 로 끈다.
    assert "design_source_stale = false" in cypher
    assert params == {"project": "proj__team_x"}
    # email 인자를 줘도 쿼리/파라미터에 새지 않아야 한다 (하위호환 무시).
    cypher2, params2 = build_reset_design_stale_query("proj", email="someone@example.com")
    assert "owner_email" not in cypher2
    assert "email" not in params2


async def test_empty_spack_ddd_marks_diagnostic_but_still_resets_stale():
    """[2026-06-01 stale 디커플] SPACK·DDD 가 빈 생성이어도:

    1) diagnostic.empty_generation 에 명시 (FE 가 '왜 비었는지' emptyGenNotice 로 안내).
    2) design_source_stale 는 그래도 reset — 재생성이 성공한 시점에서 설계는 (일부
       layer 가 비었더라도) 최신 PRD 기준으로 다시 만들어진 것이므로 '옛 PRD 기준'
       (StaleBanner)은 더는 맞지 않다. 완성도(빈 layer)는 stale 과 별개 신호.
       (이전엔 빈 생성 시 stale 유지 → 최신 PRD 로 막 재생성해도 배너가 안 사라지는
        모순이었음.)
    """
    spy: dict = {}
    gemini = FakeGemini(_empty_spack_ddd_responder(spy))
    neo = FakeNeo4j(
        responses=[
            [{"master_prd_id": "doc_prd_master_x", "prd_content": _PRD_MASTER_MD,
              "last_updated": 0, "related_master_cps_id": None, "absorbed_prd_ids": []}],
            [], [], [], [],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="empty-gen")

    result = await run_design_pipeline(ctx, DesignInput(project_name="x"))

    assert result.diagnostic["empty_generation"] == {
        "spack": True, "ddd": True, "architecture": False,
    }
    # [디커플] 빈 생성이어도 재생성 성공 → stale reset 실행 (불완전은 진단으로 노출)
    assert _executed_contains_stale_reset(neo)


async def test_full_generation_resets_stale_and_clears_empty_flags():
    """정상 (비어있지 않은) 생성이면 empty_generation 모두 False + stale reset 실행."""
    spy: dict = {}
    gemini = FakeGemini(_responder(spy))
    neo = FakeNeo4j(
        responses=[
            [{"master_prd_id": "doc_prd_master_x", "prd_content": _PRD_MASTER_MD,
              "last_updated": 0, "related_master_cps_id": None, "absorbed_prd_ids": []}],
            [], [], [], [],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="full-gen")

    result = await run_design_pipeline(ctx, DesignInput(project_name="x"))

    assert result.diagnostic["empty_generation"] == {
        "spack": False, "ddd": False, "architecture": False,
    }
    assert _executed_contains_stale_reset(neo)


async def test_design_pipeline_diagnostic_counts():
    spy: dict = {}
    gemini = FakeGemini(_responder(spy))
    neo = FakeNeo4j(
        responses=[
            [
                {
                    "master_prd_id": "doc_prd_master_x",
                    "prd_content": _PRD_MASTER_MD,
                    "last_updated": 0,
                    "related_master_cps_id": None,
                    "absorbed_prd_ids": [],
                }
            ],
            [],
            [],
            [],
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="d3")

    result = await run_design_pipeline(ctx, DesignInput(project_name="x"))
    d = result.diagnostic
    assert d["spack"]["api_count"] == 1
    assert d["spack"]["entity_count"] == 1
    assert d["spack"]["policy_count"] == 1
    assert d["ddd"]["aggregate_count"] == 1
    assert d["ddd"]["context_count"] == 1
    assert d["architecture"]["service_count"] == 2
    assert d["architecture"]["database_count"] == 0
    assert d["section_extractor"]["overview_found"]
