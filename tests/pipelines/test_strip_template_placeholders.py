r"""
[2026-05-19] strip_template_placeholders 단위 테스트.

PRD/CPS merge·rebuild 에이전트가 prompt 의 placeholder 를 그대로 emit 한 케이스
방어. 사용자 보고 (`(from `[기능 영역\n  예: 핵심 데이터 관리]`)`) 정확히
재현 + 변형 케이스 + 정상 텍스트 보존 확인.
"""
from __future__ import annotations

import pytest

from app.pipelines.base import strip_template_placeholders


# ─── 멀티라인 bracket placeholder ─────────────────────────────


def test_strip_multiline_bracket_placeholder():
    """사용자 보고: PRD Screen Architecture 에 노출된 `[기능 영역\\n  예: 핵심 데이터 관리]`."""
    src = "- `[Story 1.1]` 데이터 조회 (from `[기능 영역\n  예: 핵심 데이터 관리]`)"
    out = strip_template_placeholders(src)
    assert "예: 핵심 데이터 관리" not in out
    assert "기능 영역" not in out
    assert "미정" in out
    # 정상 부분 (Story id, 백틱) 은 그대로
    assert "[Story 1.1]" in out
    assert "데이터 조회" in out


def test_strip_multiline_bracket_placeholder_single_space_indent():
    """들여쓰기 1 space 만 있어도 매치."""
    src = "Epic-01 [도메인명\n 예: 사용자 계정 관리]"
    out = strip_template_placeholders(src)
    assert "[도메인명" not in out
    assert "예: 사용자 계정 관리" not in out
    assert "Epic-01 미정" == out


def test_strip_multiple_placeholders_in_one_string():
    """여러 leak 이 한 문서 안에 있으면 전부 치환."""
    src = (
        "Screen A: [기능 영역\n  예: 데이터 관리]\n"
        "Screen B: [기능 영역\n  예: 시스템 설정]\n"
        "정상 텍스트."
    )
    out = strip_template_placeholders(src)
    assert out.count("미정") == 2
    assert "정상 텍스트." in out


# ─── 단일라인 bracket placeholder (prd_extract.md 흔적) ───────────


def test_strip_single_line_bracket_with_example():
    """prd_extract.md 의 `[도메인명 - 예: 사용자 계정 관리]` 단일라인 placeholder."""
    src = "#### 📦 Epic 1: [도메인명 - 예: 사용자 계정 관리]"
    out = strip_template_placeholders(src)
    assert "[도메인명" not in out
    assert "예: 사용자 계정 관리" not in out
    assert out == "#### 📦 Epic 1: 미정"


def test_strip_short_example_bracket():
    """`[예: prb_01]` 처럼 짧은 example placeholder."""
    src = "- **해결 문제 매핑**: [예: prb_01] (반드시 ID 매핑)"
    out = strip_template_placeholders(src)
    assert "[예: prb_01]" not in out
    assert "미정" in out
    # 뒤의 정상 텍스트는 보존
    assert "(반드시 ID 매핑)" in out


# ─── curly placeholder (한글-only) ───────────────────────────────


def test_strip_curly_korean_only_placeholder():
    """`{에픽명}`, `{스토리 내용}` 등 한글-only curly placeholder 치환."""
    src = "#### 📦 [Epic-01] {에픽명}\n- `[Story-01.1]` {스토리 내용}"
    out = strip_template_placeholders(src)
    assert "{에픽명}" not in out
    assert "{스토리 내용}" not in out
    assert out.count("미정") == 2
    # 정상 구조 (이모지, ID) 보존
    assert "[Epic-01]" in out
    assert "[Story-01.1]" in out


def test_strip_curly_with_spaces_inside():
    """한글 사이 공백도 매치 (`{핵심 기능명}` 등)."""
    src = "- `[RES-01] {핵심 기능명}`: 매핑"
    out = strip_template_placeholders(src)
    assert "{핵심 기능명}" not in out
    assert "미정" in out


# ─── 정상 텍스트 보존 ────────────────────────────────────────


def test_does_not_touch_normal_brackets():
    """`[Story-01.1]`, `[Screen: 데이터 조회 화면]` 같은 정상 bracket 은 보존."""
    src = (
        "#### 🖥️ [Screen: 데이터 조회 화면]\n"
        "- `[Story-01.1]` 사용자가 검색한다.\n"
        "- `[RES-02]` 키워드 인덱싱"
    )
    out = strip_template_placeholders(src)
    assert out == src  # 변화 없음


def test_does_not_touch_curly_with_english_or_digits():
    """영문/숫자 섞인 `{ID-01}`, `{name: value}` 등은 정상 콘텐츠로 간주 보존."""
    src = "{Epic-01} {Story-01.1} {name: 'x'}"
    out = strip_template_placeholders(src)
    assert out == src


def test_does_not_touch_inline_code_with_brackets():
    """`` `[Story-01.1]` `` 같은 inline code 안의 bracket — 줄바꿈 없으면 미매치."""
    src = "참고: `[Story-01.1]` 는 위 epic 의 첫 story."
    out = strip_template_placeholders(src)
    assert out == src


def test_does_not_touch_italic_or_emphasis():
    """`*강조*`, `**굵게**` markdown emphasis 는 건드리지 않음 (square/curly 아님)."""
    src = "이 단락은 *중요* 합니다. **핵심**은 별표 그대로."
    out = strip_template_placeholders(src)
    assert out == src


# ─── edge cases ───────────────────────────────────────────


def test_empty_input_returns_unchanged():
    assert strip_template_placeholders("") == ""
    assert strip_template_placeholders(None) is None


def test_non_string_input_passthrough():
    # 잘못된 타입은 그대로 반환 (호출자가 보호)
    assert strip_template_placeholders(123) == 123


def test_idempotent_double_strip():
    """이미 정리된 텍스트는 두 번 호출해도 동일."""
    src = "- {에픽명} → 미정\n- [기능 영역\n  예: A] → 미정"
    once = strip_template_placeholders(src)
    twice = strip_template_placeholders(once)
    assert once == twice


def test_curly_too_long_korean_not_matched():
    """40 글자 초과 한글-only curly 는 placeholder 가 아닐 가능성 — 보존.

    실제 placeholder 는 짧다 (`{에픽명}`, `{스토리 내용}`). 긴 한글 단락이 우연히
    curly 안에 들어간 정상 콘텐츠 보호.
    """
    long_text = "가" * 50
    src = "{" + long_text + "}"
    out = strip_template_placeholders(src)
    assert out == src
