"""AI 페이스메이커 오케스트레이터 — 단계별 가이드의 골격.

[왜 이게 있나]
"한 프롬프트에 모든 지시를 때려 넣으면 LLM 이 과부하로 미스를 낸다"는 문제를
구조로 푼다. 거대 프롬프트 대신:

    매 LLM 호출 = [얇은 안전 프리앰블] + [현재 단계의 집중 프롬프트] + [상태]

오케스트레이터(이 모듈, 결정론적 Python)가 "지금 어느 단계인가"를 판단해
그 단계 전용 프롬프트 하나만 골라 합성한다. 각 프롬프트는 책임이 하나라
작고 집중되어 LLM 이 헷갈리지 않는다. 그리고 단계를 넘기는 행위 자체가
사용자를 시스템 완성까지 끌고 가는 "페이스메이커"가 된다.

[현재 골격 범위]
- GuidePhase: 단계 enum (지금은 INTERVIEW 만 실제 배선, 나머지는 확장 지점)
- GuideState: 현재 단계 + 프로젝트 상태 (단계 전환 판단 입력)
- compose_prompt: 안전 프리앰블 + 단계 프롬프트 합성 (과부하 방지의 핵심)
- next_phase: 상태 → 다음 단계 결정 (페이스메이커 진행 규칙이 자라날 곳)

[확장 방법]
1. GuidePhase 에 단계 추가 (예: REVIEW_PRD)
2. app/prompts/guide/phase_<value>.md 작성 (그 단계만의 작은 프롬프트)
3. next_phase 에 전환 규칙 한 줄 추가
거대 프롬프트를 건드리지 않고 단계가 늘어난다.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_GUIDE_PROMPT_DIR = _PROMPT_DIR / "guide"

# 모든 단계 공통으로 앞에 붙는 얇은 안전 프리앰블 (역할 고정 = 흑화 방지).
# 단계별 프롬프트에 중복 안 넣어 각 프롬프트를 작게 유지한다.
_SAFETY_FILE = "_safety.md"


class GuidePhase(str, Enum):
    """페이스메이커가 사용자를 인도하는 단계.

    str 상속 → 프롬프트 파일명(phase_<value>.md) 및 직렬화에 그대로 사용.
    """

    INTERVIEW = "interview"      # 1단계: 회의록 수집 (현재 배선됨)
    # ── 확장 지점 (프롬프트 + 전환 규칙 추가 시 활성화) ──
    # REVIEW_PRD = "review_prd"  # PRD 같이 훑고 빠진 것 짚기
    # REVIEW_DDD = "review_ddd"  # DDD 를 원숭이 말로 풀어주고 확인
    # DELIVER = "deliver"        # 결과물 안내


@dataclass
class GuideState:
    """페이스메이커 진행 상태 + 프로젝트 사실(facts).

    next_phase 가 이 상태를 보고 다음 단계를 결정한다. 골격 단계라 필드는
    최소만 — 단계가 늘면 has_* 플래그/점수를 채워 전환 규칙이 참조한다.
    """

    phase: GuidePhase = GuidePhase.INTERVIEW
    project_name: str = ""
    has_meeting_log: bool = False
    has_prd: bool = False
    has_ddd: bool = False


def _load(name: str) -> str:
    return (_GUIDE_PROMPT_DIR / name).read_text(encoding="utf-8")


def _phase_prompt_filename(phase: GuidePhase) -> str:
    return f"phase_{phase.value}.md"


def compose_with_safety(
    body_filename: str,
    *,
    variables: Optional[Dict[str, str]] = None,
) -> str:
    """[안전 프리앰블] + [guide/ 의 임의 본문 프롬프트] 합성.

    매 LLM 호출이 전체가 아니라 "공통 안전 3~4줄 + 이 작업 하나"만 보게 만들어
    과부하/지시 경쟁을 막는다. phase 프롬프트가 아닌 보조 프롬프트(예: 합성
    phase_synthesize.md)도 같은 안전 프리앰블을 공유하도록 일반화한 진입점.

    Raises:
        FileNotFoundError: 본문 파일이 없으면 (배선 누락 조기 발견).
    """
    safety = _load(_SAFETY_FILE)
    body = _load(body_filename)
    if variables:
        for key, value in variables.items():
            body = body.replace(key, value)
    return f"{safety}\n\n{body}"


def compose_prompt(
    phase: GuidePhase,
    *,
    variables: Optional[Dict[str, str]] = None,
) -> str:
    """[안전 프리앰블] + [단계 전용 프롬프트(phase_<value>.md)] 합성.

    Args:
        phase: 현재 단계. phase_<value>.md 를 로드한다.
        variables: 단계 프롬프트의 플레이스홀더 치환 ({{HISTORY}} 등).
    """
    return compose_with_safety(_phase_prompt_filename(phase), variables=variables)


def next_phase(state: GuideState) -> GuidePhase:
    """현재 상태로 다음 단계를 결정 (페이스메이커 진행 규칙).

    [골격]
    아직 회의록이 없으면 INTERVIEW. 회의록이 모이면 다음 단계로 — 지금은
    확장 지점만 표시하고 INTERVIEW 를 유지한다. 단계가 배선되면 여기에
    전환 규칙(예: has_meeting_log and not has_prd → REVIEW_PRD)이 추가된다.
    """
    if not state.has_meeting_log:
        return GuidePhase.INTERVIEW
    # TODO(phase 2+): 회의록 이후 PRD/DDD/Deliver 로 인도.
    #   if state.has_meeting_log and not state.has_prd: return GuidePhase.REVIEW_PRD
    #   if state.has_prd and not state.has_ddd:        return GuidePhase.REVIEW_DDD
    #   ...
    return GuidePhase.INTERVIEW
