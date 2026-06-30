"""
lint_routes.py IDOR 회귀 가드.

[배경]
2026-05 보안 감사 발견:
  - POST /api/v2/pipelines/lint              : 타인 프로젝트 명세로 lint 실행 →
    응답 markdown 에 타인 PRD/Spack/Skill 포함 + 본인 OAuth 토큰으로 임의
    GitHub repo 접근 + 본인 quota 소진.
  - GET  /api/v2/pipelines/lint/last         : 타인 lint 결과 조회.
  - POST /api/v2/pipelines/generate_fix_spec : 타인 lint 결과로 fix_spec markdown
    생성 (LLM).
  - POST /api/v2/projects/repos (addRepo)    : 타인 프로젝트에 임의 repo 부착.
  - GET/DELETE /api/v2/projects/repos        : 타인 repo 목록/삭제.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import lint_routes
from app.service import ownership_repository
from app.service.repo_repository import RepoIn
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
    async def fake_assert(email: str, project: str):
        raise HTTPException(status_code=403, detail="해당 프로젝트에 대한 권한이 없습니다.")
    monkeypatch.setattr(
        "app.api.lint_routes.ownership_repository.assert_owns", fake_assert
    )


@pytest.fixture
def deny_claim(monkeypatch):
    async def fake_claim(email: str, project: str):
        raise ownership_repository.ProjectOwnershipConflict(
            project=project, current_owner_hint="victim@x.com"
        )
    monkeypatch.setattr(
        "app.api.lint_routes.ownership_repository.claim_project", fake_claim
    )


# ─── LLM 라우트 (ACCESS) — 호출 전 차단 ──────────────────────


async def test_run_lint_denies_when_not_owner(deny_ownership):
    payload = lint_routes.LintRequest(
        project_name="victim_project",
        github_url="https://github.com/x/y",
    )
    with pytest.raises(HTTPException) as exc:
        await lint_routes.run_lint_route.__wrapped__(
            request=_fake_request(),
            payload=payload, wait=False, current_user=_user(),
        )
    assert exc.value.status_code == 403


async def test_get_last_lint_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await lint_routes.get_last_lint_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project",
            github_url="https://github.com/x/y",
            current_user=_user(),
        )
    assert exc.value.status_code == 403


async def test_generate_fix_spec_denies_when_not_owner(deny_ownership):
    payload = lint_routes.FixSpecRequest(
        project_name="victim_project",
        github_url="https://github.com/x/y",
        lint_result={"items": []},
    )
    with pytest.raises(HTTPException) as exc:
        await lint_routes.generate_fix_spec_route.__wrapped__(
            request=_fake_request(),
            payload=payload, wait=False, current_user=_user(),
        )
    assert exc.value.status_code == 403


# ─── Repo CRUD — POST(CREATE) + GET/DELETE(ACCESS) ──────────


async def test_add_project_repo_409_when_project_owned_by_another(deny_claim):
    payload = RepoIn(project_name="victim_project", url="https://github.com/x/y")
    with pytest.raises(HTTPException) as exc:
        await lint_routes.add_project_repo_route.__wrapped__(
            request=_fake_request(),
            payload=payload, current_user=_user(),
        )
    assert exc.value.status_code == 409


async def test_get_project_repos_denies_when_not_owner(deny_ownership):
    with pytest.raises(HTTPException) as exc:
        await lint_routes.get_project_repos_route.__wrapped__(
            request=_fake_request(),
            project_name="victim_project", current_user=_user(),
        )
    assert exc.value.status_code == 403


async def test_delete_project_repo_denies_when_not_owner(deny_ownership):
    payload = lint_routes.RepoDeleteRequest(
        project_name="victim_project", url="https://github.com/x/y",
    )
    with pytest.raises(HTTPException) as exc:
        await lint_routes.delete_project_repo_route.__wrapped__(
            request=_fake_request(),
            payload=payload, current_user=_user(),
        )
    assert exc.value.status_code == 403
