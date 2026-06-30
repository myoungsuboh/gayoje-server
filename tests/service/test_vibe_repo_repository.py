"""
vibe_repo_repository 단위 테스트.

Neo4j 호출은 mock 으로 대체 — Cypher 구조 + 파라미터 바인딩 + URL 정규화 검증.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.service import vibe_repo_repository as repo
from app.service.vibe_repo_repository import (
    VibeRepoInput,
    VibeRepoOut,
    normalize_github_url,
)


# ─── normalize_github_url ───────────────────────────────────


class TestNormalizeGithubUrl:
    def test_strips_git_suffix(self):
        url, handle = normalize_github_url("https://github.com/foo/bar.git")
        assert url == "https://github.com/foo/bar"
        assert handle == "foo"

    def test_strips_trailing_slash(self):
        url, handle = normalize_github_url("https://github.com/foo/bar/")
        assert url == "https://github.com/foo/bar"
        assert handle == "foo"

    def test_strips_path_after_repo(self):
        url, handle = normalize_github_url(
            "https://github.com/foo/bar/issues/123"
        )
        # parse_github_url 은 owner/repo 만 추출
        assert url == "https://github.com/foo/bar"
        assert handle == "foo"

    def test_ssh_url_format(self):
        # SSH format: git@github.com:foo/bar.git
        url, handle = normalize_github_url("git@github.com:foo/bar.git")
        assert url == "https://github.com/foo/bar"
        assert handle == "foo"

    def test_invalid_url_raises_value_error(self):
        with pytest.raises(ValueError, match="GitHub URL 형식"):
            normalize_github_url("not-a-url")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            normalize_github_url("")


# ─── add_vibe_repo ─────────────────────────────────────


@pytest.mark.asyncio
async def test_add_vibe_repo_returns_normalized_out():
    """ADD 가 정규화된 URL + owner_handle 을 반환하고 Cypher 에 정확히 바인딩."""
    fake_now_ms = 1737000000000
    mock_records = [{
        "repo": {
            "url": "https://github.com/foo/bar",
            "owner_handle": "foo",
            "label": "My App",
            "description": "vibe-coded login",
            "is_mine": True,
            "added_at": fake_now_ms,
            "updated_at": fake_now_ms,
        }
    }]

    with patch.object(
        repo.neo4j_client, "run_cypher", new=AsyncMock(return_value=mock_records)
    ) as mock_run:
        out = await repo.add_vibe_repo(
            email="alice@example.com",
            payload=VibeRepoInput(
                url="https://github.com/foo/bar.git/",
                label="My App",
                description="vibe-coded login",
                is_mine=True,
            ),
        )

    # 응답 검증
    assert isinstance(out, VibeRepoOut)
    assert out.url == "https://github.com/foo/bar"
    assert out.owner_handle == "foo"
    assert out.label == "My App"
    assert out.is_mine is True
    assert out.added_at == fake_now_ms

    # Cypher 호출 검증 — 정규화된 URL 이 바인딩됐는지
    assert mock_run.call_count == 1
    args, kwargs = mock_run.call_args
    cypher = args[0]
    params = args[1]
    assert "MERGE (r:VibeRepo {user_email: $email, url: $url})" in cypher
    assert params["email"] == "alice@example.com"
    assert params["url"] == "https://github.com/foo/bar"  # 정규화됨
    assert params["owner_handle"] == "foo"
    assert params["is_mine"] is True


@pytest.mark.asyncio
async def test_add_vibe_repo_rejects_invalid_url():
    """파싱 실패 URL 은 ValueError → router 가 422 매핑."""
    with pytest.raises(ValueError, match="GitHub URL 형식"):
        await repo.add_vibe_repo(
            email="alice@example.com",
            payload=VibeRepoInput(url="not-a-github-url"),
        )


@pytest.mark.asyncio
async def test_add_vibe_repo_empty_email_raises():
    with pytest.raises(ValueError, match="email"):
        await repo.add_vibe_repo(
            email="",
            payload=VibeRepoInput(url="https://github.com/foo/bar"),
        )


@pytest.mark.asyncio
async def test_add_vibe_repo_runtime_error_when_user_missing():
    """User 노드가 없으면 MERGE 매칭 실패 → 빈 결과 → RuntimeError."""
    with patch.object(
        repo.neo4j_client, "run_cypher", new=AsyncMock(return_value=[])
    ):
        with pytest.raises(RuntimeError, match="user not found"):
            await repo.add_vibe_repo(
                email="ghost@example.com",
                payload=VibeRepoInput(url="https://github.com/foo/bar"),
            )


# ─── get_vibe_repos ────────────────────────────────────


@pytest.mark.asyncio
async def test_get_vibe_repos_returns_list():
    mock_records = [
        {
            "repo": {
                "url": "https://github.com/me/app",
                "owner_handle": "me",
                "label": "my app",
                "description": "",
                "is_mine": True,
                "added_at": 1000,
                "updated_at": 2000,
            }
        },
        {
            "repo": {
                "url": "https://github.com/coworker/lib",
                "owner_handle": "coworker",
                "label": "shared lib",
                "description": "동료 작업",
                "is_mine": False,
                "added_at": 1500,
                "updated_at": 1500,
            }
        },
    ]
    with patch.object(
        repo.neo4j_client, "run_cypher", new=AsyncMock(return_value=mock_records)
    ):
        repos = await repo.get_vibe_repos("alice@example.com")

    assert len(repos) == 2
    assert repos[0].url == "https://github.com/me/app"
    assert repos[0].is_mine is True
    assert repos[1].is_mine is False
    assert repos[1].owner_handle == "coworker"


@pytest.mark.asyncio
async def test_get_vibe_repos_empty_email():
    """빈 email 은 즉시 빈 리스트 (Neo4j 호출 없음)."""
    with patch.object(
        repo.neo4j_client, "run_cypher", new=AsyncMock()
    ) as mock_run:
        result = await repo.get_vibe_repos("")
    assert result == []
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_get_vibe_repos_filters_invalid_rows():
    """url 없는 row 는 skip — 방어 로직."""
    mock_records = [
        {"repo": {}},  # url 없음 → skip
        {"repo": {"url": "https://github.com/foo/bar", "is_mine": True}},
    ]
    with patch.object(
        repo.neo4j_client, "run_cypher", new=AsyncMock(return_value=mock_records)
    ):
        repos = await repo.get_vibe_repos("alice@example.com")
    assert len(repos) == 1
    assert repos[0].url == "https://github.com/foo/bar"


# ─── delete_vibe_repo ──────────────────────────────────


@pytest.mark.asyncio
async def test_delete_vibe_repo_normalizes_url():
    """Delete 시도 URL 도 정규화 후 매칭."""
    mock_records = [{"deleted_url": "https://github.com/foo/bar"}]
    with patch.object(
        repo.neo4j_client, "run_cypher", new=AsyncMock(return_value=mock_records)
    ) as mock_run:
        ok = await repo.delete_vibe_repo(
            "alice@example.com",
            "https://github.com/foo/bar.git/"  # 정규화 대상
        )
    assert ok is True
    params = mock_run.call_args[0][1]
    assert params["url"] == "https://github.com/foo/bar"


@pytest.mark.asyncio
async def test_delete_vibe_repo_returns_false_when_not_found():
    with patch.object(
        repo.neo4j_client, "run_cypher", new=AsyncMock(return_value=[])
    ):
        ok = await repo.delete_vibe_repo(
            "alice@example.com", "https://github.com/x/y"
        )
    assert ok is False


@pytest.mark.asyncio
async def test_delete_vibe_repo_empty_inputs():
    with patch.object(
        repo.neo4j_client, "run_cypher", new=AsyncMock()
    ) as mock_run:
        assert await repo.delete_vibe_repo("", "https://github.com/foo/bar") is False
        assert await repo.delete_vibe_repo("alice@example.com", "") is False
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_delete_vibe_repo_fallback_when_normalize_fails():
    """잘못된 URL 이 DB 에 저장돼 있어도 원본 URL 로 시도 — 정리 가능."""
    mock_records = [{"deleted_url": "garbage"}]
    with patch.object(
        repo.neo4j_client, "run_cypher", new=AsyncMock(return_value=mock_records)
    ) as mock_run:
        ok = await repo.delete_vibe_repo("alice@example.com", "garbage")
    assert ok is True
    params = mock_run.call_args[0][1]
    assert params["url"] == "garbage"  # fallback — 원본 그대로


# ─── Cypher injection 회귀 ──────────────────────────────


@pytest.mark.asyncio
async def test_no_cypher_injection_in_label():
    """
    악성 label/description 이 들어와도 Cypher 에 직접 보간 안 되고 $param 바인딩.
    Neo4j driver 가 자동 escape → 인젝션 불가.
    """
    payload = VibeRepoInput(
        url="https://github.com/foo/bar",
        label="'); DETACH DELETE u; //",  # 악성 페이로드
        description="`; MATCH (n) DETACH DELETE n; //",
    )
    mock_records = [{
        "repo": {
            "url": "https://github.com/foo/bar",
            "owner_handle": "foo",
            "label": payload.label,
            "description": payload.description,
            "is_mine": True,
            "added_at": 1, "updated_at": 1,
        }
    }]
    with patch.object(
        repo.neo4j_client, "run_cypher", new=AsyncMock(return_value=mock_records)
    ) as mock_run:
        await repo.add_vibe_repo("alice@example.com", payload)

    cypher = mock_run.call_args[0][0]
    params = mock_run.call_args[0][1]
    # Cypher 본문에는 악성 문자열이 포함 안 됨
    assert "DETACH DELETE u" not in cypher
    assert "DETACH DELETE n" not in cypher
    # 악성 문자열은 $label / $description 파라미터로 전달됨 (driver 자동 escape)
    assert params["label"] == payload.label
    assert params["description"] == payload.description
