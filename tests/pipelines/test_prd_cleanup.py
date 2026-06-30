"""
주기적 Master PRD Cleanup — 다버전 누적 시 Vision/KPI/NFR 중복 정리.

핵심:
  - should_run_cleanup: 임계 버전마다 trigger (순수함수).
  - extract_spec_ids: master markdown 에서 Epic/Story ID 추출.
  - validate_cleanup_output: over-dedup 차단 가드(Epic/Story ID 보존, 길이, 섹션).
"""
from __future__ import annotations

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.prd_cleanup import (
    call_prd_cleanup_agent,
    extract_spec_ids,
    run_prd_cleanup_if_due,
    should_run_cleanup,
    validate_cleanup_output,
)
from tests.conftest import FakeGemini

pytestmark = pytest.mark.asyncio


def _ctx(gemini, neo4j=None):
    return PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="t")


_INPUT_OK = """## 🗺️ Master PRD 조감도

### 1. Product Overview
- **통합 비전**: AI 도구 통합 관리.

### 2. Epic & User Story Map
#### 📦 [Epic-01] 신청 관리
- `[Story-01.1]` 사용자 신청 ➡️ *(구현 화면: 신청서)*
#### 📦 [Epic-02] 결제 통합
- `[Story-02.1]` 통합 결제 ➡️ *(구현 화면: 결제 페이지)*

### 3. Screen Architecture
#### 🖥️ [Screen: 신청서]
- **포함된 기능**:
  - `[Story-01.1]` 사용자 신청

### 4. Global Non-Functional Requirements
- **공통 규칙**:
  - 응답 1초 이내.
"""


def test_should_run_when_interval_reached():
    """누적 - 마지막 cleanup ≥ interval → True."""
    assert should_run_cleanup(prd_total=5, last_cleanup_count=0, interval=5) is True


def test_should_run_when_interval_exceeded():
    """초과해도 True (skip 누락 복구)."""
    assert should_run_cleanup(prd_total=12, last_cleanup_count=5, interval=5) is True


def test_should_not_run_below_interval():
    """차이가 interval 미만이면 False — 평상시 4/5 merge 는 cleanup 안 함."""
    assert should_run_cleanup(prd_total=4, last_cleanup_count=0, interval=5) is False
    assert should_run_cleanup(prd_total=7, last_cleanup_count=5, interval=5) is False


def test_should_not_run_at_zero_total():
    """누적 0 이면 cleanup 할 게 없음."""
    assert should_run_cleanup(prd_total=0, last_cleanup_count=0, interval=5) is False


def test_should_run_with_custom_interval():
    """interval 인자 사용 — env override 가능."""
    assert should_run_cleanup(prd_total=3, last_cleanup_count=0, interval=3) is True
    assert should_run_cleanup(prd_total=2, last_cleanup_count=0, interval=3) is False


def test_extract_finds_epic_and_story_ids():
    md = """
    #### 📦 [Epic-01] 식물 정보 관리
    - `[Story-01.1]` 식물 등록 ➡️ *(구현 화면: 등록 화면)*
    - `[Story-01.2]` 식물 수정 ➡️ *(구현 화면: 수정 화면)*
    #### 📦 [Epic-02] 알림
    - `[Story-02.1]` 물주기 알림
    """
    ids = extract_spec_ids(md)
    assert ids == {"Epic-01", "Epic-02", "Story-01.1", "Story-01.2", "Story-02.1"}


def test_extract_ignores_unrelated_brackets():
    """[Role A] / [내용] 같은 placeholder brackets 는 무시."""
    md = "[Role A] 권한.  `[Epic-01]` 진짜 Epic. `[내용]` placeholder."
    assert extract_spec_ids(md) == {"Epic-01"}


def test_extract_empty_md_returns_empty_set():
    assert extract_spec_ids("") == set()
    assert extract_spec_ids("   \n  ") == set()


def test_extract_handles_two_digit_ids():
    """Epic-10, Story-12.34 같은 다자릿수 ID 도 인식."""
    md = "#### 📦 [Epic-10] x\n- `[Story-12.34]` y"
    assert extract_spec_ids(md) == {"Epic-10", "Story-12.34"}


def test_validate_passes_when_well_formed_dedup():
    """출력이 입력의 모든 spec ID 보존 + 4 섹션 + 길이 OK → pass."""
    ok, why = validate_cleanup_output(_INPUT_OK, _INPUT_OK)
    assert ok is True, why


def test_validate_rejects_empty_output():
    ok, why = validate_cleanup_output(_INPUT_OK, "")
    assert ok is False
    assert "비어" in why or "empty" in why.lower()


def test_validate_rejects_too_short_output():
    """출력 < 입력의 70% → 대량 삭제 차단."""
    short = _INPUT_OK[: len(_INPUT_OK) // 3]   # ~33%
    ok, why = validate_cleanup_output(_INPUT_OK, short)
    assert ok is False
    assert "길이" in why or "70%" in why


def test_validate_rejects_missing_section():
    """### 4. 가 사라지면 차단 — 구조 손상."""
    out = _INPUT_OK.replace("### 4. Global Non-Functional Requirements", "")
    ok, why = validate_cleanup_output(_INPUT_OK, out)
    assert ok is False
    assert "섹션" in why or "section" in why.lower()


def test_validate_rejects_lost_epic_id():
    """Epic-02 가 출력에서 사라지면 차단 — over-dedup 핵심 가드."""
    out = _INPUT_OK.replace("[Epic-02]", "[Removed]")
    ok, why = validate_cleanup_output(_INPUT_OK, out)
    assert ok is False
    assert "Epic-02" in why


def test_validate_rejects_lost_story_id():
    out = _INPUT_OK.replace("[Story-01.1]", "[Story-99.99]")
    ok, why = validate_cleanup_output(_INPUT_OK, out)
    assert ok is False
    assert "Story-01.1" in why


async def test_cleanup_agent_renders_prompt_and_strips_output():
    """LLM 호출 — prompt 에 master_prd_markdown 삽입, 출력은 code-block 제거."""
    fake_md_out = "```markdown\n## 🗺️ Master PRD 조감도\n### 1. ...\n```"
    gemini = FakeGemini(responses=[fake_md_out])
    ctx = _ctx(gemini)

    out = await call_prd_cleanup_agent(ctx, master_content="## 🗺️ 원본\n### 1. 비전")

    assert "## 🗺️ 원본" in gemini.calls[0]["prompt"]
    assert "```" not in out
    assert "## 🗺️ Master PRD 조감도" in out


async def test_run_returns_none_when_below_threshold():
    """interval 도달 전 → cleanup 호출 안 함 → None."""
    gemini = FakeGemini(responses=["should not be called"])
    ctx = _ctx(gemini)

    out = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=_INPUT_OK,
        prd_total=3,
        last_cleanup_count=0,
        interval=5,
    )

    assert out is None
    assert gemini.calls == []   # LLM 호출 0회 — 비용 절감 확인


async def test_run_returns_cleaned_when_due_and_validation_passes():
    """interval 도달 + 검증 통과 → 새 markdown 반환."""
    cleaned = _INPUT_OK   # 정확히 같은 ID/섹션 유지 → 검증 통과
    gemini = FakeGemini(responses=[cleaned])
    ctx = _ctx(gemini)

    out = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=_INPUT_OK,
        prd_total=5,
        last_cleanup_count=0,
        interval=5,
    )

    assert out is not None
    assert out.rstrip() == cleaned.rstrip()   # 후처리로 trailing whitespace만 다름 OK
    assert len(gemini.calls) == 1


async def test_run_returns_none_when_validation_fails():
    """interval 도달했지만 출력이 Epic ID 손실 → None (master 안 덮어씀)."""
    bad_out = _INPUT_OK.replace("[Epic-02]", "[Removed]")
    gemini = FakeGemini(responses=[bad_out])
    ctx = _ctx(gemini)

    out = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=_INPUT_OK,
        prd_total=5,
        last_cleanup_count=0,
        interval=5,
    )

    assert out is None


def test_build_query_sets_cleanup_at_version_count():
    """build_merge_master_prd_query 가 cleanup_at_version_count 를 SET 한다."""
    from app.pipelines.prd_pipeline import build_merge_master_prd_query
    cypher, params = build_merge_master_prd_query(
        project_name="food",
        merged_content="# content",
        latest_delta_id=None,
        cleanup_at_version_count=3,
    )
    assert "cleanup_at_version_count" in cypher
    assert params["cleanup_at_version_count"] == 3


def test_build_query_requires_cleanup_at_version_count():
    """필수 keyword 인자 — 호출자가 명시 누락 시 TypeError (모든 호출 흐름 갱신 강제)."""
    from app.pipelines.prd_pipeline import build_merge_master_prd_query
    with pytest.raises(TypeError):
        build_merge_master_prd_query(
            project_name="food",
            merged_content="# content",
            latest_delta_id=None,
        )


async def test_run_returns_none_when_llm_raises():
    """LLM 예외 → 삼키고 None (graceful) — merge 결과 망치지 않음."""

    class Boom:
        async def generate(self, *a, **kw):
            raise RuntimeError("gemini 500")

    ctx = _ctx(Boom())

    out = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=_INPUT_OK,
        prd_total=5,
        last_cleanup_count=0,
        interval=5,
    )

    assert out is None
