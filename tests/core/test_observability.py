"""
관측성(observability) 모듈 단위 테스트 — 구조화 로깅 + Sentry init.

[검증 범위]
  - JsonLogFormatter: 필수 필드 / 예외 첨부 / extra 보존 / 예약키 누락 방지.
  - setup_logging: LOG_FORMAT 토글에 따른 핸들러 포맷터 적용 + 컨텍스트 필터 부착.
  - init_sentry: DSN 미설정 시 no-op, sentry_sdk 미설치 안전.
"""
from __future__ import annotations

import json
import logging
import sys

import pytest

from app.core import observability
from app.core.observability import (
    JsonLogFormatter,
    capture_exception,
    init_sentry,
    setup_logging,
)
from app.core.request_context import _ContextFilter


def _make_record(msg="hello %s", args=("world",), exc_info=None, **extra):
    rec = logging.LogRecord(
        "harness.test", logging.INFO, "f.py", 1, msg, args, exc_info
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_core_fields():
    rec = _make_record(request_id="rid-1", user_email="u@e.com")
    d = json.loads(JsonLogFormatter().format(rec))
    assert d["msg"] == "hello world"
    assert d["level"] == "INFO"
    assert d["logger"] == "harness.test"
    assert d["request_id"] == "rid-1"
    assert d["user_email"] == "u@e.com"
    assert "ts" in d


def test_json_formatter_missing_context_defaults():
    # _ContextFilter 미부착 record 도 안전 default ("-").
    rec = _make_record()
    d = json.loads(JsonLogFormatter().format(rec))
    assert d["request_id"] == "-"
    assert d["user_email"] == "-"


def test_json_formatter_exception_capture():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = _make_record(msg="failed", args=(), exc_info=sys.exc_info())
    d = json.loads(JsonLogFormatter().format(rec))
    assert d["exc_type"] == "ValueError"
    assert "boom" in d["exc"]


def test_json_formatter_preserves_extra_fields():
    rec = _make_record(job_id="job-42", stage="merge")
    d = json.loads(JsonLogFormatter().format(rec))
    assert d["job_id"] == "job-42"
    assert d["stage"] == "merge"


def test_json_formatter_is_single_line():
    rec = _make_record(msg="line1\nline2", args=())
    line = JsonLogFormatter().format(rec)
    # JSON 직렬화로 개행이 이스케이프되어 한 줄 유지.
    assert "\n" not in line


def test_setup_logging_json_format(monkeypatch):
    monkeypatch.setattr(observability.settings, "LOG_FORMAT", "json", raising=False)
    monkeypatch.setattr(observability.settings, "LOG_LEVEL", "INFO", raising=False)
    setup_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert isinstance(handler.formatter, JsonLogFormatter)
    # 컨텍스트 필터가 부착돼 request_id/user_email 첨부 가능.
    assert any(isinstance(f, _ContextFilter) for f in handler.filters)


def test_setup_logging_text_format(monkeypatch):
    monkeypatch.setattr(observability.settings, "LOG_FORMAT", "text", raising=False)
    setup_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert not isinstance(root.handlers[0].formatter, JsonLogFormatter)


def test_setup_logging_idempotent(monkeypatch):
    monkeypatch.setattr(observability.settings, "LOG_FORMAT", "text", raising=False)
    setup_logging()
    setup_logging()
    setup_logging()
    # 매번 핸들러 교체 — 누적되지 않음.
    assert len(logging.getLogger().handlers) == 1


def test_init_sentry_disabled_when_no_dsn(monkeypatch):
    monkeypatch.setattr(observability.settings, "SENTRY_DSN", None, raising=False)
    assert init_sentry("backend") is False


def test_capture_exception_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(observability.settings, "SENTRY_DSN", None, raising=False)
    # DSN 없으면 조용히 통과 (예외 던지지 않음).
    capture_exception(ValueError("x"), component="worker", job="test_job")
