"""
TOKEN_ENCRYPTION_KEY 운영 부팅 fail-fast 정책.

[배경 — 2026-05 M1 픽스]
이전: 운영에서 TOKEN_ENCRYPTION_KEY 미설정 시 warning 로그만, OAuth access_token
    이 Neo4j 에 평문 저장. DB 덤프 유출 시 사용자 GitHub 토큰 즉시 사용 가능.
이후: 운영 부팅 시 미설정이면 ValueError → 컨테이너 부팅 거부.

[정책]
- 운영(production) + TOKEN_ENCRYPTION_KEY 미설정/빈 → 거부.
- 운영 외 (development/local/staging) → 미설정 허용 (편의).
- worker context (HARNESS_WORKER_CONTEXT=1) → skip — worker 는 토큰 다루지 않음.
"""
from __future__ import annotations

import os

import pytest

from app.core.config import Settings, _JWT_SECRET_MIN_LENGTH


def _make_settings(**overrides) -> Settings:
    base = {
        "ENV": "development",
        "JWT_SECRET_KEY": "x" * _JWT_SECRET_MIN_LENGTH,
        "CORS_ORIGINS": "https://example.com",
        "TOKEN_ENCRYPTION_KEY": "fake-fernet-key",
    }
    base.update(overrides)
    return Settings(**base)


# ─── 거부 (운영 + 미설정) ──


def test_missing_token_encryption_key_in_production_raises():
    """운영 환경에서 TOKEN_ENCRYPTION_KEY 가 None 이면 ValueError."""
    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
        _make_settings(ENV="production", TOKEN_ENCRYPTION_KEY=None)


def test_empty_token_encryption_key_in_production_raises():
    """운영 환경에서 빈 문자열이어도 거부 (의도되지 않은 미설정)."""
    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
        _make_settings(ENV="production", TOKEN_ENCRYPTION_KEY="")


# ─── 허용 (운영 외 환경) ──


def test_missing_token_encryption_key_in_development_allowed():
    """개발 환경에서는 미설정도 통과 (개발 편의)."""
    s = _make_settings(ENV="development", TOKEN_ENCRYPTION_KEY=None)
    assert s.TOKEN_ENCRYPTION_KEY is None
    assert not s.is_production


# ─── 허용 (운영 + 설정) ──


def test_set_token_encryption_key_in_production_passes():
    """운영 환경에서 임의 값으로 설정됐으면 통과 (Fernet 형식 검증은 별도)."""
    s = _make_settings(
        ENV="production",
        TOKEN_ENCRYPTION_KEY="any-non-empty-value",
    )
    assert s.TOKEN_ENCRYPTION_KEY == "any-non-empty-value"


# ─── worker context skip ──


def test_worker_context_skips_all_production_validation(monkeypatch):
    """HARNESS_WORKER_CONTEXT=1 이면 운영에서도 TOKEN_ENCRYPTION_KEY 미설정 허용."""
    monkeypatch.setenv("HARNESS_WORKER_CONTEXT", "1")
    s = _make_settings(
        ENV="production",
        JWT_SECRET_KEY="x" * _JWT_SECRET_MIN_LENGTH,
        TOKEN_ENCRYPTION_KEY=None,
    )
    assert s.is_production
    assert s.TOKEN_ENCRYPTION_KEY is None
