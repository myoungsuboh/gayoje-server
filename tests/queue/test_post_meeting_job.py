"""
post_meeting_pipeline_job 부분 실패 회귀 가드.

postMeeting = CPS + PRD 체이닝. 실패 시 동작:
  - CPS 도중 실패  → exception propagate, stage=cps_running, token 적재
  - CPS 성공 + PRD 실패 → exception propagate, stage=prd_running, token 적재
  - 모두 성공         → result dict, stage=done

핵심: _persist_token_usage 는 finally 절 안 (실패해도 LLM 비용만큼 적재).
"""
from __future__ import annotations

import pytest

from app.queue import jobs as jobs_module
from app.queue.jobs import post_meeting_pipeline_job
from tests.conftest import FakeGemini, FakeNeo4j, FakeRedis, make_arq_ctx

pytestmark = pytest.mark.asyncio


@pytest.fixture
def _no_usage_lookup(monkeypatch):
    """user_email 관련 DB 조회 완전 우회 — 항상 free 반환."""
    async def _get(_email):
        return None
    monkeypatch.setattr(jobs_module.usage_repository, "get_usage", _get)


@pytest.fixture
def _captured_token_calls(monkeypatch):
    """_persist_token_usage 호출 회수 추적 — finally 절 호출 검증 용."""
    calls = []

    async def _persist(user_email, accumulator, *, job_id, bucket="main"):
        calls.append({
            "email": user_email,
            "tokens": accumulator.total.total_tokens,
            "job_id": job_id,
            "bucket": bucket,
        })

    monkeypatch.setattr(jobs_module, "_persist_token_usage", _persist)
    return calls


class _CpsStub:
    """run_cps_pipeline 의 결과 객체 포함 속성 stub."""
    meeting_log_id = "log_x_v1"
    delta_cps_id = "doc_cps_x_v1"
    master_cps_id = "doc_cps_master_x"
    cps_graph = {"nodes": [], "relationships": []}
    mode = "first_run"
    diagnostic: dict = {}
    # [2026-05-25] CPS Agent 추출 모드 — 새 필드 (기본 strict).
    extraction_mode: str = "strict"
    extraction_warning = None


class _PrdStub:
    delta_prd_id = "doc_prd_x_v1"
    master_prd_id = "doc_prd_master_x"
    mode = "first_run"
    diagnostic: dict = {}


def _stub_extract():
    """_get_or_compute_extract 가 반환하는 extract dict stub.
    cps_graph 는 _prd_extract_from_cache(parse_cps_for_prd) 가 견딜 수 있게 최소 형태."""
    return {
        "cps_graph": {"nodes": [], "relationships": []},
        "prd_markdown": "# PRD",
        "prd_graph": {"nodes": [], "relationships": []},
    }


async def test_post_meeting_happy_path_sets_all_stages_in_order(
    monkeypatch, _no_usage_lookup, _captured_token_calls
):
    """CPS+PRD 모두 성공 → stage 순서: cps_running → prd_running → done.

    [batch 파이프라이닝] post_meeting 은 이제 extract(캐시) → cps_merge → prd_merge
    구조. extract 는 stub, merge 함수 단위로 mock 해 job 오케스트레이션(stage/token)만 검증.
    """
    async def _extract(*a, **k): return _stub_extract()
    async def _cps(*a, **k): return _CpsStub()
    # [2026-06-04] 오버랩: post_meeting 은 run_prd_merge 대신 _prd_merge_compute(commit
    # 클로저 반환)를 호출. compute 를 mock 해 commit 이 _PrdStub 을 돌려주게 한다.
    async def _prd_commit(): return _PrdStub()
    async def _prd_compute(*a, **k): return _prd_commit
    monkeypatch.setattr(jobs_module, "_get_or_compute_extract", _extract)
    monkeypatch.setattr(jobs_module, "run_cps_merge", _cps)
    monkeypatch.setattr(jobs_module, "_prd_merge_compute", _prd_compute)

    redis = FakeRedis()
    arq_ctx = make_arq_ctx(
        job_id="job-1",
        gemini=FakeGemini(responses=[]),
        neo4j=FakeNeo4j(),
        redis=redis,
    )

    result = await post_meeting_pipeline_job(
        arq_ctx,
        project_name="x", version="v1", date="d", meeting_content="m",
        user_email=None,
    )

    # result 구조
    assert result["cps"]["meeting_log_id"] == "log_x_v1"
    assert result["prd"]["delta_prd_id"] == "doc_prd_x_v1"
    # stage 마지막 값 = done
    assert redis.store["harness:job:job-1:stage"] == "done"
    # finally 절 호출
    assert len(_captured_token_calls) == 1
    assert _captured_token_calls[0]["job_id"] == "job-1"


async def test_post_meeting_prd_deterministic_failure_degrades_to_error_not_raise(
    monkeypatch, _no_usage_lookup, _captured_token_calls
):
    """[R1] CPS 성공 + PRD **결정적** 실패(ValueError/RuntimeError: orphan·빈-merge 등) → job 은
    성공하고 prd.mode='error' 로 강등(raise 안 함). 기존엔 CPS 커밋 후 PRD raise 가 전체 job 을
    깨 'CPS 가득/PRD 빈 + arq 무한 재시도' 비대칭을 만들었다 — 그 구조적 원천을 차단. CPS 결과는
    정상 노출되고 token 적재(finally)도 유지."""
    async def _extract(*a, **k): return _stub_extract()
    async def _cps(*a, **k): return _CpsStub()
    async def _prd_commit_fail(): raise RuntimeError("orphan/빈-merge 결정적 실패")
    async def _prd_compute(*a, **k): return _prd_commit_fail
    monkeypatch.setattr(jobs_module, "_get_or_compute_extract", _extract)
    monkeypatch.setattr(jobs_module, "run_cps_merge", _cps)
    monkeypatch.setattr(jobs_module, "_prd_merge_compute", _prd_compute)

    redis = FakeRedis()
    arq_ctx = make_arq_ctx(
        job_id="job-2",
        gemini=FakeGemini(responses=[]),
        neo4j=FakeNeo4j(),
        redis=redis,
    )

    # raise 하지 않고 정상 반환 (job 성공)
    result = await post_meeting_pipeline_job(
        arq_ctx,
        project_name="x", version="v1", date="d", meeting_content="m",
        user_email="user@x.com",
    )

    assert result["cps"]["master_cps_id"] == "doc_cps_master_x"   # CPS 정상
    assert result["prd"]["mode"] == "error"                       # PRD 는 error 강등
    assert redis.store["harness:job:job-2:stage"] == "done"       # job 완료(재시도 안 함)
    # finally 절 호출 — token 적재
    assert len(_captured_token_calls) == 1
    assert _captured_token_calls[0]["email"] == "user@x.com"


async def test_post_meeting_prd_transient_failure_still_propagates(
    monkeypatch, _no_usage_lookup, _captured_token_calls
):
    """[R1] PRD **비결정적**(transient: LLM 5xx/타임아웃/네트워크) 실패는 강등하지 않고 그대로
    전파 → arq 재시도(CPS 는 멱등 재실행)로 일시 오류 회복 기회 보존. ValueError/RuntimeError
    (결정적)만 error 로 강등 대상."""
    async def _extract(*a, **k): return _stub_extract()
    async def _cps(*a, **k): return _CpsStub()
    async def _prd_commit_transient(): raise TimeoutError("일시적 DB 지연")
    async def _prd_compute(*a, **k): return _prd_commit_transient
    monkeypatch.setattr(jobs_module, "_get_or_compute_extract", _extract)
    monkeypatch.setattr(jobs_module, "run_cps_merge", _cps)
    monkeypatch.setattr(jobs_module, "_prd_merge_compute", _prd_compute)

    redis = FakeRedis()
    arq_ctx = make_arq_ctx(
        job_id="job-2t",
        gemini=FakeGemini(responses=[]),
        neo4j=FakeNeo4j(),
        redis=redis,
    )

    with pytest.raises(TimeoutError):
        await post_meeting_pipeline_job(
            arq_ctx,
            project_name="x", version="v1", date="d", meeting_content="m",
            user_email="user@x.com",
        )

    # finally 절 호출 — 실패해도 token 적재 (회귀 가드)
    assert len(_captured_token_calls) == 1


async def test_post_meeting_cps_failure_stops_at_cps_running(
    monkeypatch, _no_usage_lookup, _captured_token_calls
):
    """CPS(merge) 실패 → PRD 자체가 호출 안 됨, stage=cps_running."""
    async def _extract(*a, **k): return _stub_extract()
    async def _cps_fail(*a, **k): raise RuntimeError("CPS LLM 오류")
    # 오버랩: PRD compute 는 동시 실행될 수 있으나, CPS 실패 시 PRD **commit(쓰기)** 은
    # 절대 실행되면 안 됨 (cps_result 대기에서 raise → commit 도달 못 함).
    async def _prd_commit_should_not_run():
        raise AssertionError("PRD commit(쓰기)은 CPS 실패 시 호출되면 안 됨")
    async def _prd_compute(*a, **k): return _prd_commit_should_not_run
    monkeypatch.setattr(jobs_module, "_get_or_compute_extract", _extract)
    monkeypatch.setattr(jobs_module, "run_cps_merge", _cps_fail)
    monkeypatch.setattr(jobs_module, "_prd_merge_compute", _prd_compute)

    redis = FakeRedis()
    arq_ctx = make_arq_ctx(
        job_id="job-3",
        gemini=FakeGemini(responses=[]),
        neo4j=FakeNeo4j(),
        redis=redis,
    )

    with pytest.raises(RuntimeError, match="CPS LLM 오류"):
        await post_meeting_pipeline_job(
            arq_ctx,
            project_name="x", version="v1", date="d", meeting_content="m",
            user_email=None,
        )

    assert redis.store["harness:job:job-3:stage"] == "cps_running"
    # finally 절 호출
    assert len(_captured_token_calls) == 1


async def test_post_meeting_returns_full_diagnostic_in_result(
    monkeypatch, _no_usage_lookup, _captured_token_calls
):
    """diagnostic / mode / id 가 dict 에 그대로 올라감 (FE 구조 의존)."""
    class _CpsWithDiag(_CpsStub):
        diagnostic = {"filter": {"mode": "FIRST_RUN"}}

    class _PrdWithDiag(_PrdStub):
        diagnostic = {"filter": {"mode": "FIRST_RUN"}}

    async def _extract(*a, **k): return _stub_extract()
    async def _cps(*a, **k): return _CpsWithDiag()
    async def _prd_commit(): return _PrdWithDiag()
    async def _prd_compute(*a, **k): return _prd_commit
    monkeypatch.setattr(jobs_module, "_get_or_compute_extract", _extract)
    monkeypatch.setattr(jobs_module, "run_cps_merge", _cps)
    monkeypatch.setattr(jobs_module, "_prd_merge_compute", _prd_compute)

    arq_ctx = make_arq_ctx(
        job_id="job-4",
        gemini=FakeGemini(responses=[]),
        neo4j=FakeNeo4j(),
        redis=FakeRedis(),
    )
    result = await post_meeting_pipeline_job(
        arq_ctx,
        project_name="x", version="v1", date="d", meeting_content="m",
        user_email=None,
    )

    assert result["cps"]["diagnostic"] == {"filter": {"mode": "FIRST_RUN"}}
    assert result["prd"]["diagnostic"] == {"filter": {"mode": "FIRST_RUN"}}
    assert result["cps"]["mode"] == "first_run"
    assert result["cps"]["master_cps_id"] == "doc_cps_master_x"
