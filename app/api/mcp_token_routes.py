"""
MCP 전용 토큰 발급/조회/회수 API.

[보안]
- 모든 엔드포인트 Depends(get_current_user) — 로그인 access_token 필요.
- 평문 토큰은 발급 (POST) 응답에서만 1회 노출. 목록/조회에서는 jti+메타만.
- 회수는 (1) peek (Neo4j read — 소유/exp 조회, state 변경 없음)
        → (2) Redis token_blacklist (즉시 발효)
        → (3) Neo4j mark revoked (감사 트레일)
  순으로 race window 최소화.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.core import security, token_blacklist
from app.core.security import get_current_user
from app.schemas import (
    McpTokenIssueRequest,
    McpTokenIssueResponse,
    McpTokenSummary,
)
from app.service import mcp_token_repository
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp-tokens", tags=["mcp-tokens"])


@router.post("", response_model=McpTokenIssueResponse, status_code=201)
async def issue_mcp_token_route(
    payload: McpTokenIssueRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> McpTokenIssueResponse:
    """MCP 전용 토큰 1개 발급. 평문은 이 응답에서만 반환된다."""
    token, jti = security.create_mcp_token(current_user.email, exp_days=90)
    try:
        row = await mcp_token_repository.create_mcp_token_record(
            email=current_user.email,
            jti=jti,
            label=payload.label,
            exp_days=90,
        )
    except mcp_token_repository.McpTokenLimitExceeded as e:
        raise HTTPException(status_code=400, detail=str(e))
    return McpTokenIssueResponse(
        token=token, jti=row.jti, label=row.label, expires_at=row.expires_at,
    )


@router.get("", response_model=list[McpTokenSummary])
async def list_mcp_tokens_route(
    current_user: UserPublic = Depends(get_current_user),
) -> list[McpTokenSummary]:
    rows = await mcp_token_repository.list_tokens_for_user(current_user.email)
    return [McpTokenSummary(**r.model_dump()) for r in rows]


@router.delete("/{jti}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_mcp_token_route(
    jti: str,
    current_user: UserPublic = Depends(get_current_user),
) -> None:
    # (1) Neo4j read — 소유권/exp 조회. state 변경 없음.
    exp_epoch = await mcp_token_repository.peek_revoke_target(
        current_user.email, jti
    )
    if exp_epoch is None:
        raise HTTPException(
            status_code=404,
            detail="존재하지 않거나 이미 회수된 토큰입니다.",
        )

    # (2) Redis — 즉시 발효. 미들웨어가 이 시점부터 401 발급.
    await token_blacklist.revoke(jti, exp_epoch)

    # (3) Neo4j write — 감사 트레일. 실패해도 인증 차단은 이미 성공한 상태.
    marked = await mcp_token_repository.mark_token_revoked(
        current_user.email, jti
    )
    if not marked:
        logger.warning(
            "mark_token_revoked returned False for jti=%s — Redis 차단은 유효", jti
        )
