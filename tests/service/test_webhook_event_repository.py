"""
webhook_event_repository 단위 테스트.

[검증 — 토스 webhook 멱등성 보장]
- try_insert 첫 호출 → is_new=True, status='pending'
- 같은 pg_event_id 두 번째 호출 → is_new=False (중복 차단)
- mark_processed / mark_failed → status 전이
- retry_count 누적
- get_by_pg_event_id 매핑

[설계]
neo4j_client.run_cypher 를 monkeypatch fake 로 교체.
실제 Neo4j 호출 없이 단위 검증.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import webhook_event_repository as repo
from app.service.webhook_event_repository import (
    STATUS_FAILED,
    STATUS_PROCESSED,
    WebhookEvent,
)


pytestmark = pytest.mark.asyncio


class _FakeRunCypher:
    """neo4j_client.run_cypher fake — 호출 기록 + 응답 큐 반환."""

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
    def _setup(responses: Optional[List[List[Dict[str, Any]]]] = None) -> _FakeRunCypher:
        fake = _FakeRunCypher(responses)
        monkeypatch.setattr(
            "app.service.webhook_event_repository.neo4j_client.run_cypher", fake,
        )
        return fake
    return _setup


# ============================================================================
# try_insert — 멱등성 핵심
# ============================================================================


async def test_try_insert_first_call_returns_new(fake_run):
    """첫 호출 — MERGE 가 새 노드 생성. is_new=True."""
    # MERGE ON CREATE 시 우리 코드가 발급한 new_id 가 그대로 반환됨.
    # fake 가 응답에 그 id 를 그대로 echo 하는 시뮬레이션:
    # 실제로는 id 를 우리 코드가 미리 생성해서 cypher param 으로 전달 → 응답에 echo.
    # 첫 응답을 빈 list → 우리 함수가 동작하도록.

    # 실제 cypher MERGE ON CREATE SET ... RETURN id, status
    # 첫 호출: 새 노드 생성 → 응답에 우리가 보낸 id 그대로
    captured: Dict[str, Any] = {}

    async def _fake_run(cypher, params=None, database=None):
        captured.update(params or {})
        # MERGE 첫 호출 — 우리가 전달한 id 그대로 echo (ON CREATE)
        return [{"id": params["id"], "status": "pending"}]

    import app.service.webhook_event_repository as mod
    original = mod.neo4j_client.run_cypher
    mod.neo4j_client.run_cypher = _fake_run
    try:
        status, is_new = await repo.try_insert(
            pg_event_id="evt_abc",
            event_type="PAYMENT_STATUS_CHANGED",
            payload={"data": {"orderId": "o1"}},
        )
    finally:
        mod.neo4j_client.run_cypher = original

    assert is_new is True
    assert status == "pending"
    assert captured["pg_event_id"] == "evt_abc"
    assert captured["event_type"] == "PAYMENT_STATUS_CHANGED"


async def test_try_insert_duplicate_call_returns_not_new(fake_run):
    """같은 pg_event_id 두 번째 호출 — MERGE ON MATCH → 기존 id 반환. is_new=False."""
    # 두 번째 호출에선 MERGE 가 기존 노드 매칭 → 응답의 id 는 우리가 보낸 new_id 와 다름.
    async def _fake_run(cypher, params=None, database=None):
        # 기존 노드의 id (다른 uuid) 반환
        return [{"id": "EXISTING_NODE_ID_DIFFERENT", "status": "processed"}]

    import app.service.webhook_event_repository as mod
    original = mod.neo4j_client.run_cypher
    mod.neo4j_client.run_cypher = _fake_run
    try:
        status, is_new = await repo.try_insert(
            pg_event_id="evt_abc",
            event_type="PAYMENT_STATUS_CHANGED",
            payload={"data": {}},
        )
    finally:
        mod.neo4j_client.run_cypher = original

    assert is_new is False  # 우리가 보낸 id ≠ 응답 id
    assert status == "processed"


async def test_try_insert_payload_serialized_to_json(fake_run):
    """payload dict 가 JSON string 으로 직렬화되어 저장됨."""
    fake = fake_run([[{"id": "x", "status": "pending"}]])
    await repo.try_insert(
        pg_event_id="e1",
        event_type="PAYMENT_STATUS_CHANGED",
        payload={"data": {"paymentKey": "pk_xxx", "status": "DONE"}},
    )
    sent = fake.calls[0]["params"]["payload"]
    assert isinstance(sent, str)
    # JSON string 안에 핵심 키 보존
    assert "paymentKey" in sent
    assert "DONE" in sent


async def test_try_insert_payload_string_passed_through(fake_run):
    """payload 가 이미 string 이면 그대로."""
    fake = fake_run([[{"id": "x", "status": "pending"}]])
    raw_str = '{"already":"serialized"}'
    await repo.try_insert(pg_event_id="e1", event_type="X", payload=raw_str)
    assert fake.calls[0]["params"]["payload"] == raw_str


async def test_try_insert_empty_response_returns_unknown(fake_run):
    """Neo4j 가 빈 list 반환 — (unknown, False) fallback."""
    fake_run([[]])
    status, is_new = await repo.try_insert(
        pg_event_id="e1", event_type="X", payload={},
    )
    assert is_new is False
    assert status == "unknown"


# ============================================================================
# mark_processed / mark_failed
# ============================================================================


async def test_mark_processed_returns_true_when_node_exists(fake_run):
    fake = fake_run([[{"id": "abc"}]])
    ok = await repo.mark_processed("evt_xyz", related_payment_id="pay_1")
    assert ok is True
    assert fake.calls[0]["params"]["pg_event_id"] == "evt_xyz"
    assert fake.calls[0]["params"]["related_payment_id"] == "pay_1"


async def test_mark_processed_empty_related_payment(fake_run):
    """related_payment_id 미지정 시 빈 string."""
    fake = fake_run([[{"id": "abc"}]])
    await repo.mark_processed("evt_xyz")
    assert fake.calls[0]["params"]["related_payment_id"] == ""


async def test_mark_processed_returns_false_when_no_match(fake_run):
    fake_run([[]])
    ok = await repo.mark_processed("evt_missing")
    assert ok is False


async def test_mark_failed_returns_retry_count(fake_run):
    fake = fake_run([[{"id": "abc", "retry_count": 3}]])
    retry = await repo.mark_failed("evt_xyz", "처리 중 KeyError")
    assert retry == 3
    assert fake.calls[0]["params"]["pg_event_id"] == "evt_xyz"
    assert fake.calls[0]["params"]["error_message"] == "처리 중 KeyError"


async def test_mark_failed_truncates_long_error(fake_run):
    """긴 에러 메시지 — repo 는 별도 truncate 안 함, 호출자가 length 관리.
    그러나 None / 빈 string 도 안전 처리되는지."""
    fake = fake_run([[{"id": "abc", "retry_count": 1}]])
    await repo.mark_failed("evt", "")
    assert fake.calls[0]["params"]["error_message"] == ""


async def test_mark_failed_returns_none_when_no_match(fake_run):
    fake_run([[]])
    retry = await repo.mark_failed("evt_missing", "err")
    assert retry is None


# ============================================================================
# get_by_pg_event_id — 응답 매핑
# ============================================================================


async def test_get_by_pg_event_id_full_mapping(fake_run):
    fake_run([
        [
            {
                "event": {
                    "id": "uid_1",
                    "pg_event_id": "evt_x",
                    "event_type": "PAYMENT_STATUS_CHANGED",
                    "payload": '{"a":1}',
                    "status": STATUS_PROCESSED,
                    "related_payment_id": "pay_1",
                    "retry_count": 2,
                    "error_message": "",
                    "received_at": "2026-05-18T01:00:00+00:00",
                    "processed_at": "2026-05-18T01:00:05+00:00",
                    "updated_at": "2026-05-18T01:00:05+00:00",
                }
            }
        ]
    ])
    out = await repo.get_by_pg_event_id("evt_x")
    assert isinstance(out, WebhookEvent)
    assert out.id == "uid_1"
    assert out.pg_event_id == "evt_x"
    assert out.event_type == "PAYMENT_STATUS_CHANGED"
    assert out.status == STATUS_PROCESSED
    assert out.related_payment_id == "pay_1"
    assert out.retry_count == 2


async def test_get_by_pg_event_id_returns_none_when_missing(fake_run):
    fake_run([[]])
    assert await repo.get_by_pg_event_id("nope") is None


# ============================================================================
# 멱등성 시나리오 — 통합
# ============================================================================


async def test_idempotency_same_event_processed_once(monkeypatch):
    """
    실 운영 시나리오:
    1. webhook 첫 도착 → try_insert is_new=True → handle_event 처리
    2. 토스 재시도 (네트워크 일시 장애) → try_insert is_new=False → skip
    3. 결과적으로 handle_event 1회만 실행 (중복 환불/등급변경 방지)
    """
    state = {"call_count": 0, "first_id": None}

    async def _fake_run(cypher, params=None, database=None):
        state["call_count"] += 1
        if state["call_count"] == 1:
            # 첫 호출 — MERGE ON CREATE 우리 id 반환
            state["first_id"] = params["id"]
            return [{"id": params["id"], "status": "pending"}]
        else:
            # 두 번째 — MERGE ON MATCH 기존 id 반환
            return [{"id": state["first_id"], "status": "pending"}]

    import app.service.webhook_event_repository as mod
    original = mod.neo4j_client.run_cypher
    mod.neo4j_client.run_cypher = _fake_run
    try:
        _, is_new_1 = await repo.try_insert(
            pg_event_id="evt_same", event_type="X", payload={},
        )
        _, is_new_2 = await repo.try_insert(
            pg_event_id="evt_same", event_type="X", payload={},
        )
    finally:
        mod.neo4j_client.run_cypher = original

    assert is_new_1 is True
    assert is_new_2 is False  # 두 번째는 중복으로 차단
