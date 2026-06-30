"""
design_pipeline_job 단위 테스트.

검증 포인트:
  - 결과 dict 가 직렬화 가능
  - spack/ddd/architecture 키 모두 채워짐
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.core.config import settings
from app.queue import jobs
from app.queue.jobs import design_pipeline_job
from tests.conftest import FakeGemini, FakeNeo4j, FakeRedis

@pytest.fixture(autouse=True)
def _no_inline_autofill(monkeypatch):
    """[2026-06-10 병렬 전환] design 의 autofill 은 이제 on_spack_ready 훅으로
    DDD/Arch 와 병렬 실행된다. 켜져 있으면 query_repository 의 전역 neo4j_client 로
    실제 저장을 시도(ServiceUnavailable 재시도로 느려짐)하므로, design 본체만 검증하는
    기본 테스트에선 토글을 끈다. autofill 검증 테스트가 True 로 켜고 generate/merge 를
    직접 monkeypatch 한다.
    """
    monkeypatch.setattr(settings, "DESIGN_AUTOFILL_API_SPECS", False)


def _prd_neo():
    return FakeNeo4j(
        responses=[
            [{"master_prd_id": "doc_prd_master_p", "prd_content": _PRD_MD,
              "last_updated": 0, "related_master_cps_id": None, "absorbed_prd_ids": []}],
            [], [], [],
        ]
    )


pytestmark = pytest.mark.asyncio


_PRD_MD = """\
## Master PRD

### 1. Product Overview
- vision

### 2. Epic & User Story Map
#### 📦 [Epic-01] EPIC
- `[Story-01.1]` x
"""


def _responder():
    def respond(prompt: str) -> str:
        if "수석 테크니컬 아키텍트" in prompt:
            return json.dumps({"apis": [], "entities": [], "policies": []})
        if "수석 도메인 아키텍트" in prompt:
            return json.dumps({"contexts": [], "aggregates": [], "entities": [], "events": []})
        if "수석 클라우드 시스템 아키텍트" in prompt:
            return json.dumps({"services": [], "databases": [], "connections": []})
        raise AssertionError("unexpected")

    return respond


async def test_design_job_returns_serializable_dict():
    gemini = FakeGemini(_responder())
    neo = FakeNeo4j(
        responses=[
            [
                {
                    "master_prd_id": "doc_prd_master_p",
                    "prd_content": _PRD_MD,
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
    ctx = {"job_id": "design-1", "gemini": gemini, "neo4j": neo}

    result = await design_pipeline_job(ctx, project_name="p")

    # 직렬화 가능
    json.loads(json.dumps(result, ensure_ascii=False))

    assert result["project_name"] == "p"
    assert result["master_prd_id"] == "doc_prd_master_p"
    assert "spack" in result
    assert "ddd" in result
    assert "architecture" in result
    assert "diagnostic" in result


async def test_design_job_cancelled_when_flag_set():
    """[2026-05-27] cancel flag(job_cancel:{job_id})가 set 돼 있으면 worker 가
    DesignPipelineCancelled 로 graceful 종료 → {'result':'cancelled'} 반환.

    비동기 큐 전환 후 중지가 worker 로 전달 안 되던 버그 수정. run_design_pipeline 은
    stage 사이 check_cancel 로 트랜잭션 전 bail → 기존 데이터 보존.
    """
    gemini = FakeGemini(_responder())
    neo = FakeNeo4j(
        responses=[
            [{"master_prd_id": "doc_prd_master_p", "prd_content": _PRD_MD,
              "last_updated": 0, "related_master_cps_id": None, "absorbed_prd_ids": []}],
            [], [], [],
        ]
    )
    redis = FakeRedis()
    await redis.set("job_cancel:design-cancel-1", "1")
    ctx = {"job_id": "design-cancel-1", "gemini": gemini, "neo4j": neo, "redis": redis}

    result = await design_pipeline_job(ctx, project_name="p")

    assert result["result"] == "cancelled"
    # 첫 stage(spack_llm) 경계에서 취소 → LLM 호출 0회 (데이터 미변경)
    assert len(gemini.calls) == 0


async def test_design_job_preflight_skips_when_over_quota(monkeypatch):
    """[2026-05 비용 가드] 이미 토큰 한도를 넘긴 사용자는 design 사이클을
    시작조차 하지 않는다 → {'result':'quota_exceeded'}, LLM 0회.
    """
    async def _always_over(_email):
        return True

    monkeypatch.setattr(jobs, "_is_over_token_quota", _always_over)

    gemini = FakeGemini(_responder())
    ctx = {"job_id": "design-q-1", "gemini": gemini, "neo4j": _prd_neo()}

    result = await design_pipeline_job(ctx, project_name="p", user_email="u@e.com")

    assert result["result"] == "quota_exceeded"
    assert result["project_name"] == "p"
    # 사전 차단 — 비싼 LLM 호출이 한 번도 일어나지 않아야 한다.
    assert len(gemini.calls) == 0


async def test_design_job_bails_midstage_when_quota_exceeded(monkeypatch):
    """[2026-05 비용 가드] SPACK 단계까지는 통과했지만 그 뒤 한도를 초과하면
    DDD/Architecture LLM 호출 *전에* bail → spack LLM 1회만, 데이터 보존.
    """
    state = {"n": 0}

    async def _over_after_first(_email):
        state["n"] += 1
        # 1회차(pre-flight)=False, 이후(ddd 전 재확인)=True
        return state["n"] > 1

    monkeypatch.setattr(jobs, "_is_over_token_quota", _over_after_first)
    # 토큰 적재는 best-effort — 테스트에서 실제 Neo4j 접근 회피.
    async def _noop(*_a, **_k):
        return 0
    monkeypatch.setattr(jobs.usage_repository, "add_tokens", _noop)

    gemini = FakeGemini(_responder())
    ctx = {"job_id": "design-q-2", "gemini": gemini, "neo4j": _prd_neo()}

    result = await design_pipeline_job(ctx, project_name="p", user_email="u@e.com")

    assert result["result"] == "quota_exceeded"
    # SPACK LLM 1회만 — DDD 호출 전에 bail.
    assert len(gemini.calls) == 1


# ─── [2026-06] design 안에 녹인 API 스펙 autofill ──────────────────


def _responder_with_api():
    """SPACK 이 빈 API 1개를 뽑는 responder — 병렬 autofill 트리거용.
    normalize_spack 이 ID 를 API-01 로 재부여한다.
    """
    def respond(prompt: str) -> str:
        if "수석 테크니컬 아키텍트" in prompt:
            return json.dumps({
                "apis": [{
                    "id": "API-01", "name": "Create X", "method": "POST",
                    "endpoint": "/x", "description": "d",
                    "error_cases": [], "auth": {},
                }],
                "entities": [], "policies": [],
            })
        if "수석 도메인 아키텍트" in prompt:
            return json.dumps({"contexts": [], "aggregates": [], "entities": [], "events": []})
        if "수석 클라우드 시스템 아키텍트" in prompt:
            return json.dumps({"services": [], "databases": [], "connections": []})
        raise AssertionError("unexpected")

    return respond


async def test_design_job_runs_parallel_autofill_when_enabled(monkeypatch):
    """[2026-06-10 병렬화] SPACK 확정 시 훅이 generate task 를 띄우고, design 저장 후
    merge_and_save_fills 로 회수한다 → 결과 dict 에 autofill 요약. team_id 전달 +
    병렬 모드 emit_progress=False 까지 확인.
    """
    from app.pipelines import api_spec_autofill_pipeline as autofill_mod

    monkeypatch.setattr(settings, "DESIGN_AUTOFILL_API_SPECS", True)
    # 비용 가드 — 실 Neo4j 조회 회피.
    async def _not_over(_email):
        return False
    monkeypatch.setattr(jobs, "_is_over_token_quota", _not_over)

    seen = {}

    async def _fake_generate(ctx, apis, *, fallback_model=None, emit_progress=True):
        seen["apis"] = apis
        seen["emit_progress"] = emit_progress
        return {apis[0].id: autofill_mod.FilledApiSpec(id=apis[0].id, generated=True)}

    async def _fake_merge(project_name, apis, generated_map, *, team_id=""):
        seen["merge"] = (project_name, team_id, sorted(generated_map))
        return autofill_mod.AutofillResult(
            apis=[], meta={"total": 1, "targetCount": 1, "generatedCount": 1},
        )

    monkeypatch.setattr(autofill_mod, "generate_api_spec_fills", _fake_generate)
    monkeypatch.setattr(autofill_mod, "merge_and_save_fills", _fake_merge)

    gemini = FakeGemini(_responder_with_api())
    ctx = {"job_id": "design-af-1", "gemini": gemini, "neo4j": _prd_neo()}

    result = await design_pipeline_job(
        ctx, project_name="p", user_email="u@e.com", team_id="team-9"
    )

    assert result["autofill"] == {"total": 1, "targetCount": 1, "generatedCount": 1}
    # 훅이 정규화된 in-memory SPACK API 로 입력을 구성 (Neo4j 재조회 없음).
    assert [a.id for a in seen["apis"]] == ["API-01"]
    # 병렬 모드 — autofill stage 마커가 design 마커와 섞이지 않게 emit 생략.
    assert seen["emit_progress"] is False
    # design 이 저장한 스코프와 동일한 team_id 로 저장.
    assert seen["merge"] == ("p", "team-9", ["API-01"])


async def test_design_job_skips_autofill_when_disabled():
    """토글 off → 훅이 task 를 만들지 않음, 결과 autofill=None."""
    gemini = FakeGemini(_responder_with_api())
    ctx = {"job_id": "design-af-2", "gemini": gemini, "neo4j": _prd_neo()}

    result = await design_pipeline_job(ctx, project_name="p", user_email="u@e.com")

    assert result["autofill"] is None


async def test_design_job_autofill_failure_does_not_break_result(monkeypatch):
    """병렬 autofill 생성이 터져도 design 결과는 그대로 반환(격리)."""
    from app.pipelines import api_spec_autofill_pipeline as autofill_mod

    monkeypatch.setattr(settings, "DESIGN_AUTOFILL_API_SPECS", True)
    async def _not_over(_email):
        return False
    monkeypatch.setattr(jobs, "_is_over_token_quota", _not_over)

    async def _boom(*_a, **_k):
        raise RuntimeError("autofill blew up")

    monkeypatch.setattr(autofill_mod, "generate_api_spec_fills", _boom)

    gemini = FakeGemini(_responder_with_api())
    ctx = {"job_id": "design-af-3", "gemini": gemini, "neo4j": _prd_neo()}

    result = await design_pipeline_job(ctx, project_name="p", user_email="u@e.com")

    # design 결과 키는 정상, autofill 만 None.
    assert result["project_name"] == "p"
    assert "spack" in result
    assert result["autofill"] is None


async def test_design_job_cancels_autofill_task_on_pipeline_error(monkeypatch):
    """design 본체가 SPACK 이후 단계에서 죽으면, 돌고 있던 병렬 autofill task 가
    finally 에서 cancel 된다 — 설계 실패 후 토큰만 태우는 누수 방지.
    """
    from app.pipelines import api_spec_autofill_pipeline as autofill_mod

    monkeypatch.setattr(settings, "DESIGN_AUTOFILL_API_SPECS", True)
    async def _not_over(_email):
        return False
    monkeypatch.setattr(jobs, "_is_over_token_quota", _not_over)

    started = {"flag": False}
    cancelled = {"flag": False}

    async def _slow_generate(ctx, apis, **_k):
        started["flag"] = True
        try:
            await asyncio.sleep(60)   # design 실패 시점까지 안 끝나는 생성
        except asyncio.CancelledError:
            cancelled["flag"] = True
            raise
        return {}

    monkeypatch.setattr(autofill_mod, "generate_api_spec_fills", _slow_generate)

    # SPACK 은 성공(API 1개 → 훅이 task 시작), DDD 프롬프트에서 폭발.
    def _respond(prompt: str) -> str:
        if "수석 테크니컬 아키텍트" in prompt:
            return json.dumps({
                "apis": [{"id": "API-01", "name": "Create X", "method": "POST",
                          "endpoint": "/x", "description": "d",
                          "error_cases": [], "auth": {}}],
                "entities": [], "policies": [],
            })
        raise RuntimeError("ddd stage blew up")

    gemini = FakeGemini(_respond)
    ctx = {"job_id": "design-af-4", "gemini": gemini, "neo4j": _prd_neo()}

    result = await design_pipeline_job(ctx, project_name="p", user_email="u@e.com")

    assert result["result"] == "error"
    # 핵심 불변식: 잡 종료 후 autofill 생성 task 가 계속 돌고 있지 않다.
    # FakeGemini 는 suspend 없이 즉시 반환하므로 task 가 시작 전에 취소될 수
    # 있다(스케줄만 되고 미실행) — 시작됐다면 반드시 CancelledError 를 받아야 한다.
    assert (not started["flag"]) or cancelled["flag"]


async def test_parallel_autofill_overlaps_design_stages(monkeypatch):
    """[2026-06-10 성능 검증] 훅이 띄운 생성 task 는 design 단계(모사 sleep)와 겹쳐
    돌아, 회수(_finish) 시점의 잔여(tail) 대기가 ≈0 이어야 한다.
    직렬이었다면 tail == 생성 시간(0.2s) 전부였을 것.
    """
    from app.pipelines import api_spec_autofill_pipeline as autofill_mod

    monkeypatch.setattr(settings, "DESIGN_AUTOFILL_API_SPECS", True)
    async def _not_over(_email):
        return False
    monkeypatch.setattr(jobs, "_is_over_token_quota", _not_over)

    GEN_SEC = 0.2

    async def _timed_generate(ctx, apis, **_k):
        await asyncio.sleep(GEN_SEC)
        return {}   # 빈 map → merge 는 저장 no-op (Neo4j 미접근)

    monkeypatch.setattr(autofill_mod, "generate_api_spec_fills", _timed_generate)

    hook, state = jobs._make_autofill_hook(object(), user_email="u@e.com")
    hook([{"id": "API-01", "name": "Create X", "method": "POST",
           "endpoint": "/x", "description": "d", "error_cases": [], "auth": {}}])
    assert state["task"] is not None

    # DDD/Architecture LLM 모사 — 생성(0.2s)보다 길게 design 이 돈다.
    await asyncio.sleep(GEN_SEC + 0.1)

    import time as _time
    t0 = _time.monotonic()
    meta = await jobs._finish_parallel_autofill(state, "p", "")
    tail = _time.monotonic() - t0

    assert meta is not None            # 회수 성공 (빈 결과라도 meta 반환)
    assert tail < GEN_SEC / 2, f"생성이 design 과 안 겹침 — tail={tail:.3f}s"


async def test_design_job_autofill_time_budget_preserves_result(monkeypatch):
    """[2026-06] autofill 생성이 잔여(tail) 예산을 초과하면(TimeoutError) design 잡을
    통째로 죽이지 않고 autofill 만 잘린다 — design 결과는 보존, autofill=None.
    """
    from app.pipelines import api_spec_autofill_pipeline as autofill_mod

    monkeypatch.setattr(settings, "DESIGN_AUTOFILL_API_SPECS", True)
    async def _not_over(_email):
        return False
    monkeypatch.setattr(jobs, "_is_over_token_quota", _not_over)

    async def _slow_generate(*_a, **_k):
        await asyncio.sleep(10)  # 예산보다 길게
        return {}

    monkeypatch.setattr(autofill_mod, "generate_api_spec_fills", _slow_generate)
    # 잔여 예산을 아주 짧게 줄여 빠르게 timeout 유발.
    monkeypatch.setattr(settings, "DESIGN_AUTOFILL_BUDGET_SEC", 0.05)

    gemini = FakeGemini(_responder_with_api())
    ctx = {"job_id": "design-af-5", "gemini": gemini, "neo4j": _prd_neo()}

    result = await design_pipeline_job(ctx, project_name="p", user_email="u@e.com")

    assert result["project_name"] == "p"
    assert "spack" in result
    assert result["autofill"] is None


async def test_design_job_provider_quota_returns_friendly_error():
    """[2026-06-10 실사고] Gemini 공급자측 429(선불 크레딧 소진/RESOURCE_EXHAUSTED) —
    generic '설계 생성 중 오류' 대신 한도 안내 메시지로 반환 (재시도 오안내 방지).
    """
    from app.clients.gemini_client import GeminiError

    def _respond(prompt: str) -> str:
        raise GeminiError("Gemini 429: prepayment credits depleted", kind="quota")

    gemini = FakeGemini(_respond)
    ctx = {"job_id": "design-q-3", "gemini": gemini, "neo4j": _prd_neo()}

    result = await design_pipeline_job(ctx, project_name="p", user_email="u@e.com")

    assert result["result"] == "error"
    assert "한도" in result["error"]
