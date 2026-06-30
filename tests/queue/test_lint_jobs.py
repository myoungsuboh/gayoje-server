"""run_lint_job + generate_fix_spec_job 직렬화 검증."""
from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from app.queue.jobs import generate_fix_spec_job, run_lint_job
from tests.conftest import FakeGemini, FakeNeo4j


pytestmark = pytest.mark.asyncio


async def test_generate_fix_spec_job_returns_serializable():
    gemini = FakeGemini(lambda p: "# Fix spec markdown")
    neo = FakeNeo4j(
        responses=[
            # Get Full Spec
            [
                {
                    "apis": [],
                    "entities": [],
                    "policies": [],
                    "contexts": [],
                    "aggregates": [],
                    "domain_entities": [],
                    "domain_events": [],
                    "services": [],
                    "databases": [],
                    "rules": [],
                }
            ]
        ]
    )
    ctx = {"job_id": "fs-1", "gemini": gemini, "neo4j": neo}
    out = await generate_fix_spec_job(
        ctx,
        project_name="x",
        github_url="https://github.com/o/r",
        lint_result={
            "score": 50,
            "cases": [
                {
                    "title": "SPACK",
                    "convergence": 50,
                    "rules": [{"rule": "r-1", "applied": False, "description": "x"}],
                }
            ],
        },
    )
    # JSON 라운드트립
    json.loads(json.dumps(out, ensure_ascii=False))
    assert out["success"] is True
    assert "markdown" in out
    assert out["metadata"]["totalFailed"] == 1
