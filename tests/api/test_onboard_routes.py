"""
onboard_from_github route 통합 가드.

[검증 범위]
1. ownership claim — 같은 이름 다른 owner 면 409
2. enqueue 정상 — 202 + task_id, user_email/user_token 전달
3. wait=true 정상 흐름 — pipeline 호출 결과 펼침
4. wait=true + GitHub 404 → 422 GITHUB_REPO_NOT_FOUND
5. wait=true + 401/403 → 422 GITHUB_REPO_PRIVATE_NEEDS_AUTH
6. wait=true + invalid URL → 422 INVALID_GITHUB_URL
7. status 라우트 — ownership 검증
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import v2_routes
from app.clients.github_client import GitHubError
from app.service import ownership_repository
from app.service.user_repository import UserPublic

pytestmark = pytest.mark.asyncio


def _fake_request():
    """slowapi limiter request 객체 우회."""
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/v2/pipelines/onboard_from_github"),
        method="POST",
    )


def _user(email: str = "u@x.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="t",
        subscription_type="free", is_admin=False,
    )


@pytest.fixture(autouse=True)
def _no_op_quota(monkeypatch):
    """quota 가드 통과 (테스트 기본). 개별 테스트가 override 가능."""
    async def _ok(*args, **kwargs):
        return None
    monkeypatch.setattr("app.api.v2_routes.quota.assert_tokens_within_limit", _ok)
    monkeypatch.setattr("app.api.v2_routes.quota.acquire_meeting_quota", _ok)


@pytest.fixture
def claim_ok(monkeypatch):
    """프로젝트 claim 성공."""
    async def _claim(email, project):
        return None
    monkeypatch.setattr(
        "app.api.v2_routes.ownership_repository.claim_project", _claim,
    )


@pytest.fixture
def claim_conflict(monkeypatch):
    """이미 다른 사용자가 프로젝트 보유 → ProjectOwnershipConflict."""
    async def _claim(email, project):
        raise ownership_repository.ProjectOwnershipConflict(
            project=project, current_owner_hint="other@x.com",
        )
    monkeypatch.setattr(
        "app.api.v2_routes.ownership_repository.claim_project", _claim,
    )


@pytest.fixture
def github_token(monkeypatch):
    """user_repository.get_github_access_token 항상 'ghp_test' 반환."""
    async def _token(email):
        return "ghp_test"
    # v2_routes 내부에서 from app.service import user_repository 한 후 함수 호출.
    # monkeypatch 대상은 user_repository.get_github_access_token.
    import app.service.user_repository as ur
    monkeypatch.setattr(ur, "get_github_access_token", _token)


@pytest.fixture
def github_token_none(monkeypatch):
    """GitHub 미연결 사용자 — None 반환."""
    async def _token(email):
        return None
    import app.service.user_repository as ur
    monkeypatch.setattr(ur, "get_github_access_token", _token)


# ─── ownership / claim ─────────────────────────────────────────────


async def test_onboard_denies_when_project_owned_by_other(claim_conflict):
    """이미 다른 사용자 소유 → 409."""
    payload = v2_routes.OnboardFromGithubRequest(
        project_name="victim_project",
        github_url="https://github.com/x/y",
    )
    with pytest.raises(HTTPException) as exc:
        await v2_routes.run_onboard_from_github.__wrapped__(
            request=_fake_request(),
            payload=payload, wait=False, current_user=_user(),
        )
    assert exc.value.status_code == 409
    assert "이미 다른 사용자" in str(exc.value.detail)


# ─── enqueue 경로 ──────────────────────────────────────────────────


async def test_onboard_enqueue_returns_task_id_with_user_token(
    monkeypatch, claim_ok, github_token,
):
    """async 모드 — enqueue 호출됨, task_id 반환, user_token 전달."""
    captured: dict = {}

    async def _enqueue(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(
        "app.api.v2_routes.enqueue_github_onboard", _enqueue,
    )

    payload = v2_routes.OnboardFromGithubRequest(
        project_name="my-app", github_url="https://github.com/u/r",
    )
    response = await v2_routes.run_onboard_from_github.__wrapped__(
        request=_fake_request(),
        payload=payload, wait=False, current_user=_user("hero@x.com"),
    )

    assert response.status == "accepted"
    assert response.task_id  # uuid 자동 생성
    # enqueue 에 정확한 인자 전달됐는지
    assert captured["project_name"] == "my-app"
    assert captured["github_url"] == "https://github.com/u/r"
    assert captured["user_token"] == "ghp_test"
    assert captured["user_email"] == "hero@x.com"


async def test_onboard_enqueue_with_none_user_token_anonymous(
    monkeypatch, claim_ok, github_token_none,
):
    """GitHub 미연결 사용자 — user_token=None 으로 enqueue (public repo 만 가능)."""
    captured: dict = {}

    async def _enqueue(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(
        "app.api.v2_routes.enqueue_github_onboard", _enqueue,
    )

    payload = v2_routes.OnboardFromGithubRequest(
        project_name="public-app", github_url="https://github.com/u/r",
    )
    response = await v2_routes.run_onboard_from_github.__wrapped__(
        request=_fake_request(),
        payload=payload, wait=False, current_user=_user(),
    )
    assert response.status == "accepted"
    assert captured["user_token"] is None


async def test_onboard_enqueue_failure_returns_503(
    monkeypatch, claim_ok, github_token,
):
    """enqueue (Redis 등) 실패 → 503."""
    async def _bad_enqueue(**kwargs):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(
        "app.api.v2_routes.enqueue_github_onboard", _bad_enqueue,
    )

    payload = v2_routes.OnboardFromGithubRequest(
        project_name="p", github_url="https://github.com/u/r",
    )
    with pytest.raises(HTTPException) as exc:
        await v2_routes.run_onboard_from_github.__wrapped__(
            request=_fake_request(),
            payload=payload, wait=False, current_user=_user(),
        )
    assert exc.value.status_code == 503


# ─── wait=true 경로 + GitHub 에러 매핑 ───────────────────────────────


async def test_onboard_wait_github_404_maps_to_422(
    monkeypatch, claim_ok, github_token_none,
):
    """wait=true 일 때 GitHub 404 → 422 + GITHUB_REPO_NOT_FOUND code."""
    async def _bad(*args, **kwargs):
        raise GitHubError("저장소 없음", status=404)

    monkeypatch.setattr(
        "app.api.v2_routes.run_github_onboard_pipeline", _bad,
    )

    payload = v2_routes.OnboardFromGithubRequest(
        project_name="p", github_url="https://github.com/u/ghost",
    )
    with pytest.raises(HTTPException) as exc:
        await v2_routes.run_onboard_from_github.__wrapped__(
            request=_fake_request(),
            payload=payload, wait=True, current_user=_user(),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "GITHUB_REPO_NOT_FOUND"


async def test_onboard_wait_github_401_maps_to_private_auth_needed(
    monkeypatch, claim_ok, github_token_none,
):
    """401/403 → 422 GITHUB_REPO_PRIVATE_NEEDS_AUTH."""
    async def _bad(*args, **kwargs):
        raise GitHubError("권한 없음", status=403)

    monkeypatch.setattr(
        "app.api.v2_routes.run_github_onboard_pipeline", _bad,
    )

    payload = v2_routes.OnboardFromGithubRequest(
        project_name="p", github_url="https://github.com/u/private-repo",
    )
    with pytest.raises(HTTPException) as exc:
        await v2_routes.run_onboard_from_github.__wrapped__(
            request=_fake_request(),
            payload=payload, wait=True, current_user=_user(),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "GITHUB_REPO_PRIVATE_NEEDS_AUTH"


async def test_onboard_wait_invalid_url_maps_to_422(
    monkeypatch, claim_ok, github_token_none,
):
    """URL 파싱 실패 (status None) → 422 INVALID_GITHUB_URL."""
    async def _bad(*args, **kwargs):
        raise GitHubError("URL 파싱 실패: not-a-url")

    monkeypatch.setattr(
        "app.api.v2_routes.run_github_onboard_pipeline", _bad,
    )

    payload = v2_routes.OnboardFromGithubRequest(
        project_name="p", github_url="not-a-url",
    )
    with pytest.raises(HTTPException) as exc:
        await v2_routes.run_onboard_from_github.__wrapped__(
            request=_fake_request(),
            payload=payload, wait=True, current_user=_user(),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INVALID_GITHUB_URL"


async def test_onboard_wait_value_error_maps_to_422(
    monkeypatch, claim_ok, github_token_none,
):
    """pipeline 의 ValueError (V1 너무 짧음 / 빈 tree 등) → 422."""
    async def _bad(*args, **kwargs):
        raise ValueError("V1 항목을 충분히 추출하지 못했습니다")

    monkeypatch.setattr(
        "app.api.v2_routes.run_github_onboard_pipeline", _bad,
    )

    payload = v2_routes.OnboardFromGithubRequest(
        project_name="p", github_url="https://github.com/u/r",
    )
    with pytest.raises(HTTPException) as exc:
        await v2_routes.run_onboard_from_github.__wrapped__(
            request=_fake_request(),
            payload=payload, wait=True, current_user=_user(),
        )
    assert exc.value.status_code == 422


async def test_onboard_wait_success_returns_pipeline_result(
    monkeypatch, claim_ok, github_token_none,
):
    """wait=true 정상 흐름 — 결과 펼침."""
    from app.pipelines.cps_pipeline.types import CpsResult
    from app.pipelines.github_onboard_pipeline import GithubOnboardResult

    mock_cps = CpsResult(
        meeting_log_id="log1", delta_cps_id="cps1",
        master_cps_id="master1", mode="first_run",
        diagnostic={}, cps_graph={"nodes": []}, extraction_mode="strict",
    )
    mock_result = GithubOnboardResult(
        project_name="p", github_url="https://github.com/u/r",
        repo_full_name="u/r", v1_markdown="some v1", v1_markdown_size=350,
        sampled_file_count=8, sampled_file_paths=["README.md"],
        cps_result=mock_cps, diagnostic={"is_private": False},
    )

    async def _ok(*args, **kwargs):
        return mock_result

    monkeypatch.setattr(
        "app.api.v2_routes.run_github_onboard_pipeline", _ok,
    )

    payload = v2_routes.OnboardFromGithubRequest(
        project_name="p", github_url="https://github.com/u/r",
    )
    response = await v2_routes.run_onboard_from_github.__wrapped__(
        request=_fake_request(),
        payload=payload, wait=True, current_user=_user(),
    )
    assert response.status == "success"
    assert response.project_name == "p"
    assert response.repo_full_name == "u/r"
    assert response.v1_markdown_size == 350
    assert response.sampled_file_count == 8
    assert response.cps_master_id == "master1"
    assert response.cps_delta_id == "cps1"
    assert response.cps_mode == "first_run"


# ─── status route ──────────────────────────────────────────────────


async def test_onboard_status_uses_ownership_filtered_helper(monkeypatch):
    """status 라우트 — get_job_status_for_user 호출 (ownership 검증 포함)."""
    captured: dict = {}

    async def _status_for_user(task_id, email):
        captured["task_id"] = task_id
        captured["email"] = email
        return {
            "task_id": task_id, "project_name": "p",
            "status": "complete", "result": None, "error": None,
            "enqueue_time": None, "finish_time": None,
        }

    monkeypatch.setattr(
        "app.api.v2_routes.get_job_status_for_user", _status_for_user,
    )

    response = await v2_routes.onboard_from_github_status.__wrapped__(
        request=_fake_request(),
        task_id="task-xyz", current_user=_user("alice@x.com"),
    )
    assert captured["task_id"] == "task-xyz"
    assert captured["email"] == "alice@x.com"
    assert response.task_id == "task-xyz"
