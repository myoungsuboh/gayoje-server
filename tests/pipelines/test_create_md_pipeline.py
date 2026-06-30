"""
create_md_pipeline 테스트:
- 3 LLM 호출이 asyncio.gather 로 병렬 실행됨
- 각 그래프 (Spack/DDD/Architecture) 가 prompt 에 정확히 전달됨
- 빈 그래프도 LLM 호출
- 빈 project_name → ValueError
- [Phase ① — 2026-05-25] 그래프의 디테일 필드 (entity.attributes, lineage,
  event.related_story_id, service.owned_aggregates) 가 프롬프트에 보존됨
- [Phase ① — 2026-05-25] 보강된 출력 형식 지시 (Lineage Health / Request body /
  에러 응답 가이드 / N/A 가시화) 가 프롬프트 파일에 박혀있음 — 회귀 방지
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.create_md_pipeline import (
    CreateMdInput,
    _ORCHESTRATOR_SECTION_CAP,
    _cap_for_orchestrator,
    run_create_md_pipeline,
)
from tests.conftest import FakeGemini, FakeNeo4j

_PROMPT_DIR = (
    Path(__file__).resolve().parent.parent.parent / "app" / "prompts"
)


@pytest.mark.asyncio
async def test_create_md_full_flow(monkeypatch):
    """SPACK/DDD/Architecture 그래프 fetch → 3 LLM → MD 3종 반환."""

    spy = {}

    def respond(prompt: str) -> str:
        if "Lead Architect" in prompt:
            spy["orchestrator_prompt"] = prompt
            return "# Orchestrator MD\n## Workflow Plan..."
        if "DDD(Domain-Driven Design)" in prompt:
            spy["ddd_prompt"] = prompt
            return "# DDD MD\n## Domain Overview..."
        if "Architecture" in prompt:
            spy["arch_prompt"] = prompt
            return "# Architecture MD\n## System Overview..."
        if "SPACK" in prompt:
            spy["spack_prompt"] = prompt
            return "# SPACK MD\n- api list..."
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    gemini = FakeGemini(respond)
    # 3개의 Cypher fetch (Spack/DDD/Arch) — query_repository 가 호출
    neo = FakeNeo4j(
        responses=[
            # Spack
            [
                {
                    "apis": [{"id": "API-01", "name": "list"}],
                    "entities": [],
                    "policies": [],
                    "internal_rels": [],
                    "implement_rels": [],
                }
            ],
            # DDD
            [
                {
                    "contexts": [{"id": "CTX-01", "name": "Tickets"}],
                    "aggregates": [],
                    "domain_entities": [],
                    "domain_events": [],
                    "internal_rels": [],
                    "trigger_rels": [],
                }
            ],
            # Architecture
            [
                {
                    "services": [{"id": "SVC-01", "name": "Backend"}],
                    "databases": [],
                    "connections": [],
                }
            ],
        ]
    )
    # query_repository 가 모듈 직접 neo4j_client 호출 — monkeypatch 필요
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher",
        neo.run_cypher,
    )

    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cm1")

    result = await run_create_md_pipeline(ctx, CreateMdInput(project_name="x"))

    assert result.project_name == "x"
    assert "SPACK MD" in result.spack_md
    assert "DDD MD" in result.ddd_md
    assert "Architecture MD" in result.arch_md
    assert "Orchestrator MD" in result.orchestrator_md

    # 3 LLM 호출 모두 발생
    assert len(gemini.calls) == 4
    # 각 프롬프트에 해당 그래프 JSON 포함
    assert '"id": "API-01"' in spy["spack_prompt"]
    assert '"id": "CTX-01"' in spy["ddd_prompt"]
    assert '"id": "SVC-01"' in spy["arch_prompt"]
    # [Gemini 평가 #2] arch 입력에 api_service_mapping 키가 항상 합성된다 (없으면 빈 리스트)
    assert '"api_service_mapping"' in spy["arch_prompt"]
    # [2026-06 병렬화] orchestrator 는 그래프 digest 를 입력으로 받는다 (MD 출력 아님)
    assert '"name": "list"' in spy["orchestrator_prompt"]      # spack digest 의 API name
    assert '"Tickets"' in spy["orchestrator_prompt"]            # ddd digest 의 context
    assert '"name": "Backend"' in spy["orchestrator_prompt"]   # arch digest 의 service
    assert '"service"' in spy["orchestrator_prompt"]            # API→담당 서비스 필드 (#199)
    assert "SPACK MD" not in spy["orchestrator_prompt"]         # MD 산문은 안 들어감

    # diagnostic — 구간별 타이밍 관측성
    assert set(result.diagnostic["timings_ms"]) == {
        "spack_md", "ddd_md", "arch_md", "orchestrator_md",
    }
    assert result.diagnostic["spack_node_count"] == 1
    assert result.diagnostic["ddd_node_count"] == 1
    assert result.diagnostic["arch_node_count"] == 1
    assert result.diagnostic["spack_size"] > 0


@pytest.mark.asyncio
async def test_create_md_empty_graphs_still_calls_llm(monkeypatch):
    """빈 그래프도 LLM 호출."""
    gemini = FakeGemini(lambda p: "# Empty section")
    neo = FakeNeo4j(responses=[[], [], []])  # 3개 fetch 모두 empty
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", neo.run_cypher
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cm2")

    result = await run_create_md_pipeline(ctx, CreateMdInput(project_name="empty"))
    # 빈 그래프지만 LLM 3회 호출
    assert len(gemini.calls) == 4
    assert result.diagnostic["spack_node_count"] == 0


# ─── [2026-06-03] orchestrator graceful degradation 회귀 테스트 ───────────────
#
# 배경:
#   orchestrator(통합 가이드)는 spack/ddd/arch MD 전문을 한 프롬프트로 받는 최대 LLM
#   호출이라 90s 타임아웃에 가장 취약했다. 기존엔 이 호출이 실패하면 예외가 전파돼
#   이미 성공한 3종 MD 까지 통째로 버려지고(사용자: 95%에서 멈췄다 터짐), arq 가 전체
#   job 을 3회 재시도하며 토큰을 낭비했다. 이제 orchestrator 실패는 격리(degrade)된다.


@pytest.mark.asyncio
async def test_create_md_orchestrator_failure_degrades_gracefully(monkeypatch):
    """orchestrator LLM 이 터져도 spack/ddd/arch 는 보존되고 job 은 성공해야 한다."""

    def respond(prompt: str) -> str:
        # orchestrator 프롬프트(= "Lead Architect" 포함)만 타임아웃으로 실패시킨다.
        if "Lead Architect" in prompt:
            raise TimeoutError("LiteLLM exhausted retries")
        if "DDD(Domain-Driven Design)" in prompt:
            return "# DDD MD\n## Domain Overview..."
        if "Architecture" in prompt:
            return "# Architecture MD\n## System Overview..."
        if "SPACK" in prompt:
            return "# SPACK MD\n- api list..."
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(responses=[[], [], []])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", neo.run_cypher
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cmDeg")

    # 예외가 전파되지 않고 정상 반환되어야 한다 (job 성공).
    result = await run_create_md_pipeline(ctx, CreateMdInput(project_name="x"))

    # 성공한 3종 MD 는 그대로 보존
    assert "SPACK MD" in result.spack_md
    assert "DDD MD" in result.ddd_md
    assert "Architecture MD" in result.arch_md
    # orchestrator 만 빈 문자열로 degrade
    assert result.orchestrator_md == ""
    # diagnostic 에 실패 플래그/원인 기록
    assert result.diagnostic["orchestrator_failed"] is True
    assert "exhausted retries" in result.diagnostic["orchestrator_error"]
    assert result.diagnostic["orchestrator_size"] == 0


@pytest.mark.asyncio
async def test_create_md_orchestrator_success_sets_failed_false(monkeypatch):
    """정상 케이스는 orchestrator_failed=False 로 기록 (degrade 회귀 방지)."""

    def respond(prompt: str) -> str:
        if "Lead Architect" in prompt:
            return "# Orchestrator MD\n## Workflow Plan..."
        return "# section"

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(responses=[[], [], []])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", neo.run_cypher
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cmOk")

    result = await run_create_md_pipeline(ctx, CreateMdInput(project_name="x"))
    assert "Orchestrator MD" in result.orchestrator_md
    assert result.diagnostic["orchestrator_failed"] is False
    assert result.diagnostic["orchestrator_error"] == ""


def test_cap_for_orchestrator_passes_small_md_through():
    """상한 이하면 원본 그대로 (정상 규모 프로젝트는 영향 없음)."""
    small = "# tiny\n" * 10
    assert _cap_for_orchestrator(small) == small
    assert _cap_for_orchestrator("") == ""


def test_cap_for_orchestrator_truncates_oversized_md():
    """상한 초과 시 잘리고 명시 마커가 붙는다."""
    huge = "x" * (_ORCHESTRATOR_SECTION_CAP + 5_000)
    out = _cap_for_orchestrator(huge)
    assert len(out) < len(huge)
    assert out.startswith("x" * 100)
    assert "일부 잘림" in out


@pytest.mark.asyncio
async def test_orchestrator_runs_concurrently_with_md_calls(monkeypatch):
    """[2026-06 병렬화] orchestrator 가 MD 생성과 '동시에' 실행되는지 — 배리어 증명.

    spack 호출과 orchestrator 호출이 **둘 다 진입해야** 게이트가 풀린다.
    직렬(구버전: orchestrator 가 spack '출력'을 입력으로 기다림)이면 spack 이
    게이트에서 영원히 대기 → timeout 으로 실패한다.
    """
    import asyncio

    gate = asyncio.Event()
    in_flight: set = set()

    class _ConcurrentGemini:
        def __init__(self) -> None:
            self.calls = []

        async def generate(self, prompt, *, temperature=0.3, **kw):
            self.calls.append(prompt)
            name = (
                "orchestrator" if "Lead Architect" in prompt
                else "spack" if "SPACK" in prompt
                else "other"
            )
            if name in ("spack", "orchestrator"):
                in_flight.add(name)
                if {"spack", "orchestrator"} <= in_flight:
                    gate.set()
                await asyncio.wait_for(gate.wait(), timeout=2.0)
            from tests.conftest import _FakeResult
            return _FakeResult(text=f"# {name} MD")

    gemini = _ConcurrentGemini()
    neo = FakeNeo4j(responses=[[], [], []])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", neo.run_cypher
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cmPar")

    result = await run_create_md_pipeline(ctx, CreateMdInput(project_name="x"))

    assert "spack MD" in result.spack_md
    assert "orchestrator MD" in result.orchestrator_md
    assert result.diagnostic["orchestrator_failed"] is False


@pytest.mark.asyncio
async def test_create_md_orchestrator_independent_of_md_outputs(monkeypatch):
    """[2026-06 병렬화] orchestrator 입력은 그래프 digest — MD 출력과 완전 독립.

    이전엔 3 MD '출력'이 orchestrator 입력이라 (1) Stage 3 직렬 대기 +1~2분,
    (2) 거대 MD 는 24K cap 절단으로 플랜이 불완전해졌다. digest 전환으로 거대한
    MD 출력이 orchestrator 프롬프트에 단 한 글자도 들어가지 않아야 한다.
    """

    captured = {}
    big = "S" * (_ORCHESTRATOR_SECTION_CAP + 10_000)

    def respond(prompt: str) -> str:
        if "Lead Architect" in prompt:
            captured["orchestrator"] = prompt
            return "# Orchestrator MD"
        if "SPACK" in prompt:
            return big  # 거대한 SPACK MD 생성
        return "# section"

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(responses=[[], [], []])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", neo.run_cypher
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cmCap")
    result = await run_create_md_pipeline(ctx, CreateMdInput(project_name="x"))

    # 거대 SPACK MD 출력은 보존되지만, orchestrator 프롬프트엔 그 산문이 전혀 없다.
    assert big in result.spack_md
    assert "S" * 200 not in captured["orchestrator"]
    # digest 골격(JSON 키)은 들어간다
    assert '"apis"' in captured["orchestrator"]
    assert '"services"' in captured["orchestrator"]


# ─── [2026-06] 진행 신호(emit_stage) — FE 3단계/문서별 체크 표시의 계약 ────────


@pytest.mark.asyncio
async def test_create_md_emits_progress_stages(monkeypatch):
    """collecting → docs 0/4 → 누적 1~4/4(완료 목록 포함) → assembling 순서."""
    stages: list[str] = []

    async def _cb(s: str) -> None:
        stages.append(s)

    gemini = FakeGemini(lambda p: "# section")
    neo = FakeNeo4j(responses=[[], [], []])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", neo.run_cypher
    )
    ctx = PipelineContext(
        gemini=gemini, neo4j=neo, idempotency_key="cmStage", stage_callback=_cb
    )
    await run_create_md_pipeline(ctx, CreateMdInput(project_name="x"))

    assert stages[0] == "md:collecting"
    assert stages[1] == "md:docs:0/4"
    assert stages[-1] == "md:assembling"

    # 문서 완료 emit 4건 — 카운트 단조 증가 + 각 emit 은 누적 목록을 담는다
    docs = stages[2:-1]
    assert [s.split(":")[2] for s in docs] == ["1/4", "2/4", "3/4", "4/4"]
    # 마지막 emit 의 완료 목록 = 4종 전부 (순서는 병렬 완료 순이라 비결정)
    assert set(docs[-1].split(":", 3)[3].split(",")) == {
        "spack", "ddd", "architecture", "orchestrator",
    }


@pytest.mark.asyncio
async def test_create_md_no_stage_callback_is_noop(monkeypatch):
    """stage_callback 미배선(테스트/sync 호출)이어도 파이프라인 동작 불변."""
    gemini = FakeGemini(lambda p: "# section")
    neo = FakeNeo4j(responses=[[], [], []])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", neo.run_cypher
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cmNoCb")
    result = await run_create_md_pipeline(ctx, CreateMdInput(project_name="x"))
    assert len(gemini.calls) == 4
    assert result.project_name == "x"


@pytest.mark.asyncio
async def test_create_md_empty_project_raises():
    ctx = PipelineContext(
        gemini=FakeGemini(lambda p: "x"), neo4j=FakeNeo4j(), idempotency_key="cm3"
    )
    with pytest.raises(ValueError, match="비어 있을 수 없습니다"):
        await run_create_md_pipeline(ctx, CreateMdInput(project_name=""))


@pytest.mark.asyncio
async def test_create_md_strips_code_blocks(monkeypatch):
    """LLM 이 ```markdown ... ``` 감싼 출력을 줘도 strip 처리."""
    def respond(prompt: str) -> str:
        return "```markdown\n# Content\n```"

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(responses=[[], [], []])
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", neo.run_cypher
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cm4")
    result = await run_create_md_pipeline(ctx, CreateMdInput(project_name="x"))
    # 코드블록 마커 제거됨
    assert "```" not in result.spack_md
    assert "# Content" in result.spack_md


# ─── [Phase ① — 2026-05-25] 디테일 필드 보존 회귀 테스트 ───────────────────
#
# 배경:
#   plant 패키지의 SPACK MD 출력에서 entity.attributes / lineage / event 의
#   related_story_id 가 누락돼 AI 코딩 에이전트가 임의 schema 를 만들었음.
#   원인은 그래프 schema 가 아니라 create_md_* 프롬프트가 해당 필드를 출력하지
#   않게 작성돼 있었기 때문. 프롬프트를 보강한 뒤 회귀를 막기 위한 테스트들.


@pytest.mark.asyncio
async def test_spack_prompt_preserves_attributes_and_lineage(monkeypatch):
    """SPACK 그래프의 entity.attributes / lineage / related_story_id 가
    프롬프트에 그대로 직렬화돼 들어가는지 검증."""

    captured = {}

    def respond(prompt: str) -> str:
        if "SPACK 그래프" in prompt:
            captured["spack"] = prompt
        return "ok"

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(
        responses=[
            [
                {
                    "apis": [
                        {
                            "id": "API-01",
                            "method": "POST",
                            "endpoint": "/api/v1/plants/{id}/growth",
                            "description": "생장 기록",
                            "related_story_id": "Story-03.1",
                        }
                    ],
                    "entities": [
                        {
                            "id": "ENT-01",
                            "name": "PlantGrowthData",
                            "attributes": ["plantId", "height", "leafCount"],
                            "description": "식물 생장 기록",
                            "lineage": {
                                "confidence": "direct",
                                "related_stories": [
                                    {
                                        "story_id": "Story-03.1",
                                        "quote": "생장 데이터를 기록한다",
                                    }
                                ],
                            },
                        }
                    ],
                    "policies": [
                        {
                            "id": "POL-01",
                            "category": "Security",
                            "description": "JWT 필수",
                            "related_entity": "PlantGrowthData",
                        }
                    ],
                    "internal_rels": [],
                    "implement_rels": [],
                }
            ],
            [],
            [],
        ]
    )
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", neo.run_cypher
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cmA")
    await run_create_md_pipeline(ctx, CreateMdInput(project_name="x"))

    spack_prompt = captured["spack"]
    # 디테일 필드가 직렬화돼 그대로 전달
    assert "plantId" in spack_prompt
    assert "leafCount" in spack_prompt
    assert "Story-03.1" in spack_prompt
    assert "생장 데이터를 기록한다" in spack_prompt
    assert '"confidence": "direct"' in spack_prompt
    # related_entity 도 전달 (Entity ↔ Policy 매핑)
    assert "PlantGrowthData" in spack_prompt


@pytest.mark.asyncio
async def test_ddd_prompt_preserves_event_story_and_lineage(monkeypatch):
    """DDD 그래프의 event.related_story_id / aggregate lineage 가 프롬프트에 보존."""

    captured = {}

    def respond(prompt: str) -> str:
        if "DDD(Domain-Driven Design)" in prompt:
            captured["ddd"] = prompt
        return "ok"

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(
        responses=[
            [],
            [
                {
                    "contexts": [{"id": "CTX-01", "name": "Plant"}],
                    "aggregates": [
                        {
                            "id": "AGG-01",
                            "name": "Plant",
                            "context_id": "CTX-01",
                            "description": "식물 애그리거트",
                            "lineage": {
                                "confidence": "inferred",
                                "related_stories": [
                                    {
                                        "story_id": "Story-02.1",
                                        "quote": "식물 정보 관리",
                                    }
                                ],
                            },
                        }
                    ],
                    "domain_entities": [],
                    "domain_events": [
                        {
                            "id": "EVT-01",
                            "name": "PlantGrowthDataRecorded",
                            "description": "생장 기록 이벤트",
                            "related_story_id": "Story-03.1",
                            "published_by_aggregate_id": "AGG-01",
                        }
                    ],
                    "internal_rels": [],
                    "trigger_rels": [],
                }
            ],
            [],
        ]
    )
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", neo.run_cypher
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cmB")
    await run_create_md_pipeline(ctx, CreateMdInput(project_name="x"))

    ddd_prompt = captured["ddd"]
    assert "PlantGrowthDataRecorded" in ddd_prompt
    assert "Story-03.1" in ddd_prompt
    assert '"confidence": "inferred"' in ddd_prompt
    assert "식물 정보 관리" in ddd_prompt
    assert '"published_by_aggregate_id": "AGG-01"' in ddd_prompt


@pytest.mark.asyncio
async def test_architecture_prompt_preserves_owned_aggregates(monkeypatch):
    """Architecture 그래프의 service.owned_aggregates / lineage 가 프롬프트에 보존."""

    captured = {}

    def respond(prompt: str) -> str:
        if "시스템 아키텍처 문서 작성" in prompt:
            captured["arch"] = prompt
        return "ok"

    gemini = FakeGemini(respond)
    neo = FakeNeo4j(
        responses=[
            [],
            [],
            [
                {
                    "services": [
                        {
                            "id": "SVC-01",
                            "name": "식물 관리 서비스",
                            "type": "BackendService",
                            "tech_stack": "Spring Boot",
                            "description": "식물 기능 핵심",
                            "owned_aggregates": ["Plant"],
                            "lineage": {
                                "confidence": "direct",
                                "related_stories": [
                                    {
                                        "story_id": "Story-03.1",
                                        "quote": "식물 모니터링",
                                    }
                                ],
                            },
                        }
                    ],
                    "databases": [],
                    "connections": [],
                    "api_service_mapping": [
                        {
                            "api_id": "API-01",
                            "service_id": "SVC-01",
                            "reason": "식물 도메인",
                        }
                    ],
                }
            ],
        ]
    )
    monkeypatch.setattr(
        "app.service.query_repository.neo4j_client.run_cypher", neo.run_cypher
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="cmC")
    await run_create_md_pipeline(ctx, CreateMdInput(project_name="x"))

    arch_prompt = captured["arch"]
    assert '"owned_aggregates"' in arch_prompt
    assert "Plant" in arch_prompt
    assert "Story-03.1" in arch_prompt
    # api_service_mapping 은 현재 ArchitectureGraph fetch 가 가져오지 않음
    # (별개 이슈) — 프롬프트는 "있으면 활용" 로 작성돼 미래 대비.


# ─── 프롬프트 파일 정적 검증 — 보강 섹션 키워드가 사라지지 않도록 ─────────


def test_spack_prompt_file_enforces_detail_output():
    """create_md_spack.md 가 디테일 출력 지시를 유지하는지 정적 검증."""
    body = (_PROMPT_DIR / "create_md_spack.md").read_text(encoding="utf-8")
    # 새 강제 섹션
    assert "명세 충실도" in body
    assert "Lineage Health" in body
    # 핵심 출력 지시
    assert "Request body" in body or "요청 본문" in body
    assert "에러 응답" in body
    assert "Attributes" in body or "속성" in body
    assert "PRD 추적성" in body or "Lineage" in body
    # N/A 가시화 강제
    assert "⚠️" in body
    # 기존 테스트가 의존하는 prompt 식별 키워드 유지
    assert "SPACK 그래프" in body
    # [A-2] 새 payload 필드들 모두 명시
    assert "path_params" in body
    assert "query_params" in body
    assert "request_body" in body
    assert "response_body" in body
    # API request_body 명시율 health 지표
    assert "API request_body 명시" in body
    # [#199 — Gemini 평가] API↔Service 매핑 출력·지표 (회귀 방지)
    assert "api_service_rels" in body
    assert "구현 서비스" in body
    assert "API ↔ Service 매핑" in body
    # [A-3] error_cases + auth
    assert "error_cases" in body
    assert "auth" in body
    assert "Error cases" in body or "에러 응답" in body
    assert "Authorization" in body or "인증/권한" in body
    assert "API error_cases 명시" in body
    # [#3] Screens 섹션
    assert "screens" in body
    assert "calls_apis" in body


def test_ddd_prompt_file_enforces_detail_output():
    body = (_PROMPT_DIR / "create_md_ddd.md").read_text(encoding="utf-8")
    assert "명세 충실도" in body
    assert "Lineage Health" in body
    assert "payload" in body
    assert "트리거 Story" in body or "related_story_id" in body
    assert "⚠️" in body
    # prompt 식별 키워드 유지
    assert "DDD(Domain-Driven Design)" in body
    # [D-1] 새 detail 필드들 명시
    assert "invariants" in body
    assert "attributes" in body
    assert "payload_fields" in body
    # Health 지표
    assert "invariants 명시" in body


def test_architecture_prompt_file_enforces_detail_output():
    body = (_PROMPT_DIR / "create_md_architecture.md").read_text(
        encoding="utf-8"
    )
    assert "명세 충실도" in body
    assert "Lineage Health" in body
    assert "owned_aggregates" in body
    assert "api_service_mapping" in body or "API ↔ Service" in body
    assert "⚠️" in body
    # prompt 식별 키워드 유지
    assert "시스템 아키텍처 문서 작성" in body
    # [D-2] 새 detail 필드들
    assert "deployment" in body
    assert "external_dependencies" in body
    assert "Auth" in body or "auth" in body
    assert "deployment 명시" in body
    # [Gemini 평가 #3] Frontend 는 Aggregate 미소유가 정상 — 오탐 ⚠️ 대신 안내 출력
    assert "Frontend 계열이면 비어있는 게 정상" in body
    assert "Frontend 계열인 서비스는 제외" in body
    # [#201 후속] 단일 서비스(모놀리식)는 매핑이 비어도 임의 배치 위험이 없다 —
    # 체크리스트(multi_service 일 때만 갭)와 동일 정책으로 ⚠️ 오탐 제거
    assert "서비스가 1개뿐" in body
    assert "단일 서비스 — 모든 API 를 해당 서비스에 구현" in body


@pytest.mark.asyncio
async def test_architecture_md_includes_api_service_mapping():
    """[Gemini 평가 #2] spack 의 HANDLED_BY 매핑이 arch 입력 JSON 에 합성된다.

    architecture.md 의 'API ↔ Service Mapping: 0' 은 데이터 부재가 아니라
    입력 누락이었다 — _call_architecture_md 가 spack 을 받아 api_service_mapping
    을 채우는지 직접 검증.
    """
    from app.pipelines.create_md_pipeline import _call_architecture_md
    from app.service.query_repository import (
        ArchitectureGraph,
        CrossMappingRel,
        SpackGraph,
    )

    captured = {}

    def respond(prompt: str) -> str:
        captured["prompt"] = prompt
        return "# Architecture MD"

    ctx = PipelineContext(
        gemini=FakeGemini(respond), neo4j=None, idempotency_key="arch-map"
    )
    spack = SpackGraph(
        apis=[
            {"id": "API-1", "method": "post", "endpoint": "/login", "name": "로그인"},
            # 메서드/경로/이름 전부 미정 — 라벨이 빈 문자열이면 id 폴백
            {"id": "API-2"},
        ],
        api_service_rels=[
            CrossMappingRel(
                source_id="API-1",
                target_id="SVC-1",
                target_name="계정 서비스",
                type="HANDLED_BY",
            ),
            CrossMappingRel(
                source_id="API-2",
                target_id="SVC-1",
                target_name="계정 서비스",
                type="HANDLED_BY",
            ),
        ],
    )
    arch = ArchitectureGraph(services=[{"id": "SVC-1", "name": "계정 서비스"}])

    await _call_architecture_md(ctx, arch, spack)

    p = captured["prompt"]
    assert '"api_service_mapping"' in p
    assert '"api_id": "API-1"' in p
    assert '"api": "POST /login — 로그인"' in p
    assert '"service_name": "계정 서비스"' in p
    # 빈 라벨 폴백 — 빈 문자열 대신 id
    assert '"api": "API-2"' in p
