"""
Trace (upstream lineage) route — `/api/v2/trace`.

[목적]
"이 API/Story/Epic 은 어떤 회의에서 출발했어?" 질문에 답하는 backward traversal.
graph_repository.trace_upstream() 의 thin wrapper + ownership 검증 + HTTP 매핑.

[Path]
- GET /api/v2/trace?project_name=X&kind=api&id=API-03

[권한]
- JWT 인증 필수 (`get_current_user`)
- 본인 소유 프로젝트만 (`assert_owns` — 403 차단)
- 시작 노드 미존재 시 404
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.security import get_current_user
from app.service import graph_repository, ownership_repository
from app.service.graph_repository import (
    SUPPORTED_TRACE_KINDS,
    UpstreamTrace,
)
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["Trace (Upstream Lineage)"])


@router.get(
    "/trace",
    response_model=UpstreamTrace,
    summary=(
        "Trace — 시작 노드(API/Story/Epic/Problem/Solution)에서 위로 거슬러 "
        "회의까지 도달하는 backward 추적"
    ),
)
async def trace_upstream_route(
    project_name: str = Query(..., min_length=1, description="대상 프로젝트명"),
    team_id: Optional[str] = None,
    kind: str = Query(
        ...,
        description=(
            "시작 노드 종류. 허용: api | story | epic | problem | resolution"
        ),
    ),
    id: str = Query(..., min_length=1, description="시작 노드의 id property"),
    current_user: UserPublic = Depends(get_current_user),
) -> UpstreamTrace:
    """
    역추적 결과를 카테고리별 노드 리스트로 반환.

    [응답 분기]
    - 시작 노드 못 찾으면 404 (정보 누설 방지 — ownership 실패와 별도로 분기)
    - `kind` 가 지원 외부면 422
    - 본인 소유 프로젝트 아니면 403 (assert_owns 가 raise)
    """
    k = (kind or "").lower().strip()
    if k not in SUPPORTED_TRACE_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"지원하지 않는 trace 시작 노드 종류: {kind!r}. "
                f"허용: {sorted(SUPPORTED_TRACE_KINDS)}"
            ),
        )

    # ownership 검증 — 다른 사용자의 노드를 trace 하려는 시도 차단.
    # assert_owns 가 raise (HTTPException 403) 하면 그대로 전파.
    await ownership_repository.assert_access(current_user.email, project_name, team_id)

    try:
        result = await graph_repository.trace_upstream(
            kind=k, start_id=id, project=project_name, team_id=team_id or ""
        )
    except ValueError as e:
        # graph_repository 내부 input validation — 라우트 단에서 막아도 안전망.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        ) from e

    if result.not_found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"시작 노드를 찾을 수 없습니다 "
                f"(kind={k!r}, id={id!r}, project={project_name!r})."
            ),
        )

    return result
