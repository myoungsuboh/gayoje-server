"""
큐 라우팅 회귀 가드 — Pro/Free 등급별 큐 분리.

[배경 — 2026-05]
Free 사용자 폭증이 Pro 사용자 SLA 를 망가뜨리지 않게 큐 분리.
client._enqueue 가 user_email 의 subscription_type 으로 PRO/FREE 큐 결정.

[케이스]
- Pro/Pro+/Pro Max user → PRO_QUEUE_NAME
- Free user             → FREE_QUEUE_NAME
- user_email 없음 (system job) → default QUEUE_NAME
- get_usage 실패         → FREE 큐 fallback (보수적)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.queue import client as queue_client
from app.queue.settings import FREE_QUEUE_NAME, PRO_QUEUE_NAME, QUEUE_NAME
from app.service.usage_repository import Usage


pytestmark = pytest.mark.asyncio


class _FakePool:
    """ArqRedis 대체. enqueue_job 호출 인자 (특히 _queue_name) 만 기록."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._next_job_id = 0

    async def enqueue_job(self, function: str, **kwargs):
        self.calls.append({"function": function, **kwargs})
        self._next_job_id += 1

        class _FakeJob:
            job_id = f"fake-{self._next_job_id}"
        return _FakeJob()


@pytest.fixture
def fake_pool(monkeypatch):
    pool = _FakePool()

    async def fake_get_pool():
        return pool
    monkeypatch.setattr(queue_client, "get_pool", fake_get_pool)
    return pool


def _set_usage(monkeypatch, subscription_type: Optional[str]):
    """usage_repository.get_usage 가 특정 등급 반환 / None 반환."""
    async def fake_get_usage(email: str):
        if subscription_type is None:
            return None
        return Usage(
            email=email,
            subscription_type=subscription_type,
            meeting_count=0, total_tokens=0, total_chars=0,
            reset_at=None,
        )
    monkeypatch.setattr(
        "app.service.usage_repository.get_usage", fake_get_usage
    )


# ─── 라우팅 ─────────────────────────────────────────────────


async def test_pro_user_routed_to_pro_queue(fake_pool, monkeypatch):
    """pro / pro_plus / pro_max → PRO_QUEUE_NAME."""
    for tier in ("pro", "pro_plus", "pro_max"):
        _set_usage(monkeypatch, tier)
        await queue_client._enqueue(
            "any_job", "task-id-1", user_email="paid@x.com",
        )
        last = fake_pool.calls[-1]
        # PRO 가 설정되어 있어야 (env 미설정 시 QUEUE_NAME 으로 fallback)
        assert last["_queue_name"] == PRO_QUEUE_NAME, (
            f"tier={tier} 인데 PRO 큐로 안 감: {last['_queue_name']}"
        )


async def test_free_user_routed_to_free_queue(fake_pool, monkeypatch):
    """free → FREE_QUEUE_NAME."""
    _set_usage(monkeypatch, "free")
    await queue_client._enqueue(
        "any_job", "task-id-2", user_email="free@x.com",
    )
    assert fake_pool.calls[-1]["_queue_name"] == FREE_QUEUE_NAME


async def test_unknown_tier_falls_back_to_free(fake_pool, monkeypatch):
    """DB 에 비정상 등급이 박혀도 보수적으로 FREE."""
    _set_usage(monkeypatch, "enterprise")  # 미정의
    await queue_client._enqueue(
        "any_job", "task-id-3", user_email="weird@x.com",
    )
    assert fake_pool.calls[-1]["_queue_name"] == FREE_QUEUE_NAME


async def test_no_user_node_falls_back_to_free(fake_pool, monkeypatch):
    """User 노드 없음 (인증만) → FREE."""
    _set_usage(monkeypatch, None)
    await queue_client._enqueue(
        "any_job", "task-id-4", user_email="ghost@x.com",
    )
    assert fake_pool.calls[-1]["_queue_name"] == FREE_QUEUE_NAME


async def test_no_user_email_uses_default_queue(fake_pool, monkeypatch):
    """system job (user_email 없음) → default QUEUE_NAME — Pro 워커 자원 보호."""
    await queue_client._enqueue("any_job", "task-id-5")  # user_email 미전달
    assert fake_pool.calls[-1]["_queue_name"] == QUEUE_NAME


# ─── [실제 분리 검증] — env 시뮬레이션 ────────────────────────


async def test_pro_and_free_route_to_different_queues_when_split(fake_pool, monkeypatch):
    """
    PRO/FREE env 가 분리된 운영 환경 시뮬레이션 — 두 사용자가 서로 다른 큐로.

    module-level 상수를 monkeypatch (테스트가 임포트한 client 의 reference 도 함께).
    """
    monkeypatch.setattr(
        "app.queue.settings.PRO_QUEUE_NAME", "test:queue:pro"
    )
    monkeypatch.setattr(
        "app.queue.settings.FREE_QUEUE_NAME", "test:queue:free"
    )
    # client.py 가 from-import 한 reference 도 동기 — 같은 변수 패치
    monkeypatch.setattr(
        "app.queue.client.PRO_QUEUE_NAME", "test:queue:pro"
    )
    monkeypatch.setattr(
        "app.queue.client.FREE_QUEUE_NAME", "test:queue:free"
    )

    # Pro user
    _set_usage(monkeypatch, "pro")
    await queue_client._enqueue("any_job", "t-pro", user_email="p@x.com")
    assert fake_pool.calls[-1]["_queue_name"] == "test:queue:pro"

    # Free user
    _set_usage(monkeypatch, "free")
    await queue_client._enqueue("any_job", "t-free", user_email="f@x.com")
    assert fake_pool.calls[-1]["_queue_name"] == "test:queue:free"


async def test_repo_lookup_failure_falls_back_to_free(fake_pool, monkeypatch):
    """get_usage 가 예외 던져도 안전하게 FREE — 운영 안정성 우선."""
    async def fake_boom(email: str):
        raise RuntimeError("Neo4j down")
    monkeypatch.setattr(
        "app.service.usage_repository.get_usage", fake_boom
    )
    await queue_client._enqueue(
        "any_job", "task-id-6", user_email="paid@x.com",
    )
    assert fake_pool.calls[-1]["_queue_name"] == FREE_QUEUE_NAME, (
        "lookup 실패 시 Pro 가 아닌 FREE 로 가야 안전 (Pro 워커 보호)"
    )
