"""
Inquiry 라우트 — 사용자 문의 작성 / 관리자 답변.

[엔드포인트]
사용자:
  - POST   /api/inquiries              — 새 문의 작성 (인증)
  - GET    /api/inquiries/me           — 내 문의 목록 + 답변

관리자:
  - GET    /api/admin/inquiries        — 리스트 (status/q/limit/offset)
  - GET    /api/admin/inquiries/stats  — 상태별 카운트 (대시보드)
  - GET    /api/admin/inquiries/{id}   — 상세
  - PATCH  /api/admin/inquiries/{id}   — 답변/상태 갱신 + 답변 시 이메일 발송

[보안]
- 사용자: get_current_user (Bearer)
- 관리자: get_admin_user
- rate limit: 사용자 작성 10/hour, admin 60/min

[이메일 알림]
- admin_reply 가 추가/변경되고 비어있지 않으면 → Resend 로 사용자에게 발송.
- email_enabled=False 면 silent skip + warning 로그.
- 발송 실패해도 본 PATCH 응답은 200.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.core import email as email_lib
from app.core.config import settings
from app.core.limiter import limiter
from app.core.security import get_admin_user, get_current_user
from app.service import inquiry_repository
from app.service.inquiry_repository import (
    INQUIRY_CATEGORIES,
    INQUIRY_STATUSES,
    MAX_BODY_LENGTH,
    MAX_REPLY_LENGTH,
    MAX_SUBJECT_LENGTH,
    Inquiry,
)
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

user_router = APIRouter(prefix="/api", tags=["Inquiry"])
admin_router = APIRouter(prefix="/api/admin", tags=["Admin", "Inquiry"])


# ===== Request DTOs =====


class CreateInquiryRequest(BaseModel):
    category: str = Field(..., description="general | bug | feature | billing | other")
    subject: str = Field(..., min_length=1, max_length=MAX_SUBJECT_LENGTH)
    body: str = Field(..., min_length=1, max_length=MAX_BODY_LENGTH)


class UpdateInquiryRequest(BaseModel):
    """admin 갱신 — status 또는 admin_reply 또는 둘 다."""
    status: Optional[str] = Field(default=None, description="open | in_progress | resolved | closed")
    admin_reply: Optional[str] = Field(default=None, max_length=MAX_REPLY_LENGTH)


# ===== Response DTOs =====


class InquiryItem(BaseModel):
    id: str
    user_email: str
    user_name: str
    category: str
    category_label: str
    subject: str
    body: str
    status: str
    status_label: str
    admin_reply: str
    admin_replied_by: str
    admin_replied_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class InquiryListResponse(BaseModel):
    inquiries: List[InquiryItem]
    total: int


class InquiryStatsResponse(BaseModel):
    open: int
    in_progress: int
    resolved: int
    closed: int
    total: int


# ===== 헬퍼 =====


def _inquiry_url(inquiry_id: str) -> str:
    """이메일 본문의 FE deep link — FRONTEND_OAUTH_CALLBACK_URL origin 활용."""
    base = settings.FRONTEND_OAUTH_CALLBACK_URL or ""
    if base:
        p = urlparse(base)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}/contact?id={inquiry_id}"
    return f"/contact?id={inquiry_id}"


def _to_item(inquiry: Inquiry) -> InquiryItem:
    return InquiryItem(**inquiry.to_dict())


# ===== 사용자 라우트 =====


@user_router.post(
    "/inquiries",
    response_model=InquiryItem,
    status_code=status.HTTP_201_CREATED,
    summary="문의 작성 — 인증 필수",
)
@limiter.limit("10/hour")
async def create_inquiry_route(
    request: Request,
    payload: CreateInquiryRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> InquiryItem:
    """
    문의 작성. spam 방지 — 10건/시간/사용자.

    [정책]
    - category 는 INQUIRY_CATEGORIES 중 하나 (잘못된 값 400)
    - subject 1~200자, body 1~5000자 (Pydantic 1차 검증)
    """
    if payload.category not in INQUIRY_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"category 는 {INQUIRY_CATEGORIES} 중 하나여야 합니다.",
        )
    inquiry = await inquiry_repository.create_inquiry(
        user_email=current_user.email,
        user_name=current_user.name or current_user.email.split("@")[0],
        category=payload.category,
        subject=payload.subject,
        body=payload.body,
    )
    if inquiry is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="문의 작성에 실패했습니다.",
        )
    logger.info(
        "inquiry created: id=%s user=%s category=%s",
        inquiry.id, current_user.email, inquiry.category,
    )
    return _to_item(inquiry)


@user_router.get(
    "/inquiries/me",
    response_model=InquiryListResponse,
    summary="내 문의 목록 + 답변",
)
@limiter.limit("60/minute")
async def list_my_inquiries_route(
    request: Request,
    current_user: UserPublic = Depends(get_current_user),
) -> InquiryListResponse:
    """사용자 본인의 모든 문의 (최신순)."""
    inquiries = await inquiry_repository.list_my_inquiries(current_user.email)
    items = [_to_item(i) for i in inquiries if i]
    return InquiryListResponse(inquiries=items, total=len(items))


# ===== 관리자 라우트 =====


@admin_router.get(
    "/inquiries/stats",
    response_model=InquiryStatsResponse,
    summary="상태별 카운트 (admin 대시보드)",
)
@limiter.limit("60/minute")
async def admin_inquiry_stats_route(
    request: Request,
    _admin: UserPublic = Depends(get_admin_user),
) -> InquiryStatsResponse:
    counts = await inquiry_repository.count_by_status()
    return InquiryStatsResponse(**counts)


@admin_router.get(
    "/inquiries",
    response_model=InquiryListResponse,
    summary="문의 리스트 — status/q 필터, 페이징",
)
@limiter.limit("60/minute")
async def admin_list_inquiries_route(
    request: Request,
    status: str = Query("", description="open | in_progress | resolved | closed | 빈 = 전체"),
    q: str = Query("", description="subject/body/user_email 부분검색"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: UserPublic = Depends(get_admin_user),
) -> InquiryListResponse:
    if status and status not in INQUIRY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status 는 {INQUIRY_STATUSES} 중 하나 또는 빈 문자열이어야 합니다.",
        )
    result = await inquiry_repository.list_admin_inquiries(
        status_filter=status, q=q, limit=limit, offset=offset,
    )
    items = [_to_item(i) for i in result["inquiries"] if i]
    return InquiryListResponse(inquiries=items, total=result["total"])


@admin_router.get(
    "/inquiries/{inquiry_id}",
    response_model=InquiryItem,
    summary="문의 상세 (admin)",
)
@limiter.limit("60/minute")
async def admin_get_inquiry_route(
    request: Request,
    inquiry_id: str,
    _admin: UserPublic = Depends(get_admin_user),
) -> InquiryItem:
    inquiry = await inquiry_repository.get_inquiry(inquiry_id)
    if inquiry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="문의를 찾을 수 없습니다.",
        )
    return _to_item(inquiry)


@admin_router.patch(
    "/inquiries/{inquiry_id}",
    response_model=InquiryItem,
    summary="문의 답변/상태 갱신 — 답변 시 사용자에게 이메일 발송",
)
@limiter.limit("30/minute")
async def admin_update_inquiry_route(
    request: Request,
    inquiry_id: str,
    payload: UpdateInquiryRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> InquiryItem:
    """
    [동작]
    - status 만 변경: 단순 상태 전이
    - admin_reply 추가/변경: 답변 작성 + admin_replied_by/at 자동 갱신 +
      Resend 로 사용자에게 답변 알림 이메일 발송 (best-effort)

    [상태 자동 전이]
    답변이 비어있지 않으면 status 가 명시 안 됐을 때 자동으로 'resolved' 로.
    이미 'closed' 인 경우는 유지 (이미 종료된 건 reopen 안 함).
    """
    # 검증
    if payload.status is not None and payload.status not in INQUIRY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status 는 {INQUIRY_STATUSES} 중 하나여야 합니다.",
        )

    # 변경 전 상태 (자동 상태 전이용)
    before = await inquiry_repository.get_inquiry(inquiry_id)
    if before is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="문의를 찾을 수 없습니다.",
        )

    # 답변이 있고 status 명시 안 됐으면 자동으로 'resolved' (closed 는 예외)
    effective_status = payload.status
    has_new_reply = (
        payload.admin_reply is not None
        and payload.admin_reply.strip() != ""
        and payload.admin_reply != before.admin_reply
    )
    if has_new_reply and effective_status is None and before.status != "closed":
        effective_status = "resolved"

    inquiry = await inquiry_repository.update_inquiry(
        inquiry_id=inquiry_id,
        status=effective_status,
        admin_reply=payload.admin_reply,
        admin_email=admin.email,
    )
    if inquiry is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="문의 갱신에 실패했습니다.",
        )

    # 답변 이메일 발송 (best-effort) — 신규 답변일 때만
    if has_new_reply and settings.email_enabled:
        try:
            subject, html, text = email_lib.render_inquiry_reply_email(
                recipient_name=inquiry.user_name or inquiry.user_email.split("@")[0],
                subject_original=inquiry.subject,
                admin_reply=inquiry.admin_reply or "",
                inquiry_url=_inquiry_url(inquiry.id),
            )
            await email_lib.send_email(
                to=inquiry.user_email, subject=subject, html=html, text=text,
            )
            logger.info(
                "inquiry reply email sent — to=%s, inquiry=%s",
                inquiry.user_email, inquiry.id,
            )
        except email_lib.EmailDisabled:
            logger.warning("inquiry reply: email disabled, skipped")
        except email_lib.EmailSendError as e:
            logger.warning(
                "inquiry reply email failed (to=%s, inquiry=%s): %s",
                inquiry.user_email, inquiry.id, e,
            )

    logger.info(
        "inquiry updated: id=%s admin=%s status=%s has_reply=%s",
        inquiry.id, admin.email, inquiry.status, bool(inquiry.admin_reply),
    )
    return _to_item(inquiry)


# ===== 일괄 회신 (bulk reply) =====
# 같은 버그를 여러 사용자가 제보한 경우, 한 번 작성으로 전원에게 개인화 답변.

# {이름}/{제목} 토큰만 1회 치환 (단일 패스 — name 값에 우연히 {제목}이 있어도 이중 치환 안 됨)
_TEMPLATE_PATTERN = re.compile(r"\{이름\}|\{제목\}")


def _apply_template(template: str, *, name: str, subject: str) -> str:
    mapping = {"{이름}": name or "", "{제목}": subject or ""}
    return _TEMPLATE_PATTERN.sub(lambda m: mapping.get(m.group(0), m.group(0)), template or "")


class BulkReplyRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1, max_length=50, description="대상 문의 id (1~50)")
    reply_template: str = Field(
        ..., min_length=1, max_length=MAX_REPLY_LENGTH,
        description="답변 템플릿. {이름}/{제목} 변수 지원",
    )
    status: Optional[str] = Field(default="resolved", description="적용 상태 (기본 resolved)")


class BulkReplyFailure(BaseModel):
    id: str
    error: str


class BulkReplyResponse(BaseModel):
    total: int           # 요청 id 수 (중복 제거 후)
    updated: int         # DB 갱신 성공 (실제 존재한 건)
    sent: int            # 이메일 발송 성공 수
    email_enabled: bool  # 메일 비활성 시 sent=0 인 이유 구분
    failed: List[BulkReplyFailure]  # 이메일 발송 실패 건 (DB 는 갱신됨)


@admin_router.post(
    "/inquiries/bulk-reply",
    response_model=BulkReplyResponse,
    summary="여러 문의에 개인화 답변 일괄 발송 — 같은 버그 다건 처리",
)
@limiter.limit("10/minute")
async def admin_bulk_reply_route(
    request: Request,
    payload: BulkReplyRequest,
    admin: UserPublic = Depends(get_admin_user),
) -> BulkReplyResponse:
    """
    [동작]
    1. ids 로 대상 문의 조회
    2. 각 건 {이름}/{제목} 치환 → DB 일괄 갱신(답변 + 상태)
    3. 각 건 사용자에게 답변 이메일 병렬 발송 (best-effort, 동시성 8)

    [안전]
    - 이메일은 건별 독립 — 일부 실패해도 나머지 발송 + DB 는 이미 갱신됨
    - email_enabled=False 면 DB 만 갱신, sent=0
    - 동기 순차 발송 시 50건이면 타임아웃 → asyncio 병렬로 회피
    """
    status_to_set = payload.status or "resolved"
    if status_to_set not in INQUIRY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status 는 {INQUIRY_STATUSES} 중 하나여야 합니다.",
        )

    # id 중복 제거 (순서 유지)
    ids = list(dict.fromkeys([i for i in payload.ids if i]))
    if not ids:
        raise HTTPException(status_code=400, detail="대상 문의 id 가 비어 있습니다.")

    # 대상 조회 — 변수 치환에 user_name/subject 필요
    targets = await inquiry_repository.get_inquiries_by_ids(ids)
    if not targets:
        raise HTTPException(status_code=404, detail="대상 문의를 찾을 수 없습니다.")

    # 건별 치환 → items + 발송 메타
    items: List[Dict[str, str]] = []
    rendered: Dict[str, Dict[str, str]] = {}
    for inq in targets:
        name = inq.user_name or inq.user_email.split("@")[0]
        reply = _apply_template(payload.reply_template, name=name, subject=inq.subject)
        items.append({"id": inq.id, "reply": reply})
        rendered[inq.id] = {"name": name, "subject": inq.subject, "reply": reply}

    # DB 일괄 갱신 (UNWIND 한 쿼리)
    updated = await inquiry_repository.bulk_update_replies(
        items=items, status=status_to_set, admin_email=admin.email,
    )

    # 이메일 병렬 발송 (best-effort)
    failed: List[BulkReplyFailure] = []
    sent = 0
    if settings.email_enabled and updated:
        semaphore = asyncio.Semaphore(8)

        async def _send_one(inq: Inquiry):
            meta = rendered.get(inq.id, {})
            name = meta.get("name") or inq.user_name or inq.user_email.split("@")[0]
            subj = meta.get("subject") or inq.subject
            reply = meta.get("reply") or (inq.admin_reply or "")
            async with semaphore:
                try:
                    subject, html, text = email_lib.render_inquiry_reply_email(
                        recipient_name=name,
                        subject_original=subj,
                        admin_reply=reply,
                        inquiry_url=_inquiry_url(inq.id),
                    )
                    await email_lib.send_email(
                        to=inq.user_email, subject=subject, html=html, text=text,
                    )
                    return (inq.id, None)
                except email_lib.EmailDisabled:
                    return (inq.id, "email_disabled")
                except email_lib.EmailSendError as e:
                    return (inq.id, f"send_failed: {e}")
                except Exception as e:  # noqa: BLE001
                    return (inq.id, f"error: {e}")

        results = await asyncio.gather(*[_send_one(i) for i in updated])
        for iid, err in results:
            if err is None:
                sent += 1
            else:
                failed.append(BulkReplyFailure(id=iid, error=err))

    logger.info(
        "bulk reply: admin=%s requested=%d updated=%d sent=%d failed=%d",
        admin.email, len(ids), len(updated), sent, len(failed),
    )
    return BulkReplyResponse(
        total=len(ids),
        updated=len(updated),
        sent=sent,
        email_enabled=bool(settings.email_enabled),
        failed=failed,
    )
