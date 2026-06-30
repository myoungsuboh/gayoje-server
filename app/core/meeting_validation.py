"""
미팅 로그 입력의 의미적 최소치 검증.

[정책 — 2026-05-18 추가]
LLM 환각 위험 차단을 위해 너무 짧거나 비정형인 입력은 LLM 호출 전에 차단.

[이전 동작 — 위험]
- `min_length=1` (Pydantic Field) → 1글자 입력도 통과
- "hi" 같은 입력 → LLM 호출 → LLM 이 환각 CPS 생성 → Neo4j 저장
- 사용자: 자기가 입력 안 한 가짜 Problem/Solution 발견 → 신뢰 붕괴
- 또한 미팅 카운트 (Free 5건 한도) 가 차감되어 사용자 손해

[현재 동작]
- 공백 제외 100자 미만 → 명시적 에러
- 줄 수 + 글자수 둘 다 매우 적으면 (회의록이라기엔 너무 짧음) 명시적 에러
- 에러 메시지에 샘플 미팅 로그 안내 포함

[FE 와 동일 검증]
FE 가 입력란에서 실시간 카운터 + 차단. BE 가 다시 검증 (FE 우회 방어).

[Why 별도 module]
- name_validation.py (이름 검증) 와 같은 패턴 — 책임 분리
- v2_routes (Pydantic) + gateway_compat_routes (dict-based) 양쪽에서 동일 호출
"""
from __future__ import annotations

import re

# ─── 검증 한도 ────────────────────────────────────────────────
#
# 200자: 회의록 한 발화 분량. "오늘 점심 뭐 먹지?" 같은 잡담은 차단되지만
#       실제 회의 한 줄 발언은 통과.
# 공백 제외 100자: 공백/줄바꿈만 가득한 공격적 입력 차단 (예: " " * 300).
#
# 이 한도는 의미적 "회의록다움" 최소치 — 너무 엄격하면 정상 사용자도 차단되니
# 보수적으로. 실제 회의 한 번 = 보통 500자~5000자.
MIN_TOTAL_CHARS = 200
MIN_NON_WHITESPACE_CHARS = 100


# ─── 에러 메시지 ─────────────────────────────────────────────
#
# 사용자에게 보일 메시지 — 차단 이유 + 다음 행동 제시.
# FE 가 그대로 노출하므로 한국어 + 친근한 톤.

_HINT_SAMPLE = (
    "샘플 회의록 형식은 GitHub 저장소의 `샘플 미팅 로그/` 폴더를 참고하세요."
)


class MeetingContentTooShort(ValueError):
    """미팅 로그 입력이 의미 있는 분석에 부족함."""

    def __init__(self, reason: str, *, chars: int, non_ws_chars: int):
        self.reason = reason
        self.chars = chars
        self.non_ws_chars = non_ws_chars
        super().__init__(reason)


def assert_meeting_content_substantial(meeting_content: str) -> None:
    """
    미팅 로그가 LLM 분석에 의미 있는 분량인지 검증.

    [통과 조건 — 다음 두 가지 모두]
      1. 전체 글자수 >= MIN_TOTAL_CHARS (기본 200)
      2. 공백 제외 글자수 >= MIN_NON_WHITESPACE_CHARS (기본 100)

    [실패 시] MeetingContentTooShort 예외 — 호출자(라우트)가 400 HTTP 로 매핑.

    [의도]
    LLM 환각 차단 + 사용자 미팅 카운트 보호 + 명확한 가이드.

    Raises:
        MeetingContentTooShort: 한도 미만.
    """
    if meeting_content is None:
        meeting_content = ""

    chars = len(meeting_content)
    non_ws_chars = len(re.sub(r"\s+", "", meeting_content))

    if chars < MIN_TOTAL_CHARS:
        raise MeetingContentTooShort(
            (
                f"미팅 로그가 너무 짧습니다 ({chars}자). "
                f"AI 가 의미 있는 분석을 하려면 최소 {MIN_TOTAL_CHARS}자 이상의 "
                f"회의록이 필요합니다. {_HINT_SAMPLE}"
            ),
            chars=chars,
            non_ws_chars=non_ws_chars,
        )

    if non_ws_chars < MIN_NON_WHITESPACE_CHARS:
        raise MeetingContentTooShort(
            (
                f"미팅 로그의 실제 내용이 너무 적습니다 "
                f"(공백 제외 {non_ws_chars}자). 최소 {MIN_NON_WHITESPACE_CHARS}자 "
                f"이상의 의미 있는 회의 내용이 필요합니다. {_HINT_SAMPLE}"
            ),
            chars=chars,
            non_ws_chars=non_ws_chars,
        )
