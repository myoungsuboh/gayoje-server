"""AI 인터뷰 라우트 — 회의록 없는 사용자의 대화형 진입.

흐름:
  FE 가 대화 history 를 매 턴 보냄 (stateless BE) → /interview/turn 이
  다음 질문(phase=ask) 또는 회의록 합성(phase=done)을 반환.
  done 의 meeting_content 를 FE 가 기존 post_meeting 에 그대로 투입한다.

설계:
- 단일 동기 LLM 호출 (tracked_pipeline_context 로 토큰 누적).
- 토큰 쿼터만 검사 — 미팅 쿼터는 실제 post_meeting 단계에서 차감.
- ownership 검사 없음 — 프로젝트 생성 전(cold start)에도 동작해야 함. 인증만 요구.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api._quota_helpers import tracked_pipeline_context
from app.clients.gemini_client import GeminiError, gemini_error_to_http
from app.core import quota
from app.core.limiter import limiter
from app.core.project_scope import scoped_project
from app.core.security import get_current_user
from app.pipelines.interview import (
    InterviewMessage,
    InterviewProjectContext,
    build_graph_summary,
    build_interview_project_context,
    build_plan_input_hash,
    build_plan_quality_score,
    get_build_plan,
    graph_interview_context,
    is_substantive_plan,
    run_interview_turn,
    run_interview_turn_stream,
    save_build_plan,
    synthesize_build_plan,
)
from app.service import ownership_repository
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["Interview"])

# 대화 history 폭주 방지 — 클라이언트가 보낼 수 있는 최대 메시지 수.
_MAX_HISTORY = 40
_MAX_CONTENT_LEN = 4000
# 기존 초안(보완 인터뷰) 최대 길이 — 토큰 폭주 방지. 회의록 한 건 규모면 충분.
_MAX_EXISTING_LEN = 20000


# ─── 요청/응답 모델 ──────────────────────────────────────────────────────


class InterviewMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=_MAX_CONTENT_LEN)


class InterviewTurnRequest(BaseModel):
    # project_name 은 선택 — cold start 시 아직 없을 수 있음. 맥락용으로만 사용.
    project_name: str = Field("", max_length=200)
    history: List[InterviewMessageIn] = Field(default_factory=list)
    # 사용자가 이미 작성한 회의록 초안 (보완 인터뷰). 비면 빈 상태에서 시작.
    # AI 가 이미 담긴 내용은 다시 묻지 않고, 최종 합성 시 초안을 보존·병합한다.
    existing_content: str = Field("", max_length=_MAX_EXISTING_LEN)
    # [T5] 소유 프로젝트면 설계 그래프 갭을 인터뷰가 우선 질문하도록 스코프 키 합성용.
    team_id: Optional[str] = None
    # [2026-06-12 보강 모드] FE 가 들고 온 우선 의제 — 예: PRD 'AI로 보완하기'가
    # 근거 부족으로 남긴 질문(needs_input). 통합 의제에서 최우선으로 다뤄진다.
    # 항목당 200자·최대 10개는 build_interview_project_context 가 한 번 더 캡.
    agenda: List[str] = Field(default_factory=list, max_length=10)


class InterviewTurnResponse(BaseModel):
    phase: Literal["ask", "done"]
    assistant_message: str
    suggestions: List[str] = Field(default_factory=list)
    coverage: List[str] = Field(default_factory=list)
    # phase=done 일 때만 채워짐 — FE 가 post_meeting 의 meeting_content 로 사용.
    meeting_content: str = ""
    # [T2] 정량 준비도 — FE 진행바("거의 다 됐어요")용. readiness 0~1, scores 는 차원별.
    readiness: float = 0.0
    scores: Dict[str, float] = Field(default_factory=dict)
    # [T3] ask 턴에 다음 집중 차원 (FE 가 "지금 여쭤보는 것: 데이터" 식 힌트). done 이면 None.
    next_focus: Optional[str] = None


class BuildPlanRequest(BaseModel):
    # 회의록 텍스트 — 인터뷰 산출물이든 이미 등록된 미팅 로그든 동일 적용.
    meeting_content: str = Field(..., min_length=1, max_length=_MAX_EXISTING_LEN)
    # 선택 — 주면 그 프로젝트의 설계 그래프(DDD/SPACK/Arch)를 읽어 플랜 품질↑.
    # (소유권 확인 후에만 읽음 — 아래 라우트 참고)
    project_name: str = Field("", max_length=200)
    team_id: Optional[str] = None


class BuildPlanResponse(BaseModel):
    recommended_stack: str = ""
    scope_now: List[str] = Field(default_factory=list)
    scope_later: List[str] = Field(default_factory=list)
    milestones: List[str] = Field(default_factory=list)
    acceptance_criteria: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    start_prompt: str = ""
    # 품질 점수(0~1) + 항목별 분해 — 객관 적합도 측정(LLM 불필요). 회귀 잠금과
    # '자기정제가 점수를 실제로 올리는가' A/B 측정의 토대. (자기정제 루프 자체는 별개 작업)
    quality_score: float = 0.0
    quality_breakdown: Dict[str, float] = Field(default_factory=dict)


# ─── 라우트 ──────────────────────────────────────────────────────────────


@router.post(
    "/interview/build_plan",
    response_model=BuildPlanResponse,
    summary="회의록 → AI-buildable 빌드 플랜 합성 (스택·범위·마일스톤·AC·start_prompt)",
)
@limiter.limit("20/minute")
async def post_build_plan(
    request: Request,
    payload: BuildPlanRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> BuildPlanResponse:
    """회의록 텍스트를 받아 build_plan 을 합성해 반환.

    인터뷰 done 이후, 또는 이미 등록된 미팅 로그에 대해 호출. 토큰 쿼터만
    검사(미팅 쿼터 차감 없음). 합성 실패는 synthesize_build_plan 내부에서
    회의록 보존 폴백으로 처리되므로 라우트는 항상 사용 가능한 플랜을 돌려준다.
    """
    await quota.assert_tokens_within_limit(current_user.email)
    task_id = str(uuid.uuid4())
    project = payload.project_name.strip()

    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key=task_id,
    ) as ctx:
        # 소유한 프로젝트면 설계 그래프를 컨텍스트로 + 저장/캐시 키(db_project)로 쓴다.
        # 소유권 없음/조회 실패 시 그래프·캐시 없이 진행 (IDOR 차단 + 흐름 보호).
        graph_summary = ""
        db_project = ""
        if project:
            try:
                await ownership_repository.assert_access(
                    current_user.email, project, payload.team_id
                )
                db_project = scoped_project(project, payload.team_id)
                graph_summary = await build_graph_summary(ctx, db_project)
            except Exception:  # noqa: BLE001 — 권한 없음/조회 실패 → 그래프·캐시 생략
                logger.info("build_plan: 그래프/캐시 생략 (권한 없음/오류) project=%s", project)
                db_project = ""
                graph_summary = ""

        # 캐시: 입력(회의록 + 그래프)의 해시가 저장된 것과 같으면 LLM 재호출 없이 재사용.
        input_hash = build_plan_input_hash(payload.meeting_content, graph_summary)
        plan = None
        from_cache = False
        if db_project:
            try:
                cached, cached_hash = await get_build_plan(ctx, db_project)
                if cached is not None and cached_hash == input_hash:
                    plan = cached
                    from_cache = True
            except Exception:  # noqa: BLE001 — 캐시 조회 실패는 무시하고 새로 생성
                plan = None

        if plan is None:
            plan = await synthesize_build_plan(
                ctx, payload.meeting_content, graph_summary=graph_summary
            )
            # 실질 합성 결과만 캐시에 저장 — 합성 실패 폴백(빈 껍데기)을 저장하면
            # 일시 장애가 캐시에 박제되어 복구 후에도 폴백을 계속 돌려주게 된다.
            if db_project and is_substantive_plan(plan):
                try:
                    await save_build_plan(ctx, db_project, plan, input_hash)
                except Exception:  # noqa: BLE001 — 저장 실패는 응답을 막지 않는다
                    logger.exception("build_plan: 저장 실패 — 무시")

    # 품질 점수 — 객관 적합도 측정(LLM 불필요). 로깅·응답 메타로 노출만(자기정제 루프의
    # 측정 토대). cached=False(신규 합성)일 때의 점수 추이가 향후 '자기정제가 점수를
    # 올리는가' A/B 의 기준선이 된다. graph_summary 를 넘겨 brownfield 에서도 정제 루프와
    # 동일한 차원(그래프 정합 포함)으로 채점 — 라우트/내부 점수 불일치 제거.
    quality_score, quality_breakdown = build_plan_quality_score(plan, graph_summary)
    logger.info(
        "build_plan: quality=%.3f breakdown=%s project=%s cached=%s",
        quality_score, quality_breakdown, project or "(none)", from_cache,
    )

    return BuildPlanResponse(
        recommended_stack=plan.recommended_stack,
        scope_now=plan.scope_now,
        scope_later=plan.scope_later,
        milestones=plan.milestones,
        acceptance_criteria=plan.acceptance_criteria,
        risks=plan.risks,
        start_prompt=plan.start_prompt,
        quality_score=quality_score,
        quality_breakdown=quality_breakdown,
    )


async def _resolve_project_context(
    payload: InterviewTurnRequest, email: str,
) -> InterviewProjectContext:
    """[T5+T8 → 2026-06-12 확장] 소유 프로젝트면 브리프+통합 의제+grounding 반환.

    인터뷰를 막지 않는 '선택적 보강·객관 보정' — IDOR 차단(assert_access 후에만
    스코프 키 사용). 프로젝트 없음/권한 없음/조회 실패 → greenfield 컨텍스트
    (brief 빈 값 = 기존 인터뷰와 동일 동작. 단 FE agenda 는 프로젝트 없이도 유효).
    """
    name = (payload.project_name or "").strip()
    if not name:
        return await build_interview_project_context("", agenda=payload.agenda)
    try:
        await ownership_repository.assert_access(email, name, payload.team_id)
        return await build_interview_project_context(
            scoped_project(name, payload.team_id),
            display_name=name,
            agenda=payload.agenda,
        )
    except Exception:  # noqa: BLE001 — 권한 없음/조회 실패 → 보강·grounding 생략
        logger.info("interview: 프로젝트 컨텍스트 생략 (권한 없음/오류) project=%s", name)
        return InterviewProjectContext()


@router.post(
    "/interview/turn",
    response_model=InterviewTurnResponse,
    summary="AI 인터뷰 한 턴 — 다음 질문 또는 회의록 합성",
)
@limiter.limit("20/minute")
async def post_interview_turn(
    request: Request,
    payload: InterviewTurnRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> InterviewTurnResponse:
    """대화 history 를 받아 다음 인터뷰 턴을 반환.

    Rate limit: IP 당 분당 20회 (짧은 대화 턴이라 post_meeting 보다 관대).
    """
    if len(payload.history) > _MAX_HISTORY:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"대화가 너무 깁니다 (최대 {_MAX_HISTORY}턴). 새로 시작해주세요.",
        )

    # 토큰 쿼터 — 인터뷰도 LLM 을 쓰므로 한도 검사. 미팅 쿼터는 차감하지 않음.
    await quota.assert_tokens_within_limit(current_user.email)

    history = [InterviewMessage(role=m.role, content=m.content) for m in payload.history]
    task_id = str(uuid.uuid4())
    pc = await _resolve_project_context(payload, current_user.email)

    async with tracked_pipeline_context(
        user_email=current_user.email, idempotency_key=task_id,
    ) as ctx:
        try:
            turn = await run_interview_turn(
                ctx, history, payload.existing_content,
                pc.gap_questions, pc.graph_readiness,
                project_brief=pc.brief, supplement=pc.supplement,
                project_scoped=pc.project_scoped,
            )
        except GeminiError as e:
            logger.exception("interview turn gemini error (task=%s)", task_id)
            raise gemini_error_to_http(e) from e

    return InterviewTurnResponse(
        phase=turn.phase,
        assistant_message=turn.assistant_message,
        suggestions=turn.suggestions,
        coverage=turn.coverage,
        meeting_content=turn.meeting_content,
        readiness=turn.readiness,
        scores=turn.scores,
        next_focus=turn.next_focus,
    )


@router.post(
    "/interview/turn/stream",
    summary="AI 인터뷰 한 턴 — SSE 스트리밍 (text/event-stream)",
)
@limiter.limit("20/minute")
async def stream_interview_turn(
    request: Request,
    payload: InterviewTurnRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> StreamingResponse:
    """대화 history 를 받아 assistant 메시지를 SSE 토큰 단위로 스트리밍.

    이벤트 형식:
      data: {"type":"token","text":"<chars>"}   — 메시지 텍스트 청크
      data: {"type":"tool","tool":"prd"}        — [B-1] 프로젝트 자료 조회 중
      data: {"type":"done","phase":"ask|done","suggestions":[...],"coverage":[...],"meeting_content":"..."}
      data: {"type":"error","message":"..."}    — Gemini 오류 시

    Rate limit: /interview/turn 과 동일 (IP 당 분당 20회).
    """
    if len(payload.history) > _MAX_HISTORY:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"대화가 너무 깁니다 (최대 {_MAX_HISTORY}턴). 새로 시작해주세요.",
        )
    await quota.assert_tokens_within_limit(current_user.email)

    history = [InterviewMessage(role=m.role, content=m.content) for m in payload.history]
    task_id = str(uuid.uuid4())
    pc = await _resolve_project_context(payload, current_user.email)

    async def event_gen():
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
        ) as ctx:
            try:
                async for event_type, data in run_interview_turn_stream(
                    ctx, history, payload.existing_content,
                    pc.gap_questions, pc.graph_readiness,
                    project_brief=pc.brief, supplement=pc.supplement,
                    project_scoped=pc.project_scoped,
                ):
                    if event_type == "token":
                        yield f"data: {json.dumps({'type': 'token', 'text': data}, ensure_ascii=False)}\n\n"
                    elif event_type == "tool":
                        # [B-1] ACTION 도구 실행 — FE 가 "프로젝트 자료 확인 중" 표시.
                        yield f"data: {json.dumps({'type': 'tool', 'tool': data}, ensure_ascii=False)}\n\n"
                    elif event_type == "finalizing":
                        # done 판정 후 회의록 합성 시작 — FE 가 "정리 중" 표시.
                        yield f"data: {json.dumps({'type': 'finalizing'}, ensure_ascii=False)}\n\n"
                    elif event_type == "done":
                        yield f"data: {json.dumps({'type': 'done', 'phase': data.phase, 'assistant_message': data.assistant_message, 'suggestions': data.suggestions, 'coverage': data.coverage, 'meeting_content': data.meeting_content, 'readiness': data.readiness, 'scores': data.scores, 'next_focus': data.next_focus}, ensure_ascii=False)}\n\n"
            except GeminiError as e:
                logger.exception("interview stream gemini error (task=%s)", task_id)
                http_exc = gemini_error_to_http(e)
                detail = http_exc.detail if isinstance(http_exc.detail, str) else str(e)
                yield f"data: {json.dumps({'type': 'error', 'message': detail}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
