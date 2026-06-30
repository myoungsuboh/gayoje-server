"""
PRD lint — design pipeline 진입 전 raw PRD 텍스트의 충실도 검사.

[B 단계 — 2026-05-25]
변환 LLM (SPACK/DDD/Architecture) 이 잘 채워줘도 PRD 가 빈약하면 결과도
빈약. 즉 "그릇은 크게 만들었는데 부어줄 물이 적은" 상태가 plant 사례의
근본 원인. 이 lint 가 사용자에게 PRD 보강 부분을 즉시 가시화.

설계 원칙:
- LLM 호출 없음 — 정규식 + 키워드 기반. ~10ms 응답.
- 한국어 우선 (PRD 가 한국어가 일반적). 영문도 지원.
- 결정적 — 같은 입력 → 같은 출력 (정규식 only).
- penalty 부드러움 — Tier 2 eval 처럼 분모 0 케이스 N/A 처리.

검사 항목:
- PRD_TOO_SHORT      : 전체 < 500 bytes
- PRD_NO_OVERVIEW    : "Overview" / "개요" 섹션 부재
- PRD_NO_STORY       : Story 표기 (Story-X / [Story X]) 0개
- PRD_NO_NFR         : NFR 또는 "비기능/Non-Functional/응답 시간" 키워드 부재
- STORY_NO_INPUT     : Story 본문에 "입력 / 요청 / Body / 필드" 없음 (Story 당)
- STORY_NO_OUTPUT    : Story 본문에 "출력 / 응답 / 결과" 없음
- STORY_NO_VALIDATION: Story 에 ">", "<", "필수", "최대/최소", "regex" 등 검증 표현 없음
- PRD_NO_ERROR_CASE  : "401/403/404/422/500" 또는 "권한 없음/검증 실패/없음" 키워드 0건
- PRD_NO_AUTH        : "JWT / OAuth / 인증 / 권한" 키워드 부재

호출 예:
    from app.pipelines.prd_lint import lint_prd
    report = lint_prd(prd_text)
    if report.score < 0.5:
        # 사용자에게 보강 권유
"""
from .linter import (
    PrdLintIssue,
    PrdLintReport,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    lint_prd,
)

__all__ = [
    "PrdLintIssue",
    "PrdLintReport",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "lint_prd",
]
