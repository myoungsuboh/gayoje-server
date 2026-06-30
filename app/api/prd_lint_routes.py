"""
[B 단계 — 2026-05-25] PRD lint endpoint.

POST /api/v2/prd-lint
  body: { "text": "PRD raw text" }
  response: { score, issues[{code,severity,message,hint,detail}], summary }

LLM 호출 없음. 정규식 + 키워드 기반 — ~10ms 응답.
인증만 필요 (project ownership 불필요 — 변환 전 단계라 project 미지정).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import get_current_user
from app.pipelines.prd_lint import lint_prd
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["PrdLint"])


class PrdLintRequest(BaseModel):
    text: str = Field(..., description="PRD raw 텍스트 (Markdown 형식)")


class PrdLintIssueResponse(BaseModel):
    code: str
    severity: str
    message: str
    hint: str = ""
    detail: Dict[str, Any] = Field(default_factory=dict)


class PrdLintResponse(BaseModel):
    score: float = Field(..., ge=0.0, le=1.0)
    issues: List[PrdLintIssueResponse] = Field(default_factory=list)
    summary: Dict[str, int] = Field(default_factory=dict)


# 너무 큰 입력은 거절 — abuse 방지.
_MAX_PRD_BYTES = 1_000_000  # 1MB


@router.post(
    "/prd-lint",
    response_model=PrdLintResponse,
    summary="PRD raw 텍스트 충실도 lint (LLM 미사용, ~10ms)",
)
async def prd_lint_route(
    payload: PrdLintRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> PrdLintResponse:
    if not isinstance(payload.text, str):
        raise HTTPException(status_code=400, detail="text 필드는 string")
    if len(payload.text.encode("utf-8")) > _MAX_PRD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"PRD 텍스트가 너무 큼 (>{_MAX_PRD_BYTES} bytes)",
        )

    report = lint_prd(payload.text)
    return PrdLintResponse(
        score=report.score,
        issues=[
            PrdLintIssueResponse(
                code=i.code,
                severity=i.severity,
                message=i.message,
                hint=i.hint,
                detail=dict(i.detail),
            )
            for i in report.issues
        ],
        summary=dict(report.summary),
    )
