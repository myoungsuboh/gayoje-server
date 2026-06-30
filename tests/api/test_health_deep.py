"""
/health/deep 회귀 가드 — Neo4j + Redis 의존성 검증.

[배경]
2026-05 도입 — Uptime 모니터링이 /health 만 호출하면 DB 끊겨도 200. 운영에서
실제 사용자 영향 큰 의존성 장애를 알람으로 잡으려면 deep 헬스체크 필요.

[정책]
- 모두 OK → 200 + {status:"healthy", checks: {neo4j:"ok", redis:"ok"}}
- 어느 하나라도 실패 → 503 + detail 에 어떤 게 실패했는지
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.main import health_deep


pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_deps(monkeypatch):
    """Neo4j + Redis 응답 토글."""
    state = {"neo4j_ok": True, "redis_ok": True}

    async def fake_cypher(cypher, params=None):
        if not state["neo4j_ok"]:
            raise RuntimeError("Neo4j down")
        return [{"ok": 1}]

    class _FakePool:
        async def ping(self):
            if not state["redis_ok"]:
                raise RuntimeError("Redis down")
            return True

    async def fake_get_pool():
        return _FakePool()

    monkeypatch.setattr(
        "app.api.main.neo4j_client.run_cypher", fake_cypher
    )
    monkeypatch.setattr(
        "app.api.main.queue_client.get_pool", fake_get_pool
    )
    return state


async def test_both_ok_returns_200(fake_deps):
    out = await health_deep()
    assert out["status"] == "healthy"
    assert out["checks"]["neo4j"] == "ok"
    assert out["checks"]["redis"] == "ok"


async def test_neo4j_down_returns_503(fake_deps):
    fake_deps["neo4j_ok"] = False
    with pytest.raises(HTTPException) as exc:
        await health_deep()
    assert exc.value.status_code == 503
    detail = exc.value.detail
    assert detail["status"] == "degraded"
    assert "error" in detail["checks"]["neo4j"]
    assert detail["checks"]["redis"] == "ok"


async def test_redis_down_returns_503(fake_deps):
    fake_deps["redis_ok"] = False
    with pytest.raises(HTTPException) as exc:
        await health_deep()
    assert exc.value.status_code == 503
    detail = exc.value.detail
    assert detail["checks"]["neo4j"] == "ok"
    assert "error" in detail["checks"]["redis"]


async def test_both_down_returns_503_with_both_errors(fake_deps):
    fake_deps["neo4j_ok"] = False
    fake_deps["redis_ok"] = False
    with pytest.raises(HTTPException) as exc:
        await health_deep()
    assert exc.value.status_code == 503
    checks = exc.value.detail["checks"]
    assert "error" in checks["neo4j"]
    assert "error" in checks["redis"]
