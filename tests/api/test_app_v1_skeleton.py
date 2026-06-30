"""BE-E01-T01 회귀 — 앱 골격.

검증:
- GET /api/v1/version → 200 + camelCase 직렬화(serverTime)
- /api/v1 prefix 적용 (prefix 없는 경로는 404)
- 도메인 디렉터리가 build_v1_router() 로 자동 등록
- 헬스 유지
- KST/UTC 시간대 util
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.api.v1 import _discover_domains, build_v1_router
from app.common.timezone import now_kst_iso, to_kst, to_utc
from app.main import app

client = TestClient(app)


def test_version_endpoint_200_and_camelcase():
    r = client.get("/api/v1/version")
    assert r.status_code == 200
    body = r.json()
    # camelCase 직렬화 — serverTime (snake_case server_time 아님)
    assert "serverTime" in body
    assert "server_time" not in body
    assert body["service"] == "gayoje-server"
    assert body["version"]
    assert body["env"]


def test_v1_prefix_applied():
    assert client.get("/api/v1/version").status_code == 200
    # prefix 없는 경로는 404
    assert client.get("/version").status_code == 404


def test_all_domains_auto_registered():
    domains = set(_discover_domains())
    expected = {
        "version", "festivals", "search", "geo", "calendar", "favorites",
        "reports", "subscriptions", "payments", "notifications",
        "instructors", "auth", "users", "admin", "intake", "ingestion",
    }
    assert expected.issubset(domains)
    # 빈 도메인 라우터도 예외 없이 포함돼 v1 라우터가 구성됨
    v1 = build_v1_router()
    paths = {getattr(r, "path", "") for r in v1.routes}
    assert "/api/v1/version" in paths


def test_health_still_served():
    assert client.get("/health").json() == {"status": "healthy"}


def test_timezone_kst_utc_roundtrip():
    dt_utc = datetime(2026, 6, 30, 0, 0, 0, tzinfo=timezone.utc)
    k = to_kst(dt_utc)
    assert k.utcoffset().total_seconds() == 9 * 3600
    assert k.hour == 9  # 00:00 UTC == 09:00 KST
    # naive 는 UTC 로 간주
    assert to_utc(datetime(2026, 1, 1, 0, 0, 0)).tzinfo == timezone.utc
    assert now_kst_iso().endswith("+09:00")
