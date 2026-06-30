"""
post_meeting_pipeline_job 끝의 자동 cleanup trigger 회귀 가드.

[설계 — 2026-05-26]
사용자에게 "AI 정리" 버튼 노출 안 함. 대신 미팅 처리 후 master PRD 가
누더기 (size > threshold 또는 Epic ID 중복) 면 백그라운드 cleanup 자동 enqueue.
사용자는 다음 PRD 조회 때 깔끔한 결과만 본다.

[검증]
- _should_trigger_cleanup: size threshold, Epic ID 중복 감지
- _maybe_trigger_auto_cleanup: enqueue 호출 / skip / 예외 swallow
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from app.queue.jobs import _should_trigger_cleanup, _maybe_trigger_auto_cleanup


pytestmark = pytest.mark.asyncio


# ─── _should_trigger_cleanup 결정성 검증 ──────────────────────


def test_empty_markdown_no_trigger():
    trigger, reason = _should_trigger_cleanup("")
    assert trigger is False


def test_small_clean_markdown_no_trigger():
    """정상 크기 + Epic ID 중복 없음 → trip 안 됨."""
    md = (
        "## 🗺️ Master PRD\n"
        "### 2. Epic & User Story Map\n"
        "#### 📦 [Epic-01] 식물 정보 관리\n"
        "- `[Story-01.1]` 사용자 등록\n"
        "#### 📦 [Epic-02] 알림 시스템\n"
        "- `[Story-02.1]` 알림 받기\n"
    )
    trigger, reason = _should_trigger_cleanup(md)
    assert trigger is False, f"reason={reason}"


def test_large_markdown_triggers_size():
    """30KB 초과 → size threshold 발동."""
    md = "## 🗺️ Master PRD\n" + ("x" * 31_000)
    trigger, reason = _should_trigger_cleanup(md)
    assert trigger is True
    assert "size" in reason


def test_duplicate_epic_id_triggers():
    """같은 Epic ID 가 2번 등장 → 누더기 — trip."""
    md = (
        "### 2. Epic & User Story Map\n"
        "#### 📦 [Epic-01] 식물 관리\n"
        "- `[Story-01.1]` 등록\n"
        "#### 📦 [Epic-01] 식물 정보\n"  # 같은 ID 중복!
        "- `[Story-01.2]` 조회\n"
    )
    trigger, reason = _should_trigger_cleanup(md)
    assert trigger is True
    assert "Epic-01" in reason or "duplicate" in reason.lower()


def test_distinct_epics_no_trigger():
    """다른 Epic ID 여러 개 → 정상 — trip 안 됨."""
    md = (
        "#### 📦 [Epic-01] A\n"
        "#### 📦 [Epic-02] B\n"
        "#### 📦 [Epic-03] C\n"
    )
    trigger, reason = _should_trigger_cleanup(md)
    assert trigger is False


# ─── [2026-06-01] Section 1 Overview 중복 detection ──────────────


def test_duplicate_product_vision_triggers():
    """Section 1 에 **Product Vision** 이 2회+ 누적 → 누더기 trip (실사용 스샷 케이스)."""
    md = (
        "### 1. Product Overview (통합 제품 비전)\n"
        "- **Product Vision**: AI 에이전트 상태를 직관적으로 파악\n"
        "- **Product Vision**: 사용자 작업을 자동화\n"
        "- **Product Vision**: 생산성을 극대화\n"
        "### 2. Epic & User Story Map\n"
        "#### 📦 [Epic-01] X\n"
    )
    trigger, reason = _should_trigger_cleanup(md)
    assert trigger is True
    assert "Product Vision" in reason


def test_duplicate_success_metrics_triggers():
    """**Success Metrics** 가 2회+ → trip."""
    md = (
        "### 1. Product Overview\n"
        "- **Success Metrics**: 작업 성공률 95%\n"
        "- **Success Metrics**: MAU 10% 증가\n"
    )
    trigger, reason = _should_trigger_cleanup(md)
    assert trigger is True
    assert "Success Metrics" in reason


def test_single_overview_label_no_trigger():
    """정리된 PRD — **통합 비전** 1회 → trip 안 됨 (cleanup 출력이 재-trip 안 되게 수렴)."""
    md = (
        "### 1. Product Overview (통합 제품 비전)\n"
        "- **통합 비전**: 단일 통합 비전\n"
        "- **핵심 타겟 및 권한**: 관리자/사용자\n"
        "### 2. Epic & User Story Map\n"
        "#### 📦 [Epic-01] X\n"
    )
    trigger, reason = _should_trigger_cleanup(md)
    assert trigger is False, f"reason={reason}"


def test_prose_vision_no_false_positive():
    """bold 라벨이 아닌 prose 'product vision' 언급 → 오탐 안 함 (** 없으면 매칭 X)."""
    md = (
        "### 1. Product Overview\n"
        "- **통합 비전**: 우리의 product vision 은 명확하다. product vision 강조.\n"
    )
    trigger, reason = _should_trigger_cleanup(md)
    assert trigger is False, f"reason={reason}"


# ─── _maybe_trigger_auto_cleanup 통합 검증 ───────────────────


@pytest.fixture
def mock_query_repo_clean(monkeypatch):
    """get_master_prd 가 작은 markdown 반환 — trip 안 됨."""
    async def fake(project_name, team_id=""):
        return SimpleNamespace(prd_content="### 2. Epic\n#### 📦 [Epic-01] X\n")
    monkeypatch.setattr(
        "app.service.query_repository.get_master_prd", fake
    )


@pytest.fixture
def mock_query_repo_dirty(monkeypatch):
    """get_master_prd 가 누더기 (Epic ID 중복) markdown 반환 — trip."""
    async def fake(project_name, team_id=""):
        return SimpleNamespace(prd_content=(
            "#### 📦 [Epic-01] A\n"
            "#### 📦 [Epic-01] A 재등장\n"  # 중복!
        ))
    monkeypatch.setattr(
        "app.service.query_repository.get_master_prd", fake
    )


@pytest.fixture
def mock_query_repo_no_master(monkeypatch):
    async def fake(project_name, team_id=""):
        return None
    monkeypatch.setattr(
        "app.service.query_repository.get_master_prd", fake
    )


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


async def test_no_master_skips_cleanup(
    mock_query_repo_no_master, fake_enqueue,
):
    """master 없으면 cleanup enqueue 안 함."""
    await _maybe_trigger_auto_cleanup(
        project_name="empty_proj", user_email="u@x", parent_job_id="j1",
    )
    assert fake_enqueue["calls"] == []


async def test_clean_master_skips_cleanup(
    mock_query_repo_clean, fake_enqueue,
):
    """정상 master 면 enqueue 안 함 (불필요 LLM 비용 차단)."""
    await _maybe_trigger_auto_cleanup(
        project_name="clean_proj", user_email="u@x", parent_job_id="j2",
    )
    assert fake_enqueue["calls"] == []


async def test_dirty_master_enqueues_cleanup(
    mock_query_repo_dirty, fake_enqueue,
):
    """누더기 master 면 cleanup enqueue (dry_run=False — 즉시 apply)."""
    await _maybe_trigger_auto_cleanup(
        project_name="dirty_proj", user_email="u@x", parent_job_id="j3",
    )
    assert len(fake_enqueue["calls"]) == 1
    call = fake_enqueue["calls"][0]
    assert call["project_name"] == "dirty_proj"
    assert call["dry_run"] is False, "auto-cleanup 은 즉시 apply (사용자 confirm X)"
    assert call["user_email"] == "u@x"


async def test_get_master_prd_failure_swallowed(
    fake_enqueue, monkeypatch,
):
    """Neo4j 일시 장애로 get_master_prd 가 raise 해도 swallow — 사용자 영향 0."""
    async def fail(project_name):
        raise RuntimeError("neo4j down")
    monkeypatch.setattr(
        "app.service.query_repository.get_master_prd", fail
    )
    # raise 없이 정상 종료
    await _maybe_trigger_auto_cleanup(
        project_name="p", user_email="u@x", parent_job_id="j4",
    )
    assert fake_enqueue["calls"] == []  # enqueue 도 안 됨


async def test_enqueue_failure_swallowed(
    mock_query_repo_dirty, monkeypatch,
):
    """enqueue 실패 (Redis 장애 등) 도 swallow."""
    async def fail(*, task_id, project_name, dry_run, user_email):
        raise RuntimeError("redis down")
    monkeypatch.setattr(
        "app.queue.client.enqueue_cleanup_master_prd", fail
    )
    # raise 없이 정상 종료
    await _maybe_trigger_auto_cleanup(
        project_name="p", user_email="u@x", parent_job_id="j5",
    )
