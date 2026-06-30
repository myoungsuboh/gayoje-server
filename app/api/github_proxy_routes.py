"""
GitHub Proxy 라우트 — FE 가 brower 에서 직접 api.github.com 을 호출하는 대신,
BE 가 사용자의 OAuth access_token 을 주입해 GitHub API 를 대리 호출한다.

[설계 근거]
- 사용자의 GitHub access_token 은 User 노드에 Fernet 암호화로 저장되어 있다.
- 토큰을 브라우저로 내려보내면 XSS 노출 위험이 있으므로 BE 프록시 형태로만 노출.
- 사용자 토큰을 쓰면 private repo 접근 가능, rate limit 도 5000/hr 로 확대.

[엔드포인트]
- GET /api/github/repo?url=...                       → 저장소 메타 (default_branch 등)
- GET /api/github/tree?url=...&ref=...               → 재귀 파일 트리
- GET /api/github/file?url=...&ref=...&path=...      → 파일 1개 텍스트 (UTF-8)

모두 인증 필요 (`get_current_user`). 미로그인 시 401.
사용자가 GitHub 미연결 상태이면 anonymous 호출이 되어 public repo 만 보임.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.clients.github_client import (
    GitHubClient,
    GitHubError,
    parse_github_url,
)
from app.core.security import get_current_user
from app.service import user_repository
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/github", tags=["GitHub Proxy"])


async def _client_for(user: UserPublic) -> GitHubClient:
    """사용자 OAuth access_token 을 주입한 GitHubClient 생성."""
    token = await user_repository.get_github_access_token(user.email)
    return GitHubClient(user_token=token)


# [2026-05 보안 점검 #4] 사용자에게 노출할 안전한 detail 메시지.
# 기존: 모든 status 에 str(e) 를 detail 로 노출 → 5xx 에는 resp.text[:200] 포함 →
# GitHub 응답 body 의 일부 (필드명 / 내부 ID / 가끔 이메일) 가 클라이언트로 누설.
# 변경: status 별 사전 정의된 안전 메시지. 실제 에러 본문은 server 로그에만.
_SAFE_DETAILS = {
    404: "GitHub 저장소를 찾을 수 없습니다.",
    403: "GitHub API 접근이 거부되었습니다 (rate limit 또는 권한 부족).",
    401: "GitHub 인증이 실패했습니다. 재연결 후 시도하세요.",
    415: "GitHub 응답이 지원되지 않는 형식입니다.",
}


def _map_error(e: GitHubError) -> HTTPException:
    """
    GitHubError → HTTPException. 사용자 메시지는 sanitized, 원본은 서버 로그.

    [정책]
    - 사전 정의된 status (401/403/404/415): _SAFE_DETAILS 매핑 사용
    - 그 외 (5xx 또는 알 수 없는 status): 502 + 일반 메시지
    - 모든 경우 원본 GitHubError 는 WARN 레벨로 server 로그에 기록
    """
    logger.warning("GitHub proxy error (status=%s): %s", e.status, e)
    status = e.status or 502
    detail = _SAFE_DETAILS.get(status, "GitHub 호출에 실패했습니다.")
    if status not in _SAFE_DETAILS:
        status = 502
    return HTTPException(status_code=status, detail=detail)


@router.get("/repos")
async def list_user_repos(
    user: UserPublic = Depends(get_current_user),
) -> Dict[str, Any]:
    """현재 OAuth 사용자의 레포 목록. GitHub 미연결이면 빈 목록 반환."""
    client = await _client_for(user)
    try:
        repos = await client.list_repos()
    except GitHubError as e:
        if e.status == 401:
            return {"repos": []}
        raise _map_error(e) from e
    return {
        "repos": [
            {
                "full_name": r.get("full_name"),
                "html_url": r.get("html_url"),
                "private": bool(r.get("private")),
                "updated_at": r.get("updated_at"),
                "language": r.get("language"),
                "default_branch": r.get("default_branch") or "main",
                "description": r.get("description") or "",
            }
            for r in repos
        ]
    }


@router.get("/repo")
async def get_repo_meta(
    url: str = Query(..., description="GitHub repo URL"),
    user: UserPublic = Depends(get_current_user),
) -> Dict[str, Any]:
    """저장소 메타 (default_branch 등) 반환."""
    try:
        ident = parse_github_url(url)
    except GitHubError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    client = await _client_for(user)
    try:
        meta = await client.get_repo(ident)
    except GitHubError as e:
        raise _map_error(e) from e

    return {
        "owner": ident.owner,
        "repo": ident.repo,
        "default_branch": meta.get("default_branch") or "main",
        "private": bool(meta.get("private")),
        "html_url": meta.get("html_url"),
    }


@router.get("/tree")
async def get_repo_tree(
    url: str = Query(..., description="GitHub repo URL"),
    ref: Optional[str] = Query(None, description="branch/tag/sha (생략 시 default_branch)"),
    user: UserPublic = Depends(get_current_user),
) -> Dict[str, Any]:
    """재귀 파일 트리. ref 미지정이면 default_branch 사용."""
    try:
        ident = parse_github_url(url)
    except GitHubError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    client = await _client_for(user)
    try:
        branch = ref
        if not branch:
            meta = await client.get_repo(ident)
            branch = meta.get("default_branch") or "main"
        tree_data = await client.get_tree(ident, branch, recursive=True)
    except GitHubError as e:
        raise _map_error(e) from e

    return {
        "owner": ident.owner,
        "repo": ident.repo,
        "branch": branch,
        "truncated": bool(tree_data.get("truncated")),
        "tree": tree_data.get("tree") or [],
    }


@router.get("/file")
async def get_repo_file(
    url: str = Query(..., description="GitHub repo URL"),
    ref: str = Query(..., description="branch/tag/sha"),
    path: str = Query(..., description="repo 내 파일 경로"),
    user: UserPublic = Depends(get_current_user),
) -> Dict[str, Any]:
    """파일 1개 텍스트 (UTF-8). 디렉토리/바이너리는 400/415 로 거부."""
    try:
        ident = parse_github_url(url)
    except GitHubError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    client = await _client_for(user)
    try:
        data = await client.get_file_content(ident, path, ref)
    except GitHubError as e:
        raise _map_error(e) from e
    return data
