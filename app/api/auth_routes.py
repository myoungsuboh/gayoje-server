"""
인증 라우트 — 회원가입 / 로그인 / 로그아웃 / 토큰 갱신 / 내 정보 + 라이브러리.

[동작]
- signup: bcrypt 해싱 후 Neo4j 에 직접 User 노드 생성
- login:  Neo4j 에서 user 조회 → bcrypt 검증 → access + refresh JWT 발급
- logout: access token jti 를 블랙리스트 등록. body 에 refresh_token
          포함 시 함께 무효화 → 재발급 경로 차단.
- me / update / delete: Neo4j 직접 조회·수정·삭제
- me/projects: 내가 OWNS 한 프로젝트 목록
- me/library:  내 Vibe Repo 라이브러리 CRUD
"""
import logging
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field

from app.core import github_oauth, google_oauth, quota
from app.core.config import settings
from app.core.limiter import limiter
from app.core.security import (
    create_access_token,
    create_refresh_token,
    get_current_user,
)
from app.schemas import (
    AccessTokenResponse,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    TokenResponse,
    UpdateMeRequest,
    UserResponse,
)
from app.clients import neo4j_client
from app.pipelines.base import Neo4jClientProxy, PipelineContext
from app.pipelines.delete_pipeline import delete_project
from app.service import (
    meeting_upload_repository as uploads,
    ownership_repository,
    usage_repository,
    user_repository as users,
    vibe_repo_repository as library,
)
from app.service.auth_service import login, logout, refresh_access_token
from app.service.meeting_upload_repository import (
    MeetingUploadDetail,
    MeetingUploadInput,
    MeetingUploadMeta,
)
from app.service.user_repository import UserPublic, touch_last_active
from app.service.vibe_repo_repository import VibeRepoInput, VibeRepoOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=True)


class LogoutRequest(BaseModel):
    """
    로그아웃 body — refresh_token 동봉 시 함께 무효화.
    """

    refresh_token: Optional[str] = Field(default=None)


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login_route(request: Request, payload: LoginRequest):
    """로그인 → access + refresh token 발급."""
    user, access, refresh = await login(payload)
    # [Phase 3 동시접속] 활성 세션 등록 — list_sessions / 강제 로그아웃에서 사용.
    from app.service.session_helper import record_access_token_session
    await record_access_token_session(access, request=request)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=UserResponse(**user.model_dump()),
    )


@router.post("/logout", response_model=MessageResponse)
async def logout_route(
    token: str = Depends(oauth2_scheme),
    body: Optional[LogoutRequest] = Body(default=None),
):
    """
    현재 access token 을 즉시 무효화 (jti 블랙리스트 등록).
    body.refresh_token 이 있으면 함께 무효화.
    """
    refresh_token = body.refresh_token if body else None
    await logout(token, refresh_token)
    return MessageResponse(message="로그아웃 되었습니다.")


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh_route(request: Request, payload: RefreshRequest):
    """
    refresh token → 새 (access, refresh) 페어 발급.

    [2026-05 회전 (rotation) 적용]
    - 응답에 새 refresh_token 포함 → FE 가 localStorage 의 refresh 도 함께 갱신.
    - 이전 refresh 는 즉시 blacklist 등록 → 동일 토큰으로 두 번째 호출은 401.
      탈취 시 한 번 쓰면 즉시 무효화되어 정상 사용자도 401 받으며 탈취 감지.

    [Phase 3 동시접속]
    새 access token 의 jti 도 활성 세션 등록 — 같은 디바이스의 토큰 회전 추적.
    """
    new_access, new_refresh = await refresh_access_token(payload.refresh_token)
    from app.service.session_helper import record_access_token_session
    await record_access_token_session(new_access, request=request)
    return AccessTokenResponse(access_token=new_access, refresh_token=new_refresh)


@router.get("/me", response_model=UserResponse)
async def me_route(current_user: UserPublic = Depends(get_current_user)):
    """현재 로그인된 사용자 정보."""
    return UserResponse(**current_user.model_dump())


# ─── /me/sessions — 활성 세션 가시화 + 강제 로그아웃 (Phase 3) ────


class ActiveSessionView(BaseModel):
    """FE 가 사용자에게 표시할 활성 세션 메타."""
    jti: str
    user_agent: str = ""
    ip: str = ""
    created_at: int = 0
    device_label: str = ""
    is_current: bool = False


class ActiveSessionsResponse(BaseModel):
    sessions: list[ActiveSessionView]
    current_jti: Optional[str] = None  # 현재 요청 jti — FE 가 "이 디바이스" 표시


@router.get("/me/sessions", response_model=ActiveSessionsResponse)
async def list_sessions_route(
    token: str = Depends(oauth2_scheme),
    current_user: UserPublic = Depends(get_current_user),
) -> ActiveSessionsResponse:
    """
    내 활성 세션 목록 — PC + 모바일 동시 접속 가시화.

    [Phase 3 — 2026-05-18]
    사용자가 "지금 어디서 로그인 중인지" 확인 → 분실/공유 디바이스 강제 로그아웃 가능.

    [응답]
    sessions: 활성 세션 array (최신순)
    current_jti: 이 요청을 보낸 세션의 jti — FE 가 "현재 디바이스" 라벨 표시
    """
    from app.core import session_registry
    from app.core.security import decode_token_lenient

    sessions = await session_registry.list_sessions(current_user.email)
    # 현재 요청 jti 식별
    payload = decode_token_lenient(token)
    current_jti = payload.get("jti") if payload else None

    return ActiveSessionsResponse(
        sessions=[
            ActiveSessionView(
                jti=s.jti,
                user_agent=s.user_agent,
                ip=s.ip,
                created_at=s.created_at,
                device_label=s.device_label,
                is_current=(s.jti == current_jti),
            )
            for s in sessions
        ],
        current_jti=current_jti,
    )


@router.delete("/me/sessions/{jti}", response_model=MessageResponse)
async def revoke_session_route(
    jti: str,
    current_user: UserPublic = Depends(get_current_user),
) -> MessageResponse:
    """
    특정 활성 세션 강제 로그아웃 — jti 블랙리스트 + 세션 레지스트리 정리.

    [보안 — ownership 검증]
    jti 의 소유자가 호출자와 같아야 함. 다른 사용자의 jti 면 403.

    [효과]
    - token_blacklist.revoke → 그 jti 의 access token 사용 시 401 응답
    - session_registry.unregister → list_sessions 응답에서 사라짐
    """
    import time
    from app.core import session_registry, token_blacklist

    owner_email = await session_registry.get_session_email(jti)
    if owner_email is None:
        # 이미 만료/제거된 세션 — 멱등 응답 (사용자 입장에서 이미 끝남)
        return MessageResponse(message="세션이 이미 종료되었습니다.")
    if owner_email.lower() != current_user.email.lower():
        # 정보 누설 방지: 404 로 응답 (jti 존재 여부 추측 못 하게)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="세션을 찾을 수 없습니다.",
        )

    # 안전 마진: 세션 exp 정보가 없으므로 1시간 후 만료로 등록 (실제 토큰 만료까지 차단 보장).
    # token_blacklist 가 EXPIREAT 으로 자동 정리.
    one_hour_from_now = int(time.time()) + 3600
    await token_blacklist.revoke(jti, one_hour_from_now)
    await session_registry.unregister_session(jti)
    return MessageResponse(message="세션이 종료되었습니다.")


# ─── /me/usage — quota 사용량 ──────────────────────────────


class _UsageLimits(BaseModel):
    """등급별 한도. FE 가 진행바 maxValue 로 사용."""

    meeting_logs: int = Field(..., description="postMeeting 등록 가능 횟수 (월간)")
    summary_chars: int = Field(..., description="회의록 한 번 입력 최대 글자수 (per-request)")
    total_tokens: int = Field(..., description="LLM 누적 토큰 한도 (월간)")


class _UsageCounters(BaseModel):
    """현재 누적 사용량. summary_chars 는 per-request 한도라 누적값 없음 (모니터링용 total_chars 만)."""

    meeting_logs: int = Field(..., description="현재 누적 postMeeting 횟수")
    total_tokens: int = Field(..., description="현재 누적 LLM 토큰")
    total_chars: int = Field(..., description="현재 누적 회의록 입력 글자수 (통계만)")


class _LiteUsage(BaseModel):
    """[2026-06] 메인 소진 후 Lite 오버플로우 사용량/캡. FE 가 'Lite 모드' 표시 + 넛지 판단.

    daily_cap=0 → 오버플로우 불가(Free 하드월). >0 이면 메인 소진 후 Lite 로 계속 작업.
    overflow_active=True → 지금 메인을 이미 소진해 Lite 모드로 동작 중.
    """

    daily_used: int = Field(..., description="이번 주기 사용한 Lite 토큰 (롤링 7일 — 필드명은 호환 유지)")
    daily_cap: int = Field(..., description="Lite 주간 캡 (0=오버플로우 없음/하드월)")
    monthly_used: int = Field(..., description="이번 cycle Lite 누적 (리포팅)")
    overflow_active: bool = Field(..., description="메인 소진으로 현재 Lite 모드인지")
    daily_reset_at: Optional[str] = Field(default=None, description="다음 Lite 주간 reset 시점 (ISO)")


class MyUsageResponse(BaseModel):
    """GET /auth/me/usage — FE 대시보드 카드용.

    [정책 — 2026-05 월간 reset]
    가입일 기준 매월 자동 reset. reset_at 은 ISO datetime 으로 다음 reset 시점.
    summary_chars 는 per-request 한도 (누적 아님) — limits 에만 노출.

    [2026-06 Lite 오버플로우] 메인(total_tokens) 소진 후 Lite 모델로 작업 지속.
    lite 섹션이 주간 사용/캡을 노출 — FE 가 'Lite 모드' 배지 + 70% 넛지 표시.
    """

    subscription_type: str = Field(..., description="'free' | 'pro' | 'pro_plus' | 'pro_max'")
    limits: _UsageLimits
    usage: _UsageCounters
    lite: _LiteUsage
    reset_at: Optional[str] = Field(
        default=None,
        description="다음 자동 reset 시점 (ISO datetime). 매월 정기 reset. "
                    "null 이면 첫 호출 시점 (BE가 자동 초기화 처리 후 다음 호출부터 값 표시).",
    )
    subscription_ends_at: Optional[str] = Field(
        default=None,
        description="관리자 기간제 부여 만료 시점 (ISO datetime). null 이면 만료 없음(영구/Free).",
    )


@router.get("/me/usage", response_model=MyUsageResponse)
async def my_usage_route(
    current_user: UserPublic = Depends(get_current_user),
) -> MyUsageResponse:
    """
    현재 사용자의 quota 사용량 + 등급별 한도.

    FE 가 대시보드 카드에서 호출. 응답:
      - subscription_type: free / pro / pro_plus / pro_max
      - limits: 등급별 한도 (meeting_logs / summary_chars / total_tokens)
      - usage: 현재 누적 (meeting_logs / total_tokens / total_chars) — 월간 reset
      - reset_at: 다음 자동 reset 시점 (ISO datetime)
    """
    # [2026-06-11] admin 이 한도를 바꾸면 가드는 15s TTL 로 전 프로세스 전파되지만,
    # 이 표시 라우트는 부팅 캐시를 읽어 "관리자 변경이 일괄 반영 안 되는" 표시 갭이 있었다.
    await quota.ensure_overrides_fresh()

    usage = await usage_repository.get_usage(current_user.email)
    # 인증 통과 + User 노드 없는 비정상 — 보수적으로 free + 0 응답 (FE 가 빈 카드 렌더).
    if usage is None:
        subscription_type = users.SUBSCRIPTION_FREE
        meeting_count = 0
        total_tokens = 0
        total_chars = 0
        reset_at = None
        lite_daily = 0
        lite_monthly = 0
        lite_daily_reset_at = None
        subscription_ends_at = None
    else:
        subscription_type = usage.subscription_type
        meeting_count = usage.meeting_count
        total_tokens = usage.total_tokens
        total_chars = usage.total_chars
        # [2026-05 월간 reset] get_usage 호출이 cypher 안에서 self-healing reset 처리.
        # 첫 호출 직후라면 usage.reset_at 에 이미 다음 reset 시점 박혀 있음.
        reset_at = usage.reset_at
        lite_daily = usage.lite_daily_tokens
        lite_monthly = usage.lite_tokens
        lite_daily_reset_at = usage.lite_daily_reset_at
        subscription_ends_at = usage.subscription_ends_at

    limits = quota.get_limits(subscription_type)
    lite_cap = quota.get_lite_daily_cap(subscription_type)
    return MyUsageResponse(
        subscription_type=subscription_type,
        limits=_UsageLimits(
            meeting_logs=limits["meeting_logs"],
            summary_chars=limits["summary_chars"],
            total_tokens=limits["total_tokens"],
        ),
        usage=_UsageCounters(
            meeting_logs=meeting_count,
            total_tokens=total_tokens,
            total_chars=total_chars,
        ),
        lite=_LiteUsage(
            daily_used=lite_daily,
            daily_cap=lite_cap,
            monthly_used=lite_monthly,
            # 메인 소진 + 오버플로우 가능(cap>0) 이면 현재 Lite 모드로 동작 중.
            overflow_active=(total_tokens >= limits["total_tokens"] and lite_cap > 0),
            daily_reset_at=lite_daily_reset_at,
        ),
        reset_at=reset_at,
        subscription_ends_at=subscription_ends_at,
    )


@router.patch("/me", response_model=UserResponse)
async def update_me_route(
    payload: UpdateMeRequest,
    current_user: UserPublic = Depends(get_current_user),
):
    """
    내 정보 수정.
    - name: 비어 있으면 변경 안 함
    - github_username: None 이면 변경 안 함, "" 면 해제
    - auto_progress: None 이면 변경 안 함. false 면 검수 게이트 모드 (CPS 만 자동).
    """
    updated = await users.update_user(
        email=current_user.email,
        name=payload.name,
        github_username=payload.github_username,
        auto_progress=payload.auto_progress,
        locale=payload.locale,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자를 찾을 수 없습니다.",
        )
    return UserResponse(**updated.model_dump())


async def _purge_owned_projects(email: str, names: Optional[list] = None) -> dict:
    """탈퇴 시 본인 단독 소유(개인) 프로젝트의 도메인 데이터 파기 (P0-5).

    개인정보처리방침의 '탈퇴 시 즉시 파기' 이행 — 미팅 로그(개인정보 포함 가능)·
    CPS/PRD/Design 을 프로젝트 단위로 삭제. 팀 프로젝트는 협업자 보호로 제외.
    best-effort: 한 프로젝트 실패가 탈퇴 자체를 막지 않는다(로그 후 계속).

    names: 호출측이 미리 떠둔 목록 — User 삭제 **후** 파기 시 OWNS 가 이미 사라져
    재조회가 빈 목록이 되므로, 라우트는 삭제 전에 목록을 확보해 넘긴다.
    """
    if names is None:
        names = await users.list_owned_project_names(email)
    ctx = PipelineContext(
        gemini=None, neo4j=Neo4jClientProxy(),
        idempotency_key="self-delete", user_email=email,
    )
    deleted = failed = 0
    for name in names:
        try:
            await delete_project(ctx, name)
            deleted += 1
        except Exception as e:  # noqa: BLE001 — best-effort, 탈퇴는 계속
            failed += 1
            logger.warning("탈퇴 데이터 파기 실패 (email=%s, project=%s): %s", email, name, e)
    if names:
        logger.info("탈퇴 데이터 파기: email=%s, 삭제 %d / 실패 %d", email, deleted, failed)
    return {"deleted": deleted, "failed": failed}


@router.delete("/me", response_model=MessageResponse)
async def delete_me_route(
    token: str = Depends(oauth2_scheme),
    current_user: UserPublic = Depends(get_current_user),
):
    """
    회원 탈퇴.
    1) 탈퇴 직전 audit log 1건 기록 (User 노드 삭제 후엔 추적 불가 → AuditLog 만 보존)
    2) Neo4j 에서 User 노드 + Vibe Repo + SubscriptionChange 이력 DETACH DELETE
    3) [P0-5] 본인 단독 소유(개인) 프로젝트의 도메인 데이터(미팅 로그·CPS/PRD/Design)
       파기 — 처리방침 '탈퇴 시 즉시 파기' 이행. 팀 프로젝트는 협업자 보호로 보존.
    4) 현재 access token 도 즉시 블랙리스트 등록 (이미 발급된 토큰의 무효화)

    파기 대상은 owner_email 매칭 개인 프로젝트뿐 — 팀 프로젝트(다른 협업자 존재 가능)는
    건드리지 않는다. last_admin 으로 탈퇴가 거부되면 데이터도 보존된다(파기는 삭제 성공 후).
    """
    # (1) 탈퇴 흔적 audit log — User 노드 삭제 전에 미리 기록.
    #     삭제가 실패해도 시도 자체는 로그로 남는 게 분쟁 시 유리.
    from app.service import audit_repository  # 지연 import (순환 방지)
    await audit_repository.write(
        actor_email=current_user.email,
        action=audit_repository.ACTION_USER_SELF_DELETE,
        target_email=current_user.email,
        payload={
            "subscription_type": current_user.subscription_type,
            "is_admin": current_user.is_admin,
            "github_username": current_user.github_username,
        },
    )
    # (1.7) [P0-5] 개인 소유 프로젝트 데이터 파기 — 처리방침 '탈퇴 시 즉시 파기' 이행.
    # last_admin 차단보다 먼저 지우면 차단된 admin 의 데이터가 사라지므로, 먼저 last_admin
    # 여부만 가볍게 확인하는 대신 순서를 보존: delete_user 가 last_admin 으로 거부하면
    # purge 도 일어나지 않아야 한다 → purge 를 delete_user 성공 판정 이후로 둘 수 없음
    # (User 삭제 후엔 OWNS 관계가 사라져 목록 조회 불가). 절충: 목록을 먼저 떠두고,
    # delete_user 성공 후 파기 실행.
    owned_before = await users.list_owned_project_names(current_user.email)

    # (2) 실제 삭제
    result = await users.delete_user(email=current_user.email)
    if result.get("status") == "last_admin":
        # 마지막 관리자는 본인 탈퇴를 차단 — admin 0명 사태 방지.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("message") or "마지막 관리자는 탈퇴할 수 없습니다.",
        )
    if result.get("status") != "deleted":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="유저 삭제 실패.",
        )
    # (3) [P0-5] 개인 소유 프로젝트 데이터 파기 — User 삭제 성공 후에만
    # (last_admin 거부 시 데이터 보존). 사전 확보한 목록 사용(OWNS 는 이미 삭제됨).
    await _purge_owned_projects(current_user.email, names=owned_before)
    await logout(token)
    return MessageResponse(message="탈퇴되었습니다.")


# ===== 소유 프로젝트 목록 =====


class OwnedProject(BaseModel):
    """
    내 소유 프로젝트 1건. Phase 2A 부터 `id` (project_id UUID) 추가 —
    FE 가 id 키 받아도 무시 가능 (additive). Phase 2D 후엔 모든 deep-link 가
    name 대신 id 로 가는 게 권장.
    """
    id: Optional[str] = None
    name: str
    owned_at: Optional[str] = None


class TeamProjectItem(BaseModel):
    id: Optional[str] = None
    name: str
    team_id: str
    created_at: Optional[str] = None


class TeamWithProjects(BaseModel):
    id: str
    name: str
    role: Optional[str] = None
    projects: list[TeamProjectItem] = []


class OwnedProjectsResponse(BaseModel):
    projects: list[OwnedProject]
    teams: list[TeamWithProjects] = []


@router.get("/me/projects", response_model=OwnedProjectsResponse)
async def list_my_projects_route(
    current_user: UserPublic = Depends(get_current_user),
) -> OwnedProjectsResponse:
    """
    내 프로젝트 목록. 개인 소유(projects) + 팀별 프로젝트(teams) 구분 반환.
    기존 클라이언트는 projects 필드만 사용 — 하위 호환.
    """
    from app.service import team_repository

    personal = await ownership_repository.list_owned_projects(current_user.email)
    my_teams = await team_repository.get_teams_for_user(current_user.email)

    teams_with_projects: list[TeamWithProjects] = []
    for t in my_teams:
        team_projects = await ownership_repository.list_team_projects(current_user.email, t["id"])
        teams_with_projects.append(TeamWithProjects(
            id=t["id"],
            name=t["name"],
            role=t.get("role"),
            projects=[TeamProjectItem(**p) for p in team_projects],
        ))

    return OwnedProjectsResponse(
        projects=[OwnedProject(**i) for i in personal],
        teams=teams_with_projects,
    )


# ===== Vibe Repo 라이브러리 =====


class LibraryListResponse(BaseModel):
    repos: list[VibeRepoOut]
    count: int


class LibraryAddResponse(BaseModel):
    status: str = "ok"
    repo: VibeRepoOut


class LibraryDeleteRequest(BaseModel):
    url: str = Field(..., min_length=1)


class LibraryDeleteResponse(BaseModel):
    status: str
    url: str


@router.get(
    "/me/library",
    response_model=LibraryListResponse,
    summary="내 Vibe Repo 라이브러리 조회 (is_mine 우선, 최근 갱신순)",
)
async def list_my_library_route(
    current_user: UserPublic = Depends(get_current_user),
) -> LibraryListResponse:
    repos = await library.get_vibe_repos(current_user.email)
    return LibraryListResponse(repos=repos, count=len(repos))


@router.post(
    "/me/library",
    response_model=LibraryAddResponse,
    summary="라이브러리에 GitHub repo 추가 (upsert — 같은 URL 재호출 시 갱신)",
)
async def add_library_repo_route(
    payload: VibeRepoInput,
    current_user: UserPublic = Depends(get_current_user),
) -> LibraryAddResponse:
    try:
        out = await library.add_vibe_repo(current_user.email, payload)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        ) from e
    return LibraryAddResponse(status="ok", repo=out)


@router.delete(
    "/me/library",
    response_model=LibraryDeleteResponse,
    summary="라이브러리에서 GitHub repo 제거 (URL 매칭)",
)
async def delete_library_repo_route(
    payload: LibraryDeleteRequest = Body(...),
    current_user: UserPublic = Depends(get_current_user),
) -> LibraryDeleteResponse:
    ok = await library.delete_vibe_repo(current_user.email, payload.url)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 URL 이 라이브러리에 없습니다.",
        )
    return LibraryDeleteResponse(status="deleted", url=payload.url)


# ===== 미팅 로그 업로드 히스토리 =====
#
# plan 페이지에서 사용자가 .txt 파일로 직접 업로드한 미팅 로그 원본을
# 사용자별로 영속화한다. 본문은 노드 property 에 직접 저장 — 크기 가드 있음.


class UploadsListResponse(BaseModel):
    """목록 응답 — 본문 제외 (payload 절약)."""

    uploads: list[MeetingUploadMeta]
    count: int


class UploadCreateResponse(BaseModel):
    status: str = "ok"
    upload: MeetingUploadMeta


class UploadDeleteResponse(BaseModel):
    status: str
    id: str


@router.get(
    "/me/uploads",
    response_model=UploadsListResponse,
    summary="내가 업로드한 미팅 로그 목록 (최근순, 메타만)",
)
async def list_my_uploads_route(
    current_user: UserPublic = Depends(get_current_user),
) -> UploadsListResponse:
    items = await uploads.list_uploads(current_user.email)
    return UploadsListResponse(uploads=items, count=len(items))


@router.post(
    "/me/uploads",
    response_model=UploadCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="미팅 로그 업로드 (본문 텍스트 저장 — 최대 1MiB)",
)
async def add_my_upload_route(
    payload: MeetingUploadInput,
    current_user: UserPublic = Depends(get_current_user),
) -> UploadCreateResponse:
    try:
        meta = await uploads.add_upload(current_user.email, payload)
    except ValueError as e:
        # 크기 초과 등 — 422 매핑
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        ) from e
    return UploadCreateResponse(status="ok", upload=meta)


@router.get(
    "/me/uploads/{upload_id}",
    response_model=MeetingUploadDetail,
    summary="업로드한 미팅 로그 본문 조회",
)
async def get_my_upload_route(
    upload_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> MeetingUploadDetail:
    detail = await uploads.get_upload(current_user.email, upload_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="업로드를 찾을 수 없습니다.",
        )
    return detail


@router.delete(
    "/me/uploads/{upload_id}",
    response_model=UploadDeleteResponse,
    summary="업로드한 미팅 로그 삭제",
)
async def delete_my_upload_route(
    upload_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> UploadDeleteResponse:
    ok = await uploads.delete_upload(current_user.email, upload_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="업로드를 찾을 수 없습니다.",
        )
    return UploadDeleteResponse(status="deleted", id=upload_id)


# ===== GitHub OAuth =====
#
# Flow:
#   1) FE → GET /auth/github/login?mode=login|link
#      → BE 가 GitHub authorize URL 로 redirect (state 토큰 포함)
#   2) 사용자가 GitHub 에서 승인 → GitHub → GET /auth/github/callback?code=...&state=...
#   3) BE 가 code↔token 교환, 사용자 정보 조회, User 노드 link/create, 우리 JWT 발급
#   4) BE → FRONTEND_OAUTH_CALLBACK_URL?access_token=...&refresh_token=...&mode=... 로 redirect
#
# 보안: github_access_token 은 user_repository.link_github /
# create_user_from_github 시점에 `app/core/token_encryption.py` 의 Fernet 으로
# 컬럼 암호화 후 Neo4j 에 저장된다. 복호화는 사용 시점 (`get_github_token`).
# TOKEN_ENCRYPTION_KEY env 가 운영에서 필수 — 미설정 시 평문 fallback (개발 편의).


class GitHubStatusResponse(BaseModel):
    linked: bool
    github_username: Optional[str] = None
    oauth_available: bool


@router.get(
    "/github/status",
    response_model=GitHubStatusResponse,
    summary="현재 사용자의 GitHub 연결 상태",
)
async def github_status_route(
    current_user: UserPublic = Depends(get_current_user),
) -> GitHubStatusResponse:
    """
    프로필 페이지가 "연결됨 / 연결" 버튼을 결정하기 위해 사용.
    oauth_available=False 면 FE 는 버튼 자체를 disable.
    """
    return GitHubStatusResponse(
        linked=bool(current_user.github_username),
        github_username=current_user.github_username,
        oauth_available=settings.github_oauth_enabled,
    )


@router.get(
    "/github/login",
    summary="GitHub OAuth 로그인 시작 — 인증 불필요, authorize URL 로 redirect",
)
async def github_login_route():
    """
    **login 모드 전용** — 신규/기존 사용자의 GitHub 로 로그인 흐름.

    link 모드 (기존 로그인된 사용자가 자기 계정에 GitHub 연결) 는 보안상
    별도 라우트 `POST /github/link` 로 분리되어 Bearer 인증 필수.
    이전 버전의 `?mode=link&link_email=` query 는 **계정 탈취** 취약점이라 제거.
    """
    github_oauth.assert_oauth_configured()
    try:
        state = github_oauth.create_state_token(mode="login")
        url = github_oauth.build_authorize_url(state)
    except github_oauth.GitHubOAuthDisabled as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        ) from e

    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


class GitHubLinkUrlResponse(BaseModel):
    url: str


@router.post(
    "/github/link",
    response_model=GitHubLinkUrlResponse,
    summary="GitHub 연결 시작 — 인증 필수, authorize URL 응답 (FE 가 받아 redirect)",
)
async def github_link_start_route(
    current_user: UserPublic = Depends(get_current_user),
) -> GitHubLinkUrlResponse:
    """
    **link 모드 전용** — 이미 로그인된 사용자가 자기 계정에 GitHub 를 연결.

    state 토큰에 들어가는 email 은 **Bearer 토큰의 current_user.email** —
    FE 가 보낸 값을 절대 신뢰하지 않음. 이전 GET 라우트의 `link_email` query
    를 신뢰해 다른 사람 계정에 link 가 가능했던 취약점을 제거.

    FE 는 axios 로 호출 → response.url 받아 `window.location.href` 로 이동.
    GET RedirectResponse 가 아닌 이유: 브라우저는 GET navigation 시 Bearer
    헤더를 보낼 수 없음.
    """
    github_oauth.assert_oauth_configured()
    try:
        state = github_oauth.create_state_token(
            mode="link", email=current_user.email
        )
        url = github_oauth.build_authorize_url(state)
    except github_oauth.GitHubOAuthDisabled as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        ) from e
    return GitHubLinkUrlResponse(url=url)


def _redirect_to_frontend(
    *,
    mode: str,
    provider: str = "github",
    access_token: Optional[str] = None,
    refresh_token: Optional[str] = None,
    error: Optional[str] = None,
    new_user: bool = False,
) -> RedirectResponse:
    """
    FE callback URL 로 token/error 동봉 redirect.

    [2026-05 보안 — token leak 차단]
    이전: 모든 파라미터를 query (`?access_token=...`) 로 전달.
        → 토큰이 server access log / Referer 헤더 / 브라우저 history 에 노출.
        → 외부 이미지/analytics 로딩 시 Referer 로 타 서비스에 토큰 전송 위험.
    이후: 민감한 토큰은 **fragment (`#`)** 로 전달 — fragment 는 RFC 7231 상
        Referer 에 미포함, 서버에 전송 안 됨, access log 안전.
        FE 가 mount 직후 history.replaceState 로 fragment 도 즉시 제거.
        에러/메타 정보 (mode/provider/error/new) 는 query 유지 — 민감 아님.

    [2026-05 추가] provider 인자 — FE 가 'GitHub' vs 'Google' 에러/성공 메시지 분기.
    default='github' 는 backward compat (legacy 호출).

    [FE 짝 작업]
    `src/pages/auth/callback.vue` 가 location.hash 에서 토큰 파싱.
    location.search 는 mode/provider/error/new 만 읽음.
    """
    base = settings.FRONTEND_OAUTH_CALLBACK_URL or ""

    # 1. 민감하지 않은 메타데이터는 query 로 — FE 가 mode/provider/error 분기에 사용.
    query_params: dict = {"mode": mode, "provider": provider}
    if error:
        query_params["error"] = error
    if new_user:
        query_params["new"] = "1"

    # 2. 토큰은 fragment 로 — Referer/access log/history 누설 차단.
    fragment_params: dict = {}
    if access_token:
        fragment_params["access_token"] = access_token
    if refresh_token:
        fragment_params["refresh_token"] = refresh_token

    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}{urlencode(query_params)}" if base else f"/?{urlencode(query_params)}"
    if fragment_params:
        url = f"{url}#{urlencode(fragment_params)}"
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get(
    "/github/callback",
    summary="GitHub OAuth callback — code → token 교환 후 FE 로 redirect",
)
async def github_callback_route(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """
    GitHub 가 호출. 성공 시 FE 의 callback URL 로 우리 JWT 동봉 redirect.
    실패 시에도 FE 로 error 동봉 redirect (FE 가 사용자에게 토스트).
    """
    if not settings.github_oauth_enabled:
        return _redirect_to_frontend(mode="error", error="oauth_disabled")

    if error:
        # 사용자가 GitHub 에서 취소하거나 GitHub 가 에러 반환
        return _redirect_to_frontend(mode="error", error=error)
    if not code or not state:
        return _redirect_to_frontend(mode="error", error="missing_code_or_state")

    # state 검증
    try:
        state_payload = github_oauth.verify_state_token(state)
    except github_oauth.GitHubOAuthError as e:
        return _redirect_to_frontend(mode="error", error=f"invalid_state:{e}")

    mode = state_payload.get("mode", "login")
    state_email = state_payload.get("email")

    # code → token 교환
    try:
        gh_token = await github_oauth.exchange_code_for_token(code)
        gh_user = await github_oauth.fetch_github_user(gh_token)
    except github_oauth.GitHubOAuthError as e:
        logger.warning("GitHub OAuth callback failed: %s", e)
        return _redirect_to_frontend(mode=mode, error=str(e))

    scopes_str = ",".join(settings.github_oauth_scopes_list)

    user: Optional[UserPublic] = None
    is_new_user = False

    if mode == "link":
        # 이미 로그인된 사용자가 자기 계정에 GitHub 연결
        if not state_email:
            return _redirect_to_frontend(mode="link", error="link_email_missing")
        # 이미 다른 user 와 연결된 GitHub 계정인지 검사 (UNIQUE 제약 위반 회피)
        existing = await users.find_by_github_id(gh_user["github_id"])
        if existing and existing.email != state_email:
            return _redirect_to_frontend(
                mode="link",
                error=f"github_already_linked_to:{existing.email}",
            )
        user = await users.link_github(
            email=state_email,
            github_id=gh_user["github_id"],
            github_username=gh_user["login"],
            github_access_token=gh_token,
            github_scopes=scopes_str,
        )
        if user is None:
            return _redirect_to_frontend(mode="link", error="user_not_found")
    else:
        # login 모드: github_id 로 먼저 찾고, 없으면 신규 생성
        user = await users.find_by_github_id(gh_user["github_id"])
        if user is None:
            user, is_new_user = await users.create_user_from_github(
                email=gh_user["email"],
                name=gh_user["name"],
                github_id=gh_user["github_id"],
                github_username=gh_user["login"],
                github_access_token=gh_token,
                github_scopes=scopes_str,
            )
            if user is None:
                return _redirect_to_frontend(mode="login", error="user_create_failed")
            if not is_new_user:
                # 같은 email 의 password 가입 사용자가 이미 존재 — 충돌.
                # mode=login 이면 link 하지 않음 (의도 표명 필요).
                # 사용자에게 알리고 link 모드로 다시 시도하라고 안내.
                return _redirect_to_frontend(
                    mode="login",
                    error=f"email_exists_use_link:{user.email}",
                )
            # ADMIN_EMAILS 에 등록된 이메일이면 즉시 admin 승격 (부팅 시점 의존 제거).
            if is_new_user and user.email.lower() in settings.admin_emails_list:
                await users.promote_admins_by_emails([user.email])

    # [2026-05-18] 정지된 계정은 토큰 발급 차단. UserPublic 에 is_suspended 가
    # 없으므로 UserInDB 로 재조회 후 검사.
    full = await users.get_user_by_email(user.email)
    if full and full.is_suspended:
        return _redirect_to_frontend(
            mode=mode,
            error=f"suspended:{full.suspended_reason or ''}",
        )

    # 성공 — 우리 JWT 발급
    access = create_access_token(user.email)
    refresh = create_refresh_token(user.email)
    await touch_last_active(user.email)
    return _redirect_to_frontend(
        mode=mode,
        access_token=access,
        refresh_token=refresh,
        new_user=is_new_user,
    )


@router.delete(
    "/github/disconnect",
    response_model=MessageResponse,
    summary="GitHub 연결 해제 (노드는 유지, GitHub 관련 필드만 제거)",
)
async def github_disconnect_route(
    current_user: UserPublic = Depends(get_current_user),
):
    if not current_user.github_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub 이 연결되어 있지 않습니다.",
        )
    # [2026-06 OAuth 전용] 마지막 남은 로그인 수단이면 해제 거부 (영구 잠김 방지).
    # 다른 소셜(Google) 연결 또는 기존 비밀번호(레거시 이메일 계정)가 있으면 허용.
    # google_email 은 UserInDB 에 없어 Neo4j 직접 조회 (google_disconnect 와 동일 방식).
    full_user = await users.get_user_by_email(current_user.email)
    has_password = bool(full_user and full_user.hashed_password)
    rows = await neo4j_client.run_cypher(
        "MATCH (u:User {email: $email}) RETURN COALESCE(u.google_email, '') AS ge",
        {"email": current_user.email},
    )
    has_google = bool(rows and (rows[0].get("ge") or ""))
    if not has_password and not has_google:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "GitHub 으로만 로그인할 수 있는 계정입니다. 연결을 해제하려면 "
                "먼저 Google 계정을 연결해주세요."
            ),
        )

    ok = await users.unlink_github(current_user.email)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub 연결 해제 실패.",
        )
    return MessageResponse(message="GitHub 연결이 해제되었습니다.")


# ============================================================================
# [2026-05] Google OAuth — GitHub 와 동일 패턴 (login + link + disconnect + status)
# ============================================================================


class GoogleStatusResponse(BaseModel):
    linked: bool
    google_email: Optional[str] = None
    oauth_available: bool


@router.get(
    "/google/status",
    response_model=GoogleStatusResponse,
    summary="현재 사용자의 Google 연결 상태",
)
async def google_status_route(
    current_user: UserPublic = Depends(get_current_user),
) -> GoogleStatusResponse:
    """프로필 페이지가 "연결됨 / 연결" 버튼을 결정하기 위해 사용."""
    # current_user 에 google_email 없으면 DB 조회 (UserPublic 에 미포함이라 별도 fetch)
    full = await users.get_user_by_email(current_user.email)
    google_email = None
    if full:
        # UserInDB 에도 google_email 없으면 raw cypher 가 필요한데, 단순화를 위해
        # admin_repository 의 get_user_detail 패턴 활용 — 또는 추가 cypher.
        # 여기선 신규 노드 필드를 직접 조회.
        rows = await neo4j_client.run_cypher(
            "MATCH (u:User {email: $email}) RETURN COALESCE(u.google_email, '') AS ge",
            {"email": current_user.email},
        )
        if rows:
            ge = rows[0].get("ge") or ""
            google_email = ge if ge else None
    return GoogleStatusResponse(
        linked=bool(google_email),
        google_email=google_email,
        oauth_available=settings.google_oauth_enabled,
    )


@router.get(
    "/google/login",
    summary="Google OAuth 로그인 시작 — 인증 불필요, authorize URL 로 redirect",
)
async def google_login_route():
    """login 모드 전용 — 신규/기존 사용자의 Google 로 로그인 흐름."""
    google_oauth.assert_oauth_configured()
    try:
        state = google_oauth.create_state_token(mode="login")
        url = google_oauth.build_authorize_url(state)
    except google_oauth.GoogleOAuthDisabled as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        ) from e
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


class GoogleLinkUrlResponse(BaseModel):
    url: str


@router.post(
    "/google/link",
    response_model=GoogleLinkUrlResponse,
    summary="Google 연결 시작 — 인증 필수, authorize URL 응답 (FE 가 받아 redirect)",
)
async def google_link_start_route(
    current_user: UserPublic = Depends(get_current_user),
) -> GoogleLinkUrlResponse:
    """link 모드 전용 — 이미 로그인된 사용자가 자기 계정에 Google 를 연결.

    state 토큰의 email 은 Bearer 토큰의 current_user.email — FE 입력 무시 (보안).
    """
    google_oauth.assert_oauth_configured()
    try:
        state = google_oauth.create_state_token(
            mode="link", email=current_user.email
        )
        url = google_oauth.build_authorize_url(state)
    except google_oauth.GoogleOAuthDisabled as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        ) from e
    return GoogleLinkUrlResponse(url=url)


@router.get(
    "/google/callback",
    summary="Google OAuth callback — code → token 교환 후 FE 로 redirect",
)
async def google_callback_route(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """Google 가 호출. 성공 시 FE 의 callback URL 로 우리 JWT 동봉 redirect."""
    # provider="google" 자동 주입 헬퍼 — FE 가 GitHub/Google 메시지 분기.
    def redirect(**kw):
        return _redirect_to_frontend(provider="google", **kw)

    if not settings.google_oauth_enabled:
        return redirect(mode="error", error="oauth_disabled")

    if error:
        return redirect(mode="error", error=error)
    if not code or not state:
        return redirect(mode="error", error="missing_code_or_state")

    try:
        state_payload = google_oauth.verify_state_token(state)
    except google_oauth.GoogleOAuthError as e:
        return redirect(mode="error", error=f"invalid_state:{e}")

    mode = state_payload.get("mode", "login")
    state_email = state_payload.get("email")

    try:
        g_token = await google_oauth.exchange_code_for_token(code)
        g_user = await google_oauth.fetch_google_user(g_token)
    except google_oauth.GoogleOAuthError as e:
        logger.warning("Google OAuth callback failed: %s", e)
        return redirect(mode=mode, error=str(e))

    user: Optional[UserPublic] = None
    is_new_user = False

    if mode == "link":
        if not state_email:
            return redirect(mode="link", error="link_email_missing")
        existing = await users.get_user_by_google_id(g_user["google_id"])
        if existing and existing.email != state_email:
            return redirect(
                mode="link",
                error=f"google_already_linked_to:{existing.email}",
            )
        user = await users.link_google(
            email=state_email,
            google_id=g_user["google_id"],
            google_email=g_user["email"],
        )
        if user is None:
            return redirect(mode="link", error="user_not_found")
    else:
        # login 모드 — google_id 로 먼저 찾고, 없으면 신규 생성
        user = await users.get_user_by_google_id(g_user["google_id"])
        if user is None:
            user, is_new_user = await users.create_user_from_google(
                email=g_user["email"],
                name=g_user["name"],
                google_id=g_user["google_id"],
            )
            if user is None:
                return redirect(mode="login", error="user_create_failed")
            if not is_new_user:
                # 같은 email 의 비밀번호 또는 GitHub 가입 사용자 — link 모드 안내
                return redirect(
                    mode="login",
                    error=f"email_exists_use_link:{user.email}",
                )
            # ADMIN_EMAILS 즉시 승격
            if is_new_user and user.email.lower() in settings.admin_emails_list:
                await users.promote_admins_by_emails([user.email])

    # [2026-05-18] 정지된 계정은 토큰 발급 차단.
    full = await users.get_user_by_email(user.email)
    if full and full.is_suspended:
        return redirect(
            mode=mode,
            error=f"suspended:{full.suspended_reason or ''}",
        )

    access = create_access_token(user.email)
    refresh = create_refresh_token(user.email)
    await touch_last_active(user.email)
    return redirect(
        mode=mode,
        access_token=access,
        refresh_token=refresh,
        new_user=is_new_user,
    )


@router.delete(
    "/google/disconnect",
    response_model=MessageResponse,
    summary="Google 연결 해제 (노드는 유지, Google 관련 필드만 제거)",
)
async def google_disconnect_route(
    current_user: UserPublic = Depends(get_current_user),
):
    # 현재 google_email 있는지 확인
    rows = await neo4j_client.run_cypher(
        "MATCH (u:User {email: $email}) RETURN COALESCE(u.google_email, '') AS ge",
        {"email": current_user.email},
    )
    has_google = bool(rows and (rows[0].get("ge") or ""))
    if not has_google:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google 이 연결되어 있지 않습니다.",
        )
    # [2026-06 OAuth 전용] 마지막 남은 로그인 수단이면 해제 거부 (영구 잠김 방지).
    # 다른 소셜(GitHub) 연결 또는 기존 비밀번호(레거시 이메일 계정)가 있으면 허용.
    full_user = await users.get_user_by_email(current_user.email)
    has_password = bool(full_user and full_user.hashed_password)
    has_github = bool(full_user and full_user.github_username)
    if not has_password and not has_github:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Google 으로만 로그인할 수 있는 계정입니다. 연결을 해제하려면 "
                "먼저 GitHub 계정을 연결해주세요."
            ),
        )
    ok = await users.unlink_google(current_user.email)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google 연결 해제 실패.",
        )
    return MessageResponse(message="Google 연결이 해제되었습니다.")


# ============================================================================
# [2026-05-18 pivot] Notion Internal Integration token 방식
# (이전: Public OAuth — 노션이 셀프서비스 등록 막아서 사용 불가)
# 사용자가 노션 워크스페이스에서 Internal Integration 만들고 secret token 발급 후
# 직접 붙여넣음. 우리 BE 가 토큰으로 노션 API 호출해서 검증 + 저장.
# ============================================================================


from app.core import notion_client as _notion_client  # noqa: E402


class NotionStatusResponse(BaseModel):
    linked: bool
    notion_workspace_name: Optional[str] = None
    oauth_available: bool = Field(
        default=True,
        description="항상 true — Internal Token 방식은 관리자 OAuth 설정 불필요",
    )


class NotionTokenRequest(BaseModel):
    token: str = Field(
        ...,
        min_length=10,
        max_length=200,
        description="노션 Internal Integration secret token (ntn_* 형식)",
    )


class NotionLinkUrlResponse(BaseModel):
    """Notion OAuth authorize URL — FE 가 받아서 window.location 으로 이동."""
    url: str


# [2026-05-19] OAuth helper 다시 import — 라우트 부활 (사용자 요청).
from app.core import notion_oauth as _notion_oauth  # noqa: E402


@router.get(
    "/notion/status",
    response_model=NotionStatusResponse,
    summary="현재 사용자의 노션 연결 상태",
)
async def notion_status_route(
    current_user: UserPublic = Depends(get_current_user),
) -> NotionStatusResponse:
    info = await users.get_notion_info(current_user.email)
    return NotionStatusResponse(
        linked=bool(info),
        notion_workspace_name=(info or {}).get("workspace_name"),
        # [2026-05-19] OAuth 환경 변수 (CLIENT_ID/SECRET/REDIRECT_URI) 가
        # 셋 다 설정돼 있을 때만 OAuth 가능. FE 가 이걸 보고 OAuth 버튼/
        # Internal Token fallback 분기.
        oauth_available=settings.notion_oauth_enabled,
    )


@router.post(
    "/notion/token",
    response_model=NotionStatusResponse,
    summary="노션 Internal Integration token 등록 (검증 + 저장)",
)
@limiter.limit("10/minute")
async def notion_submit_token_route(
    request: Request,
    payload: NotionTokenRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> NotionStatusResponse:
    """
    Flow:
    1. 사용자가 노션에서 Internal Integration 만들고 secret token 복사
    2. FE 가 이 라우트로 token 전달
    3. BE 가 토큰으로 GET /v1/users/me 호출 → 유효성 검증 + workspace_name 추출
    4. Fernet 암호화 후 User 노드에 저장
    """
    # 1) 토큰 검증 + workspace 정보 조회
    try:
        me = await _notion_client.get_me(payload.token)
    except _notion_client.NotionTokenInvalid as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "노션 토큰이 유효하지 않습니다. "
                "노션 → Settings → Connections → Develop or manage integrations 에서 "
                "Internal Integration secret 을 다시 확인해주세요."
            ),
        ) from e
    except _notion_client.NotionAPIError as e:
        logger.warning("notion token verify failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="노션 API 호출에 실패했습니다. 잠시 후 다시 시도해주세요.",
        ) from e

    info = _notion_client.extract_workspace_info(me)

    # 2) 저장 (Fernet 암호화)
    saved = await users.link_notion(
        email=current_user.email,
        notion_access_token=payload.token,
        notion_workspace_id=info.get("workspace_id") or "",
        notion_workspace_name=info.get("workspace_name") or "노션",
        notion_bot_id=info.get("bot_id") or "",
    )
    if saved is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="노션 연결 정보 저장에 실패했습니다.",
        )

    return NotionStatusResponse(
        linked=True,
        notion_workspace_name=info.get("workspace_name") or "노션",
        oauth_available=True,
    )


@router.delete(
    "/notion/disconnect",
    response_model=MessageResponse,
    summary="노션 연결 해제 (노드는 유지)",
)
async def notion_disconnect_route(
    current_user: UserPublic = Depends(get_current_user),
):
    info = await users.get_notion_info(current_user.email)
    if not info:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="노션이 연결되어 있지 않습니다.",
        )
    ok = await users.unlink_notion(current_user.email)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="노션 연결 해제 실패.",
        )
    return MessageResponse(message="노션 연결이 해제되었습니다.")


# ============================================================================
# [2026-05-19] Notion OAuth one-click 연결 (라우트 부활)
# - 2dff3c6 에서 "노션 셀프서비스 등록 막힘" 이유로 제거됐었음. 사용자 요청 —
#   노션이 지금은 OAuth 등록 가능하니 다시 켭니다.
# - core/notion_oauth.py 의 헬퍼는 그대로 살아있어 라우트만 다시 wiring.
# - Internal Token (POST /notion/token) 은 폴백으로 유지.
# ============================================================================


@router.post(
    "/notion/link",
    response_model=NotionLinkUrlResponse,
    summary="노션 OAuth 시작 — 인증 필수, authorize URL 응답",
)
async def notion_link_start_route(
    current_user: UserPublic = Depends(get_current_user),
) -> NotionLinkUrlResponse:
    """
    로그인된 사용자가 자기 계정에 노션을 연결 (OAuth one-click).

    Flow:
      1. FE 가 이 라우트 호출 → state JWT + authorize URL 반환
      2. FE 가 받은 URL 로 window.location 이동 → 사용자가 노션에서 워크스페이스 승인
      3. 노션 → BE /auth/notion/callback?code=...&state=...
      4. BE 가 code → access_token 교환 + state 검증 + 저장
      5. FE callback 페이지로 redirect (mode=notion_link)

    state JWT 에 current_user.email 동봉 → callback 에서 인증된 값으로 식별.
    FE 가 보낸 어떤 값도 신뢰하지 않음.
    """
    try:
        _notion_oauth.assert_oauth_configured()
        state = _notion_oauth.create_state_token(email=current_user.email)
        url = _notion_oauth.build_authorize_url(state)
    except _notion_oauth.NotionOAuthDisabled as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "노션 OAuth 가 운영 환경에 설정되어 있지 않습니다. "
                "관리자에게 NOTION_OAUTH_CLIENT_ID / SECRET / REDIRECT_URI 설정을 요청해주세요. "
                "(또는 Internal Integration token 방식으로 연결 가능)"
            ),
        ) from e
    return NotionLinkUrlResponse(url=url)


@router.get(
    "/notion/callback",
    summary="Notion OAuth callback — code → token 교환 후 FE 로 redirect",
)
async def notion_callback_route(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """노션 → BE redirect. token 저장 후 FE callback URL 로 redirect."""
    if not settings.notion_oauth_enabled:
        return _redirect_to_frontend(mode="error", provider="notion", error="oauth_disabled")
    if error:
        return _redirect_to_frontend(mode="error", provider="notion", error=error)
    if not code or not state:
        return _redirect_to_frontend(
            mode="error", provider="notion", error="missing_code_or_state"
        )

    try:
        state_payload = _notion_oauth.verify_state_token(state)
    except _notion_oauth.NotionOAuthError as e:
        return _redirect_to_frontend(
            mode="error", provider="notion", error=f"invalid_state:{e}"
        )

    email = state_payload.get("email")
    if not email:
        return _redirect_to_frontend(
            mode="error", provider="notion", error="state_email_missing"
        )

    try:
        token_data = await _notion_oauth.exchange_code_for_token(code)
    except _notion_oauth.NotionOAuthError as e:
        logger.warning("Notion OAuth callback failed: %s", e)
        return _redirect_to_frontend(mode="notion_link", provider="notion", error=str(e))

    saved = await users.link_notion(
        email=email,
        notion_access_token=token_data.get("access_token") or "",
        notion_workspace_id=token_data.get("workspace_id") or "",
        notion_workspace_name=token_data.get("workspace_name") or "",
        notion_bot_id=token_data.get("bot_id") or "",
    )
    if saved is None:
        return _redirect_to_frontend(
            mode="notion_link", provider="notion", error="user_not_found"
        )
    # 새 BE JWT 발급 안 함 — 이미 로그인된 사용자.
    # FE 가 mode=notion_link 보면 연결 성공 토스트.
    return _redirect_to_frontend(mode="notion_link", provider="notion")


# [2026-06 OAuth 전용] 비밀번호 찾기(forgot/reset/verify) 엔드포인트 제거.
# 이메일 가입·비번 설정 경로가 모두 사라져 재설정 대상 자체가 없다.
