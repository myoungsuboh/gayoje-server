"""
skill_routes.py IDOR 회귀 가드.

[배경]
2026-05 보안 감사 발견: /api/v2/skills/* 6개 라우트가 ownership 검증 없이
인증만으로 통과 → 본인 JWT 보유자가 타인 프로젝트의 Skill 전수 조회/수정/
삭제 가능. gateway_compat 의 _OWNERSHIP_CREATE / _OWNERSHIP_ACCESS 와 정합 X.

[픽스 패턴]
- POST /skills          : claim_project (다른 owner → 409, 본인/미존재 → 통과)
- GET/DELETE skills/*   : assert_owns (다른 owner → 403)
- POST recommend_skills : assert_owns (LLM 비용 + 응답 markdown leak 방어)
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi import HTTPException, status as http_status

from app.api import skill_routes
from app.service import ownership_repository
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio

def _fake_request():
    """slowapi limiter request 객체 우회."""
    from types import SimpleNamespace
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/v2/test"),
        method="POST",
    )



def _user(email: str = "attacker@evil.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="t",
        subscription_type="free", is_admin=False,
    )


@pytest.fixture
def deny_ownership(monkeypatch):
    """assert_owns 가 403 raise."""
    async def fake_assert(email: str, project: str):
        raise HTTPException(status_code=403, detail="해당 프로젝트에 대한 권한이 없습니다.")
    monkeypatch.setattr(
        "app.api.skill_routes.ownership_repository.assert_owns", fake_assert
    )


@pytest.fixture
def deny_claim(monkeypatch):
    """claim_project 가 ProjectOwnershipConflict raise — 다른 user 소유."""
    async def fake_claim(email: str, project: str):
        raise ownership_repository.ProjectOwnershipConflict(
            project=project, current_owner_hint="victim@x.com"
        )
    monkeypatch.setattr(
        "app.api.skill_routes.ownership_repository.claim_project", fake_claim
    )


# ─── POST /skills — CREATE 패턴 (claim_project) ─────────────


async def test_post_skills_409_when_project_owned_by_another(deny_claim):
    from app.service.skill_repository import SkillInput
    payload = skill_routes.PostSkillsRequest(
        project_name="victim_project",
        skills=[SkillInput(id="s1", name="dummy")],
    )
    with pytest.raises(HTTPException) as exc:
        await skill_routes.post_skills.__wrapped__(
            request=_fake_request(), payload=payload, current_user=_user(),
        )
    assert exc.value.status_code == 409
    assert "이미 다른 사용자가 사용 중" in str(exc.value.detail)


# ─── GET/DELETE /skills/* — ACCESS 패턴 (assert_owns) ────────


async def test_get_all_skills_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await skill_routes.get_all_skills_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project", current_user=_user(),
        )
    assert exc.value.status_code == 403


async def test_get_skill_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await skill_routes.get_skill_route.__wrapped__(
            request=_fake_request(),
            skill_id="s-1", project_name="victim_project", current_user=_user(),
        )
    assert exc.value.status_code == 403


async def test_duplicate_skill_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await skill_routes.duplicate_skill_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project", name="anything", current_user=_user(),
        )
    assert exc.value.status_code == 403


async def test_delete_skill_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await skill_routes.delete_skill_route.__wrapped__(
            request=_fake_request(),
            skill_id="s-1", project_name="victim_project", current_user=_user(),
        )
    assert exc.value.status_code == 403


# ─── POST /pipelines/recommend_skills — LLM 호출 전 차단 ────


async def test_recommend_skills_denies_when_not_owner(deny_ownership):
    payload = skill_routes.RecommendSkillsRequest(
        project_name="victim_project",
        skill_catalog=[
            skill_routes.RecommendCatalogItem(
                id="c1", name="React", description="UI lib", category="frontend",
            )
        ],
        allowed_categories=[],
    )
    with pytest.raises(HTTPException) as exc:
        await skill_routes.recommend_skills_route.__wrapped__(
            request=_fake_request(),
            payload=payload, wait=False, current_user=_user(),
        )
    assert exc.value.status_code == 403


# ─── POST /pipelines/fill_skill_triggers — LLM 호출 전 차단 ──


async def test_fill_skill_triggers_denies_when_not_owner(deny_ownership):
    payload = skill_routes.FillSkillTriggersRequest(
        project_name="victim_project",
        skills=[
            skill_routes.FillTriggerSkillItem(id="s1", name="React 규칙"),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        await skill_routes.fill_skill_triggers_route.__wrapped__(
            request=_fake_request(),
            payload=payload, current_user=_user(),
        )
    assert exc.value.status_code == 403
