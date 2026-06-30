"""
GitHub REST API 얇은 async 래퍼.

Get Repo Info / GitHub Tree 단계에서 사용.

[설계 원칙]
- public repo 만 우선 지원 (token 없으면 rate limit 60/hr per IP)
- token 있으면 GITHUB_TOKEN env 로 옮기면 5000/hr
- 404 / 403 / 5xx 는 명시적 예외 (LintError) — 파이프라인이 친절한 에러 반환
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
_GITHUB_URL_RE = re.compile(
    r"github\.com[/:]([^/]+)/([^/?#]+)", re.IGNORECASE
)


class GitHubError(RuntimeError):
    """GitHub API 비복구 실패."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RepoIdentifier:
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_github_url(raw_url: str) -> RepoIdentifier:
    """
    `https://github.com/owner/repo[.git][/...]` → RepoIdentifier.

    Trailing slash 및 `.git` 제거. 파싱 실패 시 GitHubError.
    """
    cleaned = (raw_url or "").strip().rstrip("/")
    cleaned = re.sub(r"\.git$", "", cleaned, flags=re.IGNORECASE)
    m = _GITHUB_URL_RE.search(cleaned)
    if not m:
        raise GitHubError(f"GitHub URL 파싱 실패: '{raw_url}'")
    return RepoIdentifier(owner=m.group(1), repo=m.group(2))


def _headers(user_token: Optional[str] = None) -> Dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "harness-backend/1.0",
    }
    # 우선순위: 호출자가 넘긴 user_token (OAuth) > 환경변수 GITHUB_TOKEN.
    # 사용자 토큰을 쓰면 사용자가 권한 가진 private repo 까지 읽을 수 있고,
    # rate limit 도 사용자 단위(5000/hr) 가 적용된다.
    token = user_token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class GitHubClient:
    """
    Thin async wrapper. Stateless — 매 호출마다 새 httpx client 사용.

    'continueOnFail' 동작을 코드 레벨에서 명시적 try/except 로 재현.

    user_token 이 주어지면 (OAuth access_token) 매 호출에 사용자 권한으로 인증.
    None 이면 환경변수 GITHUB_TOKEN 폴백, 그것도 없으면 anonymous (rate limit 60/hr).
    """

    def __init__(self, timeout: float = 30.0, user_token: Optional[str] = None) -> None:
        self._timeout = timeout
        self._user_token = user_token

    async def get_repo(self, ident: RepoIdentifier) -> Dict[str, Any]:
        """GET /repos/{owner}/{repo} — default_branch 등 메타 조회."""
        url = f"{_API_BASE}/repos/{ident.owner}/{ident.repo}"
        return await self._get(url, context=f"repo info ({ident.full_name})")

    async def get_tree(
        self, ident: RepoIdentifier, ref: str, recursive: bool = True
    ) -> Dict[str, Any]:
        """GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1 — 전체 파일 트리.

        ref 는 `feature/foo` 처럼 slash 를 포함할 수 있으므로 slash 는 보존하고
        나머지 특수문자만 quote (#, ?, 공백 등이 들어오면 URL 이 깨짐).
        """
        safe_ref = "/".join(quote(seg, safe="") for seg in ref.split("/"))
        url = (
            f"{_API_BASE}/repos/{ident.owner}/{ident.repo}/git/trees/{safe_ref}"
            f"{'?recursive=1' if recursive else ''}"
        )
        return await self._get(url, context=f"tree {ident.full_name}@{ref}")

    async def get_file_content(
        self,
        ident: RepoIdentifier,
        path: str,
        ref: str,
        *,
        max_bytes: int = 1_000_000,
    ) -> Dict[str, Any]:
        """
        GET /repos/{owner}/{repo}/contents/{path}?ref={ref} → 파일 1개 텍스트.

        Code 화면처럼 "사용자가 트리에서 클릭한 파일" 을 받기 위한 경로.
        max_bytes 초과면 잘라서 반환하고 `truncated=True` 마킹.

        Returns: {"content": str, "encoding": str, "size": int, "truncated": bool, "sha": str}
        Raises: GitHubError — 디렉토리/심볼릭/서브모듈/바이너리는 호출자가 판단.
        """
        # path 는 segment 단위 인코딩 필요. slash 는 보존.
        safe_path = "/".join(quote(seg, safe="") for seg in path.split("/"))
        url = (
            f"{_API_BASE}/repos/{ident.owner}/{ident.repo}/contents/{safe_path}"
            f"?ref={quote(ref, safe='')}"
        )
        data = await self._get(
            url, context=f"contents {ident.full_name}@{ref}:{path}"
        )
        # 디렉토리면 list 반환 → 호출자가 거부
        if isinstance(data, list):
            raise GitHubError(
                f"GitHub 경로가 디렉토리입니다: {path}", status=400
            )
        if data.get("type") != "file":
            raise GitHubError(
                f"GitHub 파일이 아닙니다 (type={data.get('type')}): {path}",
                status=400,
            )
        encoded = data.get("content") or ""
        encoding = (data.get("encoding") or "base64").lower()
        size = int(data.get("size") or 0)
        sha = str(data.get("sha") or "")

        if encoding != "base64":
            # GitHub 가 평문으로 줬다면 그대로 사용.
            text = encoded
            truncated = False
        else:
            try:
                raw = base64.b64decode(encoded)
            except (ValueError, TypeError) as e:
                raise GitHubError(
                    f"GitHub 파일 base64 디코드 실패: {path}: {e}", status=500
                ) from e
            truncated = False
            if max_bytes and len(raw) > max_bytes:
                raw = raw[:max_bytes]
                truncated = True
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                # 바이너리는 errors=replace 로 깨진 문자열 반환하지 않고 명시적 거부.
                raise GitHubError(
                    f"GitHub 파일이 UTF-8 텍스트가 아닙니다 (binary): {path}",
                    status=415,
                ) from None
        return {
            "content": text,
            "encoding": "utf-8",
            "size": size,
            "truncated": truncated,
            "sha": sha,
            "path": path,
        }

    async def list_repos(
        self,
        *,
        sort: str = "updated",
        per_page: int = 100,
        affiliation: str = "owner,collaborator,organization_member",
    ) -> List[Dict[str, Any]]:
        """GET /user/repos — 현재 OAuth 사용자의 레포 목록 (private 포함)."""
        url = f"{_API_BASE}/user/repos?sort={sort}&per_page={per_page}&affiliation={affiliation}"
        data = await self._get(url, context="list user repos")
        return data if isinstance(data, list) else []

    async def get_blob_text(
        self, ident: RepoIdentifier, file_sha: str, *, max_bytes: int = 32_000
    ) -> str:
        """
        GET /repos/{owner}/{repo}/git/blobs/{sha} → base64 디코드 후 텍스트 반환.

        Lint sampler 가 head-sample 만 쓸 거라 max_bytes 로 자르고 utf-8 decode.
        바이너리 / decode 실패는 빈 문자열 반환 (호출자가 무시).
        """
        url = f"{_API_BASE}/repos/{ident.owner}/{ident.repo}/git/blobs/{file_sha}"
        data = await self._get(url, context=f"blob {ident.full_name}@{file_sha[:7]}")
        encoded = data.get("content") or ""
        encoding = (data.get("encoding") or "base64").lower()
        if encoding != "base64":
            return ""
        try:
            raw = base64.b64decode(encoded)
        except (ValueError, TypeError):
            return ""
        if max_bytes and len(raw) > max_bytes:
            raw = raw[:max_bytes]
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""

    async def _get(
        self, url: str, *, context: str
    ) -> Union[Dict[str, Any], List[Any]]:
        """JSON 응답을 그대로 반환. GitHub /contents 는 디렉토리일 때 list 를 줌."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(url, headers=_headers(self._user_token))
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                raise GitHubError(f"GitHub 네트워크 오류 ({context}): {e}") from e

        if resp.status_code == 404:
            raise GitHubError(
                f"GitHub 저장소를 찾을 수 없습니다 (404, public 인지 확인): {context}",
                status=404,
            )
        if resp.status_code == 403:
            raise GitHubError(
                f"GitHub API 제한 (403, rate limit 또는 권한 부족): {context}",
                status=403,
            )
        if resp.status_code == 401:
            raise GitHubError(
                f"GitHub 인증 실패 (401): {context}", status=401
            )
        if resp.status_code >= 400:
            raise GitHubError(
                f"GitHub {resp.status_code} ({context}): {resp.text[:200]}",
                status=resp.status_code,
            )
        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001
            raise GitHubError(f"GitHub 응답 JSON 파싱 실패 ({context}): {e}") from e


# ─── Tree 필터 헬퍼 ────────────────────────────────────────


# Build Lint Context 의 codeExts.
CODE_EXTENSIONS = {
    "vue", "ts", "tsx", "js", "jsx",
    "java", "kt", "py", "go", "rs", "rb", "php",
}


def filter_code_files(
    tree_response: Dict[str, Any],
    *,
    extensions: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    GitHub tree response 에서 코드 파일(blob)만 추출.

    Returns: [{"path": str, "size": int, "sha": str}, ...]
      sha 는 후속 blob fetch (Lint sampler) 가 사용. 기존 호출자는 path/size 만
      읽으므로 추가 키는 무해.
    """
    exts = extensions or CODE_EXTENSIONS
    tree = tree_response.get("tree") or []
    out: List[Dict[str, Any]] = []
    for t in tree:
        if t.get("type") != "blob":
            continue
        path = t.get("path") or ""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext not in exts:
            continue
        out.append(
            {
                "path": path,
                "size": int(t.get("size") or 0),
                "sha": t.get("sha") or "",
            }
        )
    return out


# Lineage 분석은 더 넓은 확장자 셋 사용 (sql/yml/json 포함).
# 'Fetch All Repo Trees' 와 동등.
LINEAGE_CODE_EXTENSIONS = CODE_EXTENSIONS | {"sql", "yml", "yaml", "json"}

# repo 1개 트리 fetch 최대 대기 (get_repo + get_tree 합산). 거대/느린 repo 가
# 전체 lineage 분석을 막지 않도록 캡. env 로 운영 조정 가능.
_PER_REPO_TIMEOUT_SEC = float(os.getenv("LINEAGE_PER_REPO_TIMEOUT_SEC", "75"))


async def fetch_repo_trees_bulk(
    client: "GitHubClient",
    repos: List[Dict[str, Any]],
    *,
    extensions: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    여러 repo 의 file tree 를 fetch. 각 repo 는 독립적 (한 개 실패해도 계속).

    Args:
      repos: [{"url": str, "role": str, "label": str}, ...]
    Returns:
      [{"url": ..., "role": ..., "label": ..., "branch": ..., "files": [str]} ...]
      또는 실패 시 {"url": ..., "role": ..., "label": ..., "error": str, "status": int|None}
    """
    exts = extensions or LINEAGE_CODE_EXTENSIONS

    # [2026-05 perf/timeout fix] 이전엔 repo 들을 순차(for)로 fetch — repo 당 2회
    # GitHub 호출(get_repo + get_tree, 각 30s 타임아웃)이라 N 개면 최악 N×60s.
    # 느리거나 거대한 repo 하나가 전체 lineage 분석을 막아 FE 폴링 한계(10분)를
    # 넘겨 "타임아웃"으로 보였다. 이제:
    #   1) asyncio.gather 로 repo 별 병렬 fetch (독립적이므로)
    #   2) repo 당 _PER_REPO_TIMEOUT_SEC 캡 — 한 repo 가 늘어져도 그것만 error 처리
    #      하고 나머지는 정상 진행 (부분 실패 허용은 기존 정책과 동일)
    async def _fetch_one(r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = r.get("url")
        role = r.get("role")
        label = r.get("label")
        if not url:
            return None
        try:
            ident = parse_github_url(url)
        except GitHubError as e:
            return {"url": url, "role": role, "label": label, "error": str(e), "status": None}
        try:
            async def _do() -> Dict[str, Any]:
                meta = await client.get_repo(ident)
                branch = meta.get("default_branch") or "main"
                tree_data = await client.get_tree(ident, branch, recursive=True)
                tree = tree_data.get("tree") or []
                files = [
                    t["path"]
                    for t in tree
                    if t.get("type") == "blob"
                    and t.get("path")
                    and t["path"].rsplit(".", 1)[-1].lower() in exts
                ]
                return {"url": url, "role": role, "label": label, "branch": branch, "files": files}

            return await asyncio.wait_for(_do(), timeout=_PER_REPO_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            return {
                "url": url, "role": role, "label": label,
                "error": f"repo tree fetch 가 {_PER_REPO_TIMEOUT_SEC:.0f}초를 넘겨 건너뜀",
                "status": None,
            }
        except GitHubError as e:
            return {"url": url, "role": role, "label": label, "error": str(e), "status": e.status}

    fetched = await asyncio.gather(*[_fetch_one(r) for r in repos])
    return [f for f in fetched if f is not None]
