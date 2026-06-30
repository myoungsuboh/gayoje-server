"""
extract 단계 결과 캐시 — batch 파이프라이닝의 prefetch 기반.

[목적]
extract(순수 LLM: cps_agent + prd_extract + prd_graph) 결과를 Redis 에 저장해,
다음 버전 처리 전에 prefetch_extract_job 이 미리 계산 → 본 post_meeting job 이
재사용(LLM 3회 skip). 버전당 무거운 LLM 호출이 5→2 로 줄어 batch 벽시계 단축.

[데이터 안전]
이 캐시는 **휘발성 Redis (TTL)** 에만 쓴다. Neo4j 그래프엔 접근 0 — 즉 캐시가
틀리거나 사라져도 그래프를 손상시킬 수 없다. 캐시 미스/Redis 부재 시 None 을
반환해 호출자가 직접 extract 를 계산하는 기존 동작으로 안전 강등(graceful).

[single-flight]
prefetch 와 본 job 이 같은 키를 동시에 계산하면 토큰이 이중 과금된다(과금 cap
민감). try_acquire_extract_lock 로 한쪽만 계산하도록 조율 — 실패 측은 결과를 짧게
polling 하거나 직접 계산. lock 실패/부재 시엔 진행 허용(최악=일시적 중복 계산,
정합성 영향 0).

[키]
(project, version, content) 결정적. prefetch 와 본 job 이 동일 raw content 를
받으므로 같은 키를 산출한다. 앞뒤 공백은 normalize 해 사소한 차이로 인한 미스 방지.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# [2026-05-28] 캐시 무효화 버전 — extract 로직/프롬프트(parse_cps_for_prd fallback,
# active-fill, prd_graph reconcile 등)가 바뀌면 이 값을 bump 한다. 키 prefix 에 포함되므로
# bump 시 옛 엔트리는 자동으로 도달 불가(cache miss) → 재처리가 fresh 하게 새 로직을 탄다.
# (배포 후 stale 결과로 같은 입력이 옛 PRD 를 돌려주던 사고 방지.)
# v3 (PR #80): _reconcile_screens_in_prd_graph 의 Story/Screen 합성 + 🖥️-less header
# 매칭이 들어가서, 같은 raw content 의 옛 캐시(=reconcile 미적용 prd_graph) 를 모두 무효화.
# v4 (2026-06-04): parse_cps_for_prd 에 raw-meeting fallback 추가 + _is_valid_extract 가
# 빈 prd_markdown 거부. CPS delta 가 빈 채로 만들어진 옛 캐시(=프로젝트명만으로 환각한
# master PRD 를 굳히던 엔트리)를 모두 무효화 → 재처리가 회의 내용으로 fresh 하게 생성.
_CACHE_VERSION = "v4"
_PREFIX = f"harness:extract:{_CACHE_VERSION}:"
DEFAULT_TTL_SEC = 7200       # 2h — batch 전체 길이 여유
DEFAULT_LOCK_TTL_SEC = 180   # 3m — extract 최대 소요 + 여유
_WAIT_TIMEOUT_SEC = 90.0     # 본 job 이 prefetch 완료를 기다리는 최대 시간
_WAIT_POLL_SEC = 1.0


def extract_cache_key(project: str, version: str, content: str) -> str:
    """(project, version, content) 결정적 캐시 키.

    content 는 앞뒤 공백을 제거하고 sha256 — prefetch/본 job 이 동일 raw content 를
    받으므로 같은 키. (extract 프롬프트는 version 도 포함하므로 version 분리 필수.)
    """
    digest = hashlib.sha256((content or "").strip().encode("utf-8")).hexdigest()[:16]
    return f"{_PREFIX}{project}:{version}:{digest}"


async def get_cached_extract(redis: Any, key: str) -> Optional[Dict[str, Any]]:
    """캐시된 extract 결과 반환. miss / Redis 부재 / 파싱 오류 → None (graceful)."""
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001 — 캐시 실패가 job 을 망치면 안 됨
        logger.warning("extract cache get failed (key=%s): %s", key, e)
        return None


async def set_cached_extract(
    redis: Any, key: str, extract: Dict[str, Any], ttl_sec: int = DEFAULT_TTL_SEC
) -> None:
    """extract 결과를 캐시에 저장. Redis 부재/오류 시 조용히 무시 (best-effort)."""
    if redis is None:
        return
    try:
        await redis.set(key, json.dumps(extract, ensure_ascii=False), ex=ttl_sec)
    except Exception as e:  # noqa: BLE001
        logger.warning("extract cache set failed (key=%s): %s", key, e)


async def try_acquire_extract_lock(
    redis: Any, key: str, ttl_sec: int = DEFAULT_LOCK_TTL_SEC
) -> bool:
    """single-flight 락 획득 시도. 성공 시 True (이 워커가 계산 담당).

    Redis 부재/오류 시 True 반환 — 락 없이도 진행 허용 (최악=중복 계산, 정합성 영향 0).
    """
    if redis is None:
        return True
    try:
        res = await redis.set(key + ":lock", "1", nx=True, ex=ttl_sec)
        return bool(res)
    except Exception as e:  # noqa: BLE001
        logger.warning("extract lock acquire failed (key=%s): %s", key, e)
        return True


async def wait_for_cached_extract(
    redis: Any,
    key: str,
    timeout_sec: float = _WAIT_TIMEOUT_SEC,
    poll_interval: float = _WAIT_POLL_SEC,
) -> Optional[Dict[str, Any]]:
    """락을 다른 워커(prefetch)가 쥔 상태에서, 그 워커가 결과를 채울 때까지 짧게 대기.

    timeout 내 결과가 나타나면 반환, 아니면 None (→ 호출자가 직접 계산).
    """
    if redis is None:
        return None
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        cached = await get_cached_extract(redis, key)
        if cached is not None:
            return cached
        await asyncio.sleep(poll_interval)
    return None
