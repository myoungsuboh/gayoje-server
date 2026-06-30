"""
query_repository.update_cps_node — Phase 3.1 노드 단위 inline edit 회귀.

[검증]
- cypher 가 Problem/Solution label whitelist (다른 label 매칭 안 됨)
- project + node_id 둘 다 매칭 (IDOR 방어 이중망)
- 정상 응답 매핑 (id/label/summary)
- 노드 없음 → None
- summary parameter binding (인터폴 0)
- user_edited_at 추적 필드
"""
from __future__ import annotations

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


# ─── cypher 회귀 가드 ──────────────────────────────────


def test_update_cps_node_cypher_label_whitelist():
    """[회귀] cypher 가 Problem/Solution 만 매칭 — 다른 라벨 노드 보호."""
    cypher = q._UPDATE_CPS_NODE_CYPHER
    assert "n:Problem OR n:Solution" in cypher
    # project 필터 — IDOR 방어 이중망 (라우트 단 ownership 외 cypher 도 격리)
    assert "project: $project" in cypher
    # user_edited_at 추적
    assert "user_edited_at" in cypher


# ─── 정상 경로 ──────────────────────────────────────────


async def test_update_problem_returns_id_label_summary(fake_run):
    fake = fake_run([
        [{"id": "prb_01", "label": "Problem", "summary": "edited summary"}]
    ])
    out = await q.update_cps_node("p", "prb_01", "edited summary")
    assert out == {"id": "prb_01", "label": "Problem", "summary": "edited summary"}
    # parameter binding 검증
    params = fake.calls[0]["params"]
    assert params == {
        "project": "p",
        "node_id": "prb_01",
        "summary": "edited summary",
    }


async def test_update_solution_returns_solution_label(fake_run):
    fake_run([
        [{"id": "res_01", "label": "Solution", "summary": "new sol"}]
    ])
    out = await q.update_cps_node("p", "res_01", "new sol")
    assert out["label"] == "Solution"
    assert out["summary"] == "new sol"


# ─── 실패 경로 ──────────────────────────────────────────


async def test_update_returns_none_when_node_missing(fake_run):
    """노드 없으면 cypher 가 빈 응답 → None (라우트 404)."""
    fake_run([[]])
    out = await q.update_cps_node("p", "ghost_node", "x")
    assert out is None


async def test_update_returns_none_when_response_missing_id(fake_run):
    """응답에 id 누락 → None (방어)."""
    fake_run([[{"label": "Problem", "summary": "x"}]])
    out = await q.update_cps_node("p", "prb_01", "x")
    assert out is None


# ─── parameter binding 안전성 ──────────────────────────


async def test_update_summary_parameter_binding_safe(fake_run):
    """dangerous summary (cypher injection 시도) 가 본문에 인터폴 안 됨."""
    fake = fake_run([[{"id": "prb_01", "label": "Problem", "summary": "x"}]])
    dangerous = "x' DETACH DELETE n //"
    await q.update_cps_node("p", "prb_01", dangerous)
    # cypher 본문에 dangerous 가 직접 인터폴되지 않음
    assert dangerous not in fake.calls[0]["cypher"]
    # params 로만 전달
    assert fake.calls[0]["params"]["summary"] == dangerous
