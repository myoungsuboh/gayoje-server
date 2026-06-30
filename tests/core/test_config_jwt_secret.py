"""
JWT_SECRET_KEY 운영 부팅 fail-fast 정책 단위 테스트.

[정책]
- 운영(production) + JWT_SECRET_KEY 가 default placeholder → 부팅 거부 (토큰 위조 위험).
- 운영(production) + JWT_SECRET_KEY 가 32 byte 미만 → 부팅 거부 (무차별 대입 위험).
- 운영 외 환경 (development/local/...) → default placeholder 허용 (개발 편의).
"""
from __future__ import annotations

import pytest

from app.core.config import (
    Settings,
    _DEFAULT_JWT_SECRET_PLACEHOLDER,
    _JWT_SECRET_MIN_LENGTH,
)


def _make_settings(**overrides) -> Settings:
    """Settings 를 환경변수 없이 직접 인스턴스화 (테스트 격리).

    CORS '*' 거부 정책 + TOKEN_ENCRYPTION_KEY 운영 강제 검증과 충돌 방지.
    JWT 만 검증하려면 다른 운영 secret 도 valid 값으로 제공.
    """
    base = {
        "ENV": "development",
        "JWT_SECRET_KEY": "x" * _JWT_SECRET_MIN_LENGTH,
        "CORS_ORIGINS": "https://example.com",
        "TOKEN_ENCRYPTION_KEY": "fake-fernet-key-for-tests",  # 운영 검증 통과용 stub
    }
    base.update(overrides)
    return Settings(**base)


# ─── 거부 (운영 환경 + 약한 secret) ──


def test_default_placeholder_in_production_raises():
    """운영 환경에서 default placeholder 그대로면 ValueError."""
    with pytest.raises(ValueError, match="JWT_SECRET_KEY.*default placeholder"):
        _make_settings(
            ENV="production",
            JWT_SECRET_KEY=_DEFAULT_JWT_SECRET_PLACEHOLDER,
        )


def test_short_secret_in_production_raises():
    """운영 환경에서 32 byte 미만 secret 은 ValueError."""
    with pytest.raises(ValueError, match="너무 짧습니다"):
        _make_settings(
            ENV="production",
            JWT_SECRET_KEY="too-short-31-chars-1234567890ab",  # 31 chars
        )


def test_exact_31_chars_still_rejected():
    """경계 케이스: 31 자도 거부 (요구 길이 32 미만)."""
    secret = "a" * (_JWT_SECRET_MIN_LENGTH - 1)
    with pytest.raises(ValueError):
        _make_settings(ENV="production", JWT_SECRET_KEY=secret)


# ─── 허용 (운영 환경 + 충분한 secret) ──


def test_strong_secret_in_production_passes():
    """운영 환경에서 32 byte 이상의 임의 secret 은 통과."""
    s = _make_settings(ENV="production", JWT_SECRET_KEY="a" * _JWT_SECRET_MIN_LENGTH)
    assert s.is_production
    assert len(s.JWT_SECRET_KEY) == _JWT_SECRET_MIN_LENGTH


def test_typical_openssl_rand_hex_32_passes():
    """openssl rand -hex 32 결과(64자 hex)는 통과."""
    secret = "a" * 64  # openssl rand -hex 32 = 64 hex chars
    s = _make_settings(ENV="production", JWT_SECRET_KEY=secret)
    assert s.JWT_SECRET_KEY == secret


# ─── 허용 (운영 외 환경 — 개발 편의) ──


def test_default_placeholder_in_development_allowed():
    """개발 환경에서는 default placeholder 그대로도 통과 (편의)."""
    s = _make_settings(
        ENV="development",
        JWT_SECRET_KEY=_DEFAULT_JWT_SECRET_PLACEHOLDER,
    )
    assert s.JWT_SECRET_KEY == _DEFAULT_JWT_SECRET_PLACEHOLDER
    assert not s.is_production


def test_short_secret_in_development_allowed():
    """개발 환경에서는 짧은 secret 도 통과."""
    s = _make_settings(ENV="development", JWT_SECRET_KEY="short")
    assert s.JWT_SECRET_KEY == "short"


def test_default_placeholder_in_local_allowed():
    """'local' env 도 production 이 아니므로 허용."""
    s = _make_settings(
        ENV="local",
        JWT_SECRET_KEY=_DEFAULT_JWT_SECRET_PLACEHOLDER,
    )
    assert not s.is_production


def test_default_placeholder_in_staging_allowed():
    """'staging' env 도 production 이 아니므로 허용 (현 정책)."""
    s = _make_settings(
        ENV="staging",
        JWT_SECRET_KEY=_DEFAULT_JWT_SECRET_PLACEHOLDER,
    )
    assert not s.is_production


# ─── 운영 환경 대소문자 ──


def test_production_env_case_insensitive():
    """ENV='PRODUCTION' (대문자) 도 운영으로 인식 → default placeholder 거부."""
    with pytest.raises(ValueError):
        _make_settings(
            ENV="PRODUCTION",
            JWT_SECRET_KEY=_DEFAULT_JWT_SECRET_PLACEHOLDER,
        )
