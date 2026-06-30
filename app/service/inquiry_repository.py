"""
Inquiry — 사용자 ↔ 관리자 문의 시스템 (2026-05).

[스키마]
(:Inquiry {
    id: UUID,
    user_email: str,            # 작성자 (탈퇴해도 노드 보존)
    user_name: str,             # 작성 당시 이름 (탈퇴 후 reference 용)
    category: str,              # 'general' | 'bug' | 'feature' | 'billing' | 'other'
    subject: str,               # 제목 (max 200)
    body: str,                  # 본문 (max 5000)
    status: str,                # 'open' | 'in_progress' | 'resolved' | 'closed'
    admin_reply: str,           # 관리자 답변 (max 5000)
    admin_replied_by: str,      # 답변한 어드민 email
    admin_replied_at: datetime,
    created_at: datetime,
    updated_at: datetime
})

[관계]
사용자가 탈퇴해도 Inquiry 노드는 보존 (분쟁 추적). User 노드와 별도 관계 X —
user_email 만 reference. AuditLog 와 동일 정책.

[상태 전이]
open → in_progress → resolved → closed
(admin 이 자유 변경 가능. 사용자는 자기 문의 read-only.)

[인덱스]
- status 별 조회 (admin)
- user_email + created_at desc 조회 (사용자 내 문의)
- created_at desc (admin 리스트)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# 카테고리 — FE 와 동기화
INQUIRY_CATEGORIES = ("general", "bug", "feature", "billing", "other")
INQUIRY_CATEGORY_LABELS = {
    "general": "일반 문의",
    "bug": "버그 신고",
    "feature": "기능 제안",
    "billing": "결제 문의",
    "other": "기타",
}

# 상태 — open → in_progress → resolved → closed
INQUIRY_STATUSES = ("open", "in_progress", "resolved", "closed")
INQUIRY_STATUS_LABELS = {
    "open": "접수됨",
    "in_progress": "처리 중",
    "resolved": "답변 완료",
    "closed": "종료",
}

# 길이 제한
MAX_SUBJECT_LENGTH = 200
MAX_BODY_LENGTH = 5000
MAX_REPLY_LENGTH = 5000


# ===== 도메인 모델 =====


@dataclass(frozen=True)
class Inquiry:
    id: str
    user_email: str
    user_name: str
    category: str
    subject: str
    body: str
    status: str
    admin_reply: Optional[str] = None
    admin_replied_by: Optional[str] = None
    admin_replied_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_email": self.user_email,
            "user_name": self.user_name,
            "category": self.category,
            "category_label": INQUIRY_CATEGORY_LABELS.get(self.category, self.category),
            "subject": self.subject,
            "body": self.body,
            "status": self.status,
            "status_label": INQUIRY_STATUS_LABELS.get(self.status, self.status),
            "admin_reply": self.admin_reply or "",
            "admin_replied_by": self.admin_replied_by or "",
            "admin_replied_at": self.admin_replied_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ===== Cypher =====


_ENSURE_INQUIRY_CONSTRAINT_CYPHER = """\
CREATE CONSTRAINT inquiry_id_unique IF NOT EXISTS
FOR (i:Inquiry) REQUIRE i.id IS UNIQUE
"""

_ENSURE_INQUIRY_INDEXES_CYPHER = [
    # 사용자 본인 문의 조회 (user_email + created_at desc)
    "CREATE INDEX inquiry_user_email_idx IF NOT EXISTS FOR (i:Inquiry) ON (i.user_email)",
    # admin 리스트 — status 필터링 + 정렬
    "CREATE INDEX inquiry_status_idx IF NOT EXISTS FOR (i:Inquiry) ON (i.status)",
    # 정렬용 — 전체 + 사용자 단위 둘 다 활용
    "CREATE INDEX inquiry_created_at_idx IF NOT EXISTS FOR (i:Inquiry) ON (i.created_at)",
]


_CREATE_INQUIRY_CYPHER = """\
CREATE (i:Inquiry {
    id: randomUUID(),
    user_email: $user_email,
    user_name: $user_name,
    category: $category,
    subject: $subject,
    body: $body,
    status: 'open',
    admin_reply: '',
    admin_replied_by: '',
    admin_replied_at: null,
    created_at: datetime(),
    updated_at: datetime()
})
RETURN {
    id: i.id,
    user_email: i.user_email,
    user_name: i.user_name,
    category: i.category,
    subject: i.subject,
    body: i.body,
    status: i.status,
    admin_reply: i.admin_reply,
    admin_replied_by: i.admin_replied_by,
    admin_replied_at: toString(i.admin_replied_at),
    created_at: toString(i.created_at),
    updated_at: toString(i.updated_at)
} AS inquiry
"""


_GET_INQUIRY_CYPHER = """\
MATCH (i:Inquiry {id: $id})
RETURN {
    id: i.id,
    user_email: i.user_email,
    user_name: i.user_name,
    category: i.category,
    subject: i.subject,
    body: i.body,
    status: i.status,
    admin_reply: COALESCE(i.admin_reply, ''),
    admin_replied_by: COALESCE(i.admin_replied_by, ''),
    admin_replied_at: toString(i.admin_replied_at),
    created_at: toString(i.created_at),
    updated_at: toString(i.updated_at)
} AS inquiry
"""


# 사용자 본인 문의 목록 — 최신순.
_LIST_MY_INQUIRIES_CYPHER = """\
MATCH (i:Inquiry {user_email: $email})
RETURN {
    id: i.id,
    user_email: i.user_email,
    user_name: i.user_name,
    category: i.category,
    subject: i.subject,
    body: i.body,
    status: i.status,
    admin_reply: COALESCE(i.admin_reply, ''),
    admin_replied_by: COALESCE(i.admin_replied_by, ''),
    admin_replied_at: toString(i.admin_replied_at),
    created_at: toString(i.created_at),
    updated_at: toString(i.updated_at)
} AS inquiry
ORDER BY i.created_at DESC
"""


# admin 리스트 — status 필터 + 검색 + 페이징.
# q 비어있으면 전체, 있으면 subject / body / user_email 부분 일치.
_LIST_ADMIN_INQUIRIES_CYPHER = """\
MATCH (i:Inquiry)
WHERE ($status_filter = '' OR i.status = $status_filter)
  AND ($q = ''
       OR toLower(i.subject) CONTAINS toLower($q)
       OR toLower(i.body) CONTAINS toLower($q)
       OR toLower(i.user_email) CONTAINS toLower($q))
WITH i
ORDER BY i.created_at DESC
SKIP $offset LIMIT $limit
RETURN {
    id: i.id,
    user_email: i.user_email,
    user_name: i.user_name,
    category: i.category,
    subject: i.subject,
    body: i.body,
    status: i.status,
    admin_reply: COALESCE(i.admin_reply, ''),
    admin_replied_by: COALESCE(i.admin_replied_by, ''),
    admin_replied_at: toString(i.admin_replied_at),
    created_at: toString(i.created_at),
    updated_at: toString(i.updated_at)
} AS inquiry
"""


_COUNT_ADMIN_INQUIRIES_CYPHER = """\
MATCH (i:Inquiry)
WHERE ($status_filter = '' OR i.status = $status_filter)
  AND ($q = ''
       OR toLower(i.subject) CONTAINS toLower($q)
       OR toLower(i.body) CONTAINS toLower($q)
       OR toLower(i.user_email) CONTAINS toLower($q))
RETURN count(i) AS total
"""


# 상태별 카운트 — admin 페이지 상단 통계용.
_COUNT_BY_STATUS_CYPHER = """\
MATCH (i:Inquiry)
RETURN i.status AS status, count(*) AS cnt
"""


# 상태 + 답변 갱신 (admin 전용). 부분 갱신 — null 인 필드는 SET 안 함.
_UPDATE_INQUIRY_CYPHER = """\
MATCH (i:Inquiry {id: $id})
SET i.status = COALESCE($status, i.status),
    i.admin_reply = CASE
        WHEN $admin_reply IS NOT NULL THEN $admin_reply
        ELSE COALESCE(i.admin_reply, '')
    END,
    i.admin_replied_by = CASE
        WHEN $admin_reply IS NOT NULL AND $admin_reply <> ''
            THEN $admin_email
        ELSE COALESCE(i.admin_replied_by, '')
    END,
    i.admin_replied_at = CASE
        WHEN $admin_reply IS NOT NULL AND $admin_reply <> ''
            THEN datetime()
        ELSE i.admin_replied_at
    END,
    i.updated_at = datetime()
RETURN {
    id: i.id,
    user_email: i.user_email,
    user_name: i.user_name,
    category: i.category,
    subject: i.subject,
    body: i.body,
    status: i.status,
    admin_reply: COALESCE(i.admin_reply, ''),
    admin_replied_by: COALESCE(i.admin_replied_by, ''),
    admin_replied_at: toString(i.admin_replied_at),
    created_at: toString(i.created_at),
    updated_at: toString(i.updated_at)
} AS inquiry
"""


# ===== 부팅 헬퍼 =====


async def ensure_inquiry_constraints() -> None:
    """Inquiry.id UNIQUE + indexes ensure."""
    try:
        await neo4j_client.run_cypher(_ENSURE_INQUIRY_CONSTRAINT_CYPHER)
        for cy in _ENSURE_INQUIRY_INDEXES_CYPHER:
            await neo4j_client.run_cypher(cy)
        logger.info("inquiry: 제약 + 인덱스 ensure 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("inquiry: 제약/인덱스 실패 (%s)", e)


# ===== 함수 =====


def _row_to_inquiry(row: Dict[str, Any]) -> Optional[Inquiry]:
    if not row or not row.get("id"):
        return None
    return Inquiry(
        id=row["id"],
        user_email=row.get("user_email") or "",
        user_name=row.get("user_name") or "",
        category=row.get("category") or "general",
        subject=row.get("subject") or "",
        body=row.get("body") or "",
        status=row.get("status") or "open",
        admin_reply=row.get("admin_reply") or "",
        admin_replied_by=row.get("admin_replied_by") or "",
        admin_replied_at=row.get("admin_replied_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


async def create_inquiry(
    *,
    user_email: str,
    user_name: str,
    category: str,
    subject: str,
    body: str,
) -> Optional[Inquiry]:
    """문의 작성. category 검증은 호출자(라우트) 책임."""
    if category not in INQUIRY_CATEGORIES:
        return None
    # 길이 trim — 호출자가 Pydantic 으로 1차 검증, 여기서 한 번 더
    rows = await neo4j_client.run_cypher(
        _CREATE_INQUIRY_CYPHER,
        {
            "user_email": user_email,
            "user_name": user_name or user_email.split("@")[0],
            "category": category,
            "subject": (subject or "").strip()[:MAX_SUBJECT_LENGTH],
            "body": (body or "").strip()[:MAX_BODY_LENGTH],
        },
    )
    if not rows:
        return None
    return _row_to_inquiry((rows[0] or {}).get("inquiry") or {})


async def get_inquiry(inquiry_id: str) -> Optional[Inquiry]:
    if not inquiry_id:
        return None
    rows = await neo4j_client.run_cypher(_GET_INQUIRY_CYPHER, {"id": inquiry_id})
    if not rows:
        return None
    return _row_to_inquiry((rows[0] or {}).get("inquiry") or {})


async def list_my_inquiries(email: str) -> List[Inquiry]:
    """내 문의 전체 — 최신순. 페이징 없음 (일반 사용자는 보통 10건 미만)."""
    if not email:
        return []
    rows = await neo4j_client.run_cypher(_LIST_MY_INQUIRIES_CYPHER, {"email": email})
    return [
        _row_to_inquiry((r or {}).get("inquiry") or {})
        for r in rows
        if (r or {}).get("inquiry")
    ]


async def list_admin_inquiries(
    *,
    status_filter: str = "",
    q: str = "",
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """admin 리스트 + 카운트."""
    params = {
        "status_filter": status_filter or "",
        "q": (q or "").strip(),
        "limit": max(1, min(200, int(limit))),
        "offset": max(0, int(offset)),
    }
    rows = await neo4j_client.run_cypher(_LIST_ADMIN_INQUIRIES_CYPHER, params)
    inquiries = [
        _row_to_inquiry((r or {}).get("inquiry") or {})
        for r in rows
        if (r or {}).get("inquiry")
    ]
    cnt_rows = await neo4j_client.run_cypher(_COUNT_ADMIN_INQUIRIES_CYPHER, params)
    total = int((cnt_rows[0] or {}).get("total", 0)) if cnt_rows else 0
    return {"inquiries": inquiries, "total": total}


async def count_by_status() -> Dict[str, int]:
    """상태별 카운트 — admin 상단 통계."""
    rows = await neo4j_client.run_cypher(_COUNT_BY_STATUS_CYPHER)
    out = {s: 0 for s in INQUIRY_STATUSES}
    for r in rows:
        s = r.get("status")
        if s in out:
            out[s] = int(r.get("cnt") or 0)
    out["total"] = sum(out.values())
    return out


async def update_inquiry(
    *,
    inquiry_id: str,
    status: Optional[str] = None,
    admin_reply: Optional[str] = None,
    admin_email: str = "",
) -> Optional[Inquiry]:
    """admin 갱신 — 상태 변경 또는 답변 작성. 빈 답변 ('') 으로 답변 초기화 가능.

    [정책]
    - status: None 이면 기존 유지. INQUIRY_STATUSES 검증은 호출자.
    - admin_reply: None 이면 기존 유지. 빈 문자열 '' 도 갱신 가능 (답변 삭제).
    - admin_reply 값이 비어있지 않으면 admin_replied_by/at 도 갱신.
    """
    if not inquiry_id:
        return None
    if status is not None and status not in INQUIRY_STATUSES:
        return None
    # admin_reply 길이 제한
    if admin_reply is not None and len(admin_reply) > MAX_REPLY_LENGTH:
        admin_reply = admin_reply[:MAX_REPLY_LENGTH]
    rows = await neo4j_client.run_cypher(
        _UPDATE_INQUIRY_CYPHER,
        {
            "id": inquiry_id,
            "status": status,
            "admin_reply": admin_reply,
            "admin_email": admin_email or "",
        },
    )
    if not rows:
        return None
    return _row_to_inquiry((rows[0] or {}).get("inquiry") or {})


# ===== 일괄 회신 (bulk reply) =====
# 같은 버그를 여러 사용자가 제보한 경우, 한 번에 답변 + 상태 + 이메일.
# DB 갱신은 UNWIND 한 쿼리로 (건별 왕복 X → 50건도 단일 트랜잭션).
# 이메일 발송은 라우트에서 asyncio 병렬 (DB 와 분리).


# id 목록으로 일괄 조회 — 변수 치환에 필요한 user_name/subject 확보용.
_GET_INQUIRIES_BY_IDS_CYPHER = """\
UNWIND $ids AS wanted
MATCH (i:Inquiry {id: wanted})
RETURN {
    id: i.id,
    user_email: i.user_email,
    user_name: i.user_name,
    category: i.category,
    subject: i.subject,
    body: i.body,
    status: i.status,
    admin_reply: COALESCE(i.admin_reply, ''),
    admin_replied_by: COALESCE(i.admin_replied_by, ''),
    admin_replied_at: toString(i.admin_replied_at),
    created_at: toString(i.created_at),
    updated_at: toString(i.updated_at)
} AS inquiry
"""


# 일괄 답변 갱신 — items=[{id, reply}]. 답변은 건별 개인화(치환 완료된 값)라
# UNWIND 로 각자 다른 reply 를 SET. 상태/답변자/시각은 공통.
_BULK_UPDATE_REPLIES_CYPHER = """\
UNWIND $items AS item
MATCH (i:Inquiry {id: item.id})
SET i.admin_reply = item.reply,
    i.admin_replied_by = $admin_email,
    i.admin_replied_at = datetime(),
    i.status = $status,
    i.updated_at = datetime()
RETURN {
    id: i.id,
    user_email: i.user_email,
    user_name: i.user_name,
    category: i.category,
    subject: i.subject,
    body: i.body,
    status: i.status,
    admin_reply: COALESCE(i.admin_reply, ''),
    admin_replied_by: COALESCE(i.admin_replied_by, ''),
    admin_replied_at: toString(i.admin_replied_at),
    created_at: toString(i.created_at),
    updated_at: toString(i.updated_at)
} AS inquiry
"""


async def get_inquiries_by_ids(ids: List[str]) -> List[Inquiry]:
    """id 목록으로 일괄 조회 (존재하는 것만 반환, 순서 보장 안 함)."""
    clean = [i for i in (ids or []) if i]
    if not clean:
        return []
    rows = await neo4j_client.run_cypher(_GET_INQUIRIES_BY_IDS_CYPHER, {"ids": clean})
    return [
        _row_to_inquiry((r or {}).get("inquiry") or {})
        for r in rows
        if (r or {}).get("inquiry")
    ]


async def bulk_update_replies(
    *,
    items: List[Dict[str, str]],
    status: str,
    admin_email: str = "",
) -> List[Inquiry]:
    """여러 문의에 개인화 답변을 일괄 적용 (UNWIND 한 쿼리).

    [정책]
    - items: [{"id": str, "reply": str}] — reply 는 호출자가 변수 치환 완료한 값.
    - status: 적용할 상태 (INQUIRY_STATUSES 검증은 여기서). 잘못되면 [] 반환.
    - reply 는 MAX_REPLY_LENGTH 로 trim. id 없는 항목은 스킵.
    - 반환: 실제 갱신된 Inquiry 리스트 (이메일 발송용 메타 포함).
    """
    if not items or status not in INQUIRY_STATUSES:
        return []
    safe_items = [
        {"id": it["id"], "reply": (it.get("reply") or "")[:MAX_REPLY_LENGTH]}
        for it in items
        if it.get("id")
    ]
    if not safe_items:
        return []
    rows = await neo4j_client.run_cypher(
        _BULK_UPDATE_REPLIES_CYPHER,
        {"items": safe_items, "status": status, "admin_email": admin_email or ""},
    )
    return [
        _row_to_inquiry((r or {}).get("inquiry") or {})
        for r in rows
        if (r or {}).get("inquiry")
    ]
