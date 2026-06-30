"""
주기적 Master PRD Cleanup — 다버전 누적으로 쌓이는 Vision/KPI/NFR 중복을
의미 기반(LLM)으로 정리. 데이터 손실 차단이 최우선.

[목적]
prd_merge.md 는 Section 1(Product Overview)·Section 4(NFR)에 ADD-ONLY 정책.
다버전 처리 시 같은 비전·NFR 규칙이 반복 누적되어 누더기 master PRD 가 된다.
기존 cleanup_master_prd.md 프롬프트(의미 dedup + reconcile + over-dedup 가드
완비)를 임계 버전마다 자동 트리거해 정리.

[데이터 안전]
- 모든 입력 Epic-XX / Story-XX.Y ID 가 출력에 보존돼야 함 (validate 가드).
- 출력 길이 ≥ 입력의 70% (대량 삭제 차단).
- Section 1~4 헤더 모두 존재.
- 검증 실패 시 master 안 덮어씀, cleanup_at_version_count 도 갱신 안 함 →
  다음 trigger 에서 재시도.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_SPEC_ID_RE = re.compile(r"\b(Epic-\d+|Story-\d+\.\d+)\b")

_REQUIRED_SECTION_HEADERS = (
    re.compile(r"^###\s*1\.\s", re.MULTILINE),
    re.compile(r"^###\s*2\.\s", re.MULTILINE),
    re.compile(r"^###\s*3\.\s", re.MULTILINE),
    re.compile(r"^###\s*4\.\s", re.MULTILINE),
)
_LENGTH_RATIO_THRESHOLD = 0.7


def validate_cleanup_output(input_md: str, output_md: str) -> Tuple[bool, str]:
    """Cleanup LLM 출력을 master 에 영속화하기 전 강제 검증.

    네 가지 가드:
      1. 비어 있지 않음.
      2. 길이 ≥ 입력의 70% (대량 삭제 차단).
      3. ### 1.~### 4. 섹션 헤더 모두 존재.
      4. 입력의 모든 Epic-XX / Story-XX.Y ID 가 출력에 보존 (★핵심★).

    실패 시 (False, "이유 한 문장") 반환. 호출자는 master 안 덮어쓰고 다음에 재시도.
    """
    if not output_md or not output_md.strip():
        return False, "cleanup 출력이 비어 있음"

    in_len = len(input_md.strip())
    out_len = len(output_md.strip())
    if in_len > 0 and out_len / in_len < _LENGTH_RATIO_THRESHOLD:
        return False, (
            f"cleanup 출력이 입력의 70% 미만 (out={out_len}, in={in_len}) — 대량 삭제 의심"
        )

    for idx, pat in enumerate(_REQUIRED_SECTION_HEADERS, start=1):
        if not pat.search(output_md):
            return False, f"cleanup 출력에 ### {idx}. 섹션 헤더 누락"

    in_ids = extract_spec_ids(input_md)
    out_ids = extract_spec_ids(output_md)
    missing = sorted(in_ids - out_ids)
    if missing:
        return False, f"cleanup 출력에서 spec ID 손실: {', '.join(missing[:5])}"

    return True, "ok"


def extract_spec_ids(md: str) -> Set[str]:
    """Master PRD markdown 에서 Epic-NN / Story-NN.M 식별자를 모두 추출.

    over-dedup 검증의 핵심 — input 의 모든 spec ID 가 output 에 보존돼야 한다.
    """
    if not md or not md.strip():
        return set()
    return set(_SPEC_ID_RE.findall(md))


def should_run_cleanup(prd_total: int, last_cleanup_count: int, interval: int) -> bool:
    """누적 PRD 카운트와 마지막 cleanup 시점의 차이가 interval 이상이면 True.

    prd_total=0 이면 정리할 master 자체가 없음 → False.
    """
    if prd_total <= 0:
        return False
    return (prd_total - last_cleanup_count) >= interval


async def call_prd_cleanup_agent(ctx: Any, master_content: str) -> str:
    """cleanup_master_prd.md 프롬프트로 master PRD 의 의미 기반 dedup 정리.

    LLM 1회. 출력은 code-block fence 와 잔여 placeholder 제거 후 반환.
    호출자는 반드시 validate_cleanup_output 통과 후에만 영속화해야 한다.
    """
    # 지연 import — 모듈 로딩 순환 방지(prd_pipeline 이 본 모듈을 임포트).
    from app.pipelines.prd_pipeline import (
        _TEMPERATURE,
        _load_prompt,
        _render,
    )
    from app.pipelines.base import strip_code_blocks, strip_template_placeholders

    prompt = _render(
        _load_prompt("cleanup_master_prd.md"),
        master_prd_markdown=master_content,
    )
    result = await ctx.gemini.generate(prompt, temperature=_TEMPERATURE)
    return strip_template_placeholders(strip_code_blocks(result.text))


async def run_prd_cleanup_if_due(
    ctx: Any,
    *,
    current_master_md: str,
    prd_total: int,
    last_cleanup_count: int,
    interval: int,
) -> Optional[str]:
    """임계치 검사 → cleanup LLM → 검증. 통과 시 새 markdown, 아니면 None.

    None 의 의미: master 를 cleanup 결과로 덮어쓰지 말 것 (skip 또는 graceful fail).
    호출자는 None 이면 cleanup_at_version_count 도 갱신하지 않아야 다음에 재시도된다.

    예외는 흡수 — cleanup 실패가 merge 결과를 망치면 안 된다.
    """
    if not should_run_cleanup(prd_total, last_cleanup_count, interval):
        return None
    try:
        cleaned = await call_prd_cleanup_agent(ctx, current_master_md)
    except Exception as e:  # noqa: BLE001 — graceful
        logger.warning("prd cleanup LLM failed: %s", e)
        return None
    ok, why = validate_cleanup_output(current_master_md, cleaned)
    if not ok:
        logger.warning("prd cleanup validation failed: %s", why)
        return None
    return cleaned
