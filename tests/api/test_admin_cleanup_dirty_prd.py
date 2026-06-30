"""
POST /api/admin/cleanup-dirty-prd 회귀 가드.

[목적]
PR #52 이전에 누적된 기존 누더기 master PRD 들을 admin 1회성 일괄 cleanup.

[검증]
- 모든 master PRD 스캔
- 누더기 (size>=30KB or Epic ID 중복) 만 enqueue, clean 은 skip
- 응답에 scanned/triggered/skipped_clean/errored
- 같은 master 상태면 deterministic task_id (반복 호출 dedup)
- enqueue 실패는 errored 에 기록하고 계속 진행 (한 프로젝트 실패가 전체 중단 X)
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.api import admin_routes
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


def _admin(email: str = "admin@x.com") -> UserPublic:
    return UserPublic(
        id="a-1", email=email, name="admin",
        subscription_type="pro", is_admin=True,
    )


def _fake_request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/admin/cleanup-dirty-prd"),
        method="POST",
    )


@pytest.fixture
def fake_neo4j_rows(monkeypatch):
    """neo4j_client.run_cypher 가 mixed projects 반환."""
    rows: List[Dict[str, Any]] = [
        {
            "project_name": "clean_proj",
            "full_markdown": "#### 📦 [Epic-01] OK\n#### 📦 [Epic-02] Fine\n",
            "owner_email": "u1@x",
        },
        {
            "project_name": "dirty_dup",
            "full_markdown": "#### 📦 [Epic-01] A\n#### 📦 [Epic-01] dup\n",
            "owner_email": "u2@x",
        },
        {
            "project_name": "dirty_size",
            "full_markdown": "#### 📦 [Epic-01] X\n" + ("x" * 31_000),
            "owner_email": None,  # owner 누락 — admin email 로 fallback 확인
        },
    ]

    async def fake_run_cypher(cypher: str, params: Optional[Dict[str, Any]] = None):
        return rows

    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", fake_run_cypher
    )
    return rows


@pytest.fixture
def fake_enqueue(monkeypatch):
    state: Dict[str, Any] = {"calls": []}

    async def fake(*, task_id, project_name, dry_run, user_email):
        state["calls"].append({
            "task_id": task_id, "project_name": project_name,
            "dry_run": dry_run, "user_email": user_email,
        })
        return task_id

    monkeypatch.setattr(
        "app.queue.client.enqueue_cleanup_master_prd", fake
    )
    return state


@pytest.fixture
def fake_audit(monkeypatch):
    state: Dict[str, Any] = {"calls": []}

    async def fake(*, actor_email, action, target_email, payload):
        state["calls"].append({
            "actor_email": actor_email, "action": action,
            "target_email": target_email, "payload": payload,
        })

    monkeypatch.setattr(
        "app.api.admin_routes.audit_repository.write", fake
    )
    return state


# ─── 일괄 cleanup 동작 검증 ─────────────────────────────────


async def test_scans_all_master_prds(
    fake_neo4j_rows, fake_enqueue, fake_audit,
):
    """모든 master PRD 노드 스캔."""
    route = admin_routes.cleanup_dirty_prd_route.__wrapped__
    resp = await route(request=_fake_request(), admin=_admin())
    assert resp["scanned"] == 3


async def test_only_dirty_triggered(
    fake_neo4j_rows, fake_enqueue, fake_audit,
):
    """clean 은 skip, dirty 만 enqueue."""
    route = admin_routes.cleanup_dirty_prd_route.__wrapped__
    resp = await route(request=_fake_request(), admin=_admin())

    assert resp["triggered"] == 2  # dirty_dup + dirty_size
    assert resp["skipped_clean"] == 1  # clean_proj
    assert resp["errored"] == 0

    triggered_names = {p["project_name"] for p in resp["projects"]}
    assert "dirty_dup" in triggered_names
    assert "dirty_size" in triggered_names
    assert "clean_proj" not in triggered_names


async def test_enqueue_called_for_dirty_with_owner_email(
    fake_neo4j_rows, fake_enqueue, fake_audit,
):
    """dirty_dup → owner_email 그대로, dirty_size → admin email (owner 누락 fallback)."""
    route = admin_routes.cleanup_dirty_prd_route.__wrapped__
    await route(request=_fake_request(), admin=_admin(email="admin@x.com"))

    calls_by_project = {c["project_name"]: c for c in fake_enqueue["calls"]}
    assert calls_by_project["dirty_dup"]["user_email"] == "u2@x"
    # owner 없는 dirty_size 는 admin 으로 fallback
    assert calls_by_project["dirty_size"]["user_email"] == "admin@x.com"
    # 둘 다 즉시 apply
    assert calls_by_project["dirty_dup"]["dry_run"] is False
    assert calls_by_project["dirty_size"]["dry_run"] is False


async def test_deterministic_task_id_in_batch(
    fake_neo4j_rows, fake_enqueue, fake_audit,
):
    """task_id 가 deterministic — admin 이 2번 실행해도 같은 id (arq dedup)."""
    route = admin_routes.cleanup_dirty_prd_route.__wrapped__
    await route(request=_fake_request(), admin=_admin())
    first_ids = sorted(c["task_id"] for c in fake_enqueue["calls"])
    # 2번째 실행
    fake_enqueue["calls"].clear()
    await route(request=_fake_request(), admin=_admin())
    second_ids = sorted(c["task_id"] for c in fake_enqueue["calls"])
    assert first_ids == second_ids


async def test_enqueue_failure_recorded_continues(
    fake_neo4j_rows, fake_audit, monkeypatch,
):
    """한 프로젝트 enqueue 실패해도 나머지 계속 진행 — errored 카운트."""
    state: Dict[str, Any] = {"calls": []}

    async def fake(*, task_id, project_name, dry_run, user_email):
        # dirty_dup 만 fail
        if project_name == "dirty_dup":
            raise RuntimeError("redis flaky")
        state["calls"].append(project_name)
        return task_id

    monkeypatch.setattr(
        "app.queue.client.enqueue_cleanup_master_prd", fake
    )

    route = admin_routes.cleanup_dirty_prd_route.__wrapped__
    resp = await route(request=_fake_request(), admin=_admin())

    assert resp["triggered"] == 1  # dirty_size 만
    assert resp["errored"] == 1   # dirty_dup
    assert resp["skipped_clean"] == 1
    assert any(e["project_name"] == "dirty_dup" for e in resp["errors"])
    # 나머지는 정상 enqueue (계속 진행 보장)
    assert "dirty_size" in state["calls"]


async def test_audit_logged(
    fake_neo4j_rows, fake_enqueue, fake_audit,
):
    """admin 액션은 audit log 에 기록."""
    route = admin_routes.cleanup_dirty_prd_route.__wrapped__
    await route(request=_fake_request(), admin=_admin())
    assert len(fake_audit["calls"]) == 1
    log = fake_audit["calls"][0]
    assert log["actor_email"] == "admin@x.com"
    assert log["action"] == "cleanup_dirty_prd"
    assert log["payload"]["triggered"] == 2
    assert log["payload"]["skipped_clean"] == 1


async def test_empty_database_returns_zero_counts(
    fake_enqueue, fake_audit, monkeypatch,
):
    """master PRD 가 하나도 없을 때 0/0/0 응답."""
    async def fake_run_cypher(cypher, params=None):
        return []
    monkeypatch.setattr(
        "app.clients.neo4j_client.run_cypher", fake_run_cypher
    )

    route = admin_routes.cleanup_dirty_prd_route.__wrapped__
    resp = await route(request=_fake_request(), admin=_admin())
    assert resp["scanned"] == 0
    assert resp["triggered"] == 0
    assert resp["skipped_clean"] == 0
    assert resp["errored"] == 0
    assert fake_enqueue["calls"] == []
