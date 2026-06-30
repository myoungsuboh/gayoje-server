"""
name_validation 단위 테스트 — 폴더명 / 카테고리 입력 검증.

[정책 회귀 가드]
허용: 한글 / 영문 / 숫자 / 공백 / '-' / '_'
금지: 그 외 (특수문자, 제어문자, 이모지)
길이: 1~50자
"""
from __future__ import annotations

import pytest

from app.core.name_validation import (
    InvalidNameError,
    is_valid_name,
    validate_name,
)


# ─── 허용 케이스 ────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "Frontend Standard",
    "Backend",
    "프론트엔드 표준",
    "DB-Conventions",
    "Mobile_iOS",
    "test-2024",
    "한글 영문 Mix 123",
    "A",  # 1자 경계
    "a" * 50,  # 50자 경계
    "  Trimmed  ",  # 앞뒤 공백은 trim
])
def test_valid_names_accepted(name):
    result = validate_name(name)
    assert isinstance(result, str)
    assert is_valid_name(name)


def test_trim_strips_leading_trailing_whitespace():
    assert validate_name("  hello  ") == "hello"


# ─── 거부 케이스 ────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "",                  # 빈 문자열
    "   ",               # 공백만
    "a" * 51,            # 길이 초과
    "path/with/slash",   # 슬래시
    "back\\slash",       # 백슬래시
    "with|pipe",         # 파이프
    'with"quote',        # 따옴표
    "with'quote",        # 단일 따옴표
    "with`backtick",     # 백틱
    "with*star",         # *
    "with?question",     # ?
    "with<bracket>",     # 부등호
    "with{brace}",       # 중괄호
    "with[bracket]",     # 대괄호
    "with(paren)",       # 괄호
    "with#hash",         # 해시
    "with@at",           # @
    "with$dollar",       # $
    "with%percent",      # %
    "with&amp",          # &
    "with;semicolon",    # 세미콜론
    "with:colon",        # 콜론
    "with!exclaim",      # !
    "with~tilde",        # ~
    "with.dot",          # 점 (운영상 폴더명에 점은 혼란)
    "with,comma",        # 쉼표
    "with=equal",        # =
    "with+plus",         # +
    "이모지😀포함",        # 이모지
    "한글\n줄바꿈",         # 제어문자
    "한글\t탭",            # 탭
])
def test_invalid_names_rejected(name):
    with pytest.raises(InvalidNameError):
        validate_name(name)
    assert not is_valid_name(name)


# ─── 에러 메시지에 필드명 포함 ──────────────────────────


def test_error_message_includes_field_name():
    with pytest.raises(InvalidNameError, match="폴더 이름"):
        validate_name("invalid/name", field="폴더 이름")
    with pytest.raises(InvalidNameError, match="카테고리"):
        validate_name("", field="카테고리")


def test_length_error_shows_current_length():
    with pytest.raises(InvalidNameError, match="51자"):
        validate_name("a" * 51)


# ─── 비-문자열 입력 ──────────────────────────────────────


def test_non_string_input_raises():
    with pytest.raises(InvalidNameError):
        validate_name(None)  # type: ignore[arg-type]
    with pytest.raises(InvalidNameError):
        validate_name(123)  # type: ignore[arg-type]
