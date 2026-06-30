"""
Lint sampling 한도의 환경변수 가변화 회귀 가드 (2026-05).

[배경]
이전: 40 file 고정. 중대형 repo (>1000 files) 에서 구현 파일 누락 → false negative.
이후: LINT_MAX_SAMPLE_FILES / LINT_PER_FILE_BYTES / LINT_TOTAL_BUDGET_BYTES /
    LINT_RESIDUAL_LLM_BUDGET env 로 운영 환경별 조정.

[가드]
- env 미설정 → default 값 (40/64K/400K/80K) 유지
- env 설정 시 모듈 import 시점에 반영
- 잘못된 값 (문자/음수) → default fallback (현재는 ValueError → default)
"""
from __future__ import annotations

import importlib

import pytest


def _reload_pipeline_module():
    """env 변경 후 모듈 다시 import — module-level _int_env 가 재평가됨."""
    import app.pipelines.lint_pipeline as mod
    importlib.reload(mod)
    return mod


def test_default_values_when_env_unset(monkeypatch):
    """env 미설정이면 default."""
    monkeypatch.delenv("LINT_MAX_SAMPLE_FILES", raising=False)
    monkeypatch.delenv("LINT_PER_FILE_BYTES", raising=False)
    monkeypatch.delenv("LINT_TOTAL_BUDGET_BYTES", raising=False)
    monkeypatch.delenv("LINT_RESIDUAL_LLM_BUDGET", raising=False)
    mod = _reload_pipeline_module()
    assert mod._DEFAULT_MAX_SAMPLE_FILES == 40
    assert mod._DEFAULT_PER_FILE_BYTES == 64_000
    assert mod._DEFAULT_TOTAL_BUDGET == 400_000
    assert mod._RESIDUAL_LLM_BUDGET == 80_000


def test_env_override_takes_effect(monkeypatch):
    """env 설정 시 reload 후 반영."""
    monkeypatch.setenv("LINT_MAX_SAMPLE_FILES", "120")
    monkeypatch.setenv("LINT_PER_FILE_BYTES", "100000")
    monkeypatch.setenv("LINT_TOTAL_BUDGET_BYTES", "1000000")
    monkeypatch.setenv("LINT_RESIDUAL_LLM_BUDGET", "200000")
    mod = _reload_pipeline_module()
    assert mod._DEFAULT_MAX_SAMPLE_FILES == 120
    assert mod._DEFAULT_PER_FILE_BYTES == 100_000
    assert mod._DEFAULT_TOTAL_BUDGET == 1_000_000
    assert mod._RESIDUAL_LLM_BUDGET == 200_000


def test_invalid_env_falls_back_to_default(monkeypatch):
    """잘못된 값 (문자) → default fallback, raise 안 함."""
    monkeypatch.setenv("LINT_MAX_SAMPLE_FILES", "not-a-number")
    monkeypatch.setenv("LINT_PER_FILE_BYTES", "")
    mod = _reload_pipeline_module()
    assert mod._DEFAULT_MAX_SAMPLE_FILES == 40
    assert mod._DEFAULT_PER_FILE_BYTES == 64_000


def test_int_env_helper_negative_passes_through(monkeypatch):
    """음수도 int 라 통과 — 호출자가 sanity check 책임 (lint pipeline 도 결과적으로 비효율 응답)."""
    monkeypatch.setenv("LINT_MAX_SAMPLE_FILES", "-1")
    mod = _reload_pipeline_module()
    # 운영 실수 방지 — 음수면 sampling 안 됨 (이 동작은 별도 PR 에서 0 으로 clamp 가능).
    assert mod._DEFAULT_MAX_SAMPLE_FILES == -1
