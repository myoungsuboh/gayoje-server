"""
활성 세션 레지스트리 — Redis 기반.

[Phase 3 — 2026-05-18]
같은 사용자가 PC + 모바일 등 여러 디바이스에서 로그인할 수 있는 환경에서,
다음을 가능하게 함:

1. 활성 세션 목록 조회 — /auth/me/sessions
   사용자가 "지금 어디서 로그인 중인지" 확인 가능
2. 특정 세션 강제 로그아웃 — DELETE /auth/me/sessions/{jti}
   분실/공유한 디바이스의 세션을 즉시 끊을 수 있음

[설계]
- Redis HASH: `session:<jti>` = { email, ua, ip, created_at, last_seen, device_label }
  TTL = access token exp 시점 (자동 청소)
- Redis ZSET: `user_sessions:<email>` = { jti: exp_at_ms }  (score=exp 로 ZADD)
  사용자의 활성 jti 모두 빠르게 enumerate + 만료된 jti ZREMRANGEBYSCORE 로 정리

[Fail-open]
Redis 불가 시 session 등록/조회 실패해도 인증 흐름 자체는 막지 않음 — 가용성 우선.
강제 로그아웃은 token_blacklist 가 별도 보장 (이쪽이 안 되면 사용자에게 명시 에러).

[Heartbeat — last_seen]
매 인증 요청 시 last_seen 갱신하면 Redis 쓰기 과부하 → middleware 에서 매 N분에 1회만
업데이트 (debounce). 우선 MVP 는 created_at 만 신뢰 (Phase 4 에서 last_seen 추가).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from arq.connections import ArqRedis

from app.queue import client as queue_client

logger = logging.getLogger(__name__)

# Redis 키 prefix
_SESSION_KEY = "session:"          # HASH per jti
_USER_SET_KEY = "user_sessions:"    # ZSET per user (score=exp_at_ms)


def _session_key(jti: str) -> str:
    return f"{_SESSION_KEY}{jti}"


def _user_set_key(email: str) -> str:
    return f"{_USER_SET_KEY}{email.lower()}"


async def _redis() -> ArqRedis:
    return await queue_client.get_pool()


@dataclass
class SessionInfo:
    """활성 세션 메타 — FE 가 사용자에게 표시할 정보."""
    jti: str
    email: str
    user_agent: str = ""
    ip: str = ""
    created_at: int = 0       # epoch ms
    device_label: str = ""    # FE 가 친근하게 표시 ('Chrome on macOS' 등)
    is_current: bool = False  # 현재 요청을 보낸 세션인가 (라우트가 set)

    def to_dict(self) -> dict:
        return {
            "jti": self.jti,
            "user_agent": self.user_agent,
            "ip": self.ip,
            "created_at": self.created_at,
            "device_label": self.device_label,
            "is_current": self.is_current,
        }


async def record_session(
    *,
    email: str,
    jti: str,
    exp_epoch: int,
    user_agent: str = "",
    ip: str = "",
    device_label: str = "",
) -> None:
    """로그인 시점에 세션 메타 + user index 등록.

    [Fail-open]
    Redis 실패 시 warning 만 — 인증 흐름은 막지 않음. 다음 로그인 또는
    list_sessions 호출 시 누락된 세션 보이지 않을 뿐.
    """
    if not email or not jti or not exp_epoch:
        return
    now_ms = int(time.time() * 1000)
    exp_ms = int(exp_epoch * 1000)
    try:
        r = await _redis()
        # HASH (jti → 메타)
        await r.hset(
            _session_key(jti),
            mapping={
                "email": email.lower(),
                "ua": user_agent or "",
                "ip": ip or "",
                "created_at": str(now_ms),
                "device_label": device_label or "",
            },
        )
        # exp 시점에 자동 청소
        await r.expireat(_session_key(jti), exp_epoch)
        # ZSET (email → jti, score=exp)
        await r.zadd(_user_set_key(email), {jti: exp_ms})
    except Exception as e:  # noqa: BLE001
        logger.warning("record_session failed (email=%s, jti=%s): %s", email, jti[:8], e)


async def unregister_session(jti: str) -> None:
    """로그아웃 시 세션 메타 + user index 제거.

    [관계]
    이 함수는 token_blacklist.revoke 와 함께 호출. token_blacklist 가 jti 차단,
    이쪽은 list_sessions 에서 사라지게 정리. 둘 다 fail-open.
    """
    if not jti:
        return
    try:
        r = await _redis()
        # 먼저 email 회수 (ZSET cleanup 용)
        email_b = await r.hget(_session_key(jti), "email")
        email = email_b.decode() if isinstance(email_b, (bytes, bytearray)) else email_b
        await r.delete(_session_key(jti))
        if email:
            await r.zrem(_user_set_key(email), jti)
    except Exception as e:  # noqa: BLE001
        logger.warning("unregister_session failed (jti=%s): %s", jti[:8], e)


async def list_sessions(email: str) -> List[SessionInfo]:
    """사용자의 활성 세션 목록.

    만료된 jti (score < now) 는 ZREMRANGEBYSCORE 로 정리 (lazy cleanup).
    HASH 본체가 expire 됐어도 ZSET 에 남을 수 있어 정리 후 lookup.
    """
    if not email:
        return []
    try:
        r = await _redis()
        now_ms = int(time.time() * 1000)
        # 만료된 항목 정리 (atomic 한 한 줄)
        await r.zremrangebyscore(_user_set_key(email), 0, now_ms)
        jtis_raw = await r.zrange(_user_set_key(email), 0, -1)
        out: List[SessionInfo] = []
        for jti_raw in jtis_raw or []:
            jti = jti_raw.decode() if isinstance(jti_raw, (bytes, bytearray)) else jti_raw
            data = await r.hgetall(_session_key(jti))
            if not data:
                # ZSET 에 있지만 HASH 가 expire — ZREM 으로 정리
                await r.zrem(_user_set_key(email), jti)
                continue
            # bytes 정규화
            d = {
                (k.decode() if isinstance(k, (bytes, bytearray)) else k):
                (v.decode() if isinstance(v, (bytes, bytearray)) else v)
                for k, v in data.items()
            }
            out.append(SessionInfo(
                jti=jti,
                email=d.get("email", ""),
                user_agent=d.get("ua", ""),
                ip=d.get("ip", ""),
                created_at=int(d.get("created_at") or 0),
                device_label=d.get("device_label", ""),
            ))
        # 최신순 (created_at desc)
        out.sort(key=lambda s: s.created_at, reverse=True)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("list_sessions failed (email=%s): %s", email, e)
        return []


async def get_session_email(jti: str) -> Optional[str]:
    """jti 의 소유자 email 회수 — 강제 로그아웃 시 ownership 검증.

    Returns:
        email | None (없거나 Redis 실패)
    """
    if not jti:
        return None
    try:
        r = await _redis()
        email_b = await r.hget(_session_key(jti), "email")
        if email_b is None:
            return None
        return email_b.decode() if isinstance(email_b, (bytes, bytearray)) else email_b
    except Exception as e:  # noqa: BLE001
        logger.warning("get_session_email failed: %s", e)
        return None
