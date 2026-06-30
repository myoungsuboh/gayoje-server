"""
프로젝트 master 쓰기 잠금 (2026-06) — Redis SET NX 기반 직렬화.

[배경 — 멀티 디바이스 이중 작업]
배치(순차 처리)는 FE(브라우저) 주도라, 웹에서 배치 중에 모바일로 접속하면 같은
프로젝트에 또 작업을 시작할 수 있다. 계정당 동시 job 제한(CONCURRENCY_LIMIT=2)은
'개수'만 보고 '같은 프로젝트'는 안 보므로 정확히 2개가 동시 실행 가능하고,
merge 단계는 "master 읽기 → LLM 계산(30~120s) → master 쓰기" 구조에 잠금이 없어
**마지막 쓰기가 먼저 쓴 잡의 delta 를 덮는 lost update** 가 발생한다.

[메커니즘]
키 `harness:lock:master:{project_key}` 에 SET NX EX. project_key 는
scoped_project(project_name, team_id) — master_id(project_scope.cps_master_id)와
같은 격리 단위라서, 개인 프로젝트뿐 아니라 **팀의 두 멤버가 같은 master 를 동시에
만지는 것까지** 막는다 (user_email 을 키에 넣으면 이 케이스가 뚫림).

  - acquire: SET NX 성공까지 1s 폴링, 최대 _WAIT_TIMEOUT_SEC 대기.
    값=holder(job_id) — 토큰 비교로 남의 락 해제 방지.
  - release: 값이 내 holder 일 때만 DEL (크래시로 TTL 만료 후 다른 잡이 잡은 락을
    뒤늦게 깬 잡이 지우는 사고 방지).
  - TTL _LOCK_TTL_SEC: 크래시해 release 못 한 락 자동 해제 → 영구 차단 방지.

[가용성 우선 — concurrency.py 와 동일 철학]
Redis 장애/없음 → 잠금 없이 통과(allow). 게이트가 막혀 정상 사용자가 못 쓰는
사고보다, 드문 race 를 일시적으로 못 막는 쪽이 안전.

[프레임워크 비결합]
순수 Redis 로직만. 호출자(queue/jobs.py)가 arq ctx 의 redis 를 전달.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)

_LOCK_PREFIX = "harness:lock:master:"

# 락 TTL — merge 최장 케이스(~120s)와 post_meeting CPS+PRD 합산보다 충분히 길게.
# 크래시 잡의 락은 이 시간 후 자동 해제.
_LOCK_TTL_SEC = int(os.getenv("MASTER_LOCK_TTL_SEC", "600"))  # 10분

# 대기 한도 — 앞 잡(merge 30~120s) 1개 대기 가정이면 충분하고, arq job_timeout
# (기본 1200s) 안에 들어가야 함. 초과 시 raise → arq 재시도(3회)로 자연 회복.
_WAIT_TIMEOUT_SEC = int(os.getenv("MASTER_LOCK_WAIT_SEC", "300"))  # 5분

_POLL_INTERVAL_SEC = 1.0


class MasterLockTimeout(Exception):
    """대기 한도 내에 락을 못 잡음 — 호출자(잡)가 raise 해 arq 재시도로 회복."""


def _key(project_key: str) -> str:
    return f"{_LOCK_PREFIX}{project_key}"


async def _try_acquire(redis: Any, project_key: str, holder: str) -> bool:
    """1회 시도 — SET NX EX. Redis 오류는 True(통과) — 가용성 우선.

    [재진입] NX 실패 시 현재 값이 내 holder 면 True — 워커 크래시로 release 못 한
    락이 남았을 때 arq 가 **같은 job_id** 로 재시도하면 자기 락에 5분 막혀
    MasterLockTimeout 으로 재시도를 소모하던 엣지 회복 (TTL 갱신으로 보유 연장).
    """
    try:
        ok = await redis.set(_key(project_key), holder, nx=True, ex=_LOCK_TTL_SEC)
        if ok:
            return True
        current = await redis.get(_key(project_key))
        if current is not None and not isinstance(current, str):
            current = current.decode("utf-8", errors="replace")
        if current == holder:
            # 내 락 — TTL 연장 후 재진입.
            await redis.set(_key(project_key), holder, ex=_LOCK_TTL_SEC)
            return True
        return False
    except Exception as e:  # noqa: BLE001 — 가용성 우선
        logger.warning("master_lock: redis set 실패 — 잠금 없이 통과 (key=%s): %s",
                       project_key, e)
        return True


async def _release(redis: Any, project_key: str, holder: str) -> None:
    """내 holder 일 때만 DEL. 오류는 swallow (TTL 이 안전망)."""
    try:
        current = await redis.get(_key(project_key))
        if current is not None and not isinstance(current, str):
            current = current.decode("utf-8", errors="replace")
        if current == holder:
            await redis.delete(_key(project_key))
    except Exception as e:  # noqa: BLE001
        logger.warning("master_lock: release 실패 — TTL 만료로 자동 해제 예정 (key=%s): %s",
                       project_key, e)


@asynccontextmanager
async def master_write_lock(
    redis: Any, project_key: str, holder: str,
    *, wait_timeout: Optional[float] = None,
) -> AsyncIterator[None]:
    """프로젝트 master 쓰기 구간 직렬화 컨텍스트.

    redis 가 None(단위 테스트/legacy worker)이거나 project_key 가 비면 잠금 없이
    통과 — 기존 동작 보존. 대기 한도 초과 시 MasterLockTimeout.

    wait_timeout: 대기 한도 오버라이드(초). 워커 잡은 기본(5분, arq 재시도로 회복),
    sync HTTP 라우트(deleteMeeting 등)는 짧게 주고 409 로 변환하는 용도.
    """
    if redis is None or not project_key:
        yield
        return

    effective_wait = wait_timeout if wait_timeout is not None else _WAIT_TIMEOUT_SEC
    deadline = time.monotonic() + effective_wait
    waited = False
    while not await _try_acquire(redis, project_key, holder):
        waited = True
        if time.monotonic() >= deadline:
            raise MasterLockTimeout(
                f"프로젝트 '{project_key}' 의 master 잠금을 {effective_wait:.0f}s 안에 "
                "획득하지 못했습니다 — 같은 프로젝트의 다른 작업이 오래 실행 중입니다."
            )
        await asyncio.sleep(_POLL_INTERVAL_SEC)
    if waited:
        logger.info("master_lock: 대기 후 획득 (key=%s holder=%s)", project_key, holder)
    try:
        yield
    finally:
        await _release(redis, project_key, holder)
