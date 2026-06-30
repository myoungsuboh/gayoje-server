"""repo_repository CRUD 단위 테스트."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import repo_repository
from app.service.repo_repository import RepoIn


pytestmark = pytest.mark.asyncio


class _Fake:
    def __init__(self, responses=None):
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
            "app.service.repo_repository.neo4j_client.run_cypher", fake
        )
        return fake

    return _setup


async def test_add_repo_upserts_with_param_binding(fake_run):
    fake = fake_run(
        [
            [
                {
                    "repo": {
                        "url": "https://github.com/a/b",
                        "role": "primary",
                        "label": "main",
                        "addedAt": 1700,
                        "updatedAt": 1700,
                    }
                }
            ]
        ]
    )
    out = await repo_repository.add_repo(
        RepoIn(
            project_name="x",
            url="https://github.com/a/b",
            role="primary",
            label="main",
        )
    )
    assert out.url == "https://github.com/a/b"
    assert out.role == "primary"
    # Cypher 본문에 project 값이 보간되지 않음 (parameter binding)
    call = fake.calls[0]
    assert "$project" in call["cypher"]
    assert call["params"]["project"] == "x"
    assert call["params"]["url"] == "https://github.com/a/b"


async def test_get_repos_returns_list(fake_run):
    fake_run(
        [
            [
                {
                    "repo": {
                        "url": "https://github.com/a/b",
                        "role": "primary",
                        "label": "main",
                        "addedAt": 1700,
                        "updatedAt": 1701,
                    }
                },
                {
                    "repo": {
                        "url": "https://github.com/a/c",
                        "role": "mirror",
                        "label": "",
                        "addedAt": 1702,
                        "updatedAt": 1702,
                    }
                },
            ]
        ]
    )
    repos = await repo_repository.get_repos("x")
    assert len(repos) == 2
    assert repos[0].role == "primary"
    assert repos[1].role == "mirror"


async def test_get_repos_empty(fake_run):
    fake_run([[]])
    assert await repo_repository.get_repos("x") == []


async def test_delete_repo_returns_true(fake_run):
    fake = fake_run([[]])
    ok = await repo_repository.delete_repo("x", "https://github.com/a/b")
    assert ok is True
    assert "DETACH DELETE r" in fake.calls[0]["cypher"]
    assert fake.calls[0]["params"]["project"] == "x"


# Cypher injection regression
async def test_url_with_special_chars_is_parameterized(fake_run):
    fake = fake_run([[]])
    dangerous = "https://x.com/'); DETACH DELETE r //"
    await repo_repository.delete_repo("x", dangerous)
    # 입력값이 Cypher 본문에 직접 보간되면 안 됨
    assert dangerous not in fake.calls[0]["cypher"]
    assert fake.calls[0]["params"]["url"] == dangerous
