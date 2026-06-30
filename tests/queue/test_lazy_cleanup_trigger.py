"""
PRD 누더기 자동 해소 — B (lazy trigger) + C (admin 일괄) 회귀 가드.

[배경 — 2026-05-26]
PR #52 의 post_meeting 자동 cleanup 은 새 미팅 처리 시점부터만 동작 → 기존
누더기 PRD 는 즉시 해소 안 됨. 두 가지 해결책:
  B. PRD 조회 (GET /api/v2/prd) 시점에 lazy detection + enqueue
  C. admin endpoint (POST /api/admin/cleanup-dirty-prd) 로 일괄 백필

[검증]
- maybe_lazy_trigger_cleanup: clean → None / dirty → task_id 반환 / 예외 swallow
- _deterministic_cleanup_task_id: 같은 input 같은 id (arq dedup 보장)
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pytest


pytestmark = pytest.mark.asyncio


# ─── deterministic task_id ────────────────────────────────


def test_deterministic_task_id_same_input_same_id():
    """같은 master content → 같은 task_id (arq dedup 가능)."""
    from app.queue.jobs import _deterministic_cleanup_task_id

    md = "#### 📦 [Epic-01] X\n#### 📦 [Epic-01] dup\n"
    id1 = _deterministic_cleanup_task_id("proj_x", md)
    id2 = _deterministic_cleanup_task_id("proj_x", md)
    assert id1 == id2


def test_deterministic_task_id_different_master_different_id():
    """master content 가 바뀌면 task_id 바뀜 (cleanup 후 재 trip 가능)."""
    from app.queue.jobs import _deterministic_cleanup_task_id

    id1 = _deterministic_cleanup_task_id("proj_x", "dirty content")
    id2 = _deterministic_cleanup_task_id("proj_x", "cleaned content")
    assert id1 != id2


def test_deterministic_task_id_different_project_different_id():
    """다른 프로젝트는 별도 task_id (cross-project dedup 방지)."""
    from app.queue.jobs import _deterministic_cleanup_task_id

    md = "same"
    assert _deterministic_cleanup_task_id("a", md) != _deterministic_cleanup_task_id("b", md)


# ─── maybe_lazy_trigger_cleanup ────────────────────────────


@pytest.fixture
def fake_enqueue(monkeypatch):
    state: Dict[str, Any] = {"calls": []}

    async def fake(*, task_id, project_name, dry_run, user_email, team_id=""):
        state["calls"].append({
            "task_id": task_id, "project_name": project_name,
            "dry_run": dry_run, "user_email": user_email,
        })
        return task_id

    monkeypatch.setattr(
        "app.queue.client.enqueue_cleanup_master_prd", fake
    )
    return state


async def test_lazy_trigger_skips_clean_markdown(fake_enqueue):
    """정상 PRD 면 enqueue 안 함 (불필요 LLM 비용 차단)."""
    from app.queue.jobs import maybe_lazy_trigger_cleanup

    clean_md = (
        "#### 📦 [Epic-01] A\n"
        "#### 📦 [Epic-02] B\n"
    )
    result = await maybe_lazy_trigger_cleanup(
        project_name="clean_proj", master_markdown=clean_md, user_email="u@x",
    )
    assert result is None
    assert fake_enqueue["calls"] == []


async def test_lazy_trigger_enqueues_on_dirty_markdown(fake_enqueue):
    """누더기 PRD 면 enqueue + 반환된 task_id 확인."""
    from app.queue.jobs import maybe_lazy_trigger_cleanup

    dirty_md = (
        "#### 📦 [Epic-01] A\n"
        "#### 📦 [Epic-01] dup\n"  # 중복!
    )
    result = await maybe_lazy_trigger_cleanup(
        project_name="dirty_proj", master_markdown=dirty_md, user_email="u@x",
    )
    assert result is not None
    assert len(fake_enqueue["calls"]) == 1
    call = fake_enqueue["calls"][0]
    assert call["project_name"] == "dirty_proj"
    assert call["dry_run"] is False, "lazy trigger 도 즉시 apply (사용자 confirm X)"
    assert call["user_email"] == "u@x"
    assert call["task_id"] == result


async def test_lazy_trigger_deterministic_dedup(fake_enqueue):
    """같은 dirty PRD 를 2번 호출해도 같은 task_id — arq 가 dedup.

    BE 가 매번 enqueue 호출은 하지만 같은 task_id 라 arq 가 무시.
    여기선 fake enqueue 가 dedup 안 해서 2회 기록되지만, task_id 일관성 검증.
    """
    from app.queue.jobs import maybe_lazy_trigger_cleanup

    dirty_md = "#### 📦 [Epic-01] A\n#### 📦 [Epic-01] dup\n"
    id1 = await maybe_lazy_trigger_cleanup(
        project_name="p", master_markdown=dirty_md, user_email="u@x",
    )
    id2 = await maybe_lazy_trigger_cleanup(
        project_name="p", master_markdown=dirty_md, user_email="u@x",
    )
    assert id1 == id2, "같은 master 상태 → 같은 task_id (arq dedup)"


async def test_lazy_trigger_empty_markdown_skips(fake_enqueue):
    """빈 markdown — trip 안 됨, enqueue 안 됨."""
    from app.queue.jobs import maybe_lazy_trigger_cleanup

    result = await maybe_lazy_trigger_cleanup(
        project_name="p", master_markdown="", user_email="u@x",
    )
    assert result is None
    assert fake_enqueue["calls"] == []


async def test_lazy_trigger_enqueue_failure_swallowed(monkeypatch):
    """Redis 일시 장애로 enqueue raise → swallow, None 반환."""
    async def fail(*, task_id, project_name, dry_run, user_email):
        raise RuntimeError("redis down")
    monkeypatch.setattr(
        "app.queue.client.enqueue_cleanup_master_prd", fail
    )

    from app.queue.jobs import maybe_lazy_trigger_cleanup
    dirty_md = "#### 📦 [Epic-01] A\n#### 📦 [Epic-01] dup\n"
    # raise 없이 None 반환
    result = await maybe_lazy_trigger_cleanup(
        project_name="p", master_markdown=dirty_md, user_email="u@x",
    )
    assert result is None
