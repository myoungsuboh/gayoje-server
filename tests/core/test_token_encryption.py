"""
token_encryption helper 단위 테스트.

- key 있을 때 encrypt → decrypt round-trip
- key 없을 때 평문 그대로
- prefix 없는 평문 입력 시 decrypt 가 그대로 반환 (하위 호환)
- 잘못된 key 로 변조된 ciphertext → InvalidToken
- 빈 문자열 / None 처리
- try_decrypt 가 실패 시 None 반환
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.core import token_encryption
from app.core.config import settings


@pytest.fixture(autouse=True)
def _reset_fernet_cache():
    """각 테스트 사이에 싱글톤 캐시 리셋."""
    token_encryption._fernet_cache = None
    token_encryption._warned_disabled = False
    yield
    token_encryption._fernet_cache = None
    token_encryption._warned_disabled = False


def test_encrypt_then_decrypt_roundtrip(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key)

    plain = "gho_secret_access_token_value_123"
    encrypted = token_encryption.encrypt(plain)

    assert encrypted != plain
    assert encrypted.startswith(token_encryption.ENCRYPTED_PREFIX)

    decrypted = token_encryption.decrypt(encrypted)
    assert decrypted == plain


def test_encrypt_returns_plaintext_when_key_missing(monkeypatch):
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", None)
    plain = "gho_token"
    out = token_encryption.encrypt(plain)
    assert out == plain  # 그대로
    assert not out.startswith(token_encryption.ENCRYPTED_PREFIX)


def test_decrypt_passthrough_for_legacy_plaintext(monkeypatch):
    """하위 호환: prefix 없는 값은 평문으로 간주하고 그대로 반환."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key)

    legacy_plain = "gho_old_unencrypted_token"
    assert token_encryption.decrypt(legacy_plain) == legacy_plain


def test_decrypt_with_wrong_key_raises(monkeypatch):
    """다른 키로 만든 ciphertext 는 복호화 거부 (InvalidToken)."""
    # 키 1로 암호화
    key1 = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key1)
    encrypted = token_encryption.encrypt("secret")
    assert encrypted.startswith(token_encryption.ENCRYPTED_PREFIX)

    # 다른 키로 교체
    token_encryption._fernet_cache = None
    key2 = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key2)

    with pytest.raises(InvalidToken):
        token_encryption.decrypt(encrypted)


def test_decrypt_encrypted_without_key_raises(monkeypatch):
    """암호문인데 키 없음 — 복호화 불가."""
    fake_ciphertext = f"{token_encryption.ENCRYPTED_PREFIX}gAAAAAfake"
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", None)
    with pytest.raises(InvalidToken):
        token_encryption.decrypt(fake_ciphertext)


def test_empty_string_passthrough():
    assert token_encryption.encrypt("") == ""
    assert token_encryption.decrypt("") == ""


def test_invalid_key_format_disables_encryption(monkeypatch, caplog):
    """키 형식이 잘못되면 fallback 으로 평문 저장 (error log)."""
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    out = token_encryption.encrypt("secret")
    assert out == "secret"
    assert not token_encryption.is_enabled()


def test_try_decrypt_returns_none_on_failure(monkeypatch):
    fake_ciphertext = f"{token_encryption.ENCRYPTED_PREFIX}gAAAAAfake"
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", None)
    assert token_encryption.try_decrypt(fake_ciphertext) is None


def test_is_enabled_reflects_key_state(monkeypatch):
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", None)
    assert token_encryption.is_enabled() is False

    token_encryption._fernet_cache = None
    monkeypatch.setattr(
        settings, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    assert token_encryption.is_enabled() is True


def test_encrypt_handles_unicode(monkeypatch):
    """한글 등 multi-byte 문자열도 round-trip 안전."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", key)
    plain = "한글 토큰 + emoji 🔐 + ASCII"
    encrypted = token_encryption.encrypt(plain)
    assert token_encryption.decrypt(encrypted) == plain
