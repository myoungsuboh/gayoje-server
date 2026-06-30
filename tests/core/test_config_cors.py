"""
CORS_ORIGINS 부팅 fail-fast 정책 단위 테스트.

[정책]
- 운영(production) 에서 '*' 는 거부 (allow_credentials=True 와 조합 시 위험 표면).
- dev / local 에서는 '*' 허용 (편의).
- 정상 콤마 구분 origin 목록은 양쪽 환경에서 정상 처리.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings


def _make_settings(**overrides) -> Settings:
    """Settings 를 환경변수 없이 직접 인스턴스화 (테스트 격리).

    TOKEN_ENCRYPTION_KEY 도 운영 검증 통과용 stub 으로 채움 (CORS 만 검증 시 무관).
    """
    base = {
        "ENV": "development",
        "JWT_SECRET_KEY": "x" * 32,
        "CORS_ORIGINS": "*",
        "TOKEN_ENCRYPTION_KEY": "fake-fernet-key",
    }
    base.update(overrides)
    return Settings(**base)


def test_wildcard_origin_in_dev_returns_star():
    s = _make_settings(ENV="development", CORS_ORIGINS="*")
    assert s.cors_origins_list == ["*"]


def test_wildcard_origin_in_local_returns_star():
    s = _make_settings(ENV="local", CORS_ORIGINS="*")
    assert s.cors_origins_list == ["*"]


def test_wildcard_origin_in_production_raises():
    s = _make_settings(ENV="production", CORS_ORIGINS="*")
    with pytest.raises(ValueError, match="운영 환경"):
        _ = s.cors_origins_list


def test_wildcard_with_whitespace_still_blocked_in_production():
    """'  *  ' 같이 공백 있어도 strip 후 차단."""
    s = _make_settings(ENV="production", CORS_ORIGINS="  *  ")
    with pytest.raises(ValueError):
        _ = s.cors_origins_list


def test_explicit_origins_in_production_pass():
    s = _make_settings(
        ENV="production",
        CORS_ORIGINS="https://gayoje.example,http://127.0.0.1:8000",
    )
    assert s.cors_origins_list == [
        "https://gayoje.example",
        "http://127.0.0.1:8000",
    ]


def test_explicit_origins_in_dev_pass():
    s = _make_settings(
        ENV="development",
        CORS_ORIGINS="http://localhost:5173,http://localhost:3000",
    )
    assert s.cors_origins_list == [
        "http://localhost:5173",
        "http://localhost:3000",
    ]


def test_empty_origins_in_dev_returns_empty_list():
    """dev 에서는 빈 list 도 그대로 허용 (테스트 환경 등에서 의도적으로 비울 수 있음)."""
    s = _make_settings(ENV="development", CORS_ORIGINS="")
    assert s.cors_origins_list == []


def test_empty_origins_in_production_raises():
    """[회귀] 운영 + 빈 CORS_ORIGINS → silent CORS 차단 대신 부팅 시점 fail-fast.

    이전엔 빈 list 를 그대로 CORSMiddleware 에 넘겨 어떤 origin 도 응답에
    `Access-Control-Allow-Origin` 헤더를 받지 못하던 문제 — 200 OK 가 와도
    브라우저가 차단.
    """
    s = _make_settings(ENV="production", CORS_ORIGINS="")
    with pytest.raises(ValueError, match="비어 있습니다"):
        _ = s.cors_origins_list


def test_localhost_only_origins_in_production_raises():
    """[회귀] 운영에 default localhost 값만 남았으면 (envvar 미설정 추정) fail-fast."""
    s = _make_settings(
        ENV="production",
        CORS_ORIGINS="http://localhost:5173,http://localhost:3000",
    )
    with pytest.raises(ValueError, match="localhost 만"):
        _ = s.cors_origins_list


def test_localhost_plus_real_origin_in_production_pass():
    """localhost 와 실제 origin 이 섞여 있으면 정상 (개발자가 staging 동시 운영)."""
    s = _make_settings(
        ENV="production",
        CORS_ORIGINS="http://localhost:5173,https://example.com",
    )
    assert "https://example.com" in s.cors_origins_list
    assert "http://localhost:5173" in s.cors_origins_list


def test_127_loopback_only_in_production_raises():
    """127.0.0.1 만 있는 경우도 localhost 와 동일하게 fail-fast."""
    s = _make_settings(ENV="production", CORS_ORIGINS="http://127.0.0.1:5173")
    with pytest.raises(ValueError, match="localhost 만"):
        _ = s.cors_origins_list


def test_origins_with_whitespace_trimmed():
    s = _make_settings(
        ENV="production",
        CORS_ORIGINS=" https://a.com , https://b.com ",
    )
    assert s.cors_origins_list == ["https://a.com", "https://b.com"]
