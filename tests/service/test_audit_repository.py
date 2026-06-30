"""
audit_repository 단위 테스트 — write best-effort 정책 + 리스트 정규화.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from app.service import audit_repository

pytestmark = pytest.mark.asyncio


class _FakeRunCypher:
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
    def _setup(responses=None) -> _FakeRunCypher:
        fake = _FakeRunCypher(responses=responses)
        monkeypatch.setattr(
            "app.service.audit_repository.neo4j_client.run_cypher", fake
        )
        return fake

    return _setup


async def test_write_serializes_payload_as_json(fake_run):
    fake = fake_run([[{"id": "log-1"}]])
    log_id = await audit_repository.write(
        actor_email="admin@x.com",
        action=audit_repository.ACTION_SUBSCRIPTION_CHANGE,
        target_email="u@x.com",
        payload={"from_type": "free", "to_type": "pro"},
    )
    assert log_id == "log-1"
    params = fake.calls[0]["params"]
    assert params["actor_email"] == "admin@x.com"
    assert params["action"] == "subscription_change"
    assert params["target_email"] == "u@x.com"
    parsed = json.loads(params["payload"])
    assert parsed == {"from_type": "free", "to_type": "pro"}


async def test_write_handles_empty_payload(fake_run):
    fake = fake_run([[{"id": "log-2"}]])
    await audit_repository.write(
        actor_email="admin@x.com", action="admin_grant", target_email="u@x.com",
    )
    params = fake.calls[0]["params"]
    assert params["payload"] == "{}"
    assert params["target_email"] == "u@x.com"


async def test_write_swallows_failure_returns_none(monkeypatch):
    """결제 등 핵심 흐름이 감사 로그 실패로 막히면 안 됨 → best-effort."""

    async def _raise(*a, **kw):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(
        "app.service.audit_repository.neo4j_client.run_cypher", _raise
    )
    result = await audit_repository.write(
        actor_email="admin@x.com", action="x", target_email="t@x.com",
    )
    assert result is None  # 예외가 호출자에게 전파되면 안 됨


async def test_list_logs_parses_payload_json(fake_run):
    fake_run([
        [{
            "log": {
                "id": "log-1",
                "actor_email": "admin@x.com",
                "action": "subscription_change",
                "target_email": "u@x.com",
                "payload": '{"from_type":"free","to_type":"pro"}',
                "created_at": "2026-01-01T00:00:00",
            }
        }],
        [{"total": 1}],
    ])
    out = await audit_repository.list_logs(q="")
    assert out["total"] == 1
    assert len(out["logs"]) == 1
    log = out["logs"][0]
    assert log.action == "subscription_change"
    assert log.payload == {"from_type": "free", "to_type": "pro"}


async def test_list_logs_handles_malformed_payload(fake_run):
    """payload JSON 파싱 실패 시에도 죽지 않고 _raw 키로 보존."""
    fake_run([
        [{
            "log": {
                "id": "log-1",
                "actor_email": "admin@x.com",
                "action": "x",
                "target_email": "",
                "payload": "not-json",
                "created_at": None,
            }
        }],
        [{"total": 1}],
    ])
    out = await audit_repository.list_logs(q="")
    assert out["logs"][0].payload == {"_raw": "not-json"}
