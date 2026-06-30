#!/usr/bin/env python3
"""AI 인터뷰 품질 점검용 시뮬레이션 하니스 (수동 검증 도구).

실제 Gemini 를 호출해 "원숭이(비전공자) 사용자 ↔ AI 인터뷰" 대화를 끝까지
돌리고, 매 턴의 질문/예시답변/파악주제 + 최종 합성 회의록을 출력한다.
인터뷰가 빈 곳을 제대로 캐묻는지, 최종 회의록이 쓸만한지 눈으로 확인하는 용도.

사용법:
    # 시나리오 1개
    GEMINI_API_KEY=xxx python scripts/interview_sim.py 1

    # 전체 시나리오
    GEMINI_API_KEY=xxx python scripts/interview_sim.py all

    # LiteLLM proxy 환경이면 키 대신 LITELLM_PROXY_URL/LITELLM_MASTER_KEY 사용

사용자 답변은 아래 SCENARIOS 에 미리 스크립트되어 있다 (결정적 재현).
AI 질문 순서가 달라도 매 턴 "다음 미사용 답변"을 흘려보내고, 답변이 떨어지면
'잘 모르겠어요, 알아서 해주세요' 로 폴백 — 실제 게으른 사용자를 흉내낸다.
"""
from __future__ import annotations

import asyncio
import os
import sys

# 프로젝트 루트 import 경로
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.gemini_client import GeminiClient  # noqa: E402
from app.core import quota  # noqa: E402
from app.pipelines.base import PipelineContext  # noqa: E402
from app.pipelines.interview import InterviewMessage, run_interview_turn  # noqa: E402


class _NoNeo4j:
    """인터뷰는 Neo4j 를 안 쓰므로 호출되면 에러로 알린다."""

    def __getattr__(self, name):
        raise RuntimeError(f"interview 가 neo4j.{name} 를 호출 — 설계상 없어야 함")


SCENARIOS = {
    "1_모호한한줄": [
        "친구들이랑 돈 나누는 앱 만들고 싶어요",
        # 이후는 AI 가 캐물으면 답할 내용 — 일부러 듬성듬성
        "그냥 일반 사람들이요",
        "누가 얼마 냈는지 기록하고 정산하면 돼요",
    ],
    "2_모르겠어요사용자": [
        "쇼핑몰 같은 거요",
        "잘 모르겠어요, 알아서 해주세요",
        "음... 옷 팔 거예요",
        "결제는 있어야겠죠?",
    ],
    "3_동문서답": [
        "예약 앱이요",
        "저는 미용실 사장이에요",
        "손님이 시간 골라서 예약하면 좋겠어요",
        "노쇼가 너무 많아서요",
    ],
}

# 보완 인터뷰 시나리오 — 이미 작성한 초안이 있고, 빠진 부분만 묻는 흐름.
# 검증 포인트: AI 가 초안에 이미 있는 주제(개요/사용자/기능)는 다시 묻지 않고,
# 비어 있는 부분(로그인/결제/데이터 등)만 콕 집어 물어보는가. 최종 회의록이
# 기존 초안을 보존·병합하는가.
SUPPLEMENT_SCENARIOS = {
    "4_보완_결제데이터누락": {
        "existing": (
            "# 프로젝트 개요\n"
            "동네 책방을 위한 중고책 거래 앱. 주 사용자는 책방 주인과 동네 손님.\n\n"
            "# 핵심 사용자/역할\n- 책방 주인 (재고 등록)\n- 동네 손님 (구매)\n\n"
            "# 주요 기능\n1. 책 등록/검색\n2. 구매 요청\n3. 책방별 목록 보기\n"
        ),
        "answers": [
            # AI 가 빠진 부분(로그인/결제/알림 등)을 물어올 때 답할 내용
            "손님은 전화번호로 간단히, 주인은 따로 계정이 있어요",
            "현장 결제라 앱에서 결제는 안 해요",
            "거래 요청 오면 주인한테 알림 정도면 돼요",
        ],
    },
}

_FALLBACK_ANSWER = "잘 모르겠어요, 그 부분은 알아서 적절히 정해주세요."

C_Q = "\033[96m"   # AI 질문 - cyan
C_A = "\033[93m"   # 사용자 답변 - yellow
C_M = "\033[92m"   # 회의록 - green
C_D = "\033[90m"   # 메타 - dim
C_0 = "\033[0m"


async def run_scenario(name: str, answers: list[str], existing_content: str = "") -> None:
    print(f"\n{'='*70}\n  시나리오: {name}\n{'='*70}")
    if existing_content:
        print(f"{C_D}[기존 초안 — 보완 인터뷰]\n{existing_content.strip()}{C_0}")

    # 운영과 동일한 모델 (free 등급). Pro 모델 보려면 get_model_for_subscription('Pro').
    model = quota.get_model_for_subscription("free")
    gemini = GeminiClient(model=model)
    ctx = PipelineContext(gemini=gemini, neo4j=_NoNeo4j(), idempotency_key="sim")

    history: list[InterviewMessage] = []
    answer_queue = list(answers)
    turn_no = 0

    try:
        while turn_no < 15:
            turn_no += 1
            turn = await run_interview_turn(ctx, history, existing_content)

            print(f"\n{C_Q}[AI Q{turn_no}] {turn.assistant_message}{C_0}")
            if turn.suggestions:
                print(f"{C_D}   예시답변: {' / '.join(turn.suggestions)}{C_0}")
            if turn.coverage:
                print(f"{C_D}   파악된 주제: {', '.join(turn.coverage)}{C_0}")

            if turn.phase == "done":
                print(f"\n{C_M}{'─'*70}\n  ✅ 최종 회의록 (meeting_content)\n{'─'*70}{C_0}")
                print(f"{C_M}{turn.meeting_content}{C_0}")
                print(f"\n{C_D}  → 총 {turn_no}턴, 사용자 답변 {len([m for m in history if m.role=='user'])}회{C_0}")
                break

            # 사용자 답변 — 큐에서 꺼내거나 폴백
            ans = answer_queue.pop(0) if answer_queue else _FALLBACK_ANSWER
            print(f"{C_A}[사용자] {ans}{C_0}")

            history.append(InterviewMessage(role="assistant", content=turn.assistant_message))
            history.append(InterviewMessage(role="user", content=ans))
        else:
            print(f"\n{C_D}  ⚠️ 15턴 내 종료 안 됨 (강제 마무리 로직 점검 필요){C_0}")
    finally:
        await gemini.aclose()


async def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    cold_keys = list(SCENARIOS.keys())
    supp_keys = list(SUPPLEMENT_SCENARIOS.keys())
    all_keys = cold_keys + supp_keys

    if arg == "all":
        targets = all_keys
    elif arg in ("supp", "보완"):
        targets = supp_keys
    else:
        # "1" → 첫 시나리오 (전체 키 기준)
        try:
            targets = [all_keys[int(arg) - 1]]
        except (ValueError, IndexError):
            targets = [k for k in all_keys if arg in k] or all_keys

    for name in targets:
        if name in SUPPLEMENT_SCENARIOS:
            sc = SUPPLEMENT_SCENARIOS[name]
            await run_scenario(name, sc["answers"], existing_content=sc["existing"])
        else:
            await run_scenario(name, SCENARIOS[name])


if __name__ == "__main__":
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            or (os.getenv("LITELLM_PROXY_URL") and os.getenv("LITELLM_MASTER_KEY"))):
        print("❌ LLM 키가 없습니다. GEMINI_API_KEY 또는 LITELLM_PROXY_URL/LITELLM_MASTER_KEY 설정 필요.")
        sys.exit(1)
    asyncio.run(main())
