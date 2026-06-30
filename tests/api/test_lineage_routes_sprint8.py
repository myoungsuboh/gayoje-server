"""
lineage_routes 의 Sprint 8 P1 변경 통합 테스트.

[검증]
1. analyze_lineage_route 가 wait 가드 + assert_owns 호출
2. save/delete/import lineage_truth_route 가 audit_repository.write 호출
3. delete 의 audit 는 실제 삭제 발생 시에만
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.api import lineage_routes
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


def _user(email: str = "alice@x", is_admin: bool = False) -> UserPublic:
    u = UserPublic(id=email, email=email, name="t", created_at=None)
    # UserPublic 에 is_admin 이 있는지 모듈마다 다름 — 동적 추가.
    object.__setattr__(u, "is_admin", is_admin) if hasattr(u, "__dict__") else None
    return u


# ─── audit 호출 mock ──


@pytest.fixture
def audit_recorder(monkeypatch):
    calls: List[Dict[str, Any]] = []

    async def fake_write(**kwargs):
        calls.append(kwargs)
        return "id1"

    monkeypatch.setattr(
        "app.api.lineage_routes.audit_repository.write", fake_write
    )
    return calls


@pytest.fixture
def deny_ownership(monkeypatch):
    """assert_owns 가 항상 통과 (test 가 ownership 외 부분에 집중)."""
    async def fake(email, project):
        return None
    monkeypatch.setattr(
        "app.api.lineage_routes.ownership_repository.assert_owns", fake
    )


# ─── save: audit 호출 검증 ──


async def test_save_truth_writes_audit(audit_recorder, deny_ownership, monkeypatch):
    async def fake_save(project, item_type, item_id, expected_files, team_id=""):
        return lineage_routes.LineageTruth(
            project=project, itemType=item_type, itemId=item_id,
            expectedFiles=expected_files, updatedAt=1700,
        )
    monkeypatch.setattr(
        "app.api.lineage_routes.lineage_repository.save_lineage_truth", fake_save
    )

    payload = lineage_routes.LineageTruthUpsertRequest(
        project_name="x", item_type="api", item_id="a1",
        expected_files=["f.py", "g.py"],
    )
    out = await lineage_routes.save_lineage_truth_route(payload, _user())
    assert out.itemId == "a1"
    # audit 1건 — action 매핑 + project / item* 메타
    assert len(audit_recorder) == 1
    rec = audit_recorder[0]
    assert rec["action"] == "lineage_truth_save"
    assert rec["actor_email"] == "alice@x"
    assert rec["payload"]["project"] == "x"
    assert rec["payload"]["itemType"] == "api"
    assert rec["payload"]["itemId"] == "a1"
    assert rec["payload"]["expectedFilesCount"] == 2


# ─── delete: 실제 삭제 시에만 audit ──


async def test_delete_truth_writes_audit_when_actually_deleted(
    audit_recorder, deny_ownership, monkeypatch
):
    async def fake_delete(project, item_type, item_id, team_id=""):
        return True
    monkeypatch.setattr(
        "app.api.lineage_routes.lineage_repository.delete_lineage_truth", fake_delete
    )
    out = await lineage_routes.delete_lineage_truth_route(
        project_name="x", item_type="api", item_id="a1", current_user=_user()
    )
    assert out == {"deleted": True}
    assert len(audit_recorder) == 1
    assert audit_recorder[0]["action"] == "lineage_truth_delete"


async def test_delete_truth_skips_audit_when_noop(
    audit_recorder, deny_ownership, monkeypatch
):
    """이미 없는 (no-op) 삭제 — audit 노이즈 차단."""
    async def fake_delete(project, item_type, item_id, team_id=""):
        return False
    monkeypatch.setattr(
        "app.api.lineage_routes.lineage_repository.delete_lineage_truth", fake_delete
    )
    out = await lineage_routes.delete_lineage_truth_route(
        project_name="x", item_type="api", item_id="ghost", current_user=_user()
    )
    assert out == {"deleted": False}
    assert audit_recorder == []   # ← 비기록


# ─── import: 벌크 audit ──


async def test_import_truth_writes_audit_aggregate(
    audit_recorder, deny_ownership, monkeypatch
):
    async def fake_import(project, items, override, team_id=""):
        return {"written": 5, "skipped": 2}
    monkeypatch.setattr(
        "app.api.lineage_routes.lineage_repository.import_lineage_truth", fake_import
    )
    payload = lineage_routes.LineageTruthImportRequest(
        project_name="x",
        items=[{"itemType": "api", "itemId": f"a{i}"} for i in range(7)],
        override=False,
    )
    # request fixture mock — slowapi limiter 가 request 객체 검사 → SimpleNamespace 우회
    fake_req = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/v2/lineage/truth/import"),
        method="POST",
    )
    out = await lineage_routes.import_lineage_truth_route.__wrapped__(
        request=fake_req, payload=payload, current_user=_user(),
    )
    assert out.written == 5 and out.skipped == 2
    assert len(audit_recorder) == 1
    rec = audit_recorder[0]
    assert rec["action"] == "lineage_truth_import"
    assert rec["payload"]["written"] == 5
    assert rec["payload"]["skipped"] == 2
    assert rec["payload"]["requestedCount"] == 7
    assert rec["payload"]["override"] is False


# ─── analyze_lineage: wait 가드 + ownership ──


async def test_analyze_lineage_calls_wait_guard_and_assert_owns(monkeypatch):
    """wait 가드와 ownership 검증 호출 순서 확인."""
    calls: List[str] = []

    async def fake_assert_owns(email, project):
        calls.append(f"assert_owns:{email}:{project}")

    def fake_guard(wait, user):
        calls.append(f"guard_wait:{wait}:{getattr(user, 'is_admin', False)}")

    async def fake_get_token(email):
        return None

    async def fake_enqueue(**kwargs):
        calls.append(f"enqueue:{kwargs['task_id'][:6]}")
        return kwargs["task_id"]

    monkeypatch.setattr(lineage_routes, "guard_wait_mode", fake_guard)
    monkeypatch.setattr(
        "app.api.lineage_routes.ownership_repository.assert_owns", fake_assert_owns
    )
    monkeypatch.setattr(
        "app.service.user_repository.get_github_access_token", fake_get_token
    )
    monkeypatch.setattr(lineage_routes, "enqueue_analyze_lineage", fake_enqueue)

    payload = lineage_routes.LineageRequest(project_name="x")
    out = await lineage_routes.analyze_lineage_route(
        payload, wait=False, current_user=_user()
    )
    assert out.status == "accepted"
    # 호출 순서: guard → assert_owns → enqueue
    assert calls[0].startswith("guard_wait")
    assert calls[1].startswith("assert_owns")
    assert calls[2].startswith("enqueue")
