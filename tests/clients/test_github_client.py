"""GitHub URL parser + tree filter 단위 테스트."""
from __future__ import annotations

import pytest

from app.clients.github_client import (
    GitHubError,
    filter_code_files,
    parse_github_url,
)


def test_parse_simple_url():
    out = parse_github_url("https://github.com/owner/repo")
    assert out.owner == "owner"
    assert out.repo == "repo"
    assert out.full_name == "owner/repo"


def test_parse_trailing_git_and_slash():
    out = parse_github_url("https://github.com/owner/repo.git/")
    assert out.repo == "repo"


def test_parse_ssh_url():
    out = parse_github_url("git@github.com:owner/repo.git")
    assert out.owner == "owner"
    assert out.repo == "repo"


def test_parse_url_with_path():
    """github URL 에 추가 경로(/blob/...)가 있어도 owner/repo 만 추출."""
    out = parse_github_url("https://github.com/owner/repo/blob/main/README.md")
    assert out.owner == "owner"
    # repo 는 첫 슬래시 전까지만
    assert out.repo == "repo"


def test_parse_invalid_url_raises():
    with pytest.raises(GitHubError, match="GitHub URL 파싱 실패"):
        parse_github_url("not a github url at all")
    with pytest.raises(GitHubError):
        parse_github_url("")


def test_filter_code_files_keeps_only_supported_extensions():
    tree = {
        "tree": [
            {"path": "src/App.vue", "type": "blob", "size": 100},
            {"path": "src/index.ts", "type": "blob", "size": 200},
            {"path": "README.md", "type": "blob", "size": 50},
            {"path": "dist/bundle.css", "type": "blob", "size": 300},
            {"path": "src", "type": "tree"},  # 디렉토리 무시
            {"path": "main.py", "type": "blob"},  # size 없어도 0 으로
        ]
    }
    out = filter_code_files(tree)
    paths = [f["path"] for f in out]
    assert "src/App.vue" in paths
    assert "src/index.ts" in paths
    assert "main.py" in paths
    assert "README.md" not in paths
    assert "dist/bundle.css" not in paths


def test_filter_code_files_empty_tree():
    assert filter_code_files({"tree": []}) == []
    assert filter_code_files({}) == []


def test_filter_code_files_custom_extensions():
    tree = {"tree": [{"path": "doc.md", "type": "blob"}]}
    out = filter_code_files(tree, extensions={"md"})
    assert len(out) == 1
