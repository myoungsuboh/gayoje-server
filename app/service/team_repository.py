"""
팀(Team) 관리 — 멀티유저 협업 레이어.

[설계]
팀은 개인 사용에 추가되는 선택적 레이어. 기존 개인 소유 프로젝트는 그대로 유지.
팀 프로젝트는 Project.team_id 로 팀과 연결됨.

노드/관계 모델:
  (:Team {id, name, created_at})
  (:User)-[:MEMBER {role, joined_at}]->(:Team)
  (:Invite {id, token, team_id, team_name, invitee_email, inviter_email,
            role, status, expires_at, created_at})

역할(role):
  owner  — 팀 생성자. 팀 삭제/역할 변경 가능. 유일 owner 탈퇴 시 admin 자동 승격.
  admin  — 멤버 초대/제거/역할 변경 가능.
  member — 팀 프로젝트 작업 가능.

멤버십 조건: 유료 플랜 (pro/pro_plus/pro_max) 필수.
  → 초대 수락 시 플랜 체크 → free 면 402 (업그레이드 유도).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import uuid4

from fastapi import HTTPException, status

from app.clients import neo4j_client
from app.core.subscription import PAID_SUBSCRIPTIONS

logger = logging.getLogger(__name__)

INVITE_EXPIRE_DAYS = 7

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
_ROLE_ORDER: dict[str, int] = {ROLE_OWNER: 3, ROLE_ADMIN: 2, ROLE_MEMBER: 1}

INVITE_PENDING = "pending"
INVITE_ACCEPTED = "accepted"
INVITE_DECLINED = "declined"
INVITE_EXPIRED = "expired"
INVITE_CANCELED = "canceled"


# ─── 제약 / 인덱스 ─────────────────────────────────────────────

_ENSURE_TEAM_ID_CONSTRAINT = """\
CREATE CONSTRAINT team_id_unique IF NOT EXISTS
FOR (t:Team) REQUIRE t.id IS UNIQUE
"""

_ENSURE_INVITE_TOKEN_CONSTRAINT = """\
CREATE CONSTRAINT invite_token_unique IF NOT EXISTS
FOR (i:Invite) REQUIRE i.token IS UNIQUE
"""

_ENSURE_INVITE_ID_CONSTRAINT = """\
CREATE CONSTRAINT invite_id_unique IF NOT EXISTS
FOR (i:Invite) REQUIRE i.id IS UNIQUE
"""

_ENSURE_TEAM_NAME_INDEX = """\
CREATE INDEX team_name_idx IF NOT EXISTS
FOR (t:Team) ON (t.name)
"""

_ENSURE_INVITE_INVITEE_INDEX = """\
CREATE INDEX invite_invitee_idx IF NOT EXISTS
FOR (i:Invite) ON (i.invitee_email)
"""


async def ensure_team_constraints() -> None:
    """Team/Invite 제약·인덱스 ensure. 부팅마다 idempotent 실행."""
    steps = [
        ("Team.id UNIQUE", _ENSURE_TEAM_ID_CONSTRAINT),
        ("Invite.token UNIQUE", _ENSURE_INVITE_TOKEN_CONSTRAINT),
        ("Invite.id UNIQUE", _ENSURE_INVITE_ID_CONSTRAINT),
        ("Team name index", _ENSURE_TEAM_NAME_INDEX),
        ("Invite invitee index", _ENSURE_INVITE_INVITEE_INDEX),
    ]
    for label, cypher in steps:
        try:
            await neo4j_client.run_cypher(cypher)
            logger.info("team: %s ensure 완료", label)
        except Exception as e:  # noqa: BLE001
            logger.warning("team: %s ensure 실패 (%s)", label, e)


# ─── 만료 초대 정리 ────────────────────────────────────────────

_CLEANUP_EXPIRED_INVITES_CYPHER = """\
MATCH (i:Invite {status: $pending})
WHERE i.expires_at < datetime()
SET i.status = $expired
RETURN count(i) AS cleaned
"""


async def cleanup_expired_invites() -> int:
    """만료된 pending 초대를 expired 로 일괄 처리. 부팅 훅 용."""
    try:
        rows = await neo4j_client.run_cypher(
            _CLEANUP_EXPIRED_INVITES_CYPHER,
            {"pending": INVITE_PENDING, "expired": INVITE_EXPIRED},
        )
        n = int((rows[0] or {}).get("cleaned", 0)) if rows else 0
        if n:
            logger.info("team: 만료 초대 %d건 정리", n)
        return n
    except Exception as e:  # noqa: BLE001
        logger.warning("team: 만료 초대 정리 실패 (%s)", e)
        return 0


# ─── 플랜 체크 헬퍼 ────────────────────────────────────────────

async def _assert_paid_plan(email: str) -> None:
    """유료 플랜 아니면 402 raise. 팀 생성/초대 수락 진입 시 호출."""
    from app.service.usage_repository import get_usage
    usage = await get_usage(email)
    sub = (usage.subscription_type if usage else "free")
    if sub not in PAID_SUBSCRIPTIONS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="팀 기능은 유료 플랜 (Pro 이상) 이 필요합니다.",
        )


# ─── 팀 CRUD ──────────────────────────────────────────────────

_CREATE_TEAM_CYPHER = """\
MATCH (u:User {email: $email})
CREATE (t:Team {id: $team_id, name: $name, created_at: datetime()})
CREATE (u)-[:MEMBER {role: $role_owner, joined_at: datetime()}]->(t)
RETURN t.id AS id, t.name AS name, toString(t.created_at) AS created_at
"""


async def create_team(owner_email: str, name: str) -> dict:
    """팀 생성. owner_email 유저가 owner 로 자동 등록. 유료 플랜 필수."""
    await _assert_paid_plan(owner_email)
    team_id = str(uuid4())
    rows = await neo4j_client.run_cypher(
        _CREATE_TEAM_CYPHER,
        {"email": owner_email, "team_id": team_id, "name": name, "role_owner": ROLE_OWNER},
    )
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="유저를 찾을 수 없습니다.")
    row = rows[0]
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"], "role": ROLE_OWNER}


_GET_TEAM_CYPHER = """\
MATCH (t:Team {id: $team_id})
RETURN t.id AS id, t.name AS name, toString(t.created_at) AS created_at
"""


async def get_team(team_id: str) -> Optional[dict]:
    rows = await neo4j_client.run_cypher(_GET_TEAM_CYPHER, {"team_id": team_id})
    if not rows:
        return None
    r = rows[0]
    return {"id": r["id"], "name": r["name"], "created_at": r["created_at"]}


_GET_TEAMS_FOR_USER_CYPHER = """\
MATCH (u:User {email: $email})-[m:MEMBER]->(t:Team)
RETURN t.id AS id, t.name AS name, toString(t.created_at) AS created_at,
       m.role AS role, toString(m.joined_at) AS joined_at
ORDER BY m.joined_at DESC
"""


async def get_teams_for_user(email: str) -> List[dict]:
    rows = await neo4j_client.run_cypher(_GET_TEAMS_FOR_USER_CYPHER, {"email": email})
    return [
        {"id": r["id"], "name": r["name"], "created_at": r["created_at"],
         "role": r["role"], "joined_at": r["joined_at"]}
        for r in (rows or [])
    ]


_UPDATE_TEAM_NAME_CYPHER = """\
MATCH (u:User {email: $email})-[m:MEMBER]->(t:Team {id: $team_id})
WHERE m.role IN $allowed_roles
SET t.name = $name
RETURN t.id AS id
"""


async def update_team(actor_email: str, team_id: str, name: str) -> dict:
    """팀 이름 수정. admin 이상만 가능."""
    rows = await neo4j_client.run_cypher(
        _UPDATE_TEAM_NAME_CYPHER,
        {"email": actor_email, "team_id": team_id, "name": name,
         "allowed_roles": [ROLE_OWNER, ROLE_ADMIN]},
    )
    if not rows:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="권한이 없거나 팀을 찾을 수 없습니다.")
    return {"id": team_id, "name": name}


_DELETE_TEAM_CYPHER = """\
MATCH (u:User {email: $email})-[:MEMBER {role: $role_owner}]->(t:Team {id: $team_id})
OPTIONAL MATCH (i:Invite {team_id: $team_id})
DETACH DELETE t, i
RETURN 1 AS deleted
"""


async def delete_team(actor_email: str, team_id: str) -> None:
    """팀 삭제. owner 만 가능. 팀 프로젝트는 team_id 가 남지만 팀 노드는 제거됨."""
    rows = await neo4j_client.run_cypher(
        _DELETE_TEAM_CYPHER,
        {"email": actor_email, "team_id": team_id, "role_owner": ROLE_OWNER},
    )
    # MATCH 가 아무것도 못 찾으면 rows = [] — owner 아니거나 팀 없음.
    # DETACH DELETE 후 count(t) 는 Neo4j 에서 0 을 반환하므로 rows 비어있는지로 판단.
    if not rows:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="권한이 없거나 팀을 찾을 수 없습니다.")


# ─── 멤버 조회/관리 ───────────────────────────────────────────

_GET_MEMBERS_CYPHER = """\
MATCH (u:User)-[m:MEMBER]->(t:Team {id: $team_id})
RETURN u.email AS email, u.name AS name,
       m.role AS role, toString(m.joined_at) AS joined_at
ORDER BY
  CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
  m.joined_at ASC
"""


async def get_members(team_id: str) -> List[dict]:
    rows = await neo4j_client.run_cypher(_GET_MEMBERS_CYPHER, {"team_id": team_id})
    return [
        {"email": r["email"], "name": r["name"],
         "role": r["role"], "joined_at": r["joined_at"]}
        for r in (rows or [])
    ]


_GET_MEMBER_ROLE_CYPHER = """\
MATCH (u:User {email: $email})-[m:MEMBER]->(t:Team {id: $team_id})
RETURN m.role AS role LIMIT 1
"""


async def get_member_role(email: str, team_id: str) -> Optional[str]:
    rows = await neo4j_client.run_cypher(_GET_MEMBER_ROLE_CYPHER, {"email": email, "team_id": team_id})
    return rows[0]["role"] if rows else None


async def is_member(email: str, team_id: str) -> bool:
    return await get_member_role(email, team_id) is not None


async def assert_team_role(email: str, team_id: str, min_role: str = ROLE_MEMBER) -> str:
    """
    유저가 min_role 이상인지 확인. 아니면 403 raise.
    Returns: 현재 role.
    """
    role = await get_member_role(email, team_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="팀 멤버가 아닙니다.")
    if _ROLE_ORDER.get(role, 0) < _ROLE_ORDER.get(min_role, 0):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="권한이 부족합니다.")
    return role


_CHANGE_MEMBER_ROLE_CYPHER = """\
MATCH (actor:User {email: $actor_email})-[am:MEMBER {role: $role_owner}]->(t:Team {id: $team_id})
MATCH (target:User {email: $target_email})-[tm:MEMBER]->(t)
WHERE $target_email <> $actor_email
  AND tm.role <> $role_owner
SET tm.role = $new_role
RETURN tm.role AS role
"""


async def change_member_role(actor_email: str, team_id: str, target_email: str, new_role: str) -> None:
    """역할 변경. owner 만 가능. target 이 owner 이거나 new_role 이 owner 이면 불가."""
    if new_role == ROLE_OWNER:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="owner 역할은 직접 지정 불가합니다.")
    rows = await neo4j_client.run_cypher(
        _CHANGE_MEMBER_ROLE_CYPHER,
        {"actor_email": actor_email, "team_id": team_id,
         "target_email": target_email, "new_role": new_role, "role_owner": ROLE_OWNER},
    )
    if not rows:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="권한이 없거나 대상을 찾을 수 없습니다.")


_REMOVE_MEMBER_CYPHER = """\
MATCH (target:User {email: $target_email})-[m:MEMBER]->(t:Team {id: $team_id})
DELETE m
RETURN 1 AS removed
"""

_COUNT_OWNERS_CYPHER = """\
MATCH (u:User)-[m:MEMBER {role: $role_owner}]->(t:Team {id: $team_id})
RETURN count(u) AS total
"""

_PROMOTE_OLDEST_ADMIN_CYPHER = """\
MATCH (u:User)-[m:MEMBER]->(t:Team {id: $team_id})
WHERE m.role = $role_admin
WITH u, m ORDER BY m.joined_at ASC LIMIT 1
SET m.role = $role_owner
RETURN u.email AS promoted
"""

_PROMOTE_OLDEST_MEMBER_CYPHER = """\
MATCH (u:User)-[m:MEMBER]->(t:Team {id: $team_id})
WHERE m.role = $role_member
WITH u, m ORDER BY m.joined_at ASC LIMIT 1
SET m.role = $role_owner
RETURN u.email AS promoted
"""


async def remove_member(actor_email: str, team_id: str, target_email: str) -> None:
    """
    멤버 제거. admin+ 는 member 제거 가능, owner 는 모두 제거 가능.
    본인 탈퇴(actor == target)도 허용. 유일 owner 탈퇴 시 자동 승격.
    """
    actor_role = await get_member_role(actor_email, team_id)
    if actor_role is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="팀 멤버가 아닙니다.")

    target_role = await get_member_role(target_email, team_id)
    if target_role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="대상 멤버를 찾을 수 없습니다.")

    # 권한 체크: 본인 탈퇴 or owner/admin 이 하위 역할 제거
    is_self = actor_email == target_email
    if not is_self:
        actor_order = _ROLE_ORDER.get(actor_role, 0)
        target_order = _ROLE_ORDER.get(target_role, 0)
        if actor_order <= target_order:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="권한이 부족합니다.")

    # owner 가 탈퇴/제거 시 자동 승격 처리
    if target_role == ROLE_OWNER:
        rows = await neo4j_client.run_cypher(_COUNT_OWNERS_CYPHER, {"team_id": team_id, "role_owner": ROLE_OWNER})
        owner_count = int((rows[0] or {}).get("total", 0)) if rows else 0
        if owner_count <= 1:
            # admin → owner 승격 시도, 없으면 member 승격
            promoted_rows = await neo4j_client.run_cypher(
                _PROMOTE_OLDEST_ADMIN_CYPHER,
                {"team_id": team_id, "role_admin": ROLE_ADMIN, "role_owner": ROLE_OWNER},
            )
            if not promoted_rows:
                promoted_rows = await neo4j_client.run_cypher(
                    _PROMOTE_OLDEST_MEMBER_CYPHER,
                    {"team_id": team_id, "role_member": ROLE_MEMBER, "role_owner": ROLE_OWNER},
                )
            if promoted_rows:
                logger.info("team: owner 탈퇴 → %s 자동 승격 (team_id=%s)", promoted_rows[0].get("promoted"), team_id)

    await neo4j_client.run_cypher(_REMOVE_MEMBER_CYPHER, {"target_email": target_email, "team_id": team_id})


# ─── 초대 ─────────────────────────────────────────────────────

_CREATE_INVITE_CYPHER = """\
CREATE (i:Invite {
  id: $invite_id,
  token: $token,
  team_id: $team_id,
  team_name: $team_name,
  invitee_email: $invitee_email,
  inviter_email: $inviter_email,
  role: $role,
  status: $status_pending,
  expires_at: datetime() + duration({days: $expire_days}),
  created_at: datetime()
})
RETURN i.token AS token, toString(i.expires_at) AS expires_at
"""

_GET_TEAM_NAME_CYPHER = """\
MATCH (t:Team {id: $team_id})
RETURN t.name AS name
"""

_GET_PENDING_INVITE_BY_EMAIL_CYPHER = """\
MATCH (i:Invite {team_id: $team_id, invitee_email: $invitee_email, status: $status_pending})
WHERE i.expires_at > datetime()
RETURN i.id AS id LIMIT 1
"""


async def create_invite(
    actor_email: str, team_id: str, invitee_email: str, role: str = ROLE_MEMBER
) -> dict:
    """초대 발행. admin 이상만 가능. invitee_email 에 이미 pending 초대가 있으면 재사용."""
    if role == ROLE_OWNER:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="owner 로는 초대할 수 없습니다.")

    await assert_team_role(actor_email, team_id, min_role=ROLE_ADMIN)

    # 이미 멤버인지 체크
    if await is_member(invitee_email, team_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 팀 멤버입니다.")

    # 기존 pending 초대가 있으면 중복 방지
    existing = await neo4j_client.run_cypher(
        _GET_PENDING_INVITE_BY_EMAIL_CYPHER,
        {"team_id": team_id, "invitee_email": invitee_email, "status_pending": INVITE_PENDING},
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 대기 중인 초대가 있습니다.")

    team_rows = await neo4j_client.run_cypher(_GET_TEAM_NAME_CYPHER, {"team_id": team_id})
    team_name = team_rows[0]["name"] if team_rows else ""

    token = str(uuid4())
    invite_id = str(uuid4())
    rows = await neo4j_client.run_cypher(
        _CREATE_INVITE_CYPHER,
        {
            "invite_id": invite_id,
            "token": token,
            "team_id": team_id,
            "team_name": team_name,
            "invitee_email": invitee_email,
            "inviter_email": actor_email,
            "role": role,
            "status_pending": INVITE_PENDING,
            "expire_days": INVITE_EXPIRE_DAYS,
        },
    )
    expires_at = rows[0]["expires_at"] if rows else None
    return {
        "token": token,
        "team_id": team_id,
        "team_name": team_name,
        "invitee_email": invitee_email,
        "role": role,
        "expires_at": expires_at,
    }


_GET_INVITE_BY_TOKEN_CYPHER = """\
MATCH (i:Invite {token: $token})
RETURN i.id AS id, i.token AS token, i.team_id AS team_id,
       i.team_name AS team_name, i.invitee_email AS invitee_email,
       i.inviter_email AS inviter_email, i.role AS role, i.status AS status,
       toString(i.expires_at) AS expires_at, toString(i.created_at) AS created_at
"""


async def get_invite_by_token(token: str) -> Optional[dict]:
    rows = await neo4j_client.run_cypher(_GET_INVITE_BY_TOKEN_CYPHER, {"token": token})
    return dict(rows[0]) if rows else None


_ACCEPT_INVITE_CYPHER = """\
MATCH (i:Invite {token: $token, status: $status_pending})
WHERE i.expires_at > datetime()
MATCH (u:User {email: $email})
MATCH (t:Team {id: i.team_id})
SET i.status = $status_accepted
CREATE (u)-[:MEMBER {role: i.role, joined_at: datetime()}]->(t)
RETURN i.team_id AS team_id, i.team_name AS team_name, i.role AS role
"""


async def accept_invite(token: str, user_email: str) -> dict:
    """
    초대 수락. 유료 플랜 체크 → 이메일 일치 체크 → 멤버 등록.
    free 플랜 → 402 (업그레이드 안내).
    """
    invite = await get_invite_by_token(token)
    if not invite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="초대를 찾을 수 없습니다.")

    if invite["status"] != INVITE_PENDING:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"이미 처리된 초대입니다. (status: {invite['status']})",
        )

    if invite["invitee_email"] and invite["invitee_email"] != user_email:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="이 초대는 다른 이메일로 발송되었습니다.")

    # 유료 플랜 체크 — 핵심 보안 게이트
    await _assert_paid_plan(user_email)

    # 이미 멤버인지 재확인 (TOCTOU 방어)
    if await is_member(user_email, invite["team_id"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 팀 멤버입니다.")

    rows = await neo4j_client.run_cypher(
        _ACCEPT_INVITE_CYPHER,
        {"token": token, "email": user_email,
         "status_pending": INVITE_PENDING, "status_accepted": INVITE_ACCEPTED},
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="초대가 만료되었거나 찾을 수 없습니다.",
        )
    r = rows[0]
    return {"team_id": r["team_id"], "team_name": r["team_name"], "role": r["role"]}


_DECLINE_INVITE_CYPHER = """\
MATCH (i:Invite {token: $token, status: $status_pending, invitee_email: $email})
SET i.status = $status_declined
RETURN i.id AS id
"""


async def decline_invite(token: str, user_email: str) -> None:
    rows = await neo4j_client.run_cypher(
        _DECLINE_INVITE_CYPHER,
        {"token": token, "email": user_email,
         "status_pending": INVITE_PENDING, "status_declined": INVITE_DECLINED},
    )
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="초대를 찾을 수 없거나 이미 처리되었습니다.")


_CANCEL_INVITE_CYPHER = """\
MATCH (i:Invite {token: $token, status: $status_pending})
MATCH (actor:User {email: $actor_email})-[m:MEMBER]->(t:Team {id: i.team_id})
WHERE m.role IN $allowed_roles
SET i.status = $status_canceled
RETURN i.id AS id
"""


async def cancel_invite(actor_email: str, token: str) -> None:
    """초대 취소. admin 이상만 가능."""
    rows = await neo4j_client.run_cypher(
        _CANCEL_INVITE_CYPHER,
        {
            "token": token, "actor_email": actor_email,
            "status_pending": INVITE_PENDING, "status_canceled": INVITE_CANCELED,
            "allowed_roles": [ROLE_OWNER, ROLE_ADMIN],
        },
    )
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="초대를 찾을 수 없거나 권한이 없습니다.")


_GET_PENDING_INVITES_FOR_TEAM_CYPHER = """\
MATCH (i:Invite {team_id: $team_id, status: $status_pending})
WHERE i.expires_at > datetime()
RETURN i.token AS token, i.invitee_email AS invitee_email,
       i.inviter_email AS inviter_email, i.role AS role,
       toString(i.expires_at) AS expires_at, toString(i.created_at) AS created_at
ORDER BY i.created_at DESC
"""


async def get_pending_invites(actor_email: str, team_id: str) -> List[dict]:
    """팀의 대기 중 초대 목록. admin 이상만 조회 가능."""
    await assert_team_role(actor_email, team_id, min_role=ROLE_ADMIN)
    rows = await neo4j_client.run_cypher(
        _GET_PENDING_INVITES_FOR_TEAM_CYPHER,
        {"team_id": team_id, "status_pending": INVITE_PENDING},
    )
    return [dict(r) for r in (rows or [])]
