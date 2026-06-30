"""
recommend_skills_job 단위 테스트 — 결과가 직렬화 가능한 dict 인지.
"""
from __future__ import annotations

import json

import pytest

from app.queue.jobs import recommend_skills_job
from tests.conftest import FakeGemini, FakeNeo4j


pytestmark = pytest.mark.asyncio


def _responder():
    def respond(prompt: str) -> str:
        return json.dumps(
            {
                "recommended": [
                    # 0.92 — [2026-06 추천 신뢰 정책] 0.90 미만은 파이프라인에서 drop
                    {"id": "SKL-01", "reason": "ok", "confidence": 0.92}
                ]
            }
        )

    return respond


async def test_recommend_skills_job_returns_serializable_dict():
    gemini = FakeGemini(_responder())
    neo = FakeNeo4j(
        responses=[
            [{"cps_content": "cps", "prd_content": "prd"}]
        ]
    )
    ctx = {"job_id": "r-1", "gemini": gemini, "neo4j": neo}

    result = await recommend_skills_job(
        ctx,
        project_name="proj",
        skill_catalog=[
            {"id": "SKL-01", "name": "Auth", "description": "", "category": ""}
        ],
        allowed_categories=[],
    )
    # JSON 라운드트립 가능
    json.loads(json.dumps(result, ensure_ascii=False))

    assert len(result["recommended"]) == 1
    assert result["recommended"][0]["id"] == "SKL-01"
    assert "meta" in result
