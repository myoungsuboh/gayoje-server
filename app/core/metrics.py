"""
기본 메트릭(Metrics) — Prometheus 노출 + HTTP/job 계측.

[배경 — 2026-05 B2C 운영 강화]
관측성 공백 중 "메트릭" 담당. 이전엔 요청량/지연/에러율/job 성공률을 알 수 없어
이탈/장애 징후를 사후에도 못 봄. 가벼운 in-process 카운터(prometheus_client)로
/metrics 에 노출 → Grafana/UptimeKuma 등에서 스크랩.

[수집 항목]
  - http_requests_total{method,path,status}      : 요청 수
  - http_request_duration_seconds{method,path}   : 지연 히스토그램
  - http_requests_in_progress                    : 동시 처리 중 요청
  - worker_jobs_total{job,outcome}               : arq job 성공/실패 수
  - worker_job_duration_seconds{job}             : job 처리 시간

[카디널리티 방어]
  path 라벨은 매칭된 라우트 템플릿(`/api/billing/{id}`)만 사용 — 미매칭(404 등)은
  "other" 버킷으로 접어 path-param 폭발 방지.

[의존성 graceful degradation]
  prometheus_client 미설치 시 모든 함수가 no-op, /metrics 는 503 대신 빈 응답.
  기존 배포/테스트 환경을 절대 깨지 않음.
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover — 운영엔 항상 설치(requirements.txt).
    _PROM_AVAILABLE = False


if _PROM_AVAILABLE:
    HTTP_REQUESTS = Counter(
        "http_requests_total",
        "총 HTTP 요청 수",
        ["method", "path", "status"],
    )
    HTTP_LATENCY = Histogram(
        "http_request_duration_seconds",
        "HTTP 요청 처리 시간(초)",
        ["method", "path"],
    )
    HTTP_IN_PROGRESS = Gauge(
        "http_requests_in_progress",
        "동시 처리 중 HTTP 요청 수",
    )
    WORKER_JOBS = Counter(
        "worker_jobs_total",
        "arq worker job 처리 수",
        ["job", "outcome"],
    )
    WORKER_JOB_LATENCY = Histogram(
        "worker_job_duration_seconds",
        "arq worker job 처리 시간(초)",
        ["job"],
    )


def _route_template(request: Request) -> str:
    """매칭된 라우트의 path 템플릿. 미매칭이면 'other' (카디널리티 방어)."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "other"


class MetricsMiddleware(BaseHTTPMiddleware):
    """요청 수/지연/동시성 계측 — prometheus 미설치 시 통과만 함."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not _PROM_AVAILABLE:
            return await call_next(request)

        HTTP_IN_PROGRESS.inc()
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - start
            HTTP_IN_PROGRESS.dec()
            path = _route_template(request)
            method = request.method
            HTTP_REQUESTS.labels(method=method, path=path, status=str(status_code)).inc()
            HTTP_LATENCY.labels(method=method, path=path).observe(elapsed)


def record_job(job: str, outcome: str, duration_sec: Optional[float] = None) -> None:
    """worker job 결과 기록 — outcome: "success" | "failure". no-op safe."""
    if not _PROM_AVAILABLE:
        return
    WORKER_JOBS.labels(job=job, outcome=outcome).inc()
    if duration_sec is not None:
        WORKER_JOB_LATENCY.labels(job=job).observe(duration_sec)


def metrics_available() -> bool:
    return _PROM_AVAILABLE


# 워커 메트릭 노출 서버 — 중복 기동 방지 플래그 (on_startup 멱등성).
_worker_server_started = False


def start_worker_metrics_server(port: int) -> bool:
    """arq 워커용 독립 Prometheus 노출 서버 기동. 반환: 기동 여부.

    워커는 ASGI HTTP 서버가 없어 backend 의 /metrics 로 worker_jobs_total /
    worker_job_duration_seconds 를 볼 수 없다. prometheus_client.start_http_server
    가 데몬 스레드로 가벼운 WSGI 서버를 띄워 같은 docker 네트워크의 Prometheus 가
    `worker:<port>/metrics` 로 스크랩하게 한다.

    안전장치:
      - prometheus_client 미설치 → no-op(False). 기존 환경 보호.
      - 이미 기동됨(같은 프로세스 재호출) → 중복 bind 방지 후 True.
      - bind 실패 등 모든 예외는 흡수 — 관측성 기동이 워커 부팅을 막으면 안 됨.

    [스케일 주의] 워커 컨테이너는 각자 별도 netns 라 같은 포트를 충돌 없이 bind.
    단, `--scale worker-free=N` 처럼 replica 가 여럿이면 Prometheus 가 DNS/도커
    서비스 디스커버리로 각 인스턴스를 찾아야 한다(static `worker:port` 는 1대만 본다).
    """
    global _worker_server_started
    if not _PROM_AVAILABLE:
        return False
    if _worker_server_started:
        return True
    try:
        from prometheus_client import start_http_server

        start_http_server(port)
        _worker_server_started = True
        return True
    except Exception:  # noqa: BLE001 — 노출 서버 실패가 워커 job 처리를 막지 않게.
        import logging

        logging.getLogger("harness.app").warning(
            "워커 메트릭 노출 서버 기동 실패 (port=%s) — 메트릭 스크랩 불가, job 처리는 계속.",
            port,
        )
        return False


def render_metrics() -> tuple[bytes, str]:
    """Prometheus 텍스트 노출 포맷 반환 — (body, content_type)."""
    if not _PROM_AVAILABLE:
        return b"", "text/plain; charset=utf-8"
    return generate_latest(), CONTENT_TYPE_LATEST
