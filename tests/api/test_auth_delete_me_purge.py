"""
회원 탈퇴 시 개인 소유 프로젝트 파기 (P0-5).

[배경]
개인정보처리방침이 "회원 탈퇴 시 즉시 파기"를 약속하지만, 기존 DELETE /auth/me 는
User 노드만 지우고 프로젝트 데이터(미팅 로그 = 개인정보 포함 가능)를 보존했다 —
문서-실제 불일치. 수정: 탈퇴 시 **본인 단독 소유(개인) 프로젝트**의 도메인 데이터를
함께 파기한다. 팀 프로젝트는 협업자 보호를 위해 보존(처리방침에 예외 명시).
"""
from __future__ import annotations

import pytest

from app.api import auth_routes
from app.service import user_repository

pytestmark = pytest.mark.asyncio


async def test_list_owned_project_names_queries_by_owner_email(monkeypatch):
    captured = {}

    async def _run(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"name": "p1"}, {"name": "p2"}, {"name": None}]

    monkeypatch.setattr(user_repository.neo4j_client, "run_cypher", _run)
    names = await user_repository.list_owned_project_names("a@b.com")
    assert names == ["p1", "p2"]                       # None 행 제외
    assert "owner_email" in captured["cypher"]          # 개인 소유만 (팀 프로젝트 제외)
    assert captured["params"]["email"] == "a@b.com"


async def test_purge_owned_projects_deletes_each_and_survives_failures(monkeypatch):
    """프로젝트별 best-effort — 한 건 실패해도 나머지 계속 + ctx.user_email 로 소유 검증."""
    async def _names(email):
        assert email == "a@b.com"
        return ["p1", "p2", "p3"]
    monkeypatch.setattr(auth_routes.users, "list_owned_project_names", _names, raising=False)

    calls = []

    async def _delete_project(ctx, project_name, team_id=""):
        calls.append({"name": project_name, "user_email": ctx.user_email})
        if project_name == "p2":
            raise RuntimeError("일시 오류")
        return {"status": "deleted"}
    monkeypatch.setattr(auth_routes, "delete_project", _delete_project)

    summary = await auth_routes._purge_owned_projects("a@b.com")

    assert [c["name"] for c in calls] == ["p1", "p2", "p3"]   # 실패에도 전부 시도
    assert all(c["user_email"] == "a@b.com" for c in calls)   # Project 노드 owner 매칭용
    assert summary == {"deleted": 2, "failed": 1}
