"""
lint_repository 단위 테스트:
- save_lint_result: id 생성 + base64 cases 인코딩
- get_last_lint_result: base64 cases 디코딩 + 미존재 → None
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

import pytest

from app.service import lint_repository
from app.service.lint_repository import (
    LintCase,
    LintCaseRule,
    LintResult,
)


pytestmark = pytest.mark.asyncio


class _Fake:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher, params=None, database=None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses=None):
        fake = _Fake(responses=responses)
        monkeypatch.setattr(
            "app.service.lint_repository.neo4j_client.run_cypher", fake
        )
        return fake

    return _setup


async def test_save_lint_result_encodes_cases_as_base64(fake_run):
    fake = fake_run([[{"saved_id": "lint-x-1"}]])
    cases = [
        LintCase(
            title="SPACK 준수율",
            convergence=80,
            rules=[
                LintCaseRule(rule="r-1", description="d", applied=True),
            ],
        )
    ]
    lint_id = await lint_repository.save_lint_result(
        project="x",
        github_url="https://github.com/a/b",
        score=80,
        scanned_files=10,
        rules_checked=4,
        violations=1,
        cases=cases,
    )
    assert lint_id.startswith("lint-x-")
    # base64 인코딩된 cases 가 파라미터로 전달돼야 함
    call = fake.calls[0]
    cases_b64 = call["params"]["cases_b64"]
    decoded = json.loads(base64.b64decode(cases_b64).decode("utf-8"))
    assert decoded[0]["title"] == "SPACK 준수율"
    assert decoded[0]["rules"][0]["rule"] == "r-1"


async def test_get_last_lint_decodes_cases(fake_run):
    cases_b64 = base64.b64encode(
        json.dumps(
            [{"title": "SPACK 준수율", "convergence": 90, "rules": []}],
            ensure_ascii=False,
        ).encode()
    ).decode()
    fake_run(
        [
            [
                {
                    "lint": {
                        "id": "lint-x-1",
                        "project": "x",
                        "githubUrl": "https://github.com/a/b",
                        "score": 90,
                        "scannedFiles": 5,
                        "rulesChecked": 4,
                        "violations": 0,
                        "cases": cases_b64,
                        "savedAt": 1700000000000,
                    }
                }
            ]
        ]
    )
    result = await lint_repository.get_last_lint_result("x", "https://github.com/a/b")
    assert isinstance(result, LintResult)
    assert result.score == 90
    assert len(result.cases) == 1
    assert result.cases[0].title == "SPACK 준수율"
    assert result.saved_at == 1700000000000


async def test_get_last_lint_returns_none_when_not_found(fake_run):
    fake_run([[]])
    out = await lint_repository.get_last_lint_result("x", "https://github.com/a/b")
    assert out is None


async def test_get_last_lint_handles_corrupt_cases_b64(fake_run):
    """base64 깨진 경우 빈 cases 로 graceful."""
    fake_run(
        [
            [
                {
                    "lint": {
                        "id": "lint-x-1",
                        "project": "x",
                        "githubUrl": "https://github.com/a/b",
                        "score": 50,
                        "scannedFiles": 1,
                        "rulesChecked": 0,
                        "violations": 0,
                        "cases": "###not-base64###",
                        "savedAt": 1700000000000,
                    }
                }
            ]
        ]
    )
    out = await lint_repository.get_last_lint_result("x", "https://github.com/a/b")
    assert out is not None
    assert out.cases == []


async def test_get_last_lint_for_project_decodes(fake_run):
    """[T7] github_url 없이 project 만으로 최신 LintResult — cases 디코딩."""
    cases_b64 = base64.b64encode(
        json.dumps(
            [{"title": "SPACK", "convergence": 80, "rules": [
                {"rule": "api:POST /x", "description": "결제 API", "applied": False}
            ]}],
            ensure_ascii=False,
        ).encode()
    ).decode()
    fake = fake_run([[{"lint": {
        "id": "lint-x-2", "project": "x", "githubUrl": "https://github.com/a/b",
        "score": 70, "scannedFiles": 3, "rulesChecked": 5, "violations": 1,
        "cases": cases_b64, "savedAt": 1700000001000,
    }}]])
    result = await lint_repository.get_last_lint_for_project("x")
    assert isinstance(result, LintResult)
    assert result.cases[0].rules[0].applied is False
    # project 만 바인딩 (github_url 파라미터 없음)
    assert "github_url" not in fake.calls[0]["params"]
    assert fake.calls[0]["params"]["project"] == "x"


async def test_get_last_lint_for_project_none_when_missing(fake_run):
    fake_run([[]])
    assert await lint_repository.get_last_lint_for_project("x") is None
