"""
PRD 정확성 검증 — 1단계: 고신뢰 신호 양방향 매칭 (LLM 미사용 · 토큰 0).

[목적]
원본 회의록 ↔ 생성된 PRD 를 대조해, AI 분석이 원본을 충실히 반영했는지(누락) /
원본에 없는 내용을 지어냈는지(환각) 1차 신호를 찾는다.

[왜 '고신뢰 신호'만 보나]
한글 일반 명사는 형태소 분석 없이 정확 추출이 어려워(조사·어미로 노이즈) 위양성이
폭증한다. 그래서 1단계는 **정확히 추출 가능한 신호**만 본다:
  - 숫자+단위(20%, 5분, 80000원, 99.9%)
  - 날짜(2026-04-30, 4월 30일)
  - 영문 용어·약어(SQL, MSA, PDF, Excel, KPI) — 흔한 토큰은 stopword 제외
  - 따옴표/대괄호 인용("...", [...])
의미 단위(한글 문장의 누락/환각)는 2단계(LLM 정밀)에서 후보를 정제한다.

[결과 해석]
  - missing: 회의록엔 있는데 PRD 에 없음 → 누락 후보
  - hallucination: PRD 엔 있는데 회의록에 없음 → 환각 후보(AI 가 지어낸 수치/용어 의심)
둘 다 '후보' — 단정이 아니라 사용자/2단계 LLM 이 확정.
"""
from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple

# 날짜: 2026-04-30 / 2026.4.30 / 2026년 4월 30일 / 4월 30일
_RE_DATE = re.compile(
    r"\d{4}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}\s*일?|\d{1,2}\s*월\s*\d{1,2}\s*일"
)
# 숫자+단위 (단위 필수 — 단독 숫자/Story 번호 같은 노이즈 배제)
_RE_NUM_UNIT = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*"
    r"(?:%|퍼센트|분|초|시간|시|일간|일|주|개월|개월간|년|원|만원|억|명|건|회|배|점|위|개|GB|MB|KB|TB|ms)"
)
# 영문 용어·약어 (3자 이상)
_RE_ALPHA = re.compile(r"[A-Za-z][A-Za-z0-9+]{2,}")
# 따옴표/대괄호 인용 (2~40자, 줄바꿈 없는)
_RE_QUOTE = re.compile(
    r"['\"‘’“”\[]([^'\"‘’“”\[\]\n]{2,40})['\"‘’“”\]]"
)

# 너무 흔해 매칭 의미가 없는 영문 토큰(위양성 방지)
_ALPHA_STOP = {
    "the", "and", "for", "this", "that", "with", "from",
    "api", "http", "https", "www", "com", "org",
    "user", "data", "list", "page", "view", "story", "screen", "epic",
    "prd", "cps", "and", "name", "type", "json",
}


def _norm(s: str) -> str:
    """공백 제거 + 소문자(영문) — 표면 변형 흡수."""
    return re.sub(r"\s+", "", s).lower()


def extract_signals(text: str) -> Set[Tuple[str, str]]:
    """텍스트에서 (종류, 정규화값) 고신뢰 신호 집합 추출."""
    if not text:
        return set()
    sig: Set[Tuple[str, str]] = set()
    for m in _RE_DATE.finditer(text):
        sig.add(("date", _norm(m.group())))
    for m in _RE_NUM_UNIT.finditer(text):
        sig.add(("num", _norm(m.group())))
    for m in _RE_ALPHA.finditer(text):
        v = m.group().lower()
        if v not in _ALPHA_STOP:
            sig.add(("term", v))
    for m in _RE_QUOTE.finditer(text):
        v = _norm(m.group(1))
        if len(v) >= 2 and not v.isdigit():
            sig.add(("quote", v))
    return sig


_ORDER = {"num": 0, "date": 1, "term": 2, "quote": 3}


def _fmt(signals: Set[Tuple[str, str]]) -> List[Dict[str, str]]:
    return [
        {"type": t, "value": v}
        for t, v in sorted(signals, key=lambda x: (_ORDER.get(x[0], 9), x[1]))
    ]


def compare_fidelity(meeting_text: str, prd_text: str) -> Dict:
    """원본 회의록 ↔ PRD 신호 양방향 대조.

    Returns:
        {
          fidelity_pct: int,        # 회의록 신호 중 PRD 에 반영된 비율
          meeting_signal_count,
          matched_count,
          missing: [{type, value}],        # 누락 후보 (회의록 O / PRD X)
          hallucination: [{type, value}],  # 환각 후보 (PRD O / 회의록 X)
        }
    """
    ms = extract_signals(meeting_text)
    ps = extract_signals(prd_text)
    matched = ms & ps
    total = len(ms)
    return {
        "fidelity_pct": round(len(matched) / total * 100) if total else 100,
        "meeting_signal_count": total,
        "matched_count": len(matched),
        "missing": _fmt(ms - ps),
        "hallucination": _fmt(ps - ms),
    }
