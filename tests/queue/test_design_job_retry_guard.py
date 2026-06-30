"""
design_pipeline_job 이 예상치 못한 예외를 잡아 {result:'error'} 로 변환하는지 —
arq(max_tries) 가 3-LLM 파이프라인을 처음(SPACK)부터 재실행해 진행바가
Architecture → SPACK 으로 후퇴하는 현상을 차단한다.

[배경] Neo4j 최종 commit(execute_write transient 재시도) + Gemini(키 로테이션·
모델 폴백 재시도)가 일시 오류를 이미 처리하므로, 여기까지 전파된 예외는 결정적
오류 — 재실행해도 같은 곳에서 깨질 뿐이라 raise 대신 결과로 반환한다.
"""
from __future__ import annotations

import pytest

from app.queue import jobs as jobs_module
from app.queue.jobs import design_pipeline_job
from tests.conftest import FakeGemini, FakeNeo4j, FakeRedis, make_arq_ctx

pytestmark = pytest.mark.asyncio


async def test_design_job_converts_unexpected_exception_to_error_result(monkeypatch):
    """결정적 예외 → raise 하지 않고 {result:'error'} 반환 (arq 재시도 차단)."""
    async def _boom(*a, **k):
        # 예: Architecture 단계 normalize 실패 / 비-transient Neo4j 오류 등
        raise RuntimeError("architecture normalize exploded")

    monkeypatch.setattr(jobs_module, "run_design_pipeline", _boom)

    ctx = make_arq_ctx(
        job_id="d-boom", gemini=FakeGemini(responses=[]),
        neo4j=FakeNeo4j(), redis=FakeRedis(),
    )

    # raise 되면 안 됨 — arq 가 재시도하면서 진행바가 후퇴하는 원인이므로.
    result = await design_pipeline_job(ctx, project_name="proj-x", user_email=None)

    assert result["result"] == "error"
    assert result["project_name"] == "proj-x"
    # 사용자에게 보일 친화적 안내 (FE 가 result.error 를 토스트로 표시).
    assert result["error"]
    assert isinstance(result["error"], str)


async def test_design_job_does_not_reraise(monkeypatch):
    """어떤 예외도 job 밖으로 새지 않아야 한다 (arq 재시도 트리거 방지)."""
    async def _boom(*a, **k):
        raise ValueError("any deterministic failure")

    monkeypatch.setattr(jobs_module, "run_design_pipeline", _boom)

    ctx = make_arq_ctx(
        job_id="d-noreraise", gemini=FakeGemini(responses=[]),
        neo4j=FakeNeo4j(), redis=FakeRedis(),
    )

    # 예외가 전파되면 이 호출이 raise 하고 테스트가 실패한다.
    result = await design_pipeline_job(ctx, project_name="p", user_email=None)
    assert result["result"] == "error"
