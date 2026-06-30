"""
quota.get_model_for_subscription 단위 테스트 — 등급별 Gemini 모델 분기.

[검증 범위]
- Free 등급 → GEMINI_MODEL_FREE
- Pro 등급 → GEMINI_MODEL_PRO
- 알 수 없는 값 → 보수적으로 free 모델
- 두 env 미설정 시 → GEMINI_MODEL (legacy) 로 fallback
- 한쪽만 설정 시 → 다른 쪽은 GEMINI_MODEL 로 fallback
"""
from __future__ import annotations

import pytest

from app.core import quota
from app.core.config import settings
from app.core.subscription import (
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    SUBSCRIPTION_PRO_MAX,
    SUBSCRIPTION_PRO_PLUS,
)


def test_pro_uses_pro_model(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "GEMINI_MODEL_FREE", "model-free")
    monkeypatch.setattr(settings, "GEMINI_MODEL_PRO", "model-pro")
    assert quota.get_model_for_subscription(SUBSCRIPTION_PRO) == "model-pro"


def test_pro_plus_uses_pro_model(monkeypatch):
    """Pro+ 도 Pro 와 동일 모델 (2026-05 정책)."""
    monkeypatch.setattr(settings, "GEMINI_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "GEMINI_MODEL_FREE", "model-free")
    monkeypatch.setattr(settings, "GEMINI_MODEL_PRO", "model-pro")
    assert quota.get_model_for_subscription(SUBSCRIPTION_PRO_PLUS) == "model-pro"


def test_pro_max_uses_pro_model(monkeypatch):
    """Pro Max 도 Pro 와 동일 모델 — 차별화는 용량만."""
    monkeypatch.setattr(settings, "GEMINI_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "GEMINI_MODEL_FREE", "model-free")
    monkeypatch.setattr(settings, "GEMINI_MODEL_PRO", "model-pro")
    assert quota.get_model_for_subscription(SUBSCRIPTION_PRO_MAX) == "model-pro"


def test_free_uses_free_model(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "GEMINI_MODEL_FREE", "model-free")
    monkeypatch.setattr(settings, "GEMINI_MODEL_PRO", "model-pro")
    assert quota.get_model_for_subscription(SUBSCRIPTION_FREE) == "model-free"


def test_unknown_subscription_falls_back_to_free(monkeypatch):
    """비정상 등급값 — 보수적으로 free 모델 (cheaper)."""
    monkeypatch.setattr(settings, "GEMINI_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "GEMINI_MODEL_FREE", "model-free")
    monkeypatch.setattr(settings, "GEMINI_MODEL_PRO", "model-pro")
    assert quota.get_model_for_subscription("enterprise") == "model-free"
    assert quota.get_model_for_subscription("") == "model-free"


def test_legacy_fallback_when_both_unset(monkeypatch):
    """GEMINI_MODEL_FREE/_PRO 둘 다 None → GEMINI_MODEL 로 fallback."""
    monkeypatch.setattr(settings, "GEMINI_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "GEMINI_MODEL_FREE", None)
    monkeypatch.setattr(settings, "GEMINI_MODEL_PRO", None)
    assert quota.get_model_for_subscription(SUBSCRIPTION_FREE) == "legacy-model"
    assert quota.get_model_for_subscription(SUBSCRIPTION_PRO) == "legacy-model"


def test_partial_unset_pro_falls_back(monkeypatch):
    """GEMINI_MODEL_PRO 만 미설정 → Pro 가 GEMINI_MODEL 로 fallback (Free 는 별도 유지)."""
    monkeypatch.setattr(settings, "GEMINI_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "GEMINI_MODEL_FREE", "model-free")
    monkeypatch.setattr(settings, "GEMINI_MODEL_PRO", None)
    assert quota.get_model_for_subscription(SUBSCRIPTION_FREE) == "model-free"
    assert quota.get_model_for_subscription(SUBSCRIPTION_PRO) == "legacy-model"


def test_empty_string_treated_as_unset(monkeypatch):
    """빈 문자열도 미설정으로 간주 (env 가 비어있을 때 안전)."""
    monkeypatch.setattr(settings, "GEMINI_MODEL", "legacy-model")
    monkeypatch.setattr(settings, "GEMINI_MODEL_FREE", "")
    monkeypatch.setattr(settings, "GEMINI_MODEL_PRO", "")
    assert quota.get_model_for_subscription(SUBSCRIPTION_FREE) == "legacy-model"
    assert quota.get_model_for_subscription(SUBSCRIPTION_PRO) == "legacy-model"


def test_settings_property_consistency(monkeypatch):
    """settings.gemini_model_for_* property 가 quota 헬퍼와 일치."""
    monkeypatch.setattr(settings, "GEMINI_MODEL", "fallback")
    monkeypatch.setattr(settings, "GEMINI_MODEL_FREE", "f-model")
    monkeypatch.setattr(settings, "GEMINI_MODEL_PRO", "p-model")
    assert settings.gemini_model_for_free == "f-model"
    assert settings.gemini_model_for_pro == "p-model"
