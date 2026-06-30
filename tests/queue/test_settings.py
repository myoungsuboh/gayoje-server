"""
Redis 설정 파싱 단위 테스트.
"""
from __future__ import annotations

import importlib

import pytest


def _reload_settings(monkeypatch):
    """매번 settings 모듈을 재 import → 환경 변수 변경 반영."""
    monkeypatch.setattr(
        "os.environ",
        dict(__import__("os").environ),
        raising=False,
    )
    from app.queue import settings as s

    importlib.reload(s)
    return s


def test_redis_url_takes_priority(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://:secret@redis.example.com:6380/3")
    monkeypatch.setenv("REDIS_HOST", "ignored.example.com")
    s = _reload_settings(monkeypatch)
    cfg = s.redis_settings()
    assert cfg.host == "redis.example.com"
    assert cfg.port == 6380
    assert cfg.password == "secret"
    assert cfg.database == 3


def test_individual_vars_used_when_no_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "localhost")
    monkeypatch.setenv("REDIS_PORT", "6379")
    monkeypatch.setenv("REDIS_DB", "2")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    s = _reload_settings(monkeypatch)
    cfg = s.redis_settings()
    assert cfg.host == "localhost"
    assert cfg.port == 6379
    assert cfg.database == 2
    assert cfg.password is None


def test_queue_name_env_override(monkeypatch):
    monkeypatch.setenv("ARQ_QUEUE_NAME", "harness:test")
    s = _reload_settings(monkeypatch)
    assert s.QUEUE_NAME == "harness:test"
