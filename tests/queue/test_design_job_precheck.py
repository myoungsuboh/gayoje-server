"""
design_pipeline_job 이 DesignPrecheckFailed 를 잡아 {result:'precheck_failed'} 로
변환하는지 — arq 재시도 없이 FE 가 즉시 명확한 안내를 받도록 (cancelled 패턴 미러).
"""
from __future__ import annotations

import pytest

from app.queue import jobs as jobs_module
from app.queue.jobs import design_pipeline_job
from app.pipelines.design_pipeline import DesignPrecheckFailed
from tests.conftest import FakeGemini, FakeNeo4j, FakeRedis, make_arq_ctx

pytestmark = pytest.mark.asyncio


async def test_design_job_converts_precheck_failed(monkeypatch):
    async def _raise(*a, **k):
        raise DesignPrecheckFailed("PRD 가 누더기 상태입니다", diagnostic={"size_bytes": 40000})

    monkeypatch.setattr(jobs_module, "run_design_pipeline", _raise)

    ctx = make_arq_ctx(
        job_id="d-precheck", gemini=FakeGemini(responses=[]),
        neo4j=FakeNeo4j(), redis=FakeRedis(),
    )

    result = await design_pipeline_job(ctx, project_name="x", user_email=None)

    assert result["result"] == "precheck_failed"
    assert "누더기" in result["message"]
    assert result["diagnostic"]["size_bytes"] == 40000
    assert result["project_name"] == "x"
