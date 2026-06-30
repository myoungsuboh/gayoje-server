"""
팀 관리 API — 팀 CRUD / 멤버 관리 / 초대.

[엔드포인트]
  POST   /teams                              팀 생성 (유료 플랜 필수)
  GET    /teams                              내 팀 목록
  GET    /teams/{team_id}                    팀 상세 + 멤버
  PATCH  /teams/{team_id}                    팀 이름 수정 (admin+)
  DELETE /teams/{team_id}                    팀 삭제 (owner)
  GET    /teams/{team_id}/projects           팀 프로젝트 목록
  PATCH  /teams/{team_id}/members/{email}    역할 변경 (owner)
  DELETE /teams/{team_id}/members/{email}    멤버 제거 / 본인 탈퇴
  POST   /teams/{team_id}/invites            초대 발행 (admin+)
  GET    /teams/{team_id}/invites            대기 중 초대 목록 (admin+)
  DELETE /teams/{team_id}/invites/{token}    초대 취소 (admin+)
  GET    /invites/{token}                    초대 정보 조회 (공개 — 링크 클릭 시)
  POST   /invites/{token}/accept             초대 수락 (유료 플랜 필수)
  POST   /invites/{token}/decline            초대 거절
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.core.config import settings
from app.core.security import get_current_user
from app.service import ownership_repository, team_repository
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/teams", tags=["teams"])
invites_router = APIRouter(prefix="/api/invites", tags=["teams"])


# ─── 요청/응답 스키마 ─────────────────────────────────────────

class CreateTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class UpdateTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class TeamResponse(BaseModel):
    id: str
    name: str
    created_at: Optional[str] = None
    role: Optional[str] = None


class MemberResponse(BaseModel):
    email: str
    name: Optional[str] = None
    role: str
    joined_at: Optional[str] = None


class TeamDetailResponse(BaseModel):
    id: str
    name: str
    created_at: Optional[str] = None
    role: Optional[str] = None
    members: List[MemberResponse] = []


class ChangeMemberRoleRequest(BaseModel):
    role: str = Field(..., pattern="^(admin|member)$")


class InviteRequest(BaseModel):
    email: EmailStr
    role: str = Field(default="member", pattern="^(admin|member)$")


class InviteResponse(BaseModel):
    token: str
    team_id: str
    team_name: Optional[str] = None
    invitee_email: str
    role: str
    expires_at: Optional[str] = None
    invite_url: Optional[str] = None


class InviteInfoResponse(BaseModel):
    token: str
    team_id: str
    team_name: Optional[str] = None
    inviter_email: Optional[str] = None
    role: str
    status: str
    expires_at: Optional[str] = None


class TeamProjectResponse(BaseModel):
    id: Optional[str] = None
    name: str
    team_id: str
    created_at: Optional[str] = None


# ─── 팀 CRUD ──────────────────────────────────────────────────

@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    body: CreateTeamRequest,
    current_user: UserPublic = Depends(get_current_user),
):
    """팀 생성. 유료 플랜 필수 — free 유저 → 402."""
    result = await team_repository.create_team(current_user.email, body.name)
    return TeamResponse(**result)


@router.get("", response_model=List[TeamResponse])
async def list_my_teams(
    current_user: UserPublic = Depends(get_current_user),
):
    """내가 속한 팀 목록."""
    teams = await team_repository.get_teams_for_user(current_user.email)
    return [TeamResponse(**t) for t in teams]


@router.get("/{team_id}", response_model=TeamDetailResponse)
async def get_team(
    team_id: str,
    current_user: UserPublic = Depends(get_current_user),
):
    """팀 상세 + 멤버 목록. 팀 멤버만 조회 가능."""
    await team_repository.assert_team_role(current_user.email, team_id)
    team = await team_repository.get_team(team_id)
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="팀을 찾을 수 없습니다.")
    members = await team_repository.get_members(team_id)
    role = await team_repository.get_member_role(current_user.email, team_id)
    return TeamDetailResponse(
        **team,
        role=role,
        members=[MemberResponse(**m) for m in members],
    )


@router.patch("/{team_id}", response_model=TeamResponse)
async def update_team(
    team_id: str,
    body: UpdateTeamRequest,
    current_user: UserPublic = Depends(get_current_user),
):
    """팀 이름 수정. admin 이상만 가능."""
    result = await team_repository.update_team(current_user.email, team_id, body.name)
    return TeamResponse(id=result["id"], name=result["name"])


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: str,
    current_user: UserPublic = Depends(get_current_user),
):
    """팀 삭제. owner 만 가능."""
    await team_repository.delete_team(current_user.email, team_id)


# ─── 팀 프로젝트 목록 ─────────────────────────────────────────

@router.get("/{team_id}/projects", response_model=List[TeamProjectResponse])
async def list_team_projects(
    team_id: str,
    current_user: UserPublic = Depends(get_current_user),
):
    """팀 프로젝트 목록. 팀 멤버만 조회 가능."""
    await team_repository.assert_team_role(current_user.email, team_id)
    projects = await ownership_repository.list_team_projects(current_user.email, team_id)
    return [TeamProjectResponse(**p) for p in projects]


# ─── 멤버 관리 ────────────────────────────────────────────────

@router.patch("/{team_id}/members/{target_email}", status_code=status.HTTP_200_OK)
async def change_member_role(
    team_id: str,
    target_email: str,
    body: ChangeMemberRoleRequest,
    current_user: UserPublic = Depends(get_current_user),
):
    """멤버 역할 변경. owner 만 가능. owner 역할로 지정 불가."""
    await team_repository.change_member_role(current_user.email, team_id, target_email, body.role)
    return {"email": target_email, "role": body.role}


@router.delete("/{team_id}/members/{target_email}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    team_id: str,
    target_email: str,
    current_user: UserPublic = Depends(get_current_user),
):
    """멤버 제거. admin+ 는 하위 역할 제거 가능, 본인 탈퇴도 허용."""
    await team_repository.remove_member(current_user.email, team_id, target_email)


# ─── 초대 관리 (팀 컨텍스트) ──────────────────────────────────

@router.post("/{team_id}/invites", response_model=InviteResponse, status_code=status.HTTP_201_CREATED)
async def create_invite(
    team_id: str,
    body: InviteRequest,
    current_user: UserPublic = Depends(get_current_user),
):
    """초대 발행 + 이메일 발송. admin 이상만 가능."""
    result = await team_repository.create_invite(
        current_user.email, team_id, str(body.email), body.role
    )
    # 초대 이메일 발송 (RESEND_API_KEY 미설정이면 silent skip)
    await _send_invite_email(result)
    invite_url = _build_invite_url(result["token"])
    return InviteResponse(**result, invite_url=invite_url)


@router.get("/{team_id}/invites", response_model=List[InviteResponse])
async def list_pending_invites(
    team_id: str,
    current_user: UserPublic = Depends(get_current_user),
):
    """대기 중 초대 목록. admin 이상만 조회 가능."""
    invites = await team_repository.get_pending_invites(current_user.email, team_id)
    return [
        InviteResponse(**i, invite_url=_build_invite_url(i["token"]))
        for i in invites
    ]


@router.delete("/{team_id}/invites/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_invite(
    team_id: str,
    token: str,
    current_user: UserPublic = Depends(get_current_user),
):
    """초대 취소. admin 이상만 가능."""
    await team_repository.cancel_invite(current_user.email, token)


# ─── 초대 수락/거절 (공개 — 토큰 기반) ─────────────────────────

@invites_router.get("/{token}", response_model=InviteInfoResponse)
async def get_invite_info(token: str):
    """초대 정보 조회. 로그인 없이 접근 가능 — 초대 링크 랜딩 페이지용."""
    invite = await team_repository.get_invite_by_token(token)
    if not invite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="초대를 찾을 수 없습니다.")
    return InviteInfoResponse(
        token=invite["token"],
        team_id=invite["team_id"],
        team_name=invite.get("team_name"),
        inviter_email=invite.get("inviter_email"),
        role=invite["role"],
        status=invite["status"],
        expires_at=invite.get("expires_at"),
    )


@invites_router.post("/{token}/accept", status_code=status.HTTP_200_OK)
async def accept_invite(
    token: str,
    current_user: UserPublic = Depends(get_current_user),
):
    """
    초대 수락. 유료 플랜 필수 — free 유저 → 402.
    수락 성공 시 팀 정보 반환.
    """
    result = await team_repository.accept_invite(token, current_user.email)
    return {
        "team_id": result["team_id"],
        "team_name": result["team_name"],
        "role": result["role"],
        "message": f"'{result['team_name']}' 팀에 참여했습니다.",
    }


@invites_router.post("/{token}/decline", status_code=status.HTTP_200_OK)
async def decline_invite(
    token: str,
    current_user: UserPublic = Depends(get_current_user),
):
    """초대 거절."""
    await team_repository.decline_invite(token, current_user.email)
    return {"message": "초대를 거절했습니다."}


# ─── 이메일 발송 헬퍼 ─────────────────────────────────────────

def _build_invite_url(token: str) -> str:
    return f"{settings.FRONTEND_URL}/invite/{token}"


async def _send_invite_email(invite: dict) -> None:
    """초대 이메일 발송. 실패해도 초대 자체는 유효 (best-effort)."""
    from app.core import email as email_lib
    from app.service import notification_log_repository as nlog

    invite_url = _build_invite_url(invite["token"])
    team_name = invite.get("team_name", "팀")
    invitee = invite["invitee_email"]
    role_label = {"admin": "관리자", "member": "멤버"}.get(invite["role"], invite["role"])

    subject = f"[Harness] {team_name} 팀에 초대되었습니다"
    html = f"""
<p>안녕하세요,</p>
<p><b>{team_name}</b> 팀에 <b>{role_label}</b>으로 초대되었습니다.</p>
<p>아래 링크를 클릭하여 초대를 수락하세요 (7일 내 유효):</p>
<p><a href="{invite_url}">{invite_url}</a></p>
<p>초대를 원하지 않으시면 무시하셔도 됩니다.</p>
<p>— Harness 팀</p>
"""
    text = f"{team_name} 팀 초대: {invite_url}"

    try:
        await email_lib.send_email(
            to=invitee,
            subject=subject,
            html=html,
            text=text,
            kind=nlog.KIND_OTHER,
            log_context={"team_id": invite["team_id"], "role": invite["role"]},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("team invite email 발송 실패 (invitee=%s): %s", invitee, e)
