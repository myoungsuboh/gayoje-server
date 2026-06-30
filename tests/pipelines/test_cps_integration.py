"""
Real Gemini + Neo4j integration test.

기본은 SKIP. 활성화 방법:
  RUN_INTEGRATION=1 GEMINI_API_KEY=... NEO4J_URI=... NEO4J_PASSWORD=... \
      pytest tests/pipelines/test_cps_integration.py -v

검증 목적:
  - 프롬프트가 실제 Gemini 응답 schema 와 호환되는지 (JSON 파싱 성공)
  - 1차 실행 → 2차 (incremental) 가 master 그래프를 누적 갱신하는지
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.clients.gemini_client import GeminiClient
from app.clients import neo4j_client
from app.pipelines.base import PipelineContext
from app.pipelines.cps_pipeline import CpsInput, run_cps_pipeline


pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


_FIXTURES = Path(__file__).parent / "fixtures"


class _NeoProxy:
    async def run_cypher(self, cypher, params=None):
        return await neo4j_client.run_cypher(cypher, params)


@pytest.fixture
def project_name() -> str:
    # 충돌 방지: 환경변수로 override 가능
    return os.getenv("INTEGRATION_PROJECT_NAME", "harness_int_test")


async def test_first_run_then_incremental(project_name):
    if not (os.getenv("GEMINI_API_KEY") and os.getenv("NEO4J_URI") and os.getenv("NEO4J_PASSWORD")):
        pytest.skip("credentials missing")

    ctx = PipelineContext(gemini=GeminiClient(), neo4j=_NeoProxy(), idempotency_key="int-1")

    # 첫 실행
    v1 = CpsInput(
        project_name=project_name,
        version="v1.1",
        date="2026-05-12",
        meeting_content=(_FIXTURES / "meeting_v1_simple.txt").read_text(encoding="utf-8"),
    )
    r1 = await run_cps_pipeline(ctx, v1)
    assert r1.mode == "first_run"
    assert r1.master_cps_id

    # 증분 실행
    v2 = CpsInput(
        project_name=project_name,
        version="v1.2",
        date="2026-05-19",
        meeting_content=(_FIXTURES / "meeting_v2_increment.txt").read_text(encoding="utf-8"),
    )
    r2 = await run_cps_pipeline(ctx, v2)
    assert r2.mode == "incremental"
    assert r2.master_cps_id == r1.master_cps_id

    # 마스터 마크다운에 v1 + v2 내용이 모두 흔적이 남아있는지 약식 확인
    rows = await neo4j_client.run_cypher(
        "MATCH (m:CPS_Document {id: $id}) RETURN m.full_markdown AS md",
        {"id": r2.master_cps_id},
    )
    md = (rows[0] or {}).get("md", "") if rows else ""
    assert "OCR" in md or "영수증" in md
