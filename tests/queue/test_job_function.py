"""
arq job function 단위 테스트.

Worker context (gemini/neo4j) 를 fake 로 주입하고 job 이 dict 결과를 만드는지 검증.
실제 Redis/arq 인프라는 통합 테스트(별도)에서 검증.
"""
from __future__ import annotations

import json

import pytest

from app.queue.jobs import cps_pipeline_job
from tests.conftest import FakeGemini, FakeNeo4j


pytestmark = pytest.mark.asyncio


def _make_responder():
    def respond(prompt: str) -> str:
        if "당신은 '통합 하네스 아키텍트'" in prompt:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "id": "doc_cps_proj_v1_1",
                            "label": "CPS_Document",
                            "properties": {"project": "proj", "is_latest": True},
                        },
                        {"id": "prb_01", "label": "Problem", "properties": {"summary": "x"}},
                    ],
                    "relationships": [
                        {"source": "prb_01", "type": "EXTRACTED_FROM", "target": "doc_cps_proj_v1_1"},
                    ],
                },
                ensure_ascii=False,
            )
        if "문서 영향 범위 분석" in prompt:
            return json.dumps({"affected_sections": []})
        if "시맨틱(의미 기반)으로 병합" in prompt:
            return "## 📄 CPS\n\n### 2. Problem\n- **[PRB-01] x**: y\n"
        raise AssertionError("unexpected prompt")

    return respond


async def test_cps_pipeline_job_returns_serializable_dict():
    gemini = FakeGemini(_make_responder())
    neo = FakeNeo4j(
        responses=[
            [],  # save cps
            [],  # save meeting log
            # Get All CPS — no master/delta
            [
                {
                    "master_id": None,
                    "master_content": "",
                    "master_probs": [],
                    "latest_id": None,
                    "latest_content": "",
                    "latest_probs": [],
                    "project_name": "proj",
                }
            ],
            [],  # merge master
        ]
    )
    # arq 가 주입하는 ctx 를 흥내냄
    ctx = {"job_id": "fake-uuid-123", "gemini": gemini, "neo4j": neo}

    result = await cps_pipeline_job(
        ctx,
        project_name="proj",
        version="v1.1",
        date="2026-05-12",
        meeting_content="hello",
    )

    assert isinstance(result, dict)
    # 모든 키가 직렬화 가능한지: dict→json→dict 라운드트립
    json.loads(json.dumps(result, ensure_ascii=False))

    assert result["meeting_log_id"] == "log_proj_v1_1"
    assert result["master_cps_id"] == "doc_cps_master_proj"
    assert result["mode"] == "first_run"
    assert "filter" in result["diagnostic"]
