"""
인증/보안 유틸
- 비밀번호 검증 (bcrypt) — 기존 이메일/비번 계정 로그인 안전망용
- JWT 발급/검증
- 현재 사용자(get_current_user) 의존성 — Neo4j 직접 조회
- jti 블랙리스트 검사 — Redis (TTL 자동 청소, 수평 확장 지원)

[2026-06 OAuth 전용 전환]
신규 비번 해싱(hash_password)은 제거. 이메일/비번 '가입'·'비번 설정'·'비번 재설정
실행' 경로가 모두 사라져 평문→해시 변환이 더 이상 필요 없음. verify_password 는
기존 계정의 /auth/login 안전망을 위해 유지.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core import token_blacklist
from app.core.config import settings


# ===== 비밀번호 검증 (bcrypt) — 기존 이메일/비번 계정 /auth/login 안전망 =====
# [2026-06 OAuth 전용] 신규 해싱(hash_password)은 제거. 평문→해시 변환 경로
# (이메일 가입·비번 설정·비번 재설정)가 모두 사라져 검증만 남는다.
def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # hashed가 비정상 형식이면 False (보안: 예외를 외부에 노출하지 않음)
        return False


# ===== JWT =====
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def _create_token(sub: str, minutes: int, token_type: str) -> tuple[str, str]:
    """공통 토큰 생성. jti는 로그아웃 블랙리스트 키로 사용."""
    # Python 3.12+ 에서 utcnow() 는 deprecated — timezone-aware UTC 사용.
    now = datetime.now(timezone.utc)
    jti = str(uuid.uuid4())
    payload = {
        "sub": sub,
        "type": token_type,
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, jti


def create_access_token(email: str) -> str:
    token, _ = _create_token(email, settings.ACCESS_TOKEN_EXPIRE_MINUTES, "access")
    return token


def create_refresh_token(email: str) -> str:
    token, _ = _create_token(
        email, settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60, "refresh"
    )
    return token


def create_mcp_token(email: str, exp_days: int = 90) -> tuple[str, str]:
    """MCP 전용 JWT 발급.

    - type="mcp" — MCPAuthMiddleware 가 이 타입만 허용.
    - scope=["mcp:read"] — 현재 모든 MCP tool 이 read-only. 미래 write tool 도입 시 확장.
    - exp_days 기본 90일 — IDE 가 자주 401 안 보도록 길게.

    Returns:
        (token, jti) — jti 는 호출자가 McpToken 노드에 저장.
    """
    now = datetime.now(timezone.utc)
    jti = str(uuid.uuid4())
    payload = {
        "sub": email,
        "type": "mcp",
        "scope": ["mcp:read"],
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(days=exp_days),
    }
    token = jwt.encode(
        payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )
    return token, jti


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="토큰이 만료되었습니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않은 토큰입니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _suspended_at_to_epoch(iso: str) -> Optional[int]:
    """Neo4j toString(datetime) ISO → epoch second. 파싱 실패 시 None."""
    if not iso:
        return None
    s = iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def decode_token_lenient(token: str) -> Optional[dict]:
    """
    토큰의 jti 만 추출하기 위한 lenient 디코더.

    - 서명은 검증하지만 만료(exp) 는 무시
    - 포맷/서명 오류 시 None 반환 (예외 안 올림)

    용도: 로그아웃 시 refresh token 의 jti 를 블랙리스트에 등록하는 단 한 가지
    목적. 만료된 refresh token 도 안전하게 무효화 마킹할 수 있도록 lenient.
    인증 보호 용도로는 절대 사용 금지 (반드시 `decode_token` 사용).
    """
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except jwt.InvalidTokenError:
        return None


# ===== 현재 사용자 의존성 =====
# 순환 import 회피를 위해 함수 안에서 user_repository import.
async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
):
    """
    Authorization 헤더에서 JWT 추출 → 디코드 → Redis 블랙리스트 검사 → Neo4j 직접 조회.
    반환 타입은 UserPublic (비밀번호 해시 제거됨).
    """
    from app.service import user_repository as users  # 지연 import

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증 토큰이 필요합니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(token)

    # access token 강제 (refresh로는 보호 라우트 못 들어옴)
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="잘못된 토큰 종류입니다.",
        )

    # 블랙리스트 검사 (Redis — TTL 기반 자동 청소)
    jti = payload.get("jti")
    if jti and await token_blacklist.is_revoked(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="로그아웃된 토큰입니다.",
        )

    email: Optional[str] = payload.get("sub")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="토큰에 사용자 정보가 없습니다.",
        )

    # Neo4j 직접 조회 (탈퇴 시 즉시 차단됨)
    user_db = await users.get_user_by_email(email)
    if not user_db:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="사용자를 찾을 수 없습니다.",
        )

    # [2026-05-18] 정지 / iat 비교 — 활성 토큰 일괄 무효화.
    if user_db.is_suspended:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="계정이 정지되었습니다.",
        )
    token_iat = payload.get("iat")
    if token_iat is not None and user_db.suspended_at:
        suspended_epoch = _suspended_at_to_epoch(user_db.suspended_at)
        if suspended_epoch is not None and int(token_iat) < suspended_epoch:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="세션이 만료되었습니다. 다시 로그인해 주세요.",
            )

    return users.UserPublic.from_db(user_db)


async def get_admin_user(
    current_user=Depends(get_current_user),
):
    """관리자 전용 라우트 가드. is_admin 이 아니면 403."""
    if not getattr(current_user, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다.",
        )
    return current_user
