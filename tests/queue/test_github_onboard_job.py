"""
github_onboard_job 직렬화 + delegation 검증.

[검증]
- result 가 JSON 직렬화 가능 (arq result store 정합성).
- CpsResult 필드가 flat dict 로 변환됨.
- user_token 이 GitHubClient 에 전달됨 (private repo 대응).
- user_email 이 없으면 quota 누적 skip (warning 만).
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.pipelines.cps_pipeline.types import CpsResult
from app.pipelines.github_onboard_pipeline import GithubOnboardResult
from app.queue.jobs import github_onboard_job
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio


def _mock_cps() -> CpsResult:
    return CpsResult(
        meeting_log_id="log_x",
        delta_cps_id="cps_x_v1",
        master_cps_id="cps_master_x",
        mode="first_run",
        diagnostic={"k": "v"},
        cps_graph={"nodes": [], "edges": []},
        extraction_mode="strict",
    )


def _mock_prd() -> "PrdResult":
    from app.pipelines.prd_pipeline import PrdResult
    return PrdResult(
        delta_prd_id="prd_x_v1",
        master_prd_id="prd_master_x",
        mode="first_run",
        diagnostic={"k": "v"},
    )


def _mock_result(
    *, project_name: str = "p", repo: str = "u/r", v1_size: int = 500,
    cps: bool = True,
) -> GithubOnboardResult:
    return GithubOnboardResult(
        project_name=project_name,
        github_url=f"https://github.com/{repo}",
        repo_full_name=repo,
        v1_markdown="markdown content " * 30,
        v1_markdown_size=v1_size,
        sampled_file_count=8,
        sampled_file_paths=["README.md", "src/main.py"],
        cps_result=_mock_cps() if cps else None,
        prd_result=_mock_prd() if cps else None,
        diagnostic={"is_private": False, "fetched_count": 8},
    )


async def test_github_onboard_job_returns_serializable(monkeypatch):
    """결과가 JSON 직렬화 가능 + CpsResult 의 master/delta/mode 노출."""
    captured: dict = {}

    async def _stub_pipeline(ctx, payload, github_client=None):
        captured["payload"] = payload
        captured["github_client"] = github_client
        return _mock_result()

    monkeypatch.setattr(
        "app.queue.jobs.run_github_onboard_pipeline", _stub_pipeline,
    )

    gemini = FakeGemini(responses=[])
    neo = FakeNeo4j(responses=[])
    arq_ctx = {
        "job_id": "onb-1",
        "gemini": gemini,
        "neo4j": neo,
    }

    out = await github_onboard_job(
        arq_ctx,
        project_name="p",
        github_url="https://github.com/u/r",
        user_token=None,
        user_email=None,
    )
    # JSON 라운드트립 — arq 가 결과 직렬화 후 Redis 저장.
    json.loads(json.dumps(out, ensure_ascii=False))
    assert out["project_name"] == "p"
    assert out["repo_full_name"] == "u/r"
    assert out["v1_markdown_size"] == 500
    assert out["sampled_file_count"] == 8
    assert out["sampled_file_paths"] == ["README.md", "src/main.py"]
    assert out["cps_master_id"] == "cps_master_x"
    assert out["cps_delta_id"] == "cps_x_v1"
    assert out["cps_mode"] == "first_run"
    # [2026-05-27] PRD 필드도 평탄화 노출 — design 단계 진입 가능 확인용.
    assert out["prd_master_id"] == "prd_master_x"
    assert out["prd_delta_id"] == "prd_x_v1"
    assert out["prd_mode"] == "first_run"
    assert out["diagnostic"]["is_private"] is False


async def test_github_onboard_job_passes_user_token_to_github_client(monkeypatch):
    """user_token 이 GitHubClient(user_token=...) 로 주입됨."""
    captured: dict = {}

    async def _stub_pipeline(ctx, payload, github_client=None):
        captured["github_client_token"] = getattr(github_client, "_user_token", None)
        return _mock_result()

    monkeypatch.setattr(
        "app.queue.jobs.run_github_onboard_pipeline", _stub_pipeline,
    )

    arq_ctx = {"job_id": "onb-2", "gemini": FakeGemini(responses=[]), "neo4j": FakeNeo4j()}
    await github_onboard_job(
        arq_ctx,
        project_name="p",
        github_url="https://github.com/u/r",
        user_token="ghp_secret",
        user_email="u@x",
    )
    assert captured["github_client_token"] == "ghp_secret"


async def test_github_onboard_job_no_token_anonymous(monkeypatch):
    """user_token=None → anonymous GitHubClient (env GITHUB_TOKEN fallback)."""
    captured: dict = {}

    async def _stub_pipeline(ctx, payload, github_client=None):
        captured["github_client_token"] = getattr(github_client, "_user_token", None)
        return _mock_result()

    monkeypatch.setattr(
        "app.queue.jobs.run_github_onboard_pipeline", _stub_pipeline,
    )

    arq_ctx = {"job_id": "onb-3", "gemini": FakeGemini(responses=[]), "neo4j": FakeNeo4j()}
    await github_onboard_job(
        arq_ctx,
        project_name="p", github_url="https://github.com/u/r",
        user_token=None, user_email=None,
    )
    assert captured["github_client_token"] is None


async def test_github_onboard_job_handles_none_cps_result(monkeypatch):
    """cps_result 가 None 인 케이스 (이론상 일어나지 않지만 안전망) → cps_* 필드 None."""
    async def _stub_pipeline(ctx, payload, github_client=None):
        return _mock_result(cps=False)

    monkeypatch.setattr(
        "app.queue.jobs.run_github_onboard_pipeline", _stub_pipeline,
    )

    arq_ctx = {"job_id": "onb-4", "gemini": FakeGemini(responses=[]), "neo4j": FakeNeo4j()}
    out = await github_onboard_job(
        arq_ctx,
        project_name="p", github_url="https://github.com/u/r",
        user_token=None, user_email=None,
    )
    assert out["cps_master_id"] is None
    assert out["cps_delta_id"] is None
    assert out["cps_mode"] is None
