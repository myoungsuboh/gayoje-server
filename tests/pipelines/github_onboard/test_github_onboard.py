"""
github_onboard_pipeline 회귀 가드.

[검증 범위]
1. select_onboard_files — D2 샘플링 우선순위 / 차단 / 결정성
2. V1 markdown 가드 — 너무 짧으면 raise, 너무 크면 truncate
3. e2e — GitHub fake + Gemini fake + monkeypatch CPS → V1 + cps_result 반환
4. private repo / 404 — GitHubError 그대로 전파
5. 분석 가능 파일 0 → ValueError (친화적 안내)
"""
from __future__ import annotations

import pytest
from dataclasses import replace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from app.clients.github_client import (
    GitHubError,
    RepoIdentifier,
)
from app.pipelines.base import PipelineContext
from app.pipelines.cps_pipeline.types import CpsResult
from app.pipelines.prd_pipeline import PrdResult
from app.pipelines.github_onboard_pipeline import (
    GithubOnboardInput,
    _MAX_V1_LENGTH,
    _MIN_V1_LENGTH,
    _classify,
    _is_blocked,
    run_github_onboard_pipeline,
    select_onboard_files,
)
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio


# ─── select_onboard_files (pure function) ────────────────────────────


def _blob(path: str, *, size: int = 100, sha: str = "deadbeef") -> Dict[str, Any]:
    return {"path": path, "type": "blob", "sha": sha, "size": size}


def test_select_prioritizes_readme_first():
    """README 가 1순위 — 다른 파일이 많아도 무조건 첫 자리."""
    blobs = [
        _blob("src/main.py", size=500),
        _blob("package.json", size=200),
        _blob("README.md", size=1000),
        _blob("src/lib.py", size=300),
    ]
    selected = select_onboard_files(blobs, max_files=10)
    assert selected[0]["path"] == "README.md"
    # priority 순서: README → manifest → entry → code
    paths = [s["path"] for s in selected]
    assert paths.index("README.md") < paths.index("package.json")
    assert paths.index("package.json") < paths.index("src/main.py")


def test_select_skips_binary_extensions():
    """이미지/binary 파일은 차단."""
    blobs = [
        _blob("logo.png"),
        _blob("hero.jpg"),
        _blob("docs/diagram.svg"),
        _blob("README.md"),
        _blob("main.py"),
    ]
    selected = select_onboard_files(blobs)
    paths = {s["path"] for s in selected}
    assert "logo.png" not in paths
    assert "hero.jpg" not in paths
    assert "docs/diagram.svg" not in paths
    assert "README.md" in paths
    assert "main.py" in paths


def test_select_skips_node_modules_and_build_artifacts():
    """차단 prefix (node_modules, dist 등) skip."""
    blobs = [
        _blob("node_modules/react/index.js"),
        _blob("dist/bundle.js"),
        _blob("build/main.css"),
        _blob(".git/HEAD"),
        _blob("__pycache__/main.cpython-313.pyc"),
        _blob("README.md"),
        _blob("src/main.py"),
    ]
    selected = select_onboard_files(blobs)
    paths = {s["path"] for s in selected}
    assert all("node_modules" not in p for p in paths)
    assert all("dist/" not in p for p in paths)
    assert all("build/" not in p for p in paths)
    assert all(".git/" not in p for p in paths)
    assert all("__pycache__" not in p for p in paths)
    assert "README.md" in paths
    assert "src/main.py" in paths


def test_select_max_files_respected():
    """max_files 한도 준수."""
    blobs = [_blob(f"src/file{i}.py", size=100) for i in range(100)]
    blobs.append(_blob("README.md"))
    selected = select_onboard_files(blobs, max_files=10)
    assert len(selected) == 10
    # README 는 항상 포함
    assert any(s["path"] == "README.md" for s in selected)


def test_select_deterministic_order():
    """동일 입력 → 동일 출력 (decreasing/random 입력에도 정렬 일관성)."""
    blobs1 = [
        _blob("src/a.py", size=100),
        _blob("src/b.py", size=100),
        _blob("README.md", size=500),
        _blob("package.json", size=200),
    ]
    blobs2 = list(reversed(blobs1))  # 입력 순서 뒤집어도
    selected1 = select_onboard_files(blobs1)
    selected2 = select_onboard_files(blobs2)
    assert [s["path"] for s in selected1] == [s["path"] for s in selected2]


def test_select_returns_empty_for_no_eligible_files():
    """README/manifest/entry/config/code 가 하나도 없는 repo → 빈 결과."""
    blobs = [
        _blob("logo.png"),
        _blob("docs/screenshot.jpg"),
        _blob(".gitignore"),  # 분류 안 됨 (no extension match)
    ]
    selected = select_onboard_files(blobs)
    # .gitignore 는 _CODE_EXTENSIONS 에 없음, README 도 아님, manifest 도 아님 → priority 99 → 빠짐.
    assert all(s["path"] != "logo.png" for s in selected)


def test_select_prefers_smaller_files_within_same_priority():
    """동일 priority (예: code) 안에선 size 작은 것 우선 — budget 효율."""
    blobs = [
        _blob("README.md"),
        _blob("src/huge.py", size=10000),
        _blob("src/tiny.py", size=100),
        _blob("src/medium.py", size=1000),
    ]
    selected = select_onboard_files(blobs)
    code_files = [s for s in selected if s["_category"] == "code"]
    sizes = [f["size"] for f in code_files]
    assert sizes == sorted(sizes), "size 작은 순으로 정렬 안 됨"


def test_classify_core_code_dir_has_higher_priority():
    """[Phase D] 핵심 디렉토리(controllers/services 등) 코드는 일반 코드보다 우선(priority 작음)."""
    core_pri, core_cat = _classify("src/controllers/user_controller.py")
    plain_pri, plain_cat = _classify("src/utils/helpers.py")
    assert core_pri < plain_pri
    assert core_cat == "code" and plain_cat == "code"  # category 는 유지(회귀 안전)


def test_classify_core_segment_exact_match_not_substring():
    """오탐 방지: 'microservice' 같은 부분일치는 핵심으로 안 침(세그먼트 정확 매칭)."""
    pri, _ = _classify("src/microservice/x.py")
    assert pri == 4  # 'service' substring 이지만 세그먼트가 아니므로 일반 코드


def test_select_prefers_core_code_dir_over_smaller_plain():
    """[Phase D] 핵심 디렉토리 파일이 size 가 더 커도 일반 코드보다 먼저 선택."""
    blobs = [
        _blob("src/utils/big_helper.py", size=100),
        _blob("src/services/payment.py", size=5000),
    ]
    selected = select_onboard_files(blobs)
    assert selected[0]["path"] == "src/services/payment.py"


# ─── _classify / _is_blocked (pure) ──────────────────────────────────


def test_classify_buckets():
    assert _classify("README.md")[1] == "readme"
    assert _classify("Readme.rst")[1] == "readme"  # 대소문자 무관
    assert _classify("package.json")[1] == "manifest"
    assert _classify("pyproject.toml")[1] == "manifest"
    assert _classify("src/main.py")[1] == "entry"
    assert _classify("App.vue")[1] == "entry"
    assert _classify("Dockerfile")[1] == "config"
    assert _classify("vite.config.ts")[1] == "config"
    assert _classify("src/utils/helpers.py")[1] == "code"
    assert _classify("unknown.xyz")[1] == "other"


def test_is_blocked_extensions():
    assert _is_blocked("logo.png")
    assert _is_blocked("path/to/icon.ICO")  # 대소문자 무관
    assert _is_blocked("font.woff2")
    assert _is_blocked("output.min.js")
    assert not _is_blocked("README.md")
    assert not _is_blocked("src/main.py")


def test_is_blocked_path_prefixes():
    assert _is_blocked("node_modules/react/index.js")
    assert _is_blocked(".git/HEAD")
    assert _is_blocked("dist/bundle.js")
    assert _is_blocked("vendor/lib.js")
    assert _is_blocked("__pycache__/foo.pyc")
    assert not _is_blocked("src/utils.py")


# ─── FakeGitHubClient ────────────────────────────────────────────────


class _FakeGitHubClient:
    """onboard pipeline 의 GitHubClient 인터페이스 모킹.

    onboard 에서 사용하는 메서드만: get_repo, get_tree, get_blob_text.
    """

    def __init__(
        self,
        *,
        repo: Optional[Dict[str, Any]] = None,
        tree: Optional[List[Dict[str, Any]]] = None,
        blobs: Optional[Dict[str, str]] = None,
        repo_error: Optional[GitHubError] = None,
        tree_error: Optional[GitHubError] = None,
    ):
        self._repo = repo or {"default_branch": "main", "private": False}
        self._tree = tree or []
        self._blobs = blobs or {}  # sha → text
        self._repo_error = repo_error
        self._tree_error = tree_error
        self.calls: List[Dict[str, Any]] = []

    async def get_repo(self, ident: RepoIdentifier) -> Dict[str, Any]:
        self.calls.append({"op": "get_repo", "ident": ident.full_name})
        if self._repo_error:
            raise self._repo_error
        return self._repo

    async def get_tree(
        self, ident: RepoIdentifier, ref: str, recursive: bool = True,
    ) -> Dict[str, Any]:
        self.calls.append({"op": "get_tree", "ident": ident.full_name, "ref": ref})
        if self._tree_error:
            raise self._tree_error
        return {"tree": self._tree}

    async def get_blob_text(
        self, ident: RepoIdentifier, file_sha: str, *, max_bytes: int = 32_000,
    ) -> str:
        self.calls.append({"op": "get_blob_text", "sha": file_sha[:8]})
        text = self._blobs.get(file_sha, "")
        if max_bytes and len(text) > max_bytes:
            return text[:max_bytes]
        return text


# ─── Mock CPS pipeline (delegation 검증용) ────────────────────────────


def _mock_cps_result(project_name: str) -> CpsResult:
    return CpsResult(
        meeting_log_id=f"log_{project_name}_v1_0",
        delta_cps_id=f"doc_cps_{project_name}_v1_0",
        master_cps_id=f"doc_cps_master_{project_name}",
        mode="first_run",
        diagnostic={"stub": True},
        cps_graph={"nodes": [], "edges": []},
        extraction_mode="strict",
    )


def _mock_prd_result(project_name: str) -> PrdResult:
    return PrdResult(
        delta_prd_id=f"doc_prd_{project_name}_v1_0",
        master_prd_id=f"doc_prd_master_{project_name}",
        mode="first_run",
        diagnostic={"stub": True},
    )


def _mock_cps_and_prd(project_name: str):
    """_delegate_to_cps_and_prd 의 (CpsResult, PrdResult) tuple 반환 mock."""
    return _mock_cps_result(project_name), _mock_prd_result(project_name)


# ─── e2e ──────────────────────────────────────────────────────────────


_VALID_V1 = """## 1. 프로젝트 개요
이 프로젝트는 사용자 인증 / 데이터 관리 / 알림 기능을 제공하는 웹 애플리케이션이다.

## 2. 주요 기능
- 사용자 로그인 — JWT 기반 인증
- 데이터 등록 — REST API 제공

## 3. 사용자 시나리오
사용자는 로그인 후 데이터를 등록한다. 시스템은 그 데이터를 저장하고 다른 사용자에게 알림을 발송한다.

## 4. 기술 스택
- **언어**: Python 3.13
- **프레임워크**: FastAPI

## 5. NFR 추정
- **성능**: API 응답 200ms 이내 (추정)
- **보안**: JWT 인증 사용
"""


async def test_e2e_onboard_returns_v1_and_cps(monkeypatch):
    """정상 흐름 — GitHub fake + Gemini fake + mock CPS → V1 + cps_result 반환."""
    fake_gh = _FakeGitHubClient(
        repo={"default_branch": "main", "private": False, "full_name": "owner/myrepo"},
        tree=[
            _blob("README.md", sha="readme_sha", size=500),
            _blob("package.json", sha="pkg_sha", size=200),
            _blob("src/main.py", sha="main_sha", size=300),
        ],
        blobs={
            "readme_sha": "# My Repo\nA web app for managing tasks.",
            "pkg_sha": '{"dependencies": {"vue": "^3.0"}}',
            "main_sha": "def main(): pass",
        },
    )
    gemini = FakeGemini(responses=[_VALID_V1])
    neo4j = FakeNeo4j(responses=[])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="onb-1")

    async def _stub_cps(ctx_arg, project_name, v1_markdown, team_id=""):
        # delegate target — 호출됐는지 spy.
        return _mock_cps_and_prd(project_name)

    monkeypatch.setattr(
        "app.pipelines.github_onboard_pipeline._delegate_to_cps_and_prd", _stub_cps,
    )

    result = await run_github_onboard_pipeline(
        ctx,
        GithubOnboardInput(
            project_name="my-app",
            github_url="https://github.com/owner/myrepo",
            user_email="u@x",
        ),
        github_client=fake_gh,
    )

    assert result.project_name == "my-app"
    assert result.repo_full_name == "owner/myrepo"
    assert result.v1_markdown.startswith("## 1. 프로젝트 개요")
    assert result.v1_markdown_size >= _MIN_V1_LENGTH
    assert result.sampled_file_count == 3
    assert result.cps_result is not None
    assert result.cps_result.delta_cps_id == "doc_cps_my-app_v1_0"
    # [2026-05-27] PRD 도 자동 생성 — postMeeting 흐름과 동일.
    assert result.prd_result is not None
    assert result.prd_result.master_prd_id == "doc_prd_master_my-app"
    assert result.prd_result.mode == "first_run"
    assert result.diagnostic["prd_mode"] == "first_run"
    # README / package.json / main.py 모두 샘플링
    assert "README.md" in result.sampled_file_paths
    # diagnostic
    assert result.diagnostic["default_branch"] == "main"
    assert result.diagnostic["is_private"] is False
    assert result.diagnostic["tree_blob_count"] == 3
    assert result.diagnostic["fetched_count"] == 3


async def test_v1_too_short_raises_value_error(monkeypatch):
    """LLM 이 너무 짧게 응답 → ValueError + CPS 미위임."""
    fake_gh = _FakeGitHubClient(
        tree=[_blob("README.md", sha="r", size=100)],
        blobs={"r": "# Title\nHello"},
    )
    gemini = FakeGemini(responses=["짧음"])  # 200 자 미만
    ctx = PipelineContext(gemini=gemini, neo4j=FakeNeo4j(), idempotency_key="onb-2")

    cps_called = {"n": 0}

    async def _spy_cps(ctx_arg, project_name, v1_markdown, team_id=""):
        cps_called["n"] += 1
        return _mock_cps_and_prd(project_name)

    monkeypatch.setattr(
        "app.pipelines.github_onboard_pipeline._delegate_to_cps_and_prd", _spy_cps,
    )

    with pytest.raises(ValueError, match="V1 항목을 충분히 추출하지 못"):
        await run_github_onboard_pipeline(
            ctx,
            GithubOnboardInput(
                project_name="p", github_url="https://github.com/o/r",
                user_email="u@x",
            ),
            github_client=fake_gh,
        )

    # CPS 위임 안 됨 (raise 전에) — 트랜잭션 보존
    assert cps_called["n"] == 0


async def test_v1_too_large_truncated(monkeypatch):
    """50K 자 초과 V1 → truncate + warning (raise 안 함)."""
    fake_gh = _FakeGitHubClient(
        tree=[_blob("README.md", sha="r", size=100)],
        blobs={"r": "# Title"},
    )
    huge_v1 = "## 1. 프로젝트 개요\n" + ("긴 본문 " * 50000)  # > 50K
    assert len(huge_v1) > _MAX_V1_LENGTH
    gemini = FakeGemini(responses=[huge_v1])
    ctx = PipelineContext(gemini=gemini, neo4j=FakeNeo4j(), idempotency_key="onb-3")

    captured_v1: Dict[str, Any] = {}

    async def _capture_cps(ctx_arg, project_name, v1_markdown, team_id=""):
        captured_v1["text"] = v1_markdown
        return _mock_cps_and_prd(project_name)

    monkeypatch.setattr(
        "app.pipelines.github_onboard_pipeline._delegate_to_cps_and_prd", _capture_cps,
    )

    result = await run_github_onboard_pipeline(
        ctx,
        GithubOnboardInput(
            project_name="p", github_url="https://github.com/o/r",
            user_email="u@x",
        ),
        github_client=fake_gh,
    )
    # truncate 됨 — CPS 에 들어간 본문 길이가 한도
    assert len(captured_v1["text"]) == _MAX_V1_LENGTH
    # result.v1_markdown_size 도 truncate 후 길이
    assert result.v1_markdown_size == _MAX_V1_LENGTH


async def test_repo_not_found_propagates_github_error():
    """get_repo 404 → GitHubError 그대로 전파 (API route 가 422 변환)."""
    fake_gh = _FakeGitHubClient(
        repo_error=GitHubError("GitHub 저장소를 찾을 수 없습니다", status=404),
    )
    gemini = FakeGemini(responses=[])
    ctx = PipelineContext(gemini=gemini, neo4j=FakeNeo4j(), idempotency_key="onb-4")

    with pytest.raises(GitHubError) as exc:
        await run_github_onboard_pipeline(
            ctx,
            GithubOnboardInput(
                project_name="p", github_url="https://github.com/o/ghost",
                user_email="u@x",
            ),
            github_client=fake_gh,
        )
    assert exc.value.status == 404
    # LLM 안 불림 — 트랜잭션 무영향
    assert gemini.calls == []


async def test_empty_tree_raises_value_error():
    """tree 가 비어있거나 분석 가능 파일 0 → ValueError (LLM 호출 안 됨)."""
    fake_gh = _FakeGitHubClient(tree=[])  # 빈 repo
    gemini = FakeGemini(responses=[])
    ctx = PipelineContext(gemini=gemini, neo4j=FakeNeo4j(), idempotency_key="onb-5")

    with pytest.raises(ValueError, match="분석 가능한 텍스트 파일"):
        await run_github_onboard_pipeline(
            ctx,
            GithubOnboardInput(
                project_name="p", github_url="https://github.com/o/r",
                user_email="u@x",
            ),
            github_client=fake_gh,
        )
    # LLM 안 불림
    assert gemini.calls == []


async def test_url_invalid_raises_github_error():
    """parse_github_url 실패 → GitHubError."""
    fake_gh = _FakeGitHubClient()
    gemini = FakeGemini(responses=[])
    ctx = PipelineContext(gemini=gemini, neo4j=FakeNeo4j(), idempotency_key="onb-6")

    with pytest.raises(GitHubError, match="URL 파싱"):
        await run_github_onboard_pipeline(
            ctx,
            GithubOnboardInput(
                project_name="p", github_url="not-a-github-url",
                user_email="u@x",
            ),
            github_client=fake_gh,
        )


async def test_private_repo_meta_recorded(monkeypatch):
    """private repo 도 정상 처리 — diagnostic 에 is_private=True 기록."""
    fake_gh = _FakeGitHubClient(
        repo={"default_branch": "develop", "private": True},
        tree=[_blob("README.md", sha="r", size=100)],
        blobs={"r": "# Private repo content"},
    )
    gemini = FakeGemini(responses=[_VALID_V1])
    ctx = PipelineContext(gemini=gemini, neo4j=FakeNeo4j(), idempotency_key="onb-7")

    async def _stub_cps(ctx_arg, project_name, v1, team_id=""):
        return _mock_cps_and_prd(project_name)

    monkeypatch.setattr(
        "app.pipelines.github_onboard_pipeline._delegate_to_cps_and_prd", _stub_cps,
    )

    result = await run_github_onboard_pipeline(
        ctx,
        GithubOnboardInput(
            project_name="p", github_url="https://github.com/o/r",
            user_email="u@x",
        ),
        github_client=fake_gh,
    )
    assert result.diagnostic["is_private"] is True
    assert result.diagnostic["default_branch"] == "develop"


async def test_prompt_includes_repo_metadata(monkeypatch):
    """LLM prompt 에 repo full name + project name + file count 포함."""
    fake_gh = _FakeGitHubClient(
        tree=[_blob("README.md", sha="r", size=100)],
        blobs={"r": "# README content"},
    )
    gemini = FakeGemini(responses=[_VALID_V1])
    ctx = PipelineContext(gemini=gemini, neo4j=FakeNeo4j(), idempotency_key="onb-8")

    async def _stub_cps(ctx_arg, project_name, v1, team_id=""):
        return _mock_cps_and_prd(project_name)

    monkeypatch.setattr(
        "app.pipelines.github_onboard_pipeline._delegate_to_cps_and_prd", _stub_cps,
    )

    await run_github_onboard_pipeline(
        ctx,
        GithubOnboardInput(
            project_name="my-special-app", github_url="https://github.com/foo/bar",
            user_email="u@x",
        ),
        github_client=fake_gh,
    )
    prompt = gemini.calls[0]["prompt"]
    assert "foo/bar" in prompt
    assert "my-special-app" in prompt
    assert "README.md" in prompt
    assert "README content" in prompt
    # temperature 0.1 (결정성)
    assert gemini.calls[0]["temperature"] == 0.1
