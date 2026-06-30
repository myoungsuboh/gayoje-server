"""
arq worker entry point.

실행:
  arq app.queue.worker.WorkerSettings

운영:
  backend 와 분리된 별도 `worker` 컨테이너에서 실행. docker-compose.yml 의
  `worker` 서비스가 이 entrypoint 를 띄운다. backend 와 같은 이미지(build: .)
  를 공유하므로 코드 변경이 곧 worker 변경.

  단일 워커 (dev / 소규모 운영):
    ARQ_QUEUE_NAME 미설정 → "harness:jobs" 단일 큐. client._enqueue 도 분리 env
    미설정이면 모두 동일 큐로 → backward compat.

  등급별 워커 분리 (운영 권장 — 2026-05 도입):
    backend container env:
      ARQ_QUEUE_NAME_PRO=harness:jobs:pro
      ARQ_QUEUE_NAME_FREE=harness:jobs:free
    worker-pro container env:
      ARQ_QUEUE_NAME=harness:jobs:pro   (Pro 사용자 작업만 처리, SLA 보장)
    worker-free container env:
      ARQ_QUEUE_NAME=harness:jobs:free  (Free 사용자 작업 처리)

    효과: Free 폭증이 Pro 처리 시간을 망가뜨리지 않음. Pro 사용자가 항상 빈
    워커 슬롯 보유. docker-compose 의 worker-pro 는 1+ replica, worker-free 는
    부하에 따라 scale.

  scale:
    docker compose up -d --scale worker-pro=1 --scale worker-free=2

  각 worker 인스턴스는 `ARQ_MAX_JOBS` 만큼 동시 처리.

  분리 효과:
    - post_meeting 같은 long-running job 이 web request latency 와 격리
    - worker OOM 으로 죽어도 web 살아있음 (반대도 성립)
    - worker 환경에는 JWT_SECRET / OAuth secret 미주입 → 침해 표면 축소
"""
from __future__ import annotations

import functools
import os
import time

from app.core.metrics import record_job
from app.core.observability import capture_exception
from app.queue.jobs import (
    analyze_lineage_job,
    autofill_api_specs_job,
    cleanup_master_prd_job,
    cps_pipeline_job,
    create_md_job,
    delete_meeting_job,
    design_pipeline_job,
    generate_fix_spec_job,
    github_onboard_job,
    on_shutdown,
    on_startup,
    post_meeting_pipeline_job,
    prd_pipeline_job,
    prefetch_extract_job,
    recommend_skills_job,
    run_lint_job,
)
from app.queue.settings import QUEUE_NAME, redis_settings


# ===== Job 계측 래퍼 (2026-05 관측성) =====
def _instrument(job_func):
    """job 성공/실패 카운트 + 처리시간 메트릭, 미처리 예외 Sentry 전송.

    functools.wraps 로 __name__ 보존 → arq 가 동일 이름으로 등록/디스패치하므로
    client._enqueue 의 함수명 기반 enqueue 와 호환. 제어 흐름 예외(취소/쿼터초과)는
    job 내부에서 이미 catch 되므로 여기엔 진짜 미처리 예외만 도달.
    asyncio.CancelledError(BaseException)는 `except Exception` 이 안 잡음 — 정상 종료/타임아웃 노이즈 방지.
    """

    @functools.wraps(job_func)
    async def wrapper(ctx, *args, **kwargs):
        start = time.perf_counter()
        job_name = job_func.__name__
        try:
            result = await job_func(ctx, *args, **kwargs)
            record_job(job_name, "success", time.perf_counter() - start)
            return result
        except Exception as exc:  # noqa: BLE001 — 계측 후 재전파 (arq 재시도 유지).
            record_job(job_name, "failure", time.perf_counter() - start)
            capture_exception(exc, component="worker", job=job_name)
            raise

    return wrapper


class WorkerSettings:
    """
    arq 가 import 해서 사용하는 worker 설정.

    동시성/재시도 정책:
      - max_jobs: 동시 실행 가능 job 수. 256MB 머신은 1~2 권장 (LLM 호출이 메모리 큼).
      - max_tries: 일시 오류 (GeminiError transient) 시 자동 재시도 횟수.
      - job_timeout: 1 job 최대 시간. CPS 30~120 초, design pipeline 대형 PRD 시 10~20분.
        기본 1200s (20분) — Spack + DDD + Architecture 3 stage sequential LLM chain 대응.
        운영에서 ARQ_JOB_TIMEOUT_SEC env 로 오버라이드 가능.
      - keep_result: job 결과를 Redis 에 보관할 시간 (status 조회용).
    """

    # _instrument 로 감싸 메트릭/에러추적 부착 (__name__ 보존 → enqueue 호환).
    functions = [
        _instrument(f)
        for f in (
            cps_pipeline_job,
            post_meeting_pipeline_job,
            prd_pipeline_job,
            prefetch_extract_job,
            design_pipeline_job,
            recommend_skills_job,
            run_lint_job,
            generate_fix_spec_job,
            analyze_lineage_job,
            autofill_api_specs_job,
            delete_meeting_job,
            create_md_job,
            github_onboard_job,
            cleanup_master_prd_job,
        )
    ]
    redis_settings = redis_settings()
    queue_name = QUEUE_NAME

    on_startup = on_startup
    on_shutdown = on_shutdown

    max_jobs = int(os.getenv("ARQ_MAX_JOBS", "2"))
    max_tries = int(os.getenv("ARQ_MAX_TRIES", "3"))
    job_timeout = int(os.getenv("ARQ_JOB_TIMEOUT_SEC", "1200"))  # 20분 — design 3-stage chain 대응
    keep_result = int(os.getenv("ARQ_KEEP_RESULT_SEC", "3600"))  # 1h
