"""
Pydantic Schemas (요청/응답 DTO)
"""
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class UserResponse(BaseModel):
    """외부 노출용 유저 정보 (비밀번호 해시 제거됨)."""
    id: str
    email: EmailStr
    name: str
    github_username: Optional[str] = None
    created_at: Optional[str] = None
    subscription_type: str = "free"
    is_admin: bool = False
    # [2026-05] auto_progress — FE 가 stage 별 자동 진행 분기.
    # true (default) = postMeeting 시 CPS+PRD 자동. false = CPS 만 (검수 게이트).
    auto_progress: bool = True
    # [2026-06] locale — UI 표시 언어. 미설정 시 'ko'.
    locale: str = "ko"


# ===== 로그인 =====
class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    """
    refresh 응답 — access 와 함께 회전된 refresh 도 동봉 (2026-05 H1 픽스).

    refresh_token 은 매 호출마다 새 값. FE 는 응답 받은 즉시 localStorage 갱신.
    이전 refresh 는 이 호출 직후 blacklist 등록되어 재사용 불가.
    """
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# ===== 회원정보 수정 =====
class UpdateMeRequest(BaseModel):
    """
    내 정보 수정. 세 필드 모두 optional — 전달된 것만 갱신.

    - `name`: None 또는 빈 문자열이면 변경 안 함.
    - `github_username`: None 이면 변경 안 함, 빈 문자열 "" 이면 연결 해제(clear).
    - `auto_progress` (2026-05): None 이면 변경 안 함.
        true  → postMeeting 이 CPS+PRD 자동 체이닝 (default)
        false → 검수 게이트 모드 (CPS 만 생성 후 사용자가 PRD/Design 명시 트리거)
    - `locale` (2026-06): None 이면 변경 안 함. 지원값: ko | en | ja | zh.
    """
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    github_username: Optional[str] = Field(default=None, max_length=100)
    auto_progress: Optional[bool] = Field(default=None)
    locale: Optional[str] = Field(default=None, pattern=r"^(ko|en|ja|zh)$")


# ===== 공용 응답 =====
class MessageResponse(BaseModel):
    status: str = "success"
    message: str


# ===== 게이트웨이 공통 응답 =====
class ApiResponse(BaseModel):
    """
    도메인 라우트 공통 응답 wrapper. `data` 안에 실제 페이로드.
    (frontend 가 historically `response.data.result` 패턴을 쓰므로 핸들러는
    `data={"result": ...}` 형태로 채움.)
    """
    status: str
    data: Any


# ===== MCP Tokens =====

class McpTokenIssueRequest(BaseModel):
    label: str = Field(
        min_length=1, max_length=80,
        description="사용자 식별용 라벨 (예: '노트북-Cursor')",
    )

    @field_validator("label", mode="before")
    @classmethod
    def _strip_label(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v


class McpTokenIssueResponse(BaseModel):
    """발급 직후 1회 응답 — token 평문 포함."""
    token: str
    jti: str
    label: str
    expires_at: str


class McpTokenSummary(BaseModel):
    """목록 조회 응답 — 평문 토큰 없음."""
    jti: str
    label: str
    created_at: str
    last_used_at: Optional[str] = None
    expires_at: str
    revoked: bool
