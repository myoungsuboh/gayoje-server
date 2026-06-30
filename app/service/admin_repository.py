"""
관리자 전용 Neo4j 쿼리 — 사용자 목록/검색/상세, 구독 변경 + 이력, admin 토글.

[설계]
- 사용자 목록은 단순 LIMIT/OFFSET 페이지네이션 (초기 운영 규모 < 수천 명 가정).
- 구독 변경 시 (:SubscriptionChange) 노드를 별도로 생성하고
  (:User)-[:SUBSCRIPTION_HISTORY]->(:SubscriptionChange) 로 연결.
  향후 promotion / discount / coupon 같은 더 풍부한 변경 사유로 확장 가능.
- admin 토글 시 본인이 본인을 강등하는 것을 차단 (라우트 단에서 가드).
- 마지막 admin 1명만 남았을 때 그 admin 을 강등하는 것도 차단 (last-admin 보호).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.clients import neo4j_client
from app.core import quota
from app.service.user_repository import SUBSCRIPTION_FREE, SUBSCRIPTION_TYPES

logger = logging.getLogger(__name__)


# ===== 도메인 모델 =====


class AdminUserRow(BaseModel):
    """관리자 화면 테이블 행."""
    id: str
    email: str
    name: str
    github_username: Optional[str] = None
    subscription_type: str = SUBSCRIPTION_FREE
    subscription_updated_at: Optional[str] = None
    # [2026-06] 기간제 부여 만료 시점(ISO). None = 영구. FE 가 "N일 후 Free" 표시.
    subscription_ends_at: Optional[str] = None
    is_admin: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # [2026-05-18] 정지 상태
    is_suspended: bool = False
    suspended_at: Optional[str] = None
    suspended_reason: Optional[str] = None
    suspended_by_email: Optional[str] = None
    unsuspended_at: Optional[str] = None
    # 비밀번호 가입자 vs OAuth-only — FE 가 "비번 초기화" 버튼 활성화 여부 판단.
    has_password: bool = False
    # [2026-05-27] 이번 cycle 토큰 사용량 (관리자 % 표시). 목록 쿼리에서만 채워지고,
    # usage_total_tokens 가 없는 detail/change 응답에선 None 유지.
    token_used: Optional[int] = None
    token_limit: Optional[int] = None
    token_pct: Optional[float] = None


class SubscriptionChangeRow(BaseModel):
    """구독 변경 이력 1건."""
    id: str
    from_type: str
    to_type: str
    reason: Optional[str] = None
    changed_by_email: Optional[str] = None
    changed_at: Optional[str] = None


class AdminUserDetail(BaseModel):
    """사용자 상세 — 테이블 행 + 부가 통계."""
    user: AdminUserRow
    github_linked_at: Optional[str] = None
    vibe_repo_count: int = 0
    meeting_upload_count: int = 0
    project_count: int = 0
    subscription_history: List[SubscriptionChangeRow] = []


# ===== Cypher =====


_ENSURE_SUBSCRIPTION_CHANGE_INDEX_CYPHER = """\
// 사용자별 변경 이력 정렬용 인덱스. changed_at desc 조회 빈도가 높음.
CREATE INDEX subscription_change_user_email IF NOT EXISTS
FOR (s:SubscriptionChange) ON (s.user_email, s.changed_at)
"""

_LIST_USERS_CYPHER = """\
// 사용자 목록 (검색 + 페이지네이션). q 가 비면 전체.
MATCH (u:User)
WHERE $q = '' OR toLower(u.email) CONTAINS $q OR toLower(u.name) CONTAINS $q
   OR toLower(COALESCE(u.github_username, '')) CONTAINS $q
WITH u
ORDER BY u.created_at DESC
SKIP $offset
LIMIT $limit
RETURN {
    id: u.id,
    email: u.email,
    name: u.name,
    github_username: COALESCE(u.github_username, ''),
    subscription_type: COALESCE(u.subscription_type, 'free'),
    subscription_updated_at: toString(u.subscription_updated_at),
    subscription_ends_at: toString(u.subscription_ends_at),
    is_admin: COALESCE(u.is_admin, false),
    created_at: toString(u.created_at),
    updated_at: toString(u.updated_at),
    is_suspended: COALESCE(u.is_suspended, false),
    suspended_at: toString(u.suspended_at),
    suspended_reason: COALESCE(u.suspended_reason, ''),
    suspended_by_email: COALESCE(u.suspended_by_email, ''),
    unsuspended_at: toString(u.unsuspended_at),
    has_password: COALESCE(u.hashed_password, '') <> '',
    usage_total_tokens: COALESCE(u.usage_total_tokens, 0)
} AS user
"""

_COUNT_USERS_CYPHER = """\
MATCH (u:User)
WHERE $q = '' OR toLower(u.email) CONTAINS $q OR toLower(u.name) CONTAINS $q
   OR toLower(COALESCE(u.github_username, '')) CONTAINS $q
RETURN count(u) AS total
"""

_COUNT_ADMINS_CYPHER = """\
MATCH (u:User) WHERE u.is_admin = true
RETURN count(u) AS admins
"""

_GET_USER_DETAIL_CYPHER = """\
MATCH (u:User {email: $email})
OPTIONAL MATCH (u)-[:HAS_VIBE_REPO]->(vr:VibeRepo)
WITH u, count(DISTINCT vr) AS vibe_count
OPTIONAL MATCH (u)-[:UPLOADED_MEETING_LOG]->(m:MeetingUpload)
WITH u, vibe_count, count(DISTINCT m) AS meeting_count
OPTIONAL MATCH (u)-[:OWNS]->(p:Project)
WITH u, vibe_count, meeting_count, count(DISTINCT p) AS project_count
RETURN {
    user: {
        id: u.id,
        email: u.email,
        name: u.name,
        github_username: COALESCE(u.github_username, ''),
        subscription_type: COALESCE(u.subscription_type, 'free'),
        subscription_updated_at: toString(u.subscription_updated_at),
        subscription_ends_at: toString(u.subscription_ends_at),
        is_admin: COALESCE(u.is_admin, false),
        created_at: toString(u.created_at),
        updated_at: toString(u.updated_at),
        is_suspended: COALESCE(u.is_suspended, false),
        suspended_at: toString(u.suspended_at),
        suspended_reason: COALESCE(u.suspended_reason, ''),
        suspended_by_email: COALESCE(u.suspended_by_email, ''),
        unsuspended_at: toString(u.unsuspended_at),
        has_password: COALESCE(u.hashed_password, '') <> ''
    },
    github_linked_at: toString(u.github_linked_at),
    vibe_repo_count: vibe_count,
    meeting_upload_count: meeting_count,
    project_count: project_count
} AS detail
"""

_LIST_SUBSCRIPTION_HISTORY_CYPHER = """\
// 사용자의 구독 변경 이력 (최신순). 페이지네이션은 limit 만.
MATCH (u:User {email: $email})-[:SUBSCRIPTION_HISTORY]->(s:SubscriptionChange)
RETURN {
    id: s.id,
    from_type: s.from_type,
    to_type: s.to_type,
    reason: COALESCE(s.reason, ''),
    changed_by_email: COALESCE(s.changed_by_email, ''),
    changed_at: toString(s.changed_at)
} AS change
ORDER BY s.changed_at DESC
LIMIT $limit
"""

_CHANGE_SUBSCRIPTION_CYPHER = """\
// 구독 변경 + 이력 노드 생성. from_type 은 현재 값에서 채움.
//
// [2026-05 월간 reset 정책 정합성]
// 등급 변경 시 카운터(usage_meeting_count / usage_total_tokens / usage_total_chars)는
// 그대로 유지 (사용자 가치 보존 — 결제 직후 "사용량 0/N" 으로 보여서 손해본 느낌 방지).
// 반면 reset_at 은 now+1mo 로 갱신 — 새 등급의 한도가 적용되는 새 cycle 시작 명시.
//
// 시나리오:
//   Free 4/5 → Pro 결제: 카운터 4 유지(4/100), reset_at 새로 30일. (가치 보존)
//   Pro 50/100 → Free 강등: 카운터 50 유지(50/5 = 즉시 차단). admin reset_usage 가 해소.
//   Pro+ 150/200 → Pro 다운그레이드: 카운터 150 유지(150/100 = 즉시 차단). 동상.
//
// 강등 시 사용자 즉시 차단되는 위험은 의식적 trade-off — 강등은 거의 admin 수동 작업
// 이라 운영자가 reset 수동 호출로 정리 가능.
MATCH (u:User {email: $email})
WITH u, COALESCE(u.subscription_type, 'free') AS from_type
SET u.subscription_type = $to_type,
    u.subscription_updated_at = datetime(),
    // [2026-06] 기간제 부여 — duration_months 만큼 후 만료(usage 경로 self-heal 이 free 로 강등).
    //   duration_months NULL = 영구(Paddle 웹훅 경로 포함). Free 강등 시엔 만료 무의미 → null.
    u.subscription_ends_at = CASE
        WHEN $duration_months IS NULL OR $to_type = 'free' THEN null
        ELSE datetime() + duration({months: $duration_months})
    END,
    u.usage_reset_at = datetime() + duration({months: 1}),
    u.updated_at = datetime()
CREATE (s:SubscriptionChange {
    id: randomUUID(),
    user_email: u.email,
    from_type: from_type,
    to_type: $to_type,
    ends_at: u.subscription_ends_at,
    reason: $reason,
    changed_by_email: $changed_by_email,
    changed_at: datetime()
})
CREATE (u)-[:SUBSCRIPTION_HISTORY]->(s)
RETURN {
    user: {
        id: u.id,
        email: u.email,
        name: u.name,
        github_username: COALESCE(u.github_username, ''),
        subscription_type: u.subscription_type,
        subscription_updated_at: toString(u.subscription_updated_at),
        subscription_ends_at: toString(u.subscription_ends_at),
        is_admin: COALESCE(u.is_admin, false),
        created_at: toString(u.created_at),
        updated_at: toString(u.updated_at),
        is_suspended: COALESCE(u.is_suspended, false),
        suspended_at: toString(u.suspended_at),
        suspended_reason: COALESCE(u.suspended_reason, ''),
        suspended_by_email: COALESCE(u.suspended_by_email, ''),
        unsuspended_at: toString(u.unsuspended_at),
        has_password: COALESCE(u.hashed_password, '') <> ''
    },
    change: {
        id: s.id,
        from_type: s.from_type,
        to_type: s.to_type,
        reason: s.reason,
        changed_by_email: s.changed_by_email,
        changed_at: toString(s.changed_at)
    }
} AS result
"""

_SET_ADMIN_CYPHER = """\
// admin 토글 — atomic last-admin 보호.
// 동시 요청 race condition 방어: count + condition + SET 을 단일 cypher 안에서 처리.
// (라우트 단의 두 단계 검증은 두 어드민이 동시에 서로를 강등할 때 둘 다 통과될 수 있음.)
//
// 강등 (is_admin=false) 인데 대상이 현재 admin 이고 admin 총수가 1이면 거부.
MATCH (u:User {email: $email})
OPTIONAL MATCH (a:User) WHERE a.is_admin = true
WITH u, count(a) AS admin_count
WITH u, admin_count,
     ($is_admin = false AND COALESCE(u.is_admin, false) AND admin_count <= 1) AS would_orphan
CALL {
    WITH u, would_orphan
    WITH u WHERE NOT would_orphan
    SET u.is_admin = $is_admin, u.updated_at = datetime()
    RETURN u AS updated
}
RETURN CASE
    WHEN would_orphan THEN {
        status: 'last_admin',
        message: '마지막 관리자입니다. 다른 관리자를 먼저 지정한 뒤 해제하세요.'
    }
    ELSE {
        status: 'ok',
        user: {
            id: u.id,
            email: u.email,
            name: u.name,
            github_username: COALESCE(u.github_username, ''),
            subscription_type: COALESCE(u.subscription_type, 'free'),
            subscription_updated_at: toString(u.subscription_updated_at),
            subscription_ends_at: toString(u.subscription_ends_at),
            is_admin: u.is_admin,
            created_at: toString(u.created_at),
            updated_at: toString(u.updated_at),
            is_suspended: COALESCE(u.is_suspended, false),
            suspended_at: toString(u.suspended_at),
            suspended_reason: COALESCE(u.suspended_reason, ''),
            suspended_by_email: COALESCE(u.suspended_by_email, ''),
            unsuspended_at: toString(u.unsuspended_at),
            has_password: COALESCE(u.hashed_password, '') <> ''
        }
    }
END AS result
"""


_SUSPEND_USER_CYPHER = """\
// 계정 정지 — atomic last-admin 보호.
// 대상이 active admin 이고, 현재 active admin 수 <= 1 이면 거부.
// active admin = is_admin=true AND is_suspended=false.
MATCH (u:User {email: $email})
OPTIONAL MATCH (a:User)
  WHERE a.is_admin = true AND COALESCE(a.is_suspended, false) = false
WITH u, count(a) AS active_admin_count
WITH u, active_admin_count,
     (COALESCE(u.is_admin, false)
      AND COALESCE(u.is_suspended, false) = false
      AND active_admin_count <= 1) AS would_orphan
CALL {
    WITH u, would_orphan
    WITH u WHERE NOT would_orphan
    SET u.is_suspended = true,
        u.suspended_at = datetime(),
        u.suspended_reason = $reason,
        u.suspended_by_email = $by,
        u.updated_at = datetime()
    RETURN u AS updated
}
RETURN CASE
    WHEN would_orphan THEN {
        status: 'last_admin',
        message: '마지막 관리자입니다. 다른 관리자를 먼저 지정한 뒤 정지하세요.'
    }
    ELSE {
        status: 'ok',
        user: {
            id: u.id, email: u.email, name: u.name,
            github_username: COALESCE(u.github_username, ''),
            subscription_type: COALESCE(u.subscription_type, 'free'),
            subscription_updated_at: toString(u.subscription_updated_at),
            subscription_ends_at: toString(u.subscription_ends_at),
            is_admin: COALESCE(u.is_admin, false),
            created_at: toString(u.created_at),
            updated_at: toString(u.updated_at),
            is_suspended: u.is_suspended,
            suspended_at: toString(u.suspended_at),
            suspended_reason: COALESCE(u.suspended_reason, ''),
            suspended_by_email: COALESCE(u.suspended_by_email, ''),
            unsuspended_at: toString(u.unsuspended_at),
            has_password: COALESCE(u.hashed_password, '') <> ''
        }
    }
END AS result
"""


_UNSUSPEND_USER_CYPHER = """\
// 정지 해제 — last-admin 보호 불필요 (해제는 admin 수를 줄이지 않음).
// suspended_reason/by 는 보존 (이력 참고). unsuspended_at 갱신.
MATCH (u:User {email: $email})
SET u.is_suspended = false,
    u.unsuspended_at = datetime(),
    u.updated_at = datetime()
RETURN {
    status: 'ok',
    user: {
        id: u.id, email: u.email, name: u.name,
        github_username: COALESCE(u.github_username, ''),
        subscription_type: COALESCE(u.subscription_type, 'free'),
        subscription_updated_at: toString(u.subscription_updated_at),
        subscription_ends_at: toString(u.subscription_ends_at),
        is_admin: COALESCE(u.is_admin, false),
        created_at: toString(u.created_at),
        updated_at: toString(u.updated_at),
        is_suspended: false,
        suspended_at: toString(u.suspended_at),
        suspended_reason: COALESCE(u.suspended_reason, ''),
        suspended_by_email: COALESCE(u.suspended_by_email, ''),
        unsuspended_at: toString(u.unsuspended_at),
        has_password: COALESCE(u.hashed_password, '') <> ''
    }
} AS result
"""


# ===== 함수 =====


async def ensure_subscription_history_index() -> None:
    try:
        await neo4j_client.run_cypher(_ENSURE_SUBSCRIPTION_CHANGE_INDEX_CYPHER)
        logger.info("admin_repository: SubscriptionChange 인덱스 ensure 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("admin_repository: 인덱스 생성 실패: %s", e)


def _token_usage_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """row 에 usage_total_tokens 가 있으면 등급 한도 대비 토큰 사용 요약을 채운다.
    detail/change 응답처럼 usage_total_tokens 가 없으면 빈 dict → 모델 default(None)."""
    used = row.get("usage_total_tokens")
    if used is None:
        return {}
    return quota.token_usage_summary(
        used, row.get("subscription_type") or SUBSCRIPTION_FREE
    )


def _row_to_admin_user(row: Dict[str, Any]) -> Optional[AdminUserRow]:
    if not row or not row.get("email"):
        return None
    return AdminUserRow(
        id=row.get("id", ""),
        email=row["email"],
        name=row.get("name", ""),
        github_username=row.get("github_username") or None,
        subscription_type=row.get("subscription_type") or SUBSCRIPTION_FREE,
        subscription_updated_at=row.get("subscription_updated_at"),
        subscription_ends_at=row.get("subscription_ends_at"),
        is_admin=bool(row.get("is_admin")),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        is_suspended=bool(row.get("is_suspended", False)),
        suspended_at=row.get("suspended_at") or None,
        suspended_reason=row.get("suspended_reason") or None,
        suspended_by_email=row.get("suspended_by_email") or None,
        unsuspended_at=row.get("unsuspended_at") or None,
        has_password=bool(row.get("has_password", False)),
        **_token_usage_fields(row),
    )


async def list_users(
    q: str = "", limit: int = 50, offset: int = 0
) -> Dict[str, Any]:
    q_norm = (q or "").strip().lower()
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    rows = await neo4j_client.run_cypher(
        _LIST_USERS_CYPHER, {"q": q_norm, "limit": limit, "offset": offset}
    )
    users: List[AdminUserRow] = []
    for r in rows or []:
        u = _row_to_admin_user(r.get("user") or {})
        if u:
            users.append(u)

    count_rows = await neo4j_client.run_cypher(_COUNT_USERS_CYPHER, {"q": q_norm})
    total = int(((count_rows or [{}])[0]).get("total") or 0)

    return {"users": users, "total": total, "limit": limit, "offset": offset}


async def get_user_detail(email: str) -> Optional[AdminUserDetail]:
    rows = await neo4j_client.run_cypher(_GET_USER_DETAIL_CYPHER, {"email": email})
    if not rows:
        return None
    detail = (rows[0] or {}).get("detail") or {}
    user = _row_to_admin_user(detail.get("user") or {})
    if not user:
        return None

    history_rows = await neo4j_client.run_cypher(
        _LIST_SUBSCRIPTION_HISTORY_CYPHER, {"email": email, "limit": 50}
    )
    history: List[SubscriptionChangeRow] = []
    for r in history_rows or []:
        c = r.get("change") or {}
        if c.get("id"):
            history.append(
                SubscriptionChangeRow(
                    id=c.get("id", ""),
                    from_type=c.get("from_type") or SUBSCRIPTION_FREE,
                    to_type=c.get("to_type") or SUBSCRIPTION_FREE,
                    reason=c.get("reason") or None,
                    changed_by_email=c.get("changed_by_email") or None,
                    changed_at=c.get("changed_at"),
                )
            )

    return AdminUserDetail(
        user=user,
        github_linked_at=detail.get("github_linked_at"),
        vibe_repo_count=int(detail.get("vibe_repo_count") or 0),
        meeting_upload_count=int(detail.get("meeting_upload_count") or 0),
        project_count=int(detail.get("project_count") or 0),
        subscription_history=history,
    )


async def change_subscription(
    *,
    target_email: str,
    to_type: str,
    reason: Optional[str],
    changed_by_email: str,
    duration_months: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """구독 변경 + 이력 노드 생성. 대상 사용자가 없으면 None.

    duration_months: 기간제 부여 — N개월 후 free 로 자동 강등(usage 경로 self-heal).
                     None = 영구(Paddle 웹훅 경로 기본). to_type=free 면 무시(만료 없음).
    """
    if to_type not in SUBSCRIPTION_TYPES:
        raise ValueError(f"invalid subscription_type: {to_type}")
    rows = await neo4j_client.run_cypher(
        _CHANGE_SUBSCRIPTION_CYPHER,
        {
            "email": target_email,
            "to_type": to_type,
            "reason": reason or "",
            "changed_by_email": changed_by_email,
            "duration_months": duration_months,
        },
    )
    if not rows:
        return None
    result = (rows[0] or {}).get("result") or {}
    user = _row_to_admin_user(result.get("user") or {})
    if not user:
        return None
    change = result.get("change") or {}
    return {
        "user": user,
        "change": SubscriptionChangeRow(
            id=change.get("id", ""),
            from_type=change.get("from_type") or SUBSCRIPTION_FREE,
            to_type=change.get("to_type") or SUBSCRIPTION_FREE,
            reason=change.get("reason") or None,
            changed_by_email=change.get("changed_by_email") or None,
            changed_at=change.get("changed_at"),
        ),
    }


async def count_admins() -> int:
    rows = await neo4j_client.run_cypher(_COUNT_ADMINS_CYPHER)
    return int(((rows or [{}])[0]).get("admins") or 0)


async def set_admin(
    *, target_email: str, is_admin: bool
) -> Dict[str, Any]:
    """
    admin 토글. last-admin 보호는 cypher 안에서 atomic 처리.

    Returns:
      { status: 'ok' | 'last_admin' | 'not_found', user?: AdminUserRow, message?: str }
    """
    rows = await neo4j_client.run_cypher(
        _SET_ADMIN_CYPHER, {"email": target_email, "is_admin": bool(is_admin)}
    )
    if not rows:
        return {"status": "not_found"}
    result = (rows[0] or {}).get("result") or {}
    status = result.get("status")
    if status == "last_admin":
        return {"status": "last_admin", "message": result.get("message")}
    if status == "ok":
        user = _row_to_admin_user(result.get("user") or {})
        if not user:
            return {"status": "not_found"}
        return {"status": "ok", "user": user}
    return {"status": "not_found"}


async def suspend_user(
    *, target_email: str, reason: Optional[str], by_admin_email: str,
) -> Dict[str, Any]:
    """
    사용자 정지. atomic last-admin 보호.

    Returns:
      { status: 'ok' | 'last_admin' | 'not_found',
        user?: AdminUserRow, message?: str }
    """
    rows = await neo4j_client.run_cypher(
        _SUSPEND_USER_CYPHER,
        {"email": target_email, "reason": (reason or ""), "by": by_admin_email},
    )
    if not rows:
        return {"status": "not_found"}
    result = (rows[0] or {}).get("result") or {}
    s = result.get("status")
    if s == "last_admin":
        return {"status": "last_admin", "message": result.get("message")}
    if s == "ok":
        user = _row_to_admin_user(result.get("user") or {})
        if not user:
            return {"status": "not_found"}
        return {"status": "ok", "user": user}
    return {"status": "not_found"}


async def unsuspend_user(*, target_email: str) -> Dict[str, Any]:
    """
    사용자 정지 해제. suspended_reason / suspended_by_email 은 보존 (이력 참고).

    Returns:
      { status: 'ok' | 'not_found', user?: AdminUserRow }
    """
    rows = await neo4j_client.run_cypher(
        _UNSUSPEND_USER_CYPHER, {"email": target_email}
    )
    if not rows:
        return {"status": "not_found"}
    result = (rows[0] or {}).get("result") or {}
    user = _row_to_admin_user(result.get("user") or {})
    if not user:
        return {"status": "not_found"}
    return {"status": "ok", "user": user}


# ── 대시보드 KPI: DAU / WAU / MAU ──────────────────────────────────────────
_ACTIVE_STATS_CYPHER = """\
MATCH (u:User)
WHERE u.last_active_at IS NOT NULL
RETURN
  count(CASE WHEN u.last_active_at >= datetime() - duration({days: 1})   THEN 1 END) AS dau,
  count(CASE WHEN u.last_active_at >= datetime() - duration({days: 7})   THEN 1 END) AS wau,
  count(CASE WHEN u.last_active_at >= datetime() - duration({days: 30})  THEN 1 END) AS mau,
  count(u) AS tracked
"""

_TOTAL_USERS_CYPHER = """\
MATCH (u:User)
RETURN count(u) AS total
"""


class ActiveStats(BaseModel):
    dau: int = 0
    wau: int = 0
    mau: int = 0
    total_users: int = 0


async def get_active_stats() -> ActiveStats:
    """DAU / WAU / MAU + 전체 사용자 수. last_active_at 미설정 유저는 0 처리."""
    try:
        rows = await neo4j_client.run_cypher(_ACTIVE_STATS_CYPHER)
        row = (rows or [{}])[0]
        total_rows = await neo4j_client.run_cypher(_TOTAL_USERS_CYPHER)
        total = int(((total_rows or [{}])[0]).get("total") or 0)
        return ActiveStats(
            dau=int(row.get("dau") or 0),
            wau=int(row.get("wau") or 0),
            mau=int(row.get("mau") or 0),
            total_users=total,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("get_active_stats 실패: %s", e)
        return ActiveStats()
