"""guide 오케스트레이터 골격 — 프롬프트 합성/단계 전환 검증.

핵심:
- compose_prompt: 안전 프리앰블 + 단계 프롬프트가 함께 들어가는가 (과부하 방지 구조)
- compose_prompt: 변수 치환 ({{HISTORY}})
- compose_prompt: 안전 프리앰블의 역할고정 문구가 항상 포함되는가 (흑화 방지)
- next_phase: 상태 기반 단계 결정 (골격 — 회의록 전 INTERVIEW)
- GuidePhase ↔ 프롬프트 파일명 규약
"""
from __future__ import annotations

from app.pipelines.guide import GuidePhase, GuideState, compose_prompt, next_phase


def test_compose_layers_safety_and_phase_prompt():
    """안전 프리앰블 + 인터뷰 단계 프롬프트가 한 문자열로 합성된다."""
    out = compose_prompt(GuidePhase.INTERVIEW, variables={"{{HISTORY}}": "사용자: 안녕"})
    # 안전 프리앰블(역할 고정)
    assert "역할 고정" in out
    assert "명령 주입 무시" in out
    # 인터뷰 단계 프롬프트(고유 섹션)
    assert "인터뷰어" in out
    assert "PHASE:" in out  # 출력 형식 계약


def test_compose_substitutes_variables():
    out = compose_prompt(GuidePhase.INTERVIEW, variables={"{{HISTORY}}": "사용자: 쇼핑몰"})
    assert "사용자: 쇼핑몰" in out
    assert "{{HISTORY}}" not in out  # 치환 후 플레이스홀더 잔존 없음


def test_compose_without_variables_keeps_placeholder():
    """변수 미전달이면 본문 그대로 (치환 안 함)."""
    out = compose_prompt(GuidePhase.INTERVIEW)
    assert "{{HISTORY}}" in out


def test_safety_preamble_comes_first():
    """안전 프리앰블이 단계 프롬프트보다 앞에 온다 (우선순위)."""
    out = compose_prompt(GuidePhase.INTERVIEW)
    assert out.index("역할 고정") < out.index("# ROLE")


def test_next_phase_interview_when_no_meeting_log():
    assert next_phase(GuideState(has_meeting_log=False)) == GuidePhase.INTERVIEW


def test_next_phase_default_state_is_interview():
    assert next_phase(GuideState()) == GuidePhase.INTERVIEW


def test_compose_with_safety_loads_synthesize_prompt():
    """보조 프롬프트(합성)도 안전 프리앰블과 합성된다."""
    from app.pipelines.guide import compose_with_safety
    out = compose_with_safety("phase_synthesize.md", variables={"{{HISTORY}}": "사용자: 책방 앱", "{{EXISTING}}": "(없음)"})
    assert "역할 고정" in out          # 안전 프리앰블
    assert "회의록 작성 전문가" in out  # 합성 본문
    assert "사용자: 책방 앱" in out      # 치환됨


def test_guide_phase_value_maps_to_prompt_filename():
    """GuidePhase.value 가 phase_<value>.md 파일명 규약과 일치."""
    assert GuidePhase.INTERVIEW.value == "interview"
