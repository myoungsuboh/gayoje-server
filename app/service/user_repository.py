"""
User Repository — Neo4j 의 User 노드와 직접 통신.

[책임]
- User 노드 CRUD (Cypher 직접 실행)
- 응답을 UserInDB / UserPublic dataclass 로 정규화
- 비밀번호 해싱은 호출자(auth_service) 책임 — 이 모듈은 평문 비번을 다루지 않음
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel

from app.clients import neo4j_client
from app.core import token_encryption

logger = logging.getLogger(__name__)


# ===== 도메인 모델 =====
# Subscription 상수는 `app/core/subscription.py` 가 단일 source of truth.
# 여기서 re-export 하여 기존 import 경로 (from app.service.user_repository import SUBSCRIPTION_FREE)
# 호환 유지. lightweight 모듈 (예: app.core.quota) 은 직접 app.core.subscription 에서 import →
# token_encryption / config / settings 평가 chain 회피.
from app.core.subscription import (  # noqa: F401 — re-export
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    SUBSCRIPTION_TYPES,
)


class UserInDB(BaseModel):
    """Neo4j 에서 받아온 유저 원본 (hashed_password 포함)."""

    id: str
    email: str
    name: str
    hashed_password: str
    github_username: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    subscription_type: str = SUBSCRIPTION_FREE
    is_admin: bool = False
    # [2026-05] auto_progress — true 면 postMeeting 이 CPS+PRD 자동 체이닝.
    # false 면 CPS 만 생성 + 사용자가 PRD/Design 단계별 명시 트리거 (검수 게이트).
    # 미설정 사용자는 default true (기존 동작 호환).
    auto_progress: bool = True
    # [2026-06] locale — 미설정 시 'ko'.
    locale: str = "ko"
    # [2026-05-18] 관리자 계정 정지 — reversible. is_suspended=True 면 모든 인증 경로 차단.
    is_suspended: bool = False
    # JWT iat 비교 기준 — 정지 시점 이전 발급 토큰을 일괄 무효화하는 핵심 timestamp.
    suspended_at: Optional[str] = None
    suspended_reason: Optional[str] = None
    suspended_by_email: Optional[str] = None
    unsuspended_at: Optional[str] = None


class UserPublic(BaseModel):
    """외부 응답용 (비밀번호 해시 제거)."""

    id: str
    email: str
    name: str
    github_username: Optional[str] = None
    created_at: Optional[str] = None
    subscription_type: str = SUBSCRIPTION_FREE
    is_admin: bool = False
    # [2026-05] auto_progress — FE 가 stage 별 트리거 분기 결정에 사용.
    auto_progress: bool = True
    # [2026-06] locale — UI 표시 언어. 미설정 User 는 'ko' 기본값.
    locale: str = "ko"

    @classmethod
    def from_db(cls, db: UserInDB) -> "UserPublic":
        return cls(
            id=db.id,
            email=db.email,
            name=db.name,
            github_username=db.github_username,
            created_at=db.created_at,
            subscription_type=db.subscription_type,
            is_admin=db.is_admin,
            auto_progress=db.auto_progress,
            locale=db.locale,
        )


# ===== Cypher 상수 =====
# 모든 Cypher 는 ExecuteQuery 패턴. 인라인 템플릿 대신 표준 `$param` 바인딩 — Cypher injection 안전.

_CREATE_USER_CYPHER = """\
// 회원가입: User 노드 생성. email 은 UNIQUE 가정.
// Backend 에서 비밀번호를 bcrypt 로 해싱한 뒤 hashed_password 로 전달.
//
// [2026-05 monthly reset anchor]
// usage_reset_at 을 가입 시점에 +1mo 로 박아 둠 — "가입일 기준 매월 reset"
// 정책의 정확한 구현. 이전엔 첫 LLM 호출 시점에 박혀 last-access-anchored
// 였지만, 이제 캘린더 일관성 (가입 1/15 → reset 매월 15일) 확보.
// usage_repository 의 self-healing 분기 (reset_at NULL OR 지났으면 갱신) 가
// 그대로 작동 — 가입 후 1달 지나면 자동 재설정.
MERGE (u:User {email: $email})
ON CREATE SET
    u.id = randomUUID(),
    u.name = $name,
    u.hashed_password = $hashed_password,
    u.github_username = '',
    u.subscription_type = 'free',
    u.subscription_updated_at = datetime(),
    u.is_admin = false,
    u.created_at = datetime(),
    u.updated_at = datetime(),
    u.usage_reset_at = datetime() + duration({months: 1}),
    u.created = true
ON MATCH SET
    u.created = false
WITH u, u.created AS created
RETURN
    CASE WHEN created THEN
        { status: 'created', user: {
            id: u.id, email: u.email, name: u.name,
            github_username: u.github_username,
            subscription_type: COALESCE(u.subscription_type, 'free'),
            is_admin: COALESCE(u.is_admin, false),
            created_at: toString(u.created_at)
        } }
    ELSE
        { status: 'conflict', message: '이미 가입된 이메일입니다.' }
    END AS result
"""

_GET_USER_BY_EMAIL_CYPHER = """\
// 로그인 / /me 조회용: email 로 사용자 1명 반환
// [2026-05] auto_progress 노출 — FE 가 stage 별 자동 진행 여부 결정.
// [2026-06] locale 노출 — FE 가 UI 언어 결정.
MATCH (u:User {email: $email})
RETURN {
    id: u.id,
    email: u.email,
    name: u.name,
    hashed_password: u.hashed_password,
    github_username: COALESCE(u.github_username, ''),
    subscription_type: COALESCE(u.subscription_type, 'free'),
    is_admin: COALESCE(u.is_admin, false),
    auto_progress: COALESCE(u.auto_progress, true),
    locale: COALESCE(u.locale, 'ko'),
    is_suspended: COALESCE(u.is_suspended, false),
    suspended_at: toString(u.suspended_at),
    suspended_reason: COALESCE(u.suspended_reason, ''),
    suspended_by_email: COALESCE(u.suspended_by_email, ''),
    unsuspended_at: toString(u.unsuspended_at),
    created_at: toString(u.created_at),
    updated_at: toString(u.updated_at)
} AS user
"""

_MIGRATE_USER_DEFAULTS_CYPHER = """\
// 기존 User 에 subscription_type / is_admin / is_suspended default 채우기. Idempotent.
MATCH (u:User)
WHERE u.subscription_type IS NULL OR u.is_admin IS NULL OR u.is_suspended IS NULL
SET u.subscription_type = COALESCE(u.subscription_type, 'free'),
    u.subscription_updated_at = COALESCE(u.subscription_updated_at, datetime()),
    u.is_admin = COALESCE(u.is_admin, false),
    u.is_suspended = COALESCE(u.is_suspended, false)
RETURN count(u) AS migrated
"""

_PROMOTE_ADMIN_BY_EMAILS_CYPHER = """\
// .env 의 ADMIN_EMAILS 로 지정된 이메일을 부팅 시 자동 admin 으로 승격.
// 사용자가 아직 가입 전이면 아무 일도 안 일어남 — 가입 후 다음 부팅 때 적용.
UNWIND $emails AS adminEmail
MATCH (u:User {email: adminEmail})
SET u.is_admin = true,
    u.updated_at = datetime()
RETURN collect(u.email) AS promoted
"""

_GET_GITHUB_TOKEN_CYPHER = """\
// 사용자 OAuth access_token (암호화 상태) 조회 — GitHub API 프록시 호출 시 사용.
MATCH (u:User {email: $email})
RETURN COALESCE(u.github_access_token, '') AS token
"""

_SET_PASSWORD_CYPHER = """\
// 비밀번호 설정/변경. 비번 해싱은 호출자(auth_service or route) 책임.
MATCH (u:User {email: $email})
SET u.hashed_password = $hashed_password,
    u.updated_at = datetime()
RETURN u.email AS email
"""

_UPDATE_USER_CYPHER = """\
// 유저 정보 수정. name / github_username / auto_progress / locale 모두 선택적 (None 이면 갱신 안 함).
// COALESCE: 입력값이 빈 문자열이 아닌 경우만 덮어쓰고, 빈 문자열이면 기존값 유지.
// auto_progress 는 bool 이라 None 체크만 (false 도 유효한 갱신값).
MATCH (u:User {email: $email})
SET u.name = CASE WHEN $name IS NOT NULL AND $name <> '' THEN $name ELSE u.name END,
    u.github_username = CASE WHEN $github_username IS NOT NULL THEN $github_username ELSE COALESCE(u.github_username, '') END,
    u.auto_progress = CASE WHEN $auto_progress IS NOT NULL THEN $auto_progress ELSE COALESCE(u.auto_progress, true) END,
    u.locale = CASE WHEN $locale IS NOT NULL AND $locale <> '' THEN $locale ELSE COALESCE(u.locale, 'ko') END,
    u.updated_at = datetime()
RETURN {
    id: u.id,
    email: u.email,
    name: u.name,
    github_username: COALESCE(u.github_username, ''),
    subscription_type: COALESCE(u.subscription_type, 'free'),
    is_admin: COALESCE(u.is_admin, false),
    auto_progress: COALESCE(u.auto_progress, true),
    locale: COALESCE(u.locale, 'ko'),
    updated_at: toString(u.updated_at)
} AS user
"""

_DELETE_USER_CYPHER = """\
// 유저 탈퇴: 노드 + 모든 관계 + Vibe Repo + SubscriptionChange 이력까지 정리.
// last-admin 보호: 본인이 admin 이고 admin 이 1명뿐이면 거부 (status: 'last_admin').
//
// admin_count 계산은 본인 포함 (본인이 1명뿐인 admin 이면 admin_count=1).
// 본인이 admin 이 아닌 경우는 admin_count 와 무관하게 통과.
MATCH (u:User {email: $email})
OPTIONAL MATCH (a:User) WHERE a.is_admin = true
WITH u, count(a) AS admin_count
WITH u, admin_count,
     (COALESCE(u.is_admin, false) AND admin_count <= 1) AS would_orphan
CALL {
    WITH u, would_orphan
    WITH u WHERE NOT would_orphan
    OPTIONAL MATCH (u)-[:HAS_VIBE_REPO]->(r:VibeRepo {user_email: u.email})
    OPTIONAL MATCH (u)-[:SUBSCRIPTION_HISTORY]->(s:SubscriptionChange)
    WITH u, collect(DISTINCT r) AS vibeRepos, collect(DISTINCT s) AS subChanges, u.email AS deletedEmail
    FOREACH (vr IN vibeRepos | DETACH DELETE vr)
    FOREACH (sc IN subChanges | DETACH DELETE sc)
    DETACH DELETE u
    RETURN deletedEmail
}
RETURN CASE
    WHEN would_orphan THEN { status: 'last_admin', message: '마지막 관리자입니다. 탈퇴 전에 다른 관리자를 지정하세요.' }
    ELSE { status: 'deleted', email: $email }
END AS result
"""

_ENSURE_CONSTRAINTS_CYPHER = """\
// Idempotent: IF NOT EXISTS 로 중복 생성 에러 방지.
CREATE CONSTRAINT user_email_unique IF NOT EXISTS
FOR (u:User) REQUIRE u.email IS UNIQUE
"""

_ENSURE_GITHUB_ID_CONSTRAINT_CYPHER = """\
// GitHub id 는 유저당 1:1. NULL 은 허용 (Neo4j UNIQUE 의 표준 동작).
CREATE CONSTRAINT user_github_id_unique IF NOT EXISTS
FOR (u:User) REQUIRE u.github_id IS UNIQUE
"""

_ENSURE_GOOGLE_ID_CONSTRAINT_CYPHER = """\
// [2026-05] Google id (sub) 도 유저당 1:1. NULL 허용.
CREATE CONSTRAINT user_google_id_unique IF NOT EXISTS
FOR (u:User) REQUIRE u.google_id IS UNIQUE
"""

_ENSURE_VIBE_REPO_INDEX_CYPHER = """\
// VibeRepo 의 (user_email, url) 조회 성능을 위한 composite index.
CREATE INDEX vibe_repo_user_url IF NOT EXISTS
FOR (r:VibeRepo) ON (r.user_email, r.url)
"""

# ===== GitHub OAuth 연결 =====

_FIND_BY_GITHUB_ID_CYPHER = """\
// OAuth callback 시 이미 연결된 사용자가 있는지 확인.
MATCH (u:User {github_id: $github_id})
RETURN {
    id: u.id,
    email: u.email,
    name: u.name,
    github_username: COALESCE(u.github_username, ''),
    subscription_type: COALESCE(u.subscription_type, 'free'),
    is_admin: COALESCE(u.is_admin, false),
    created_at: toString(u.created_at)
} AS user
LIMIT 1
"""

_LINK_GITHUB_CYPHER = """\
// 기존 사용자(email 매칭) 에 GitHub 연결 + access_token 저장.
// github_id 가 이미 다른 User 에 연결돼 있으면 UNIQUE 제약 위반 → 호출자가 처리.
MATCH (u:User {email: $email})
SET u.github_id = $github_id,
    u.github_username = $github_username,
    u.github_access_token = $github_access_token,
    u.github_scopes = $github_scopes,
    u.github_linked_at = datetime(),
    u.updated_at = datetime()
RETURN {
    id: u.id,
    email: u.email,
    name: u.name,
    github_username: u.github_username,
    subscription_type: COALESCE(u.subscription_type, 'free'),
    is_admin: COALESCE(u.is_admin, false),
    created_at: toString(u.created_at)
} AS user
"""

_UNLINK_GITHUB_CYPHER = """\
MATCH (u:User {email: $email})
REMOVE u.github_id, u.github_access_token, u.github_scopes, u.github_linked_at
SET u.github_username = '',
    u.updated_at = datetime()
RETURN u.email AS email
"""

# ===== Notion OAuth (2026-05-17) =====
# GitHub 와 달리 login 모드 없음 — 로그인 사용자의 추가 연결만 지원.
# 토큰은 Fernet 암호화 후 저장 — notion API 호출 시 try_decrypt.
_LINK_NOTION_CYPHER = """\
MATCH (u:User {email: $email})
SET u.notion_access_token = $notion_access_token,
    u.notion_workspace_id = $notion_workspace_id,
    u.notion_workspace_name = $notion_workspace_name,
    u.notion_bot_id = $notion_bot_id,
    u.notion_linked_at = datetime(),
    u.updated_at = datetime()
RETURN u.email AS email,
       u.notion_workspace_name AS notion_workspace_name
"""

_UNLINK_NOTION_CYPHER = """\
MATCH (u:User {email: $email})
REMOVE u.notion_access_token,
       u.notion_workspace_id,
       u.notion_workspace_name,
       u.notion_bot_id,
       u.notion_linked_at,
       u.notion_export_map
SET u.updated_at = datetime()
RETURN u.email AS email
"""

_GET_NOTION_INFO_CYPHER = """\
MATCH (u:User {email: $email})
RETURN u.notion_workspace_id AS workspace_id,
       u.notion_workspace_name AS workspace_name,
       u.notion_bot_id AS bot_id,
       u.notion_access_token AS access_token_enc,
       toString(u.notion_linked_at) AS linked_at
"""

_CREATE_USER_FROM_GITHUB_CYPHER = """\
// OAuth 신규 가입: password 없음 (hashed_password = ''). 이메일 로그인 차단됨.
// [2026-05 monthly reset anchor] usage_reset_at 가입 시 초기화 — _CREATE_USER_CYPHER 와 동일 정책.
MERGE (u:User {email: $email})
ON CREATE SET
    u.id = randomUUID(),
    u.name = $name,
    u.hashed_password = '',
    u.github_id = $github_id,
    u.github_username = $github_username,
    u.github_access_token = $github_access_token,
    u.github_scopes = $github_scopes,
    u.github_linked_at = datetime(),
    u.subscription_type = 'free',
    u.subscription_updated_at = datetime(),
    u.is_admin = false,
    u.created_at = datetime(),
    u.updated_at = datetime(),
    u.usage_reset_at = datetime() + duration({months: 1}),
    u.created = true
ON MATCH SET
    u.created = false
WITH u, u.created AS created
RETURN
    CASE WHEN created THEN
        { status: 'created', user: {
            id: u.id, email: u.email, name: u.name,
            github_username: u.github_username,
            subscription_type: COALESCE(u.subscription_type, 'free'),
            is_admin: COALESCE(u.is_admin, false),
            created_at: toString(u.created_at)
        } }
    ELSE
        { status: 'exists', user: {
            id: u.id, email: u.email, name: u.name,
            github_username: COALESCE(u.github_username, ''),
            subscription_type: COALESCE(u.subscription_type, 'free'),
            is_admin: COALESCE(u.is_admin, false),
            created_at: toString(u.created_at)
        } }
    END AS result
"""


# ===== Google OAuth 연결 (2026-05) =====

_FIND_BY_GOOGLE_ID_CYPHER = """\
MATCH (u:User {google_id: $google_id})
RETURN {
    id: u.id,
    email: u.email,
    name: u.name,
    github_username: COALESCE(u.github_username, ''),
    google_email: COALESCE(u.google_email, ''),
    subscription_type: COALESCE(u.subscription_type, 'free'),
    is_admin: COALESCE(u.is_admin, false),
    created_at: toString(u.created_at)
} AS user
LIMIT 1
"""

_LINK_GOOGLE_CYPHER = """\
// 기존 사용자(email 매칭) 에 Google 연결. token 저장 안 함 (단발성).
MATCH (u:User {email: $email})
SET u.google_id = $google_id,
    u.google_email = $google_email,
    u.google_linked_at = datetime(),
    u.updated_at = datetime()
RETURN {
    id: u.id,
    email: u.email,
    name: u.name,
    github_username: COALESCE(u.github_username, ''),
    google_email: u.google_email,
    subscription_type: COALESCE(u.subscription_type, 'free'),
    is_admin: COALESCE(u.is_admin, false),
    created_at: toString(u.created_at)
} AS user
"""

_UNLINK_GOOGLE_CYPHER = """\
MATCH (u:User {email: $email})
REMOVE u.google_id, u.google_email, u.google_linked_at
SET u.updated_at = datetime()
RETURN u.email AS email
"""

_CREATE_USER_FROM_GOOGLE_CYPHER = """\
// OAuth 신규 가입 (Google): password 없음 → 이메일 로그인 차단.
MERGE (u:User {email: $email})
ON CREATE SET
    u.id = randomUUID(),
    u.name = $name,
    u.hashed_password = '',
    u.google_id = $google_id,
    u.google_email = $email,
    u.google_linked_at = datetime(),
    u.subscription_type = 'free',
    u.subscription_updated_at = datetime(),
    u.is_admin = false,
    u.created_at = datetime(),
    u.updated_at = datetime(),
    u.created = true
ON MATCH SET
    u.created = false
WITH u, u.created AS created
RETURN
    CASE WHEN created THEN
        { status: 'created', user: {
            id: u.id, email: u.email, name: u.name,
            github_username: COALESCE(u.github_username, ''),
            google_email: u.google_email,
            subscription_type: COALESCE(u.subscription_type, 'free'),
            is_admin: COALESCE(u.is_admin, false),
            created_at: toString(u.created_at)
        } }
    ELSE
        { status: 'exists', user: {
            id: u.id, email: u.email, name: u.name,
            github_username: COALESCE(u.github_username, ''),
            google_email: COALESCE(u.google_email, ''),
            subscription_type: COALESCE(u.subscription_type, 'free'),
            is_admin: COALESCE(u.is_admin, false),
            created_at: toString(u.created_at)
        } }
    END AS result
"""


# ===== Helpers =====


def _first_row(records: list[dict]) -> Optional[dict]:
    """Neo4j run_cypher 결과 list 의 첫 row. 비었으면 None."""
    if not records:
        return None
    return records[0]


# ===== CRUD =====


async def ensure_user_constraints() -> None:
    """
    App 부팅 시 1회 호출. User.email UNIQUE + VibeRepo composite index ensure.
    실패해도 부팅 막지 않음 (Neo4j 미연결 환경, e.g. 일부 테스트).
    """
    try:
        await neo4j_client.run_cypher(_ENSURE_CONSTRAINTS_CYPHER)
        await neo4j_client.run_cypher(_ENSURE_GITHUB_ID_CONSTRAINT_CYPHER)
        await neo4j_client.run_cypher(_ENSURE_GOOGLE_ID_CONSTRAINT_CYPHER)
        await neo4j_client.run_cypher(_ENSURE_VIBE_REPO_INDEX_CYPHER)
        logger.info("user_repository: 제약 + 인덱스 ensure 완료")
    except Exception as e:  # noqa: BLE001 — 부팅 가드
        logger.warning("user_repository: 제약/인덱스 생성 실패 (Neo4j 연결 확인): %s", e)


async def migrate_user_defaults() -> None:
    """기존 User 노드에 subscription_type / is_admin default 채우기. Idempotent."""
    try:
        records = await neo4j_client.run_cypher(_MIGRATE_USER_DEFAULTS_CYPHER)
        row = _first_row(records or [])
        migrated = (row or {}).get("migrated", 0)
        if migrated:
            logger.info("user_repository: subscription/is_admin default migration → %s 명", migrated)
    except Exception as e:  # noqa: BLE001 — 부팅 가드
        logger.warning("user_repository: default migration 실패: %s", e)


async def promote_admins_by_emails(emails: list[str]) -> list[str]:
    """ADMIN_EMAILS 에 해당하는 사용자를 admin 으로 승격. 가입 전이면 skip."""
    if not emails:
        return []
    try:
        records = await neo4j_client.run_cypher(
            _PROMOTE_ADMIN_BY_EMAILS_CYPHER, {"emails": [e.lower() for e in emails]}
        )
        row = _first_row(records or [])
        promoted = (row or {}).get("promoted") or []
        if promoted:
            logger.info("user_repository: 부팅 시 admin 승격 → %s", promoted)
        return promoted
    except Exception as e:  # noqa: BLE001 — 부팅 가드
        logger.warning("user_repository: admin 승격 실패: %s", e)
        return []


async def get_user_by_email(email: str) -> Optional[UserInDB]:
    """이메일로 유저 조회 (hashed_password 포함). 없으면 None."""
    records = await neo4j_client.run_cypher(
        _GET_USER_BY_EMAIL_CYPHER, {"email": email}
    )
    row = _first_row(records)
    if not row:
        return None
    user = row.get("user") or {}
    if not user.get("email"):
        return None

    return UserInDB(
        id=user.get("id", ""),
        email=user["email"],
        name=user.get("name", ""),
        hashed_password=user.get("hashed_password", ""),
        github_username=user.get("github_username") or None,
        created_at=user.get("created_at"),
        updated_at=user.get("updated_at"),
        subscription_type=user.get("subscription_type") or SUBSCRIPTION_FREE,
        is_admin=bool(user.get("is_admin")),
        # [2026-05] legacy 사용자 (field 없음) 는 default True — 기존 자동 흐름 보존.
        auto_progress=bool(user.get("auto_progress", True)),
        # [2026-06] legacy 사용자 (field 없음) 는 default 'ko'.
        locale=user.get("locale") or "ko",
        is_suspended=bool(user.get("is_suspended", False)),
        suspended_at=user.get("suspended_at") or None,
        suspended_reason=user.get("suspended_reason") or None,
        suspended_by_email=user.get("suspended_by_email") or None,
        unsuspended_at=user.get("unsuspended_at") or None,
    )


async def update_user(
    email: str,
    name: Optional[str] = None,
    github_username: Optional[str] = None,
    auto_progress: Optional[bool] = None,
    locale: Optional[str] = None,
) -> Optional[UserPublic]:
    """
    이름 / GitHub username / auto_progress / locale 변경. 전달된 필드만 갱신.

    Args:
        email: 대상 유저
        name: None 또는 빈 문자열이면 기존값 유지
        github_username: None 이면 기존값 유지, "" (빈 문자열) 이면 해제 (= clear)
        auto_progress: None 이면 기존값 유지. true/false 명시 시 갱신.
        locale: None 이면 기존값 유지. 지원값: ko | en | ja | zh.

    Returns:
        업데이트된 UserPublic, 없으면 None.
    """
    records = await neo4j_client.run_cypher(
        _UPDATE_USER_CYPHER,
        {
            "email": email,
            "name": name,
            "github_username": github_username,
            "auto_progress": auto_progress,
            "locale": locale,
        },
    )
    row = _first_row(records)
    if not row:
        return None
    user = row.get("user") or {}
    if not user.get("email"):
        return None
    return UserPublic(
        id=user.get("id", ""),
        email=user["email"],
        name=user.get("name", ""),
        github_username=user.get("github_username") or None,
        created_at=user.get("updated_at"),
        subscription_type=user.get("subscription_type") or SUBSCRIPTION_FREE,
        is_admin=bool(user.get("is_admin")),
        auto_progress=bool(user.get("auto_progress", True)),
        locale=user.get("locale") or "ko",
    )


async def set_password(email: str, hashed_password: str) -> bool:
    """
    비밀번호 설정 또는 변경. 호출자가 bcrypt 해싱 후 전달.
    OAuth-only 가입자(빈 hashed_password)의 비번 첫 설정에도 사용.
    """
    records = await neo4j_client.run_cypher(
        _SET_PASSWORD_CYPHER,
        {"email": email, "hashed_password": hashed_password},
    )
    row = _first_row(records)
    return bool(row and row.get("email"))


_LIST_OWNED_PROJECTS_CYPHER = """\
MATCH (u:User {email: $email})-[:OWNS]->(p:Project)
WHERE p.owner_email = $email
RETURN p.name AS name
"""


async def list_owned_project_names(email: str) -> list:
    """본인 단독 소유(개인) 프로젝트 이름 목록 — 탈퇴 시 데이터 파기 대상 (P0-5).

    owner_email 매칭으로 개인 소유만 — 팀 프로젝트는 협업자 보호로 제외(처리방침 예외).
    """
    records = await neo4j_client.run_cypher(_LIST_OWNED_PROJECTS_CYPHER, {"email": email})
    return [r.get("name") for r in (records or []) if r.get("name")]


async def delete_user(email: str) -> dict:
    """
    유저 삭제. User 노드 + 모든 관계 + Vibe Repo + SubscriptionChange 이력까지 제거.

    last-admin 보호: 본인이 유일한 admin 이면 'last_admin' 상태로 거부.

    Returns:
      { status: 'deleted' | 'last_admin' | 'not_found', message?: str, email?: str }

    Project 노드 (OWNS 관계 끝) 는 ownership_repository.remove_user_ownerships 또는
    delete_project 책임. 이 함수는 User 단위 데이터만 정리.
    """
    records = await neo4j_client.run_cypher(_DELETE_USER_CYPHER, {"email": email})
    row = _first_row(records)
    if not row:
        return {"status": "not_found"}
    result = row.get("result") or {}
    return {
        "status": result.get("status") or "not_found",
        "message": result.get("message"),
        "email": result.get("email"),
    }


# ===== GitHub OAuth =====


def _row_to_public(user: dict) -> Optional[UserPublic]:
    if not user or not user.get("email"):
        return None
    return UserPublic(
        id=user.get("id", ""),
        email=user["email"],
        name=user.get("name", ""),
        github_username=user.get("github_username") or None,
        created_at=user.get("created_at"),
        subscription_type=user.get("subscription_type") or SUBSCRIPTION_FREE,
        is_admin=bool(user.get("is_admin")),
    )


async def get_github_access_token(email: str) -> Optional[str]:
    """
    User 노드에 저장된 GitHub OAuth access_token 을 복호화하여 평문 반환.

    Returns:
      - 토큰이 있으면 평문 access_token (복호화 성공 시)
      - 토큰이 없거나(미연결) 복호화 실패 시 None
    """
    records = await neo4j_client.run_cypher(
        _GET_GITHUB_TOKEN_CYPHER, {"email": email}
    )
    row = _first_row(records)
    if not row:
        return None
    encrypted = row.get("token") or ""
    if not encrypted:
        return None
    return token_encryption.try_decrypt(encrypted)


async def find_by_github_id(github_id: int) -> Optional[UserPublic]:
    """OAuth callback 시 이미 연결된 사용자가 있는지 조회."""
    records = await neo4j_client.run_cypher(
        _FIND_BY_GITHUB_ID_CYPHER, {"github_id": int(github_id)}
    )
    row = _first_row(records)
    if not row:
        return None
    return _row_to_public(row.get("user") or {})


async def link_github(
    *,
    email: str,
    github_id: int,
    github_username: str,
    github_access_token: str,
    github_scopes: str = "",
) -> Optional[UserPublic]:
    """
    기존 사용자(email 매칭) 에 GitHub 연결. access_token 은 저장 직전 Fernet 암호화.

    Returns: UserPublic 또는 None (해당 email 의 user 가 없을 때).
    Raises: Cypher 가 UNIQUE 제약 위반을 던지면 호출자가 처리 (이미 다른 user 와 연결됨).
    """
    encrypted_token = token_encryption.encrypt(github_access_token)
    records = await neo4j_client.run_cypher(
        _LINK_GITHUB_CYPHER,
        {
            "email": email,
            "github_id": int(github_id),
            "github_username": github_username,
            "github_access_token": encrypted_token,
            "github_scopes": github_scopes,
        },
    )
    row = _first_row(records)
    if not row:
        return None
    return _row_to_public(row.get("user") or {})


async def unlink_github(email: str) -> bool:
    """GitHub 연결 해제. 노드는 유지되고 GitHub 관련 property 만 제거."""
    records = await neo4j_client.run_cypher(_UNLINK_GITHUB_CYPHER, {"email": email})
    row = _first_row(records)
    return bool(row and row.get("email"))


# ===== Notion (2026-05-17) =====


async def link_notion(
    *,
    email: str,
    notion_access_token: str,
    notion_workspace_id: str,
    notion_workspace_name: str,
    notion_bot_id: str,
) -> Optional[str]:
    """노션 연결 — access_token 은 Fernet 암호화 후 저장. workspace_name 반환."""
    encrypted_token = token_encryption.encrypt(notion_access_token)
    records = await neo4j_client.run_cypher(
        _LINK_NOTION_CYPHER,
        {
            "email": email,
            "notion_access_token": encrypted_token,
            "notion_workspace_id": notion_workspace_id or "",
            "notion_workspace_name": notion_workspace_name or "",
            "notion_bot_id": notion_bot_id or "",
        },
    )
    row = _first_row(records)
    if not row:
        return None
    return row.get("notion_workspace_name") or ""


async def unlink_notion(email: str) -> bool:
    """노션 연결 해제 — 토큰 + workspace 메타 제거."""
    records = await neo4j_client.run_cypher(_UNLINK_NOTION_CYPHER, {"email": email})
    row = _first_row(records)
    return bool(row and row.get("email"))


async def get_notion_info(email: str) -> Optional[dict]:
    """노션 연결 정보 + 복호화된 access_token. 미연결이면 None.

    응답 dict:
        {
            "workspace_id": str,
            "workspace_name": str,
            "bot_id": str,
            "access_token": str (복호화),
            "linked_at": str | None,
        }
    """
    records = await neo4j_client.run_cypher(
        _GET_NOTION_INFO_CYPHER, {"email": email}
    )
    row = _first_row(records)
    if not row or not row.get("access_token_enc"):
        return None
    return {
        "workspace_id": row.get("workspace_id") or "",
        "workspace_name": row.get("workspace_name") or "",
        "bot_id": row.get("bot_id") or "",
        "access_token": token_encryption.try_decrypt(row["access_token_enc"]),
        "linked_at": row.get("linked_at"),
    }


# ===== Notion export 페이지 매핑 (멱등 재공유용) =====
# 개인 프로젝트 동명 충돌을 피하려고 :Project 노드가 아닌 User 노드에 JSON 으로 저장.
# 구조: u.notion_export_map = '{ "<scoped_project>": {hub_page_id, cps_page_id,
#       prd_page_id, design_page_id, synced_at}, ... }'

_GET_EXPORT_MAP_CYPHER = """\
MATCH (u:User {email: $email})
RETURN u.notion_export_map AS map_json
"""

_SET_EXPORT_MAP_CYPHER = """\
MATCH (u:User {email: $email})
SET u.notion_export_map = $map_json,
    u.updated_at = datetime()
RETURN u.email AS email
"""


async def _load_notion_export_map(email: str) -> dict:
    """User 노드의 notion_export_map(JSON 문자열) → dict. 없거나 파싱 실패 시 {}."""
    import json

    records = await neo4j_client.run_cypher(_GET_EXPORT_MAP_CYPHER, {"email": email})
    row = _first_row(records)
    raw = row.get("map_json") if row else None
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def get_notion_export_map(email: str, project_name: str) -> Optional[dict]:
    """(email, project_name) 에 저장된 Notion export 페이지 매핑. 없으면 None.

    반환: {hub_page_id, cps_page_id, prd_page_id, design_page_id, synced_at} | None
    """
    m = await _load_notion_export_map(email)
    entry = m.get(project_name)
    return entry if isinstance(entry, dict) else None


async def save_notion_export_map(
    email: str,
    project_name: str,
    *,
    hub_page_id: Optional[str] = None,
    cps_page_id: Optional[str] = None,
    prd_page_id: Optional[str] = None,
    design_page_id: Optional[str] = None,
) -> None:
    """프로젝트별 Notion export 페이지 id 매핑 저장 (read-modify-write).

    None 인 인자는 기존 값 유지(coalesce). 매 호출 synced_at(epoch ms) 갱신.
    """
    import json
    import time

    m = await _load_notion_export_map(email)
    entry = dict(m.get(project_name) or {})
    if hub_page_id is not None:
        entry["hub_page_id"] = hub_page_id
    if cps_page_id is not None:
        entry["cps_page_id"] = cps_page_id
    if prd_page_id is not None:
        entry["prd_page_id"] = prd_page_id
    if design_page_id is not None:
        entry["design_page_id"] = design_page_id
    entry["synced_at"] = int(time.time() * 1000)
    m[project_name] = entry
    await neo4j_client.run_cypher(
        _SET_EXPORT_MAP_CYPHER,
        {"email": email, "map_json": json.dumps(m, ensure_ascii=False)},
    )


async def create_user_from_github(
    *,
    email: str,
    name: str,
    github_id: int,
    github_username: str,
    github_access_token: str,
    github_scopes: str = "",
) -> tuple[Optional[UserPublic], bool]:
    """
    OAuth 신규 가입 — password 없음. access_token 은 Fernet 암호화 후 저장.

    같은 email 이 이미 있으면 (`status='exists'`) 연결하지 않고 user 만 반환 →
    호출자가 link 모드로 다시 부르거나 사용자에게 안내.

    Returns:
        (user, is_new) — is_new=True 면 신규 생성. False 면 기존 노드.
    """
    encrypted_token = token_encryption.encrypt(github_access_token)
    records = await neo4j_client.run_cypher(
        _CREATE_USER_FROM_GITHUB_CYPHER,
        {
            "email": email,
            "name": name,
            "github_id": int(github_id),
            "github_username": github_username,
            "github_access_token": encrypted_token,
            "github_scopes": github_scopes,
        },
    )
    row = _first_row(records)
    if not row:
        return None, False
    result = row.get("result") or {}
    user = _row_to_public(result.get("user") or {})
    is_new = result.get("status") == "created"
    return user, is_new


# ===== Google OAuth (2026-05) =====


async def get_user_by_google_id(google_id: str) -> Optional[UserPublic]:
    """OAuth callback 시 이미 연결된 사용자가 있는지 조회."""
    records = await neo4j_client.run_cypher(
        _FIND_BY_GOOGLE_ID_CYPHER, {"google_id": str(google_id)}
    )
    row = _first_row(records)
    if not row:
        return None
    return _row_to_public(row.get("user") or {})


async def link_google(
    *, email: str, google_id: str, google_email: str,
) -> Optional[UserPublic]:
    """
    기존 사용자(email 매칭) 에 Google 연결. token 저장 안 함 (단발성 userinfo).

    Returns: UserPublic 또는 None (해당 email 의 user 가 없을 때).
    Raises: Cypher UNIQUE 제약 위반 (이미 다른 user 와 연결됨) — 호출자가 처리.
    """
    records = await neo4j_client.run_cypher(
        _LINK_GOOGLE_CYPHER,
        {"email": email, "google_id": str(google_id), "google_email": google_email},
    )
    row = _first_row(records)
    if not row:
        return None
    return _row_to_public(row.get("user") or {})


async def unlink_google(email: str) -> bool:
    """Google 연결 해제."""
    records = await neo4j_client.run_cypher(_UNLINK_GOOGLE_CYPHER, {"email": email})
    row = _first_row(records)
    return bool(row and row.get("email"))


async def create_user_from_google(
    *, email: str, name: str, google_id: str,
) -> tuple[Optional[UserPublic], bool]:
    """
    OAuth 신규 가입 (Google) — password 없음. 같은 email 기존 user 가 있으면 link 안 함.

    Returns:
        (user, is_new) — is_new=True 면 신규 생성. False 면 기존 노드 (호출자가
        link 로 처리하거나 안내).
    """
    records = await neo4j_client.run_cypher(
        _CREATE_USER_FROM_GOOGLE_CYPHER,
        {"email": email, "name": name, "google_id": str(google_id)},
    )
    row = _first_row(records)
    if not row:
        return None, False
    result = row.get("result") or {}
    user = _row_to_public(result.get("user") or {})
    is_new = result.get("status") == "created"
    return user, is_new


# ── last_active_at ──────────────────────────────────────────────────────────
_TOUCH_LAST_ACTIVE_CYPHER = """\
MATCH (u:User {email: $email})
SET u.last_active_at = datetime()
"""

async def touch_last_active(email: str) -> None:
    """로그인 / 토큰 갱신 시 last_active_at 를 현재 시각으로 갱신. Fail-silent."""
    try:
        await neo4j_client.run_cypher(_TOUCH_LAST_ACTIVE_CYPHER, {"email": email})
    except Exception as e:  # noqa: BLE001
        logger.warning("touch_last_active 실패 (email=%s): %s", email, e)
