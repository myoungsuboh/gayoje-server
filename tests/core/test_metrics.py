"""
메트릭(metrics) 모듈 + /metrics 엔드포인트 테스트.

[검증 범위]
  - record_job: prometheus 카운터/히스토그램 증가, no-op safe.
  - MetricsMiddleware: 요청 계측 → http_requests_total 노출.
  - /metrics 엔드포인트: 활성/비활성(METRICS_ENABLED) 토글.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.core import metrics


pytestmark = pytest.mark.skipif(
    not metrics.metrics_available(),
    reason="prometheus_client 미설치 — 메트릭 비활성 환경",
)


def test_render_metrics_returns_text():
    body, content_type = metrics.render_metrics()
    assert isinstance(body, (bytes, bytearray))
    assert "text" in content_type


def test_record_job_increments_counter():
    body_before, _ = metrics.render_metrics()
    metrics.record_job("unit_test_job", "success", 0.5)
    metrics.record_job("unit_test_job", "failure", 1.0)
    body_after, _ = metrics.render_metrics()
    text = body_after.decode()
    assert 'worker_jobs_total{job="unit_test_job",outcome="success"}' in text
    assert 'worker_jobs_total{job="unit_test_job",outcome="failure"}' in text


def test_metrics_endpoint_enabled():
    from app.core.config import settings
    import app.api.main as m

    settings.METRICS_ENABLED = True
    client = TestClient(m.app)
    # 어떤 요청이든 한 번 보내 계측 발생.
    client.get("/health")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
    # 라우트 템플릿이 path 라벨로 — /health 가 기록됨.
    assert "/health" in resp.text


def test_metrics_endpoint_disabled():
    from app.core.config import settings
    import app.api.main as m

    original = settings.METRICS_ENABLED
    settings.METRICS_ENABLED = False
    try:
        client = TestClient(m.app)
        resp = client.get("/metrics")
        assert resp.status_code == 404
    finally:
        settings.METRICS_ENABLED = original


def test_start_worker_metrics_server_idempotent_and_scrapable():
    """워커 노출 서버: 기동되면 worker_jobs_total 이 그 포트의 /metrics 로 보인다."""
    import socket
    import urllib.request

    # 비어있는 포트 확보 (테스트 격리 — 운영 기본 9100 과 충돌 회피).
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    # 기동 전 카운터를 한 번 올려 노출 대상이 있게 함.
    metrics.record_job("worker_server_probe", "success", 0.1)

    # 다른 테스트가 플래그를 세웠을 수 있으니 리셋 — 확보한 빈 포트로 실제 bind 강제.
    metrics._worker_server_started = False
    started = metrics.start_worker_metrics_server(port)
    assert started is True
    # 재호출은 중복 bind 없이 멱등하게 True.
    assert metrics.start_worker_metrics_server(port) is True

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as resp:
        text = resp.read().decode()
    assert 'worker_jobs_total{job="worker_server_probe",outcome="success"}' in text


def test_in_progress_gauge_settles_to_zero():
    from app.core.config import settings
    import app.api.main as m

    settings.METRICS_ENABLED = True
    client = TestClient(m.app)
    client.get("/health")
    body, _ = metrics.render_metrics()
    text = body.decode()
    # 모든 요청 처리 완료 후 in-progress 는 0 으로 수렴.
    for line in text.splitlines():
        if line.startswith("http_requests_in_progress "):
            assert float(line.split()[1]) == 0.0
