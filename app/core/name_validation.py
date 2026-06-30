"""
사용자 입력 이름(폴더명 / 카테고리 등) 의 안전성 검증.

[정책]
허용: 한글, 영문, 숫자, 공백, '-', '_'
금지: 그 외 모든 특수문자, 제어문자
길이: 1 ~ 50 자

[Why]
- Cypher injection 방어 — 이름이 param 바인딩으로만 전달되어 직접적 위험은
  적지만, label / property key 위치에 잘못 들어가면 위험. 보수적으로 차단.
- UX — 특수문자 (`/`, `\`, `|`, 따옴표, 이모지 등) 이 폴더명에 들어가면
  FE 렌더링 / URL encoding / 정렬 등에서 일관성 깨짐.
- 검색 — 안전한 ASCII + 한글 만 허용하면 검색 정규화 단순.

[FE 와 동일 검증]
FE 가 입력란에서 위반 표시 + 차단. BE 가 다시 검증 (FE 우회 방어).
"""
from __future__ import annotations

import re

# 한글 (가-힣) + 영문 + 숫자 + 공백(space) + 하이픈 + 언더스코어
# Note: 한글 자모 (ㄱ-ㅎ, ㅏ-ㅣ) 는 제외 — 완성형만 (가-힣). 운영상 거의 영향 없음.
# \s 대신 literal space — \s 는 \n, \t 까지 포함하므로 줄바꿈/탭이 들어간
# 폴더명이 허용되는 문제가 있음.
_NAME_PATTERN = re.compile(r"^[가-힣a-zA-Z0-9 \-_]+$")

_MIN_LENGTH = 1
_MAX_LENGTH = 50


class InvalidNameError(ValueError):
    """이름 검증 실패."""


def validate_name(name: str, *, field: str = "이름") -> str:
    """
    사용자 입력 이름 검증 + trim. 통과 시 정규화된 문자열 반환, 실패 시 InvalidNameError.

    Args:
        name: 검증할 입력
        field: 에러 메시지에 쓸 필드명 (예: "폴더 이름", "카테고리")

    Returns:
        앞뒤 공백 제거된 normalized 이름

    Raises:
        InvalidNameError: 길이 위반 또는 패턴 위반
    """
    if not isinstance(name, str):
        raise InvalidNameError(f"{field} 은 문자열이어야 합니다.")
    normalized = name.strip()
    if len(normalized) < _MIN_LENGTH:
        raise InvalidNameError(f"{field} 을 입력해주세요.")
    if len(normalized) > _MAX_LENGTH:
        raise InvalidNameError(
            f"{field} 은 최대 {_MAX_LENGTH}자까지 입력 가능합니다. (현재 {len(normalized)}자)"
        )
    if not _NAME_PATTERN.match(normalized):
        raise InvalidNameError(
            f"{field} 에는 한글, 영문, 숫자, 공백, '-', '_' 만 사용 가능합니다."
        )
    return normalized


def is_valid_name(name: str) -> bool:
    """검증 결과만 bool 로 반환 (예외 던지지 않음). FE 검증 미러용."""
    try:
        validate_name(name)
        return True
    except InvalidNameError:
        return False
