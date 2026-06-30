"""
인증 서비스 로직.
- 유저 데이터는 Neo4j 에 직접 저장/조회
- 비밀번호 해싱/검증 + JWT 발급/검증은 backend 책임
- 로그아웃 블랙리스트(jti)는 Redis 에 TTL 로 저장 (access + refresh 둘 다)
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status

from app.core import token_blacklist
from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    decode_token_lenient,
    verify_password,
)
from app.schemas import LoginRequest
from app.service import audit_repository, user_repository as users
from app.service.user_repository import UserInDB, UserPublic


async def _auto_promote_if_in_admin_emails(email: str) -> bool:
    """
    가입 직후 호출 — ADMIN_EMAILS env 에 해당 이메일이 있으면 즉시 admin 으로 승격.

    이렇게 하지 않으면 가입 전 ADMIN_EMAILS 가 설정된 경우 부팅 시점에는 노드가 없어
    승격이 skip 되고, 가입 후에는 다음 부팅 때까지 admin 0명 상태가 됨 (운영 사고).

    승격 성공 시 audit log 도 함께 기록 — actor='SYSTEM:ADMIN_EMAILS'.
    """
    admins = settings.admin_emails_list
    if not admins:
        return False
    if email.lower() not in admins:
        return False
    promoted = await users.promote_admins_by_emails([email])
    if promoted:
        # 시스템 자동 액션도 감사 기록 — 누가 admin 이 됐는지 영구 추적.
        await audit_repository.write(
            actor_email=audit_repository.SYSTEM_ACTOR,
            action=audit_repository.ACTION_SYSTEM_ADMIN_GRANT,
            target_email=email,
            payload={"reason": "ADMIN_EMAILS env auto-promote on signup"},
        )
    return bool(promoted)


async def login(payload: LoginRequest) -> tuple[UserPublic, str, str]:
    """로그인. Neo4j 에서 user 조회 → bcrypt 비교 → JWT 발급."""
    user_db: UserInDB | None = await users.get_user_by_email(payload.email)

    # 보안: 이메일 없음/비번 틀림을 동일 메시지로 (enumeration 방지)
    if not user_db or not verify_password(payload.password, user_db.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이메일 또는 비밀번호가 올바르지 않습니다.",
        )

    # [2026-05-18] 정지된 계정 차단 — 비번 검증 후 검사 (enumeration 방어 일관).
    if user_db.is_suspended:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "account_suspended",
                "message": _suspended_message(user_db.suspended_reason),
            },
        )

    access = create_access_token(user_db.email)
    refresh = create_refresh_token(user_db.email)
    await users.touch_last_active(user_db.email)
    return UserPublic.from_db(user_db), access, refresh


async def logout(
    access_token: str,
    refresh_token: Optional[str] = None,
) -> None:
    """
    현재 access token 의 jti 를 Redis 블랙리스트에 등록 (TTL = 토큰 잔여 만료).
    refresh_token 이 제공되면 함께 무효화 → 재발급 경로 차단.

    멱등성: Redis SET overwrite 이므로 중복 호출도 안전.
    """

    async def _blacklist(token_str: str, *, strict: bool) -> None:
        """
        - strict=True : 서명/포맷 검증 실패 또는 jti/exp 누락 시 HTTPException
                        (access token 무효화는 호출자에게 실패를 알려야 함)
        - strict=False: 만료/포맷 오류 시 조용히 skip
                        (이미 만료된 refresh token 은 무효화할 필요 없음)
        """
        if strict:
            payload = decode_token(token_str)
        else:
            payload = decode_token_lenient(token_str)
            if payload is None:
                return

        jti = payload.get("jti")
        exp = payload.get("exp")
        if not jti or not exp:
            if strict:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="블랙리스트 처리에 필요한 정보가 없습니다.",
                )
            return

        await token_blacklist.revoke_if_new(jti, int(exp))

    # 1) access token — strict 모드 (실패 시 호출자에게 에러 전파)
    await _blacklist(access_token, strict=True)

    # 2) refresh token — lenient 모드 (있으면 무효화, 만료/누락은 silent skip)
    if refresh_token:
        await _blacklist(refresh_token, strict=False)

    # 3) [Phase 3 동시접속] 활성 세션 레지스트리에서 제거 — list_sessions 응답에서 사라짐.
    # 위 _blacklist 가 jti 자체를 차단 (인증 실패), 이건 가시화 정리.
    from app.core import session_registry
    payload_access = decode_token_lenient(access_token)
    if payload_access and payload_access.get("jti"):
        await session_registry.unregister_session(payload_access["jti"])


async def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """
    refresh token 으로 새 (access, refresh) 페어 발급 — **회전 (rotation) 패턴**.

    [2026-05 보안 강화 — H1 픽스]
    이전: access 만 재발급, refresh 무한 재사용 가능 (7일).
        → 탈취 시 7일 동안 모든 access 발급 가능. /logout 없이는 무효화 0.
    이후: 호출 1회당 (1) 사용된 refresh jti 를 blacklist 등록 (재사용 차단),
        (2) 새 access + **새 refresh** 발급 — 두 토큰 모두 회전.
        → 탈취된 refresh 가 한 번 쓰이면 즉시 무효화. 정상 사용자도 자기 토큰이
          이미 갱신됐는데 같은 refresh 로 다시 호출하면 401 (탈취 신호).

    [반환 변경]
    str → tuple[str, str]. 라우트가 새 refresh 도 함께 응답에 포함해야 함.
    FE 도 응답에서 새 refresh 받아 localStorage 갱신.
    """
    payload = decode_token(refresh_token)

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token이 아닙니다.",
        )

    email = payload.get("sub")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="토큰에 사용자 정보가 없습니다.",
        )

    # [회전 가드 1] 이 refresh 가 이전에 이미 사용됐는지 확인.
    # is_revoked=True → 회전된 토큰 또는 logout 된 토큰. 재사용 차단.
    jti = payload.get("jti")
    if jti and await token_blacklist.is_revoked(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이미 사용되었거나 로그아웃된 refresh token입니다.",
        )

    # 탈퇴된 유저의 refresh token 차단
    user_db = await users.get_user_by_email(email)
    if not user_db:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="사용자를 찾을 수 없습니다.",
        )

    # [2026-05-18] 정지된 계정 차단 + iat 비교로 정지 이전 발급 토큰 거부.
    _enforce_not_suspended(user_db, token_iat=payload.get("iat"))

    # [회전 가드 2] 현재 refresh 의 jti 를 blacklist 등록 — 다음 호출 시 401 발생.
    # TTL = refresh exp 까지 (이미 만료된 토큰은 자연 청소).
    exp = payload.get("exp")
    if jti and exp:
        await token_blacklist.revoke_if_new(jti, int(exp))

    # 새 페어 발급
    new_access = create_access_token(email)
    new_refresh = create_refresh_token(email)
    await users.touch_last_active(email)
    return new_access, new_refresh


def _suspended_message(reason: Optional[str]) -> str:
    """정지 사용자에게 보여줄 메시지. reason 입력 시 사용자에게 노출, 아니면 일반 안내."""
    if reason and reason.strip():
        return f"계정이 정지되었습니다. 사유: {reason.strip()}"
    return "계정이 정지되었습니다. 고객센터로 문의해 주세요."


def _enforce_not_suspended(
    user_db: UserInDB, *, token_iat: Optional[int],
) -> None:
    """
    정지된 계정이거나, 토큰 iat 이 마지막 정지 시점 이전이면 401.

    - 현재 is_suspended=True → 즉시 401
    - is_suspended=False 여도 token.iat < user.suspended_at 이면 401 (안전망)
    """
    if user_db.is_suspended:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="계정이 정지되었습니다.",
        )
    if token_iat is None or not user_db.suspended_at:
        return
    suspended_epoch = _to_epoch(user_db.suspended_at)
    if suspended_epoch is None:
        return
    if int(token_iat) < suspended_epoch:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="세션이 만료되었습니다. 다시 로그인해 주세요.",
        )


def _to_epoch(iso: str) -> Optional[int]:
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
