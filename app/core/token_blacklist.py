"""
JWT jti 블랙리스트 — Redis SET + TTL.

[설계]
- 단일 Fly 머신에 묶인 SQLite 보다, Redis 가 수평 확장 친화 + TTL 자동 청소.
- arq 가 이미 같은 Redis 를 쓰므로 추가 리소스 없음.
- 키 포맷: `revoked_jti:<jti>` → 값 `"1"`. EXPIREAT 으로 토큰 exp 시점에 자동 제거.

[멱등성]
- `revoke(jti, exp)` 는 같은 jti 로 여러 번 호출돼도 안전 (SET overwrite).
- exp 가 이미 과거면 즉시 만료 → Redis 가 바로 evict.

[Fail-open + Circuit breaker]
- Redis 가용 불가 시 `is_revoked` 는 False (fail-open) — 매 요청 hang 방지.
- 한 번 실패하면 `_CB_COOLDOWN_SEC` (default 60초) 동안 Redis 시도 자체를 skip.
  arq 의 retry policy 가 짧지 않으면 token_blacklist 의 모든 호출이 같은 timeout
  을 누적해 응답 시간을 망치는데, circuit breaker 가 이를 차단.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from arq.connections import ArqRedis

from app.queue import client as queue_client

logger = logging.getLogger(__name__)

_KEY_PREFIX = "revoked_jti:"

# Circuit breaker — Redis 한 번 fail 후 N초 동안 시도 안 함.
_CB_COOLDOWN_SEC = int(os.getenv("REDIS_CB_COOLDOWN_SEC", "60"))
_last_failure_at: float = 0.0


def _key(jti: str) -> str:
    return f"{_KEY_PREFIX}{jti}"


def _cb_open() -> bool:
    """현재 circuit breaker 가 OPEN (Redis 호출 skip) 상태인가?"""
    return (time.monotonic() - _last_failure_at) < _CB_COOLDOWN_SEC


def _mark_failure() -> None:
    global _last_failure_at
    _last_failure_at = time.monotonic()


def _mark_success() -> None:
    """성공 시 cooldown 즉시 reset (다음 호출부터 정상)."""
    global _last_failure_at
    _last_failure_at = 0.0


async def _redis() -> ArqRedis:
    """arq pool 을 그대로 사용 (Redis client 호환)."""
    return await queue_client.get_pool()


async def is_revoked(jti: str) -> bool:
    """jti 가 블랙리스트에 있으면 True. Redis 미가용이면 False (fail-open).

    fail-open 정책 근거:
      - Redis 다운 시 모든 로그인 사용자를 강제 로그아웃하는 건 가용성 큰 손해.
      - 그러나 명시 로그아웃은 Redis 가용 시점에만 효과적 — 운영 알람 필요.
      - 동일 토큰의 만료(exp) 검증은 JWT 자체에서 이루어지므로 보안 하한선 존재.

    [Circuit breaker]
      - 직전 호출이 실패했으면 `_CB_COOLDOWN_SEC` 동안 Redis 호출 자체 skip.
      - 매 인증 요청마다 timeout 누적되어 응답 hang 되는 것 방지.
    """
    if _cb_open():
        return False  # 빠른 fail-open
    try:
        r = await _redis()
        val = await r.get(_key(jti))
        _mark_success()
        return val is not None
    except Exception as e:  # noqa: BLE001
        _mark_failure()
        logger.error(
            "token_blacklist.is_revoked failed (fail-open, CB armed %ds): %s",
            _CB_COOLDOWN_SEC, e,
        )
        return False


async def revoke(jti: str, exp_epoch: int) -> None:
    """
    jti 를 블랙리스트에 등록. exp_epoch (초) 시점에 자동 제거.

    exp_epoch 이 이미 과거여도 호출자 책임 없음 — TTL=0 으로 즉시 evict.
    """
    if _cb_open():
        # Redis 다운 중 — 로그아웃은 client-side 토큰 삭제 + JWT exp 에 의존
        return
    ttl = max(1, int(exp_epoch) - int(time.time()))
    r = await _redis()
    await r.set(_key(jti), "1", ex=ttl)
    _mark_success()


async def revoke_if_new(jti: Optional[str], exp_epoch: Optional[int]) -> bool:
    """
    jti / exp 가 모두 유효하면 등록하고 True. 누락 / Redis 실패 시 False.
    """
    if not jti or not exp_epoch:
        return False
    try:
        await revoke(jti, int(exp_epoch))
        return True
    except Exception as e:  # noqa: BLE001
        _mark_failure()
        logger.error("token_blacklist.revoke failed: %s", e)
        return False
