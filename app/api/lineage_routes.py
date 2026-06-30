"""
Lineage 라우트 (PR8) — analyzeLineage + getLastLineage.

[엔드포인트]
- POST   /api/v2/pipelines/lineage              → analyzeLineage
- POST   /api/v2/pipelines/lineage?wait=true    → 동기 실행 (큐 미사용)
- GET    /api/v2/pipelines/lineage/status/{task_id}  → 비동기 결과 조회
- GET    /api/v2/pipelines/lineage/last?project_name=X  → getLastLineage
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from app.api._schemas import PipelineStatusResponse

from app.clients import neo4j_client
from app.core.limiter import limiter
from app.core.security import get_current_user
from app.pipelines.base import PipelineContext
from app.pipelines.lineage_pipeline import LineageInput, run_lineage_pipeline
from app.queue.client import enqueue_analyze_lineage
from app.queue.status_guard import get_job_status_for_user
from app.core.wait_guard import guard_wait_mode
from app.service import audit_repository, lineage_repository, ownership_repository
from app.service.lineage_repository import (
    LineageHistoryItem,
    LineageResult,
    LineageResultData,
    LineageTruth,
)
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["Lineage"])


# 공통 어댑터 — pipelines.base.Neo4jClientProxy (run_cypher + run_in_transaction 노출)
from app.pipelines.base import Neo4jClientProxy as _Neo4jProxy


class _NullGemini:
    """lineage 파이프라인은 LLM 미사용 — GeminiClient 초기화(env var 검증)도 회피."""

    async def generate(self, *args, **kwargs):  # pragma: no cover — never called
        raise RuntimeError("lineage pipeline 은 Gemini 를 호출하지 않습니다.")


def _build_context(idempotency_key: str, user_email: str = "") -> PipelineContext:
    """[Phase 2D] user_email — lineage 자체는 LLM 미사용이지만 멀티테넌시
    격리(향후 pipeline 들이 ctx.user_email 참조)에 일관성 유지."""
    return PipelineContext(
        gemini=_NullGemini(),  # lineage 는 LLM 미사용
        neo4j=_Neo4jProxy(),
        idempotency_key=idempotency_key,
        user_email=user_email or "",
    )


# ─── Schemas ───────────────────────────────────────────────────


class LineageRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None


class LineageResponse(BaseModel):
    status: str
    task_id: str
    result: Optional[LineageResultData] = None


# ─── Routes ─────────────────────────────────────────────────────


@router.post(
    "/pipelines/lineage",
    response_model=LineageResponse,
    summary="analyzeLineage — 산출물 ↔ GitHub 파일 deterministic 매칭",
)
async def analyze_lineage_route(
    payload: LineageRequest,
    wait: bool = Query(False, description="true 시 동기 대기"),
    current_user: UserPublic = Depends(get_current_user),
) -> LineageResponse:
    # ?wait=true 운영 admin 가드 — 동기 모드 web worker 점거 (DoS) 차단 (Sprint 8 P1).
    guard_wait_mode(wait, current_user)
    # 본인 프로젝트만 lineage 분석 — 다른 사용자 프로젝트의 task 생성 차단.
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    task_id = str(uuid.uuid4())

    # 사용자 OAuth access_token — private repo tree fetch 용.
    from app.service import user_repository
    user_token = await user_repository.get_github_access_token(current_user.email)

    if wait:
        ctx = _build_context(idempotency_key=task_id, user_email=current_user.email)
        try:
            result = await run_lineage_pipeline(
                ctx,
                LineageInput(project_name=payload.project_name, team_id=payload.team_id or ""),
                user_token=user_token,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
            ) from e
        return LineageResponse(status="success", task_id=task_id, result=result)

    try:
        await enqueue_analyze_lineage(
            task_id=task_id,
            project_name=payload.project_name,
            user_token=user_token,
            team_id=payload.team_id or "",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("enqueue lineage failed (task=%s)", task_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {e}",
        ) from e
    return LineageResponse(status="accepted", task_id=task_id)


@router.get(
    "/pipelines/lineage/status/{task_id}",
    response_model=PipelineStatusResponse,
    summary="analyzeLineage 작업 상태 조회",
)
@limiter.limit("60/minute")
async def lineage_status_route(
    request: Request,
    task_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> PipelineStatusResponse:
    # ownership 검증 후 status 반환 — 다른 사용자의 task_id 우회 차단 (Sprint 8 P0).
    info = await get_job_status_for_user(task_id, current_user.email)
    return PipelineStatusResponse(**info)


@router.get(
    "/pipelines/lineage/last",
    response_model=LineageResult,
    summary="getLastLineage — 프로젝트의 가장 최근 lineage 결과",
)
async def get_last_lineage_route(
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> LineageResult:
    # [2026-06] 비소유(아직 claim 안 된 본인 신규 프로젝트 포함) → 데이터 미접근(전역 name
    # read 라 IDOR 방지) + lineage 미존재와 동일하게 404. pre-claim 의 403(권한오류) 노이즈를
    # 제거하고 'claim 됐지만 lineage 없음'과 동작을 일치시킨다. (팀 플랜 미달은 402 raise.)
    if not await ownership_repository.can_access(current_user.email, project_name, team_id):
        raise HTTPException(status_code=404, detail="Lineage 결과를 찾을 수 없습니다.")
    result = await lineage_repository.get_last_lineage(project_name, team_id=team_id or "")
    if result is None:
        raise HTTPException(
            status_code=404, detail="Lineage 결과를 찾을 수 없습니다."
        )
    return result


# ─── Lineage History ────────────────────────────────────────────


@router.get(
    "/lineage/history",
    response_model=List[LineageHistoryItem],
    summary="lineage 분석 이력 (최신순). 본문 미포함 — 목록용 경량 응답.",
)
async def list_lineage_history_route(
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    current_user: UserPublic = Depends(get_current_user),
) -> List[LineageHistoryItem]:
    # [2026-06] 비소유 → 빈 이력(200 []). 데이터 미접근(IDOR 방지).
    if not await ownership_repository.can_access(current_user.email, project_name, team_id):
        return []
    return await lineage_repository.get_lineage_history(project_name, limit=limit, team_id=team_id or "")


@router.get(
    "/lineage/history/{lineage_id}",
    response_model=LineageResult,
    summary="특정 lineage 분석 본문 조회 (history → diff 화면)",
)
async def get_lineage_by_id_route(
    lineage_id: str,
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    current_user: UserPublic = Depends(get_current_user),
) -> LineageResult:
    # [2026-06] 비소유 → 데이터 미접근(IDOR 방지) + 미존재와 동일 404.
    if not await ownership_repository.can_access(current_user.email, project_name, team_id):
        raise HTTPException(status_code=404, detail="해당 lineage 결과를 찾을 수 없습니다.")
    out = await lineage_repository.get_lineage_by_id(project_name, lineage_id, team_id=team_id or "")
    if out is None:
        raise HTTPException(
            status_code=404,
            detail="해당 lineage 결과를 찾을 수 없습니다.",
        )
    return out


# ─── Lineage Truth (정답 라벨) ──────────────────────────────────


class LineageTruthUpsertRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    item_type: str = Field(..., min_length=1)
    item_id: str = Field(..., min_length=1)
    expected_files: List[str] = Field(default_factory=list)


class LineageTruthImportRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    items: List[Dict[str, Any]] = Field(default_factory=list)
    override: bool = False


class LineageTruthImportResponse(BaseModel):
    written: int
    skipped: int


@router.post(
    "/lineage/truth",
    response_model=LineageTruth,
    summary="lineage 정답 라벨 upsert",
)
async def save_lineage_truth_route(
    payload: LineageTruthUpsertRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> LineageTruth:
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    try:
        saved = await lineage_repository.save_lineage_truth(
            payload.project_name,
            payload.item_type,
            payload.item_id,
            payload.expected_files,
            team_id=payload.team_id or "",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        ) from e
    # 변경 이력 — Sprint 8 P1 (admin 패널 추적성과 동일 정책).
    await audit_repository.write(
        actor_email=current_user.email,
        action=audit_repository.ACTION_LINEAGE_TRUTH_SAVE,
        payload={
            "project": payload.project_name,
            "itemType": payload.item_type,
            "itemId": payload.item_id,
            "expectedFilesCount": len(payload.expected_files or []),
        },
    )
    return saved


@router.get(
    "/lineage/truth",
    response_model=List[LineageTruth],
    summary="lineage 정답 라벨 목록 (선택적 itemType 필터)",
)
async def list_lineage_truth_route(
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    item_type: Optional[str] = Query(None),
    current_user: UserPublic = Depends(get_current_user),
) -> List[LineageTruth]:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    return await lineage_repository.list_lineage_truth(project_name, item_type=item_type, team_id=team_id or "")


@router.delete(
    "/lineage/truth",
    summary="lineage 정답 라벨 삭제",
)
async def delete_lineage_truth_route(
    project_name: str = Query(..., min_length=1),
    team_id: Optional[str] = None,
    item_type: str = Query(..., min_length=1),
    item_id: str = Query(..., min_length=1),
    current_user: UserPublic = Depends(get_current_user),
) -> Dict[str, Any]:
    await ownership_repository.assert_access(current_user.email, project_name, team_id)
    deleted = await lineage_repository.delete_lineage_truth(
        project_name, item_type, item_id, team_id=team_id or ""
    )
    # 실제 삭제 발생했을 때만 audit — no-op (이미 없음) 은 비기록.
    if deleted:
        await audit_repository.write(
            actor_email=current_user.email,
            action=audit_repository.ACTION_LINEAGE_TRUTH_DELETE,
            payload={
                "project": project_name,
                "itemType": item_type,
                "itemId": item_id,
            },
        )
    return {"deleted": deleted}


@router.post(
    "/lineage/truth/import",
    response_model=LineageTruthImportResponse,
    summary="lineage 정답 라벨 벌크 import (CSV/JSON)",
)
# 벌크 mutation — 다른 v2 mutation (cps/prd/design 3/minute) 와 동일 보수적 limit.
# DoS 표면 (대량 truth 생성) + audit 노이즈 둘 다 차단.
@limiter.limit("3/minute")
async def import_lineage_truth_route(
    request: Request,
    payload: LineageTruthImportRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> LineageTruthImportResponse:
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    try:
        result = await lineage_repository.import_lineage_truth(
            payload.project_name, payload.items, override=payload.override,
            team_id=payload.team_id or "",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        ) from e
    # 벌크 작업 — 개별 itemId 노출 대신 집계만 기록 (이력 noise 감소).
    await audit_repository.write(
        actor_email=current_user.email,
        action=audit_repository.ACTION_LINEAGE_TRUTH_IMPORT,
        payload={
            "project": payload.project_name,
            "override": payload.override,
            "written": result.get("written", 0),
            "skipped": result.get("skipped", 0),
            "requestedCount": len(payload.items or []),
        },
    )
    return LineageTruthImportResponse(**result)
