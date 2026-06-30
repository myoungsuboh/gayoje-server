"""AI 페이스메이커 — 단계별 가이드 오케스트레이션.

거대 프롬프트 대신 "얇은 안전 프리앰블 + 단계별 집중 프롬프트"를 Python 이
합성한다. 인터뷰는 그 위의 1단계(INTERVIEW). 자세한 설계는 orchestrator.py 참고.
"""
from app.pipelines.guide.orchestrator import (
    GuidePhase,
    GuideState,
    compose_prompt,
    compose_with_safety,
    next_phase,
)

__all__ = ["GuidePhase", "GuideState", "compose_prompt", "compose_with_safety", "next_phase"]
