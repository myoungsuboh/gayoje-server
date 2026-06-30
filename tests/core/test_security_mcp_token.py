"""create_mcp_token / decode 검증."""
from __future__ import annotations

import jwt
import pytest

from app.core import security
from app.core.config import settings


def test_create_mcp_token_returns_token_and_jti():
    token, jti = security.create_mcp_token("u@e.com", exp_days=90)
    payload = jwt.decode(
        token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
    )
    assert payload["sub"] == "u@e.com"
    assert payload["type"] == "mcp"
    assert payload["jti"] == jti
    assert payload["scope"] == ["mcp:read"]
    # exp 가 약 90일 후
    assert payload["exp"] - payload["iat"] >= 89 * 24 * 60 * 60


def test_create_mcp_token_unique_jti_per_call():
    _, jti1 = security.create_mcp_token("u@e.com", exp_days=90)
    _, jti2 = security.create_mcp_token("u@e.com", exp_days=90)
    assert jti1 != jti2
