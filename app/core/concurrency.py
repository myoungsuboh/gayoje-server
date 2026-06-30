"""
계정당 동시 무거운 job 제한 (2026-06) — Redis ZSET 기반.

[목표]
한 계정이 무거운 LLM job 을 동시에 여러 개 돌리는 것을 차단. 일일 Lite 캡이
'월 총량'을 막는다면, 이건 '순간 병렬/스크립트 폭주' + 워커 용량을 보호한다 (상호보완).

[메커니즘]
사용자별 ZSET `harness:inflight:{email}` — member=task_id, score=enqueue 시각(epoch).
  - acquire: stale(점수 < now - STALE_AGE) 정리 → 개수 확인 → limit 미만이면 추가.
  - release: job 종료 시 ZREM (worker finally). 정확한 즉시 해제 → 다음 작업 바로 가능.
  - stale 정리: 크래시해 release 못 한 슬롯을 STALE_AGE(>job_timeout) 후 자동 회수 →
    사용자가 영구히 막히는 사고 방지.

[가용성 우선]
Redis 장애 / email 없음 → 통과(allow). 게이트 자체가 막혀 정상 사용자가 못 쓰는
사고보다, 어뷰즈를 일시적으로 못 막는 쪽이 안전.

[프레임워크 비결합]
이 모듈은 순수 Redis 로직만. HTTP 429 변환은 호출자(queue.client._enqueue)가 담당.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_INFLIGHT_PREFIX = "harness:inflight:"

# 계정당 동시 무거운 job 한도 (운영 조정 가능). 2026-06 정책: 2.
DEFAULT_CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "2"))

# stale 회수 기준 — job_timeout(기본 1200s) 보다 길게 잡아 '실행 중' job 슬롯을
# 잘못 회수하지 않게. 크래시 슬롯은 이 시간 후 자동 해제.
_STALE_AGE_SEC = int(os.getenv("CONCURRENCY_STALE_AGE_SEC", "1500"))  # 25분

# 동시성 제한 대상 — 무거운 사용자 LLM job (overflow token-mining 벡터).
# prefetch(내부 최적화) / lineage(LLM 없음) / delete·onboard·cleanup(미니flow) 제외.
HEAVY_JOBS = frozenset({
    "post_meeting_pipeline_job",
    "cps_pipeline_job",
    "prd_pipeline_job",
    "design_pipeline_job",
    "run_lint_job",
    "generate_fix_spec_job",
    "create_md_job",
    "recommend_skills_job",
    "autofill_api_specs_job",
})


def _key(email: str) -> str:
    return f"{_INFLIGHT_PREFIX}{email}"


async def try_acquire_slot(
    redis: Any, email: str, task_id: str, *, limit: int = DEFAULT_CONCURRENCY_LIMIT
) -> bool:
    """슬롯 확보 시도. limit 초과면 False(추가 안 함), 아니면 True(추가).

    redis 없음 / email 없음 / Redis 오류 → True(통과, 가용성 우선).
    """
    if not email or redis is None:
        return True
    key = _key(email)
    now = time.time()
    try:
        # 1) 크래시로 남은 stale 슬롯 회수
        await redis.zremrangebyscore(key, "-inf", now - _STALE_AGE_SEC)
        # 2) 현재 in-flight 개수
        count = await redis.zcard(key)
        # 이미 같은 task_id 가 있으면(중복 제출) 새 슬롯 아님 — 통과.
        if count >= limit and not await redis.zscore(key, task_id):
            return False
        # 3) 슬롯 추가 + 키 안전 만료(누수 2차 방어)
        await redis.zadd(key, {task_id: now})
        await redis.expire(key, _STALE_AGE_SEC * 2)
        return True
    except Exception as e:  # noqa: BLE001 — Redis 장애 시 통과 (가용성 우선)
        logger.warning("concurrency acquire 실패 (email=%s) — 통과: %s", email, e)
        return True


async def release_slot(redis: Any, email: str, task_id: str) -> None:
    """job 종료 시 슬롯 해제 (ZREM). 없는 멤버면 no-op (비-heavy job 안전)."""
    if not email or redis is None:
        return
    try:
        await redis.zrem(_key(email), task_id)
    except Exception as e:  # noqa: BLE001 — best-effort (stale 정리가 안전망)
        logger.warning("concurrency release 실패 (email=%s, task=%s): %s", email, task_id, e)


# ─── 프로젝트 단위 inflight (2026-06 멀티디바이스 이중작업) ──────────
#
# [배경] 계정당 제한(위)은 '개수'만 봐서 웹 배치 중 모바일이 같은 프로젝트에
# 또 enqueue 가능(2개까지 허용). master_lock(워커 merge 직렬화)이 데이터는
# 지키지만, 사용자는 이유 모를 대기를 겪는다. enqueue 시점에 같은 프로젝트의
# inflight 잡이 있으면 409 로 명확히 안내 — UX 가드 (데이터 가드는 master_lock).
#
# [대상] master CPS/PRD 를 쓰는 잡만 (master_lock 과 동일 스코프). design/lint
# 등은 master 와 무관해 과차단 방지 위해 제외.

_PROJECT_INFLIGHT_PREFIX = "harness:inflight:project:"

MASTER_WRITE_JOBS = frozenset({
    "post_meeting_pipeline_job",
    "cps_pipeline_job",
    "prd_pipeline_job",
    # [2026-06 감사 G2] delete 도 master rebuild(쓰기) — 다른 기기의 merge 와
    # 동시 enqueue 시 같은 409 안내. (cleanup_master_prd_job 은 의도적으로 제외 —
    # post_meeting 이 auto-enqueue 하므로 게이트에 걸리면 배치 UX 가 깨짐.
    # 데이터는 워커의 master_write_lock 이 보호.)
    "delete_meeting_job",
    # [2026-06 후속] design 은 master CPS/PRD 는 안 쓰지만 설계 그래프를
    # Wipe-and-Redraw(DETACH DELETE→MERGE, 3 stage) 로 써서, 동시 실행 시 stage 별
    # 결과가 섞여 그래프 정합성이 깨진다. autofill 은 그 그래프의 API 노드를 패치 —
    # design 의 wipe 와 겹치면 유실. 둘 다 같은 프로젝트 게이트로 직렬화.
    "design_pipeline_job",
    "autofill_api_specs_job",
})


def _project_key(project_key: str) -> str:
    return f"{_PROJECT_INFLIGHT_PREFIX}{project_key}"


async def try_acquire_project(redis: Any, project_key: str, task_id: str) -> bool:
    """프로젝트 inflight 마커 확보 — 이미 다른 잡이 있으면 False (limit=1 고정).

    같은 task_id 재제출(arq dedup 경로)은 통과. redis 없음/오류 → True(가용성 우선).
    """
    if not project_key or redis is None:
        return True
    key = _project_key(project_key)
    now = time.time()
    try:
        await redis.zremrangebyscore(key, "-inf", now - _STALE_AGE_SEC)
        count = await redis.zcard(key)
        if count >= 1 and not await redis.zscore(key, task_id):
            return False
        await redis.zadd(key, {task_id: now})
        await redis.expire(key, _STALE_AGE_SEC * 2)
        return True
    except Exception as e:  # noqa: BLE001 — 가용성 우선
        logger.warning("project inflight acquire 실패 (key=%s) — 통과: %s", project_key, e)
        return True


async def is_project_busy(redis: Any, project_key: str) -> bool:
    """프로젝트에 inflight master 쓰기 잡이 있는지 조회 (FE 사전 표시용).

    stale 정리 후 zcard>0. redis 없음/오류 → False (busy 아님 — 표시용이라 보수적
    차단보다 미표시가 안전, 실제 차단은 enqueue 게이트가 담당).
    """
    if not project_key or redis is None:
        return False
    key = _project_key(project_key)
    try:
        await redis.zremrangebyscore(key, "-inf", time.time() - _STALE_AGE_SEC)
        return (await redis.zcard(key)) > 0
    except Exception as e:  # noqa: BLE001
        logger.warning("project busy 조회 실패 (key=%s) — False: %s", project_key, e)
        return False


async def release_project(redis: Any, project_key: str, task_id: str) -> None:
    """잡 종료 시 프로젝트 마커 해제. 없는 멤버면 no-op."""
    if not project_key or redis is None:
        return
    try:
        await redis.zrem(_project_key(project_key), task_id)
    except Exception as e:  # noqa: BLE001 — best-effort (stale 정리가 안전망)
        logger.warning("project inflight release 실패 (key=%s, task=%s): %s",
                       project_key, task_id, e)
