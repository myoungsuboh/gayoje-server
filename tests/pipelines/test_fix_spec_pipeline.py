"""fix_spec_pipeline 단위 + e2e 테스트."""
from __future__ import annotations

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.fix_spec_pipeline import (
    FixSpecInput,
    _parse_failures,
    run_fix_spec_pipeline,
)
from tests.conftest import FakeGemini, FakeNeo4j


# 동기/비동기 혼재 — async 만 데코레이터로 마킹.


# ─── _parse_failures ────────────────────────────────────────────


def _make_lint_result(score=80, applied_all=False):
    return {
        "score": score,
        "cases": [
            {
                "title": "SPACK 준수율",
                "convergence": 100 if applied_all else 50,
                "rules": [
                    {"rule": "r-1", "description": "d1", "applied": True},
                    {
                        "rule": "r-2",
                        "description": "d2",
                        "applied": True if applied_all else False,
                    },
                ],
            },
            {
                "title": "DDD 준수율",
                "convergence": 100 if applied_all else 50,
                "rules": [
                    {
                        "rule": "r-3",
                        "description": "d3",
                        "applied": True if applied_all else False,
                    }
                ],
            },
        ],
    }


def test_parse_failures_extracts_owner_repo_and_unapplied():
    payload = FixSpecInput(
        project_name="x",
        github_url="https://github.com/owner/repo.git",
        lint_result=_make_lint_result(score=70, applied_all=False),
    )
    out = _parse_failures(payload)
    assert out["owner"] == "owner"
    assert out["repo"] == "repo"
    assert out["githubUrl"] == "https://github.com/owner/repo"
    assert out["score"] == 70
    assert out["totalFailed"] == 2
    assert len(out["failedByCategory"]) == 2  # 두 카테고리 모두 실패 있음


def test_parse_failures_returns_zero_when_all_applied():
    payload = FixSpecInput(
        project_name="x",
        github_url="https://github.com/owner/repo",
        lint_result=_make_lint_result(score=100, applied_all=True),
    )
    out = _parse_failures(payload)
    assert out["totalFailed"] == 0
    assert out["hasFailures"] is False
    assert out["failedByCategory"] == []


def test_parse_failures_with_empty_lint_result():
    payload = FixSpecInput(
        project_name="x",
        github_url="https://github.com/o/r",
        lint_result={},
    )
    out = _parse_failures(payload)
    assert out["totalFailed"] == 0
    assert out["hasFailures"] is False


# ─── run_fix_spec_pipeline e2e ─────────────────────────────────


@pytest.mark.asyncio
async def test_fix_spec_early_return_when_no_failures():
    payload = FixSpecInput(
        project_name="x",
        github_url="https://github.com/o/r",
        lint_result=_make_lint_result(score=100, applied_all=True),
    )
    gemini = FakeGemini(lambda p: "no call")
    neo = FakeNeo4j()
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="fs1")

    result = await run_fix_spec_pipeline(ctx, payload)
    assert result.success is True
    assert result.markdown is None
    assert "100%" in result.message
    # LLM/Neo4j 호출 없음
    assert len(gemini.calls) == 0
    assert len(neo.executed) == 0


@pytest.mark.asyncio
async def test_fix_spec_full_flow_returns_markdown():
    payload = FixSpecInput(
        project_name="x",
        github_url="https://github.com/o/r",
        lint_result=_make_lint_result(score=60, applied_all=False),
    )

    def respond(prompt: str) -> str:
        # fix_spec 프롬프트 확인
        assert "수정 명세서" in prompt or "Fix Spec" in prompt or "명세 작성 전문가" in prompt
        return "# 🎯 작업 개요\n현재 점수 60% → ..."

    gemini = FakeGemini(respond)
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
                    "rules": [{"id": "SKL-01", "name": "Java Naming"}],
                }
            ]
        ]
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="fs2")

    result = await run_fix_spec_pipeline(ctx, payload)
    assert result.success is True
    assert result.markdown is not None
    assert "작업 개요" in result.markdown
    assert result.filename and result.filename.endswith(".md")
    assert result.metadata["currentScore"] == 60
    assert result.metadata["totalFailed"] == 2
