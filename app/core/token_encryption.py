"""
대칭 암호화 helper — OAuth access_token 같은 민감한 시크릿을 컬럼에 저장하기 전에 암호화.

[알고리즘]
Fernet (cryptography 라이브러리). 내부적으로 AES-128-CBC + HMAC-SHA256 → 변조 검증
포함된 authenticated encryption. nonce 는 라이브러리가 자동 생성, ciphertext 에 포함.

[키 관리]
- TOKEN_ENCRYPTION_KEY 환경 변수 — base64(urlsafe) 인코딩된 32 byte. 부재하면
  암호화 비활성화 (개발 편의) → 평문 그대로 저장. 운영에서는 반드시 설정.
- 키 생성: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- 키 회전: 멀티키 지원은 후속 PR. 현재는 단일 키.

[하위 호환]
- 평문으로 이미 저장된 token: decrypt 호출 시 ENCRYPTED_PREFIX 가 없으면 그대로 반환.
- 신규 저장: 키 있으면 prefix 붙여 암호화, 없으면 평문 (logger 가 warning).
"""
from __future__ import annotations

import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)

# 암호문 식별자 — 평문과 구분하기 위한 prefix. Fernet 토큰 자체는 'gAAAAA' 로 시작하지만,
# 명시적 prefix 가 더 안전 (혹시 GitHub 토큰 형식 변경 등에서 충돌 방지).
ENCRYPTED_PREFIX = "enc:v1:"


_fernet_cache: Optional[Fernet] = None
_warned_disabled = False


def _get_fernet() -> Optional[Fernet]:
    """싱글톤 Fernet 인스턴스. 키 없으면 None 반환 (호출자가 평문 저장 경로 선택)."""
    global _fernet_cache, _warned_disabled
    if _fernet_cache is not None:
        return _fernet_cache

    key = settings.TOKEN_ENCRYPTION_KEY
    if not key:
        if not _warned_disabled:
            logger.warning(
                "TOKEN_ENCRYPTION_KEY 미설정 — OAuth access_token 등이 평문으로 저장됩니다. "
                "운영 환경에서는 반드시 키를 설정하세요."
            )
            _warned_disabled = True
        return None

    try:
        _fernet_cache = Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        logger.error(
            "TOKEN_ENCRYPTION_KEY 형식이 잘못되었습니다 (base64 urlsafe 32 byte 필요): %s", e
        )
        return None
    return _fernet_cache


def is_enabled() -> bool:
    """암호화 활성 여부 — 키 유효성까지 확인."""
    return _get_fernet() is not None


def encrypt(plaintext: str) -> str:
    """
    문자열을 암호화. 키 없으면 평문 그대로 반환 (개발 편의 + warning).

    Returns:
        ENCRYPTED_PREFIX + base64 ciphertext, 또는 원본 (키 없을 때).
    """
    if not plaintext:
        return plaintext
    fernet = _get_fernet()
    if fernet is None:
        return plaintext
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_PREFIX}{token}"


def decrypt(value: str) -> str:
    """
    암호문을 복호화. prefix 없으면 평문으로 간주하고 그대로 반환 (하위 호환).

    Raises:
        InvalidToken: 키는 있으나 위변조/잘못된 키.
    """
    if not value:
        return value
    if not value.startswith(ENCRYPTED_PREFIX):
        # 평문 (구버전 데이터 또는 키 비활성 상태에서 저장된 것)
        return value
    fernet = _get_fernet()
    if fernet is None:
        # 암호문인데 키 없음 — 복호화 불가
        logger.error(
            "암호화된 token 을 복호화하려 했으나 TOKEN_ENCRYPTION_KEY 가 설정되지 않았습니다."
        )
        raise InvalidToken("암호화 키가 설정되지 않았습니다.")
    ciphertext = value[len(ENCRYPTED_PREFIX):]
    return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def try_decrypt(value: str) -> Optional[str]:
    """복호화 실패 시 None 반환 (raise 안 함). 로깅만."""
    try:
        return decrypt(value)
    except InvalidToken as e:
        logger.warning("Token 복호화 실패: %s", e)
        return None
