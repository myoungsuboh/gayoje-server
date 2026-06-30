"""
evals.run_real_llm — 실 Gemini 호출 실행기의 회귀 보호.

이 환경에서는 실 LLM 호출 불가 (API key 부재). 그러나:
  1. 자격증명 부재 시 명확한 에러로 정상 종료 (rc=3)
  2. PRD fixture 가 존재
  3. CLI 진입점이 ImportError 없이 시작
"""
from __future__ import annotations

from pathlib import Path

import pytest

from evals.run_real_llm import _SCENARIOS_DIR, _check_credentials, main


def test_credentials_check_returns_reason_when_no_env(monkeypatch):
    """API key 없으면 사람이 읽을 reason 반환."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_URL", raising=False)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    reason = _check_credentials()
    assert reason is not None
    assert "GEMINI_API_KEY" in reason


def test_credentials_check_passes_with_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    assert _check_credentials() is None


def test_credentials_check_passes_with_google_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key-for-test")
    assert _check_credentials() is None


def test_credentials_check_passes_with_litellm_pair(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://proxy.example")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "fake-master")
    assert _check_credentials() is None


def test_main_returns_nonzero_when_no_credentials(monkeypatch, capsys):
    """자격증명 부재 시 rc=3 + stderr 에 안내 메시지."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_URL", raising=False)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    rc = main(["plant"])
    assert rc == 3
    captured = capsys.readouterr()
    assert "자격증명 부족" in captured.err
    assert "GEMINI_API_KEY" in captured.err


def test_plant_prd_input_exists():
    """plant 시나리오의 PRD fixture 가 존재 + 핵심 Story/NFR 포함."""
    prd = _SCENARIOS_DIR / "plant" / "prd_input.md"
    assert prd.exists(), f"PRD fixture 부재: {prd}"
    text = prd.read_text(encoding="utf-8")
    # 핵심 Story 키워드
    assert "Story 3.2" in text  # 생장 기록 등록
    assert "Story 4.2" in text  # 환경 제어 설정 생성
    # 필드/제약 정보
    assert "height" in text
    assert "leafCount" in text
    assert "healthStatus" in text
    assert "HEALTHY" in text and "DEAD" in text
    # NFR
    assert "NFR-01" in text
    assert "OAuth 2.0" in text
    # Error handling
    assert "401" in text and "403" in text and "404" in text and "422" in text
