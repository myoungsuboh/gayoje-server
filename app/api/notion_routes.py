"""
Notion 페이지 검색 / 미리보기 / 미팅 로그 import 라우트 (Phase 2).

[엔드포인트]
  - GET  /api/v2/notion/pages?q=&cursor=&page_size=
        → 현재 사용자 워크스페이스의 페이지 검색 (메타만, 슬림화 응답)
  - GET  /api/v2/notion/pages/{page_id}/preview
        → 페이지 전체 블록 → markdown 변환 결과 (import 전 미리보기)
  - POST /api/v2/notion/normalize
        → 3-Tier 분류 후 정형화. 등록 X.
        · 분류 LLM 호출 → ACCEPT/WARN 은 정형화 LLM 진행, BLOCK 은 400 차단
        · 응답 (ACCEPT/WARN): { original_markdown, normalized_markdown,
                                  classification: {type, confidence, reason, tier} }
        · 응답 (BLOCK): 400 NOTION_CONTENT_NOT_SUPPORTED + detail.classification
  - POST /api/v2/notion/import
        → 페이지를 회의록으로 등록 → post_meeting 파이프라인 enqueue
        body.meeting_content 제공 시: 그 텍스트를 그대로 사용 (정형화/편집된 결과)
        body.meeting_content 누락 시: BE 가 원본 markdown 으로 폴백 (legacy)

[보안 / 가드]
  - 모든 라우트: Bearer JWT 필수 (get_current_user)
  - notion 미연결: 412 Precondition Failed (FE 가 profile 페이지로 안내)
  - 토큰 401: notion access_token 폐기 처리 후 412 응답 (자동 unlink)
  - quota:
      · /normalize: assert_tokens_within_limit 만 (LLM 비용 가드). 미팅 카운트 X.
      · /import: post_meeting 과 동일한 풀 가드 (토큰/글자수/미팅수)
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.clients import notion_client as notion
from app.clients.notion_client import (
    NotionClient,
    NotionError,
    NotionRateLimited,
    NotionUnauthorized,
)
from app.core import notion_to_markdown
from app.core import quota
from app.core.limiter import limiter
from app.core.meeting_validation import (
    MeetingContentTooShort,
    assert_meeting_content_substantial,
)
from app.core.security import get_current_user
from app.api._quota_helpers import tracked_pipeline_context
from app.pipelines.base import Neo4jClientProxy as _Neo4jProxy
from app.pipelines.notion_classify_pipeline import (
    NotionClassifyInput,
    run_notion_classify,
)
from app.pipelines.notion_normalize_pipeline import (
    NotionNormalizeInput,
    run_notion_normalize,
)
from app.clients.gemini_client import GeminiError, gemini_error_to_http
from app.queue.client import enqueue_post_meeting
from app.service import notion_export_service, ownership_repository, user_repository
from app.service.ownership_repository import ProjectOwnershipConflict
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/notion", tags=["Notion Import"])


# ===== Schemas =====


class NotionPageSummary(BaseModel):
    """페이지 검색 결과 슬림화 — FE 가 카드로 보여줄 최소 정보."""

    id: str
    title: str
    icon: Optional[str] = None  # emoji 또는 image URL
    url: Optional[str] = None
    last_edited_time: Optional[str] = None  # ISO8601
    parent_type: Optional[str] = None  # 'workspace' | 'page_id' | 'database_id'


class NotionSearchResponse(BaseModel):
    results: List[NotionPageSummary]
    has_more: bool
    next_cursor: Optional[str] = None


class NotionPreviewResponse(BaseModel):
    page_id: str
    title: str
    markdown: str
    char_count: int
    block_count: int
    last_edited_time: Optional[str] = None
    truncated: bool = False


class NotionImportRequest(BaseModel):
    page_id: str = Field(..., description="Notion 페이지 ID 또는 URL (출처 추적용)")
    project_name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1, description="미팅 버전 (예: 'v1.0')")
    date: str = ""
    # [Phase 3] 정형화 + 편집된 결과를 그대로 등록할 때 사용. 누락 시 BE 가
    # Notion 원본을 다시 fetch → markdown 변환해서 등록 (legacy 동작).
    # 제공 시: 클라이언트가 책임지는 내용. assert_meeting_content_substantial 가드는
    # 똑같이 적용 → 200자 미만이면 400 차단.
    meeting_content: Optional[str] = Field(
        None,
        max_length=8 * 1024 * 1024,
        description="정형화 + 편집된 회의록 본문. 누락 시 BE 가 원본으로 폴백.",
    )
    previous_cps_id: Optional[str] = None
    previous_prd_id: Optional[str] = None
    team_id: Optional[str] = None


class NotionNormalizeRequest(BaseModel):
    page_id: str = Field(..., description="Notion 페이지 ID 또는 URL")
    project_name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1, description="미팅 버전 (예: 'v1.0')")
    team_id: Optional[str] = None


class NotionClassification(BaseModel):
    """페이지 분류 결과 — FE 가 chip + 경고 UI 그리는 데 사용."""

    type: str        # 'meeting_log' | 'retrospective' | 'spec_doc' | 'task_request' | 'general_doc' | 'unknown'
    confidence: float
    reason: str
    tier: str        # 'ACCEPT' | 'WARN' | 'BLOCK'


class NotionNormalizeResponse(BaseModel):
    page_id: str
    title: str
    original_markdown: str       # Notion → markdown (정형화 전)
    normalized_markdown: str     # 표준 미팅 로그 포맷으로 LLM 정형화 결과
    original_char_count: int
    normalized_char_count: int
    truncated: bool = False      # 원본이 너무 길어 LLM 입력 잘렸는지
    classification: NotionClassification   # ACCEPT/WARN 페이지의 분류 결과


class NotionImportResponse(BaseModel):
    status: str
    task_id: str
    page_id: str
    title: str
    markdown_char_count: int


class NotionExportRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    docs: List[str] = Field(default_factory=lambda: ["cps", "prd", "design"])
    parent_page_id: Optional[str] = Field(
        None, description="최초 공유 시 허브를 생성할 Notion 상위 페이지 ID. 이후엔 무시(매핑 기억)."
    )
    team_id: Optional[str] = None


class NotionExportResult(BaseModel):
    doc: str                       # cps | prd | design | hub
    status: str                    # created | updated | skipped | failed | need_parent
    url: Optional[str] = None
    error: Optional[str] = None


class NotionExportResponse(BaseModel):
    hub_url: Optional[str] = None
    results: List[NotionExportResult] = []


# ===== 헬퍼 =====


async def _get_notion_token_or_412(email: str) -> str:
    """노션 연결 안 됐으면 412 Precondition Failed. FE 가 profile 페이지로 안내."""
    info = await user_repository.get_notion_info(email)
    if not info or not info.get("access_token"):
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={
                "code": "NOTION_NOT_LINKED",
                "message": "Notion 워크스페이스가 연결되어 있지 않습니다. 프로필에서 연결해주세요.",
            },
        )
    return info["access_token"]


async def _handle_notion_error(email: str, e: NotionError) -> HTTPException:
    """
    NotionClient 예외를 HTTPException 으로 변환. 401 은 자동 unlink.

    호출 패턴:
        try:
            ...
        except NotionError as e:
            raise await _handle_notion_error(email, e)
    """
    if isinstance(e, NotionUnauthorized):
        # 토큰 폐기됨 — 사용자가 Notion 쪽에서 integration 제거했거나 만료.
        # 자동 unlink 해서 FE 가 다시 연결 유도하게.
        try:
            await user_repository.unlink_notion(email)
        except Exception:  # noqa: BLE001
            logger.exception("auto-unlink notion failed for %s", email)
        return HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={
                "code": "NOTION_TOKEN_REVOKED",
                "message": "Notion 연결이 끊어졌습니다. 프로필에서 다시 연결해주세요.",
            },
        )
    if isinstance(e, NotionRateLimited):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "NOTION_RATE_LIMITED",
                "message": "Notion API 호출 제한에 걸렸습니다. 잠시 후 다시 시도해주세요.",
                "retry_after": e.retry_after,
            },
        )
    if e.status == 404:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "NOTION_NOT_FOUND",
                "message": "해당 페이지를 찾을 수 없거나 통합에 접근 권한이 없습니다.",
            },
        )
    if e.status == 400:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "NOTION_BAD_REQUEST", "message": str(e)},
        )
    # 5xx / 네트워크 / 알 수 없음
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": "NOTION_UPSTREAM_ERROR", "message": str(e)},
    )


def _to_summary(page: Dict[str, Any]) -> NotionPageSummary:
    parent = page.get("parent") or {}
    return NotionPageSummary(
        id=page.get("id") or "",
        title=notion.extract_page_title(page),
        icon=notion.extract_page_icon(page),
        url=page.get("url"),
        last_edited_time=page.get("last_edited_time"),
        parent_type=parent.get("type"),
    )


# ===== Preview 캐시 (60초, in-process) =====
# 사용자가 검색 후 같은 페이지 미리보기 여러 번 누르는 케이스 보호. 작은 LRU.

_PREVIEW_CACHE_TTL = 60.0
_PREVIEW_CACHE_MAX = 64
_preview_cache: Dict[str, tuple[float, NotionPreviewResponse]] = {}


def _preview_cache_get(key: str) -> Optional[NotionPreviewResponse]:
    entry = _preview_cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if (time.time() - ts) > _PREVIEW_CACHE_TTL:
        _preview_cache.pop(key, None)
        return None
    return value


def _preview_cache_set(key: str, value: NotionPreviewResponse) -> None:
    if len(_preview_cache) >= _PREVIEW_CACHE_MAX:
        # 가장 오래된 1개 제거 (단순 cleanup — 정확한 LRU 까진 불필요).
        oldest_key = min(_preview_cache, key=lambda k: _preview_cache[k][0])
        _preview_cache.pop(oldest_key, None)
    _preview_cache[key] = (time.time(), value)


# ===== Routes =====


@router.get(
    "/pages",
    response_model=NotionSearchResponse,
    summary="Notion 워크스페이스 페이지 검색 (현재 사용자)",
)
@limiter.limit("30/minute")
async def list_notion_pages(
    request: Request,
    q: str = Query("", description="검색어. 빈 값이면 최근 수정 페이지 목록."),
    cursor: Optional[str] = Query(None, description="다음 페이지 커서"),
    page_size: int = Query(25, ge=1, le=100),
    current_user: UserPublic = Depends(get_current_user),
) -> NotionSearchResponse:
    token = await _get_notion_token_or_412(current_user.email)
    client = NotionClient(user_token=token)
    try:
        data = await client.search_pages(
            query=q, page_size=page_size, start_cursor=cursor
        )
    except NotionError as e:
        raise await _handle_notion_error(current_user.email, e)

    raw_results = data.get("results") or []
    # filter='page' 로 요청해도 가끔 database 가 섞일 수 있어 한 번 더 거름.
    summaries = [_to_summary(p) for p in raw_results if p.get("object") == "page"]
    return NotionSearchResponse(
        results=summaries,
        has_more=bool(data.get("has_more")),
        next_cursor=data.get("next_cursor"),
    )


@router.get(
    "/pages/{page_id}/preview",
    response_model=NotionPreviewResponse,
    summary="Notion 페이지 → markdown 변환 미리보기",
)
@limiter.limit("20/minute")
async def preview_notion_page(
    request: Request,
    page_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> NotionPreviewResponse:
    cache_key = f"{current_user.email}:{page_id}"
    cached = _preview_cache_get(cache_key)
    if cached is not None:
        return cached

    token = await _get_notion_token_or_412(current_user.email)
    client = NotionClient(user_token=token)
    try:
        page = await client.get_page(page_id)
        blocks = await client.get_page_blocks(page_id)
    except NotionError as e:
        raise await _handle_notion_error(current_user.email, e)

    md = notion_to_markdown.blocks_to_markdown(blocks)
    title = notion.extract_page_title(page)
    response = NotionPreviewResponse(
        page_id=page.get("id") or page_id,
        title=title,
        markdown=md,
        char_count=len(md),
        block_count=_count_blocks(blocks),
        last_edited_time=page.get("last_edited_time"),
        truncated=False,
    )
    _preview_cache_set(cache_key, response)
    return response


@router.post(
    "/normalize",
    response_model=NotionNormalizeResponse,
    summary="Notion 페이지 markdown → 표준 미팅 로그 포맷 LLM 정형화 (등록 X)",
)
@limiter.limit("6/minute")
async def normalize_notion_page(
    request: Request,
    payload: NotionNormalizeRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> NotionNormalizeResponse:
    """
    정형화 단독 — 사용자가 결과 확인/편집 후 별도 /import 호출로 등록.

    [가드]
    - notion 연결 필수 (없으면 412)
    - assert_tokens_within_limit (LLM 비용 가드)
    - meeting quota acquire 안 함 (등록 X, preview 성격)

    [흐름]
    1. Notion 페이지 fetch + markdown 변환 (preview 와 동일)
    2. tracked_pipeline_context 로 LLM 호출 (등급별 모델, 토큰 자동 누적)
    3. 정형화 결과 + 원본 markdown 둘 다 응답 (FE 가 비교 표시)
    """
    token = await _get_notion_token_or_412(current_user.email)
    client = NotionClient(user_token=token)
    try:
        page = await client.get_page(payload.page_id)
        blocks = await client.get_page_blocks(payload.page_id)
    except NotionError as e:
        raise await _handle_notion_error(current_user.email, e)

    title = notion.extract_page_title(page)
    original_md = notion_to_markdown.blocks_to_markdown(blocks)
    if not original_md.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "NOTION_PAGE_EMPTY",
                "message": "선택한 Notion 페이지가 비어있거나 변환 가능한 콘텐츠가 없습니다.",
            },
        )

    # LLM 비용 가드 (LLM 호출 전):
    #   - tokens: 누적 LLM 토큰 한도
    #   - summary_chars: 한 번에 LLM 에 보낼 글자수 한도 — *원본 markdown* 기준.
    #     이 가드 없으면 사용자가 거대한 Notion 페이지로 /normalize 호출해서
    #     LLM 에 등급 한도 초과 분량 전송 후, 압축된 결과 (한도 안) 를 /import 에
    #     보내 등록할 수 있음 → summary_chars 한도가 실질적으로 우회됨.
    # /import 도 동일 가드 호출하지만, /normalize 단계의 LLM 비용은 거기서 막을 수 없음.
    await quota.assert_tokens_within_limit(current_user.email)
    await quota.assert_summary_within_limit(current_user.email, original_md)

    # 등급별 모델 선택 + 토큰 자동 누적 (sync route)
    task_id = str(uuid.uuid4())

    # ── 1단계: 분류 (3-Tier) — 회의록 아닌 페이지는 BLOCK ──
    # 분류 LLM call 1회. BLOCK 이면 정형화 LLM 호출 안 함 (비용/품질 보호).
    try:
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=f"{task_id}-classify",
        ) as ctx:
            classification = await run_notion_classify(
                ctx,
                NotionClassifyInput(page_title=title, markdown=original_md),
            )
    except GeminiError as e:
        logger.exception("notion classify gemini error (task=%s)", task_id)
        raise gemini_error_to_http(e) from e

    if classification.tier == "BLOCK":
        # 거부 — 정형화 LLM 호출 X. 사용자에게 분류 근거 노출.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "NOTION_CONTENT_NOT_SUPPORTED",
                "message": (
                    "이 페이지는 Harness 가 분석하는 회의록 형식이 아닙니다. "
                    "소프트웨어 개발 회의/논의 내용을 담은 페이지를 선택해주세요."
                ),
                "classification": {
                    "type": classification.type,
                    "confidence": classification.confidence,
                    "reason": classification.reason,
                    "tier": classification.tier,
                },
            },
        )

    # ── 2단계: 정형화 (ACCEPT / WARN 둘 다 진행. WARN 은 응답에 표시) ──
    pipeline_input = NotionNormalizeInput(
        project_name=payload.project_name,
        version=payload.version,
        page_title=title,
        page_url=page.get("url") or "",
        last_edited=page.get("last_edited_time") or "",
        original_markdown=original_md,
    )
    try:
        async with tracked_pipeline_context(
            user_email=current_user.email, idempotency_key=task_id,
        ) as ctx:
            result = await run_notion_normalize(ctx, pipeline_input)
    except GeminiError as e:
        logger.exception("notion normalize gemini error (task=%s)", task_id)
        raise gemini_error_to_http(e) from e
    except ValueError as e:
        logger.warning("notion normalize value error (task=%s): %s", task_id, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "NOTION_NORMALIZE_FAILED",
                "message": "정형화에 실패했습니다. 다시 시도해주세요.",
            },
        ) from e

    return NotionNormalizeResponse(
        page_id=page.get("id") or payload.page_id,
        title=title,
        original_markdown=original_md,
        normalized_markdown=result.normalized_markdown,
        original_char_count=len(original_md),
        normalized_char_count=result.char_count,
        truncated=result.truncated,
        classification=NotionClassification(
            type=classification.type,
            confidence=classification.confidence,
            reason=classification.reason,
            tier=classification.tier,
        ),
    )


@router.post(
    "/import",
    response_model=NotionImportResponse,
    summary="Notion 페이지를 회의록으로 import → post_meeting 파이프라인 enqueue",
)
@limiter.limit("3/minute")
async def import_notion_page(
    request: Request,
    payload: NotionImportRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> NotionImportResponse:
    """
    post_meeting 라우트와 같은 quota / claim 가드. body.meeting_content 제공 시
    그 텍스트를 그대로 사용 (정형화 + 사용자 편집된 결과). 누락 시 BE 가
    원본 Notion markdown 으로 폴백 (legacy 동작).
    """
    token = await _get_notion_token_or_412(current_user.email)
    client = NotionClient(user_token=token)

    # 1. Notion fetch — 최소한 title / url 메타는 필요 (페이지 존재 검증 + 출처 링크).
    # body.meeting_content 가 와도 page_id 의 유효성은 검증해야 함 (악의적 ID 차단).
    try:
        page = await client.get_page(payload.page_id)
    except NotionError as e:
        raise await _handle_notion_error(current_user.email, e)

    title = notion.extract_page_title(page)
    page_url = page.get("url") or payload.page_id

    # 2. meeting_content 결정 — 클라이언트 제공 vs BE 폴백.
    if payload.meeting_content is not None:
        # 클라이언트가 정형화 + 편집 결과 그대로 전달.
        # 출처 메타는 자동 prepend — LLM 도 출처를 인지 + 사용자가 추적 가능.
        # 클라이언트가 이미 헤더를 넣었다면 중복 방지 위해 헤더 감지.
        client_body = payload.meeting_content
        if not client_body.lstrip().startswith("<!-- imported from Notion"):
            meeting_content = (
                f"<!-- imported from Notion: {page_url} -->\n\n{client_body}"
            )
        else:
            meeting_content = client_body
    else:
        # legacy 폴백 — 원본 markdown fetch + 헤더 prepend.
        try:
            blocks = await client.get_page_blocks(payload.page_id)
        except NotionError as e:
            raise await _handle_notion_error(current_user.email, e)
        md_body = notion_to_markdown.blocks_to_markdown(blocks)
        if not md_body.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "NOTION_PAGE_EMPTY",
                    "message": "선택한 Notion 페이지가 비어있거나 변환 가능한 콘텐츠가 없습니다.",
                },
            )
        meeting_content = (
            f"# {title}\n\n"
            f"<!-- imported from Notion: {page_url} -->\n\n"
            f"{md_body}"
        )

    # 2.5. 의미적 최소치 검증 — 클라이언트가 짧은 내용 보내거나 원본이 짧으면 차단.
    # quota 차감 *전* 차단 — 사용자 손해 방지.
    try:
        assert_meeting_content_substantial(meeting_content)
    except MeetingContentTooShort as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "NOTION_PAGE_TOO_SHORT",
                "message": str(e),
                "chars": e.chars,
                "non_ws_chars": e.non_ws_chars,
            },
        ) from e

    # 2. project claim (충돌 시 409)
    try:
        await ownership_repository.claim(
            current_user.email, payload.project_name, payload.team_id
        )
    except ProjectOwnershipConflict as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{e.project}' 는 이미 다른 사용자가 사용 중인 프로젝트 이름입니다. 다른 이름을 사용하세요.",
        ) from e

    # 3. quota 가드 — post_meeting 과 동일 순서
    await quota.assert_tokens_within_limit(current_user.email)
    await quota.assert_summary_within_limit(current_user.email, meeting_content)
    await quota.acquire_meeting_quota(current_user.email)

    # 4. enqueue post_meeting (CPS + PRD 체인)
    task_id = str(uuid.uuid4())
    try:
        await enqueue_post_meeting(
            task_id=task_id,
            project_name=payload.project_name,
            version=payload.version,
            date=payload.date,
            meeting_content=meeting_content,
            previous_cps_id=payload.previous_cps_id,
            previous_prd_id=payload.previous_prd_id,
            user_email=current_user.email,
            team_id=payload.team_id or "",
        )
    except HTTPException:
        raise  # [2026-06] 동시성 429 등 의도된 HTTP 에러는 503 으로 가리지 말고 그대로 전파
    except Exception as e:  # noqa: BLE001
        logger.exception("notion import enqueue failed (task=%s)", task_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"queue unavailable: {e}",
        ) from e

    return NotionImportResponse(
        status="accepted",
        task_id=task_id,
        page_id=page.get("id") or payload.page_id,
        title=title,
        markdown_char_count=len(meeting_content),
    )


# ===== 내부 =====


def _count_blocks(blocks: List[Dict[str, Any]]) -> int:
    """children 포함 총 블록 수 — preview 응답의 디버그 메타."""
    total = 0
    for b in blocks:
        total += 1
        children = b.get("_children") or []
        if children:
            total += _count_blocks(children)
    return total


# ===== Export (CPS/PRD/설계 → Notion 허브) =====


@router.post(
    "/export",
    response_model=NotionExportResponse,
    summary="CPS/PRD/설계를 Notion 허브 페이지로 공유 (멱등 갱신)",
)
@limiter.limit("6/minute")
async def export_to_notion(
    request: Request,
    payload: NotionExportRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> NotionExportResponse:
    """연동된 사용자의 Notion 워크스페이스에 프로젝트 허브+하위 페이지를 생성/갱신.

    가드: 소유권/팀 접근(assert_access) → Notion 연결(412 if 미연결) → export 서비스.
    Notion 오류는 _handle_notion_error 로 401(자동 unlink)→412 / 429 / 502 매핑.
    """
    # 다른 사용자/팀 프로젝트 export 차단 — read 라우트와 동일한 가드.
    await ownership_repository.assert_access(
        current_user.email, payload.project_name, payload.team_id
    )
    token = await _get_notion_token_or_412(current_user.email)
    client = NotionClient(user_token=token)
    try:
        out = await notion_export_service.export_project_to_notion(
            email=current_user.email,
            project_name=payload.project_name,
            team_id=payload.team_id or "",
            docs=payload.docs,
            parent_page_id=payload.parent_page_id,
            client=client,
        )
    except NotionError as e:
        raise await _handle_notion_error(current_user.email, e)
    return NotionExportResponse(**out)
