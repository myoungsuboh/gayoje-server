"""BE-E01-T02 회귀 — 환경 분리 / feature flag / 공공데이터 키 / 시크릿 마스킹.

기존 fail-fast(JWT/CORS/암호화키) 검증은 test_config_jwt_secret·test_config_cors·
test_config_token_encryption 가 커버. 여기서는 T02 가산분 + fail-fast 유지를 확인.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings, mask_db_url, mask_secret


def _s(**kw) -> Settings:
    base = dict(_env_file=None, JWT_SECRET_KEY="x" * 40)
    base.update(kw)
    return Settings(**base)


def test_env_separation():
    # 운영은 TOKEN_ENCRYPTION_KEY 필수(fail-fast) — 분리 검증엔 더미 키 동봉.
    assert _s(ENV="production", TOKEN_ENCRYPTION_KEY="k").is_production
    s_stg = _s(ENV="staging")
    assert s_stg.is_staging and not s_stg.is_development
    assert _s(ENV="stg").is_staging
    dev = _s(ENV="development")
    assert dev.is_development and not dev.is_production and not dev.is_staging


def test_feature_flags_default_and_toggle():
    flags = _s().feature_flags()
    assert flags["ingest_public_api"] is True  # 북극성 PoC 대상 — 기본 on
    assert flags["payments"] is False
    assert flags["notifications"] is False
    assert flags["ingest_crawler"] is False
    assert _s(FEATURE_PAYMENTS=True).FEATURE_PAYMENTS is True


def test_data_go_kr_multi_key_parsing():
    s = _s(DATA_GO_KR_SERVICE_KEY="keyA, keyB  keyC")
    assert s.data_go_kr_service_keys == ["keyA", "keyB", "keyC"]
    # 빈 값 명시(.env 의 실 키가 os.environ 에 올라와도 init kwarg 가 우선 → 환경 비의존)
    assert _s(DATA_GO_KR_SERVICE_KEY="").data_go_kr_service_keys == []


def test_mask_helpers():
    assert mask_secret("abcdefgh") == "abcd***"
    assert mask_secret("ab") == "***"
    assert mask_secret(None) == ""
    masked = mask_db_url("postgresql+asyncpg://user:secretpw@host:5432/db")
    assert "secretpw" not in masked
    assert "user:***@host" in masked
    # 자격증명 없는 URL 은 원본 보존
    assert mask_db_url("sqlite+aiosqlite:///./x.db") == "sqlite+aiosqlite:///./x.db"


def test_safe_summary_masks_secrets_not_plain():
    s = _s(
        JWT_SECRET_KEY="supersecretvalue1234567890abcdef",
        DATA_GO_KR_SERVICE_KEY="mykey12345",
        DATABASE_URL="postgresql+asyncpg://u:pw@h/db",
    )
    summary = s.safe_summary()
    assert "supersecret" not in str(summary["JWT_SECRET_KEY"])
    assert str(summary["JWT_SECRET_KEY"]).endswith("***")
    assert "pw@" not in str(summary["DATABASE_URL"])
    # 비-시크릿은 평문 유지
    assert summary["ENV"] == "development"
    assert summary["FEATURE_INGEST_PUBLIC_API"] is True


def test_production_fail_fast_still_enforced():
    # placeholder JWT → 운영 부팅 거부 (기존 fail-fast 유지)
    with pytest.raises(ValueError):
        _s(
            ENV="production",
            JWT_SECRET_KEY="change-me-to-a-long-random-secret-string",
            TOKEN_ENCRYPTION_KEY="k",
        )
