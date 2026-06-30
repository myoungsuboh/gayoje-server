"""
query_repository.update_api_error_and_auth — [AI 초안 보완] 단일 API 노드 부분 갱신.

[검증]
- Cypher 가 단일 API 노드만 MATCH (project + id 둘 다 — IDOR 이중망)
- Wipe-and-Redraw (DETACH DELETE) 가 절대 포함 안 됨 (다른 노드 무손상 보장)
- error_cases/auth 만 SET (다른 속성 미변경)
- error_cases(list)/auth(dict) → JSON string 직렬화 (parameter binding, 인터폴 0)
- source/reviewed 메타가 직렬화 결과에 그대로 보존
- 노드 매칭 성공 → True, 없음 → False
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from app.service import query_repository as q


pytestmark = pytest.mark.asyncio


class _FakeRun:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher: str, params: Optional[Dict[str, Any]] = None,
                       database: Optional[str] = None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        return self._responses.pop(0) if self._responses else []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses=None):
        fake = _FakeRun(responses)
        monkeypatch.setattr(
            "app.service.query_repository.neo4j_client.run_cypher", fake
        )
        return fake
    return _setup


# ─── Cypher 회귀 가드 ──────────────────────────────────


def test_cypher_matches_single_api_node_with_project_isolation():
    """단일 API 노드만 MATCH — project + id 둘 다 (IDOR 이중망)."""
    cypher = q._UPDATE_API_SPECS_CYPHER
    assert "MATCH (a:API {id: $id, project: $project})" in cypher
    assert "a.error_cases = $error_cases" in cypher
    assert "a.auth = $auth" in cypher
    assert "updated_at" in cypher


def test_cypher_never_wipes():
    """[회귀] DETACH DELETE 가 절대 없음 — Wipe-and-Redraw 재사용 금지."""
    cypher = q._UPDATE_API_SPECS_CYPHER
    assert "DETACH DELETE" not in cypher
    assert "DELETE" not in cypher
    # error_cases/auth 외 다른 SPACK 속성을 건드리지 않음 (부분 SET).
    assert "a.name" not in cypher
    assert "a.endpoint" not in cypher
    assert "a.request_body" not in cypher


# ─── 정상 경로 ──────────────────────────────────────────


async def test_serializes_error_cases_and_auth_as_json_string(fake_run):
    fake = fake_run([[{"id": "API-01"}]])
    error_cases = [
        {"status": 404, "code": "NOT_FOUND", "source": "ai_draft", "reviewed": False},
    ]
    auth = {"required": True, "description": "본인만", "source": "ai_draft", "reviewed": False}

    ok = await q.update_api_error_and_auth("proj", "API-01", error_cases, auth)
    assert ok is True

    params = fake.calls[0]["params"]
    assert params["project"] == "proj"
    assert params["id"] == "API-01"
    # JSON string 직렬화 — Neo4j primitive 제약 우회 (기존 SPACK 저장과 동일).
    assert isinstance(params["error_cases"], str)
    assert isinstance(params["auth"], str)
    # round-trip 검증 + 메타 보존
    decoded_cases = json.loads(params["error_cases"])
    assert decoded_cases[0]["source"] == "ai_draft"
    assert decoded_cases[0]["reviewed"] is False
    decoded_auth = json.loads(params["auth"])
    assert decoded_auth["source"] == "ai_draft"
    assert decoded_auth["reviewed"] is False
    assert decoded_auth["description"] == "본인만"


async def test_returns_false_when_node_missing(fake_run):
    """매칭되는 API 노드가 없으면 (빈 결과) False — 호출자가 부분 실패 처리."""
    fake_run([[]])  # 빈 응답 = 노드 없음
    ok = await q.update_api_error_and_auth("proj", "NOPE", [], {})
    assert ok is False


async def test_handles_empty_inputs(fake_run):
    """error_cases/auth 가 비어도 안전하게 직렬화."""
    fake = fake_run([[{"id": "API-01"}]])
    ok = await q.update_api_error_and_auth("proj", "API-01", [], {})
    assert ok is True
    params = fake.calls[0]["params"]
    assert params["error_cases"] == "[]"
    assert params["auth"] == "{}"
