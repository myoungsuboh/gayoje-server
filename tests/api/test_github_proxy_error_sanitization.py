"""
github_proxy _map_error sanitization 회귀 — 2026-05 보안 점검 #4.

[보호하는 동작]
이전: 502 응답 detail 에 GitHubError 원문 (resp.text[:200] 포함) 그대로 노출 →
       GitHub 응답 body 일부 누설.
변경: 사전 정의된 안전 메시지만 클라이언트에 노출. 원본은 server 로그.
"""
from __future__ import annotations

import logging

import pytest

from app.api.github_proxy_routes import _map_error, _SAFE_DETAILS
from app.clients.github_client import GitHubError


@pytest.mark.parametrize("status", [401, 403, 404, 415])
def test_known_status_uses_predefined_detail(status):
    """[회귀] 사전 정의된 status 는 안전 메시지만."""
    e = GitHubError(
        f"GitHub {status} (owner=secret repo=internal): "
        f"raw body with email=victim@x.com api_id=ABC123",
        status=status,
    )
    exc = _map_error(e)
    assert exc.status_code == status
    assert exc.detail == _SAFE_DETAILS[status]
    # 원본 메시지 어떤 부분도 누설 안 됨
    assert "victim@x.com" not in exc.detail
    assert "ABC123" not in exc.detail
    assert "secret" not in exc.detail


def test_unknown_5xx_becomes_502_with_generic_message():
    """[회귀] 500/503 같이 unknown status 는 502 + 일반 메시지."""
    e = GitHubError("internal: stack trace leaked here", status=500)
    exc = _map_error(e)
    assert exc.status_code == 502
    assert "GitHub 호출에 실패했습니다" in exc.detail
    assert "stack trace" not in exc.detail


def test_no_status_becomes_502():
    """[회귀] status=None (네트워크 에러 등) 도 502 로 안전 메시지."""
    e = GitHubError("connection timeout to api.github.com from 10.0.0.5", status=None)
    exc = _map_error(e)
    assert exc.status_code == 502
    assert "10.0.0.5" not in exc.detail
    assert "api.github.com" not in exc.detail


def test_raw_error_logged_to_server(caplog):
    """[회귀] sanitization 후에도 운영 디버깅 위해 원본은 logger.warning 으로."""
    e = GitHubError("GitHub 502 (owner=a repo=b): internal-error-id=XYZ", status=502)
    with caplog.at_level(logging.WARNING, logger="app.api.github_proxy_routes"):
        _map_error(e)
    # logged 안 메시지에는 원본 detail 이 포함 (server-only).
    log_text = caplog.text
    assert "XYZ" in log_text or "GitHub proxy error" in log_text


def test_safe_details_keys_match_expected_statuses():
    """[회귀] 401/403/404/415 4개 status 만 사전 정의 — 다른 건 일반화."""
    assert set(_SAFE_DETAILS.keys()) == {401, 403, 404, 415}


def test_403_detail_does_not_include_context():
    """[회귀] 가장 흔한 403 (rate limit) 의 detail 이 context 정보 미포함."""
    e = GitHubError("GitHub API 제한 (403, rate limit 또는 권한 부족): owner=acme repo=top-secret", status=403)
    exc = _map_error(e)
    assert "top-secret" not in exc.detail
    assert "acme" not in exc.detail
