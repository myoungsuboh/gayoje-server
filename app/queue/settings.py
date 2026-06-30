"""
arq RedisSettings.

REDIS_URL 환경변수가 우선. 없으면 host/port/db 개별 변수 사용.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

from arq.connections import RedisSettings


def redis_settings() -> RedisSettings:
    """
    arq 가 사용할 RedisSettings 를 환경에서 구성.

    Priority:
      1. REDIS_URL=redis://[:pw@]host:port/db
      2. REDIS_HOST / REDIS_PORT / REDIS_PASSWORD / REDIS_DB 개별 변수

    [Fast-fail 정책]
    arq 의 default 는 conn_timeout=5 + conn_retries=5 (총 ~25초 대기) — Redis 가
    꺼져있을 때 token_blacklist 의 매 요청이 그만큼 hang. dev/소규모 운영에서는
    Redis 가 항상 켜져있지 않을 수 있으므로 빠른 fail 정책 (1초 × 1회) 으로
    오버라이드. 운영 트래픽이 커지면 환경변수로 늘릴 수 있도록 opt.
    """
    conn_timeout = int(os.getenv("REDIS_CONN_TIMEOUT_SEC", "1"))
    conn_retries = int(os.getenv("REDIS_CONN_RETRIES", "1"))
    conn_retry_delay = int(os.getenv("REDIS_CONN_RETRY_DELAY_SEC", "1"))

    url = os.getenv("REDIS_URL")
    if url:
        p = urlparse(url)
        return RedisSettings(
            host=p.hostname or "localhost",
            port=p.port or 6379,
            password=(p.password or None),
            database=int((p.path or "/0").lstrip("/") or 0),
            conn_timeout=conn_timeout,
            conn_retries=conn_retries,
            conn_retry_delay=conn_retry_delay,
        )
    return RedisSettings(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        database=int(os.getenv("REDIS_DB", "0")),
        conn_timeout=conn_timeout,
        conn_retries=conn_retries,
        conn_retry_delay=conn_retry_delay,
    )


# 큐 이름. 여러 환경(dev/prod)이 같은 Redis 를 공유하면 prefix 분리.
#
# [2026-05] 등급별 큐 분리.
# Pro/Pro+/Pro Max 사용자 작업과 Free 사용자 작업을 다른 큐로 라우팅 →
# Free 사용자 폭증이 Pro 처리 SLA 를 깨지 않게 격리.
# 워커 구성 (docker-compose):
#   worker-pro  → ARQ_QUEUE_NAME=harness:jobs:pro  (Pro 전용 SLA 보장)
#   worker-free → ARQ_QUEUE_NAME=harness:jobs:free (Free 처리)
# 단일 워커 운영 시: ARQ_QUEUE_NAME=harness:jobs (legacy 통합) — 기존 호환.
QUEUE_NAME = os.getenv("ARQ_QUEUE_NAME", "harness:jobs")
# [기본값 정책] 두 env 미설정 시 PRO=FREE=QUEUE_NAME → 단일 워커 dev 환경 backward compat.
# 운영에서 분리하려면 두 env 모두 명시 (예: harness:jobs:pro / harness:jobs:free)
# 후 worker-pro / worker-free 컨테이너의 ARQ_QUEUE_NAME 을 각각 설정.
PRO_QUEUE_NAME = os.getenv("ARQ_QUEUE_NAME_PRO") or QUEUE_NAME
FREE_QUEUE_NAME = os.getenv("ARQ_QUEUE_NAME_FREE") or QUEUE_NAME
