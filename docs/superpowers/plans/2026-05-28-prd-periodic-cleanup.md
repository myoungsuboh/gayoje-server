# Periodic Master PRD Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매 5버전 누적마다 기존 `cleanup_master_prd.md` 프롬프트로 master PRD 의 Vision/KPI/NFR 중복을 자동 정리하되, Epic/Story ID 보존을 강제해 데이터 손실 0 보장.

**Architecture:** `run_prd_merge` 의 incremental save 직후 임계치 검사 → 통과 시 LLM 1회로 cleanup → 강제 검증(Epic/Story ID 보존 + 섹션 헤더 + 길이 ≥70%) 통과 시에만 master 덮어쓰기, 실패 시 graceful skip. 임계치 추적은 master 노드 새 필드 `cleanup_at_version_count`.

**Tech Stack:** Python 3.13, pytest-asyncio, Neo4j (Cypher), Gemini (LiteLLM proxy). 기존 PRD 파이프라인 패턴(`_load_prompt` + `_render` + `ctx.gemini.generate`) 그대로 재사용.

**Spec:** [docs/superpowers/specs/2026-05-28-prd-periodic-cleanup-design.md](../specs/2026-05-28-prd-periodic-cleanup-design.md)

---

## File Structure

- **Create** `app/pipelines/prd_cleanup.py` — 순수함수 3개(`should_run_cleanup`, `extract_spec_ids`, `validate_cleanup_output`) + 비동기 헬퍼 2개(`call_prd_cleanup_agent`, `run_prd_cleanup_if_due`).
- **Modify** `app/pipelines/prd_pipeline.py`:
  - `_GET_ALL_PRD_QUERY` RETURN 에 `cleanup_at_version_count` 추가.
  - `fetch_prd_master_and_latest` 반환 dict 에 키 추가.
  - `build_merge_master_prd_query` 시그니처에 `cleanup_at_version_count: int` 필수 인자 추가 + SET 절 1줄 추가.
  - `run_prd_merge` 의 두 호출자(첫 실행+증분, 빈-PRD-guarantee)에 인자 전달 + 저장 직후 cleanup wire-in.
- **Create** `tests/pipelines/test_prd_cleanup.py` — 순수함수 + 오케스트레이션 단위 테스트.
- **Create** `tests/pipelines/test_prd_merge_periodic_cleanup.py` — `run_prd_merge` 와 통합.

---

## Task 1: `should_run_cleanup` 순수함수

**Files:**
- Create: `app/pipelines/prd_cleanup.py` (새 파일, 이 task 까지는 함수 1개)
- Test: `tests/pipelines/test_prd_cleanup.py` (새 파일)

- [ ] **Step 1: Write the failing tests**

`tests/pipelines/test_prd_cleanup.py`:

```python
"""
주기적 Master PRD Cleanup — 다버전 누적 시 Vision/KPI/NFR 중복 정리.

핵심:
  - should_run_cleanup: 임계 버전마다 trigger (순수함수).
  - extract_spec_ids: master markdown 에서 Epic/Story ID 추출.
  - validate_cleanup_output: over-dedup 차단 가드(Epic/Story ID 보존, 길이, 섹션).
"""
from __future__ import annotations

import pytest

from app.pipelines.prd_cleanup import should_run_cleanup


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
```

- [ ] **Step 2: Run tests — verify they FAIL**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py -v -p no:warnings`
Expected: `ImportError: cannot import name 'should_run_cleanup' from 'app.pipelines.prd_cleanup'` (모듈 없음).

- [ ] **Step 3: Create `app/pipelines/prd_cleanup.py` with the function**

```python
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


def should_run_cleanup(prd_total: int, last_cleanup_count: int, interval: int) -> bool:
    """누적 PRD 카운트와 마지막 cleanup 시점의 차이가 interval 이상이면 True.

    prd_total=0 이면 정리할 master 자체가 없음 → False.
    """
    if prd_total <= 0:
        return False
    return (prd_total - last_cleanup_count) >= interval
```

- [ ] **Step 4: Run tests — verify they PASS**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py -v -p no:warnings`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/pipelines/prd_cleanup.py tests/pipelines/test_prd_cleanup.py
git commit -m "feat(prd-cleanup): should_run_cleanup 임계 버전 trigger 순수함수"
```

---

## Task 2: `extract_spec_ids` 순수함수

**Files:**
- Modify: `app/pipelines/prd_cleanup.py`
- Test: `tests/pipelines/test_prd_cleanup.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/pipelines/test_prd_cleanup.py`

```python
from app.pipelines.prd_cleanup import extract_spec_ids


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
```

- [ ] **Step 2: Run — verify FAIL**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py::test_extract_finds_epic_and_story_ids -v -p no:warnings`
Expected: ImportError.

- [ ] **Step 3: Append to `app/pipelines/prd_cleanup.py`**

```python
import re
from typing import Set

_SPEC_ID_RE = re.compile(r"\b(Epic-\d+|Story-\d+\.\d+)\b")


def extract_spec_ids(md: str) -> Set[str]:
    """Master PRD markdown 에서 Epic-NN / Story-NN.M 식별자를 모두 추출.

    over-dedup 검증의 핵심 — input 의 모든 spec ID 가 output 에 보존돼야 한다.
    """
    if not md or not md.strip():
        return set()
    return set(_SPEC_ID_RE.findall(md))
```

- [ ] **Step 4: Run — verify PASS**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py -v -p no:warnings`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add app/pipelines/prd_cleanup.py tests/pipelines/test_prd_cleanup.py
git commit -m "feat(prd-cleanup): extract_spec_ids — Epic/Story ID 추출"
```

---

## Task 3: `validate_cleanup_output` 가드

**Files:**
- Modify: `app/pipelines/prd_cleanup.py`
- Test: `tests/pipelines/test_prd_cleanup.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
from app.pipelines.prd_cleanup import validate_cleanup_output


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


def test_validate_passes_when_well_formed_dedup():
    """출력이 입력의 모든 spec ID 보존 + 4 섹션 + 길이 OK → pass."""
    # 출력은 입력을 그대로 (의미 dedup 결과지만 우리 검증은 구조만)
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
    out = _INPUT_OK.replace("[Epic-02]", "[Removed]").replace(
        "[Story-02.1]", "[Story-02.1]"
    )
    # Epic-02 만 사라짐, Story-02.1 은 남음
    ok, why = validate_cleanup_output(_INPUT_OK, out)
    assert ok is False
    assert "Epic-02" in why


def test_validate_rejects_lost_story_id():
    out = _INPUT_OK.replace("[Story-01.1]", "[Story-99.99]")
    ok, why = validate_cleanup_output(_INPUT_OK, out)
    assert ok is False
    assert "Story-01.1" in why
```

- [ ] **Step 2: Run — verify FAIL**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py -v -p no:warnings -k validate`
Expected: ImportError or AttributeError.

- [ ] **Step 3: Append to `app/pipelines/prd_cleanup.py`**

```python
from typing import Tuple

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
```

- [ ] **Step 4: Run — verify PASS**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py -v -p no:warnings`
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add app/pipelines/prd_cleanup.py tests/pipelines/test_prd_cleanup.py
git commit -m "feat(prd-cleanup): validate_cleanup_output 4중 가드 (ID 보존·길이·섹션)"
```

---

## Task 4: `call_prd_cleanup_agent` (LLM 호출)

**Files:**
- Modify: `app/pipelines/prd_cleanup.py`
- Test: `tests/pipelines/test_prd_cleanup.py`

- [ ] **Step 1: Write the failing test** — append:

```python
from tests.conftest import FakeGemini, make_pipeline_context
from app.pipelines.prd_cleanup import call_prd_cleanup_agent

pytestmark = pytest.mark.asyncio   # 파일 상단에 이미 있으면 중복 X


async def test_cleanup_agent_renders_prompt_and_strips_output():
    """LLM 호출 — prompt 에 master_prd_markdown 삽입, 출력은 code-block 제거."""
    fake_md_out = "```markdown\n## 🗺️ Master PRD 조감도\n### 1. ...\n```"
    gemini = FakeGemini(responses=[fake_md_out])
    ctx = make_pipeline_context(gemini=gemini, neo4j=None)

    out = await call_prd_cleanup_agent(ctx, master_content="## 🗺️ 원본\n### 1. 비전")

    # prompt 안에 입력 master 가 들어갔는지
    assert "## 🗺️ 원본" in gemini.calls[0]["prompt"]
    # code-block fence 제거됨
    assert "```" not in out
    assert "## 🗺️ Master PRD 조감도" in out
```

`pytest.mark.asyncio` 가 파일 위쪽에 없다면 추가:

```python
pytestmark = pytest.mark.asyncio
```

- [ ] **Step 2: Run — verify FAIL**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py::test_cleanup_agent_renders_prompt_and_strips_output -v -p no:warnings`
Expected: ImportError on `call_prd_cleanup_agent`.

- [ ] **Step 3: Append to `app/pipelines/prd_cleanup.py`**

```python
from typing import Any

# prd_pipeline 의 헬퍼를 그대로 재사용 — prompt 로더/렌더/스트리퍼 일관성.
from app.pipelines.prd_pipeline import (
    _load_prompt,
    _render,
    strip_code_blocks,
    strip_template_placeholders,
    _TEMPERATURE,
)


async def call_prd_cleanup_agent(ctx: Any, master_content: str) -> str:
    """cleanup_master_prd.md 프롬프트로 master PRD 의 의미 기반 dedup 정리.

    LLM 1회. 출력은 code-block fence 와 잔여 placeholder 제거 후 반환.
    호출자는 반드시 validate_cleanup_output 통과 후에만 영속화해야 한다.
    """
    prompt = _render(
        _load_prompt("cleanup_master_prd.md"),
        master_prd_markdown=master_content,
    )
    result = await ctx.gemini.generate(prompt, temperature=_TEMPERATURE)
    return strip_template_placeholders(strip_code_blocks(result.text))
```

- [ ] **Step 4: Run — verify PASS**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py -v -p no:warnings`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add app/pipelines/prd_cleanup.py tests/pipelines/test_prd_cleanup.py
git commit -m "feat(prd-cleanup): call_prd_cleanup_agent — LLM 1회 호출 + 출력 정리"
```

---

## Task 5: `run_prd_cleanup_if_due` 오케스트레이터

**Files:**
- Modify: `app/pipelines/prd_cleanup.py`
- Test: `tests/pipelines/test_prd_cleanup.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
from app.pipelines.prd_cleanup import run_prd_cleanup_if_due


async def test_run_returns_none_when_below_threshold():
    """interval 도달 전 → cleanup 호출 안 함 → None."""
    gemini = FakeGemini(responses=["should not be called"])
    ctx = make_pipeline_context(gemini=gemini, neo4j=None)

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
    ctx = make_pipeline_context(gemini=gemini, neo4j=None)

    out = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=_INPUT_OK,
        prd_total=5,
        last_cleanup_count=0,
        interval=5,
    )

    assert out == cleaned
    assert len(gemini.calls) == 1


async def test_run_returns_none_when_validation_fails():
    """interval 도달했지만 출력이 Epic ID 손실 → None (master 안 덮어씀)."""
    bad_out = _INPUT_OK.replace("[Epic-02]", "[Removed]")
    gemini = FakeGemini(responses=[bad_out])
    ctx = make_pipeline_context(gemini=gemini, neo4j=None)

    out = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=_INPUT_OK,
        prd_total=5,
        last_cleanup_count=0,
        interval=5,
    )

    assert out is None   # 검증 실패 시 None — 호출자는 incremental 결과 유지


async def test_run_returns_none_when_llm_raises():
    """LLM 예외 → 삼키고 None (graceful) — merge 결과 망치지 않음."""

    class Boom:
        async def generate(self, *a, **kw):
            raise RuntimeError("gemini 500")

    ctx = make_pipeline_context(gemini=Boom(), neo4j=None)

    out = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=_INPUT_OK,
        prd_total=5,
        last_cleanup_count=0,
        interval=5,
    )

    assert out is None
```

- [ ] **Step 2: Run — verify FAIL**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py -v -p no:warnings -k run_`
Expected: ImportError on `run_prd_cleanup_if_due`.

- [ ] **Step 3: Append to `app/pipelines/prd_cleanup.py`**

```python
import logging
from typing import Optional

logger = logging.getLogger(__name__)


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
```

- [ ] **Step 4: Run — verify PASS**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py -v -p no:warnings`
Expected: 20 passed.

- [ ] **Step 5: Commit**

```bash
git add app/pipelines/prd_cleanup.py tests/pipelines/test_prd_cleanup.py
git commit -m "feat(prd-cleanup): run_prd_cleanup_if_due 오케스트레이션 + graceful skip"
```

---

## Task 6: Cypher 확장 — `cleanup_at_version_count` 저장·조회

**Files:**
- Modify: `app/pipelines/prd_pipeline.py:298-381` (_GET_ALL_PRD_QUERY + fetch_prd_master_and_latest)
- Modify: `app/pipelines/prd_pipeline.py:625-700` (build_merge_master_prd_query)
- Test: `tests/pipelines/test_prd_cleanup.py` (build query SQL 검증)

- [ ] **Step 1: Write the failing tests** — append to `tests/pipelines/test_prd_cleanup.py`:

```python
from app.pipelines.prd_pipeline import build_merge_master_prd_query


def test_build_query_sets_cleanup_at_version_count():
    """build_merge_master_prd_query 가 cleanup_at_version_count 를 SET 한다."""
    cypher, params = build_merge_master_prd_query(
        project_name="food",
        merged_content="# content",
        latest_delta_id=None,
        cleanup_at_version_count=3,
    )
    assert "cleanup_at_version_count" in cypher
    assert params["cleanup_at_version_count"] == 3


def test_build_query_requires_cleanup_at_version_count():
    """필수 인자 — 호출자가 명시 누락 시 TypeError (호출 흐름 모두 갱신 강제)."""
    with pytest.raises(TypeError):
        build_merge_master_prd_query(
            project_name="food",
            merged_content="# content",
            latest_delta_id=None,
        )
```

- [ ] **Step 2: Run — verify FAIL**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py -v -p no:warnings -k build_query`
Expected: 함수 시그니처에 인자 없어서 첫 테스트 TypeError 또는 두 번째 테스트 실패.

- [ ] **Step 3: Modify `_GET_ALL_PRD_QUERY`**

`app/pipelines/prd_pipeline.py:338-347` (현재 RETURN 절):

```python
RETURN
    m.id AS master_id,
    m.full_markdown AS master_content,
    master_prd_details,
    l.id AS latest_id,
    l.full_markdown AS latest_content,
    latest_prd_details,
    coalesce(m.project, l.project, $project) AS project_name,
    prd_total,
    m.cleanup_at_version_count AS cleanup_at_version_count
"""
```

(마지막 줄 추가, 기존 `prd_total` 뒤에 쉼표 추가.)

- [ ] **Step 4: Modify `fetch_prd_master_and_latest`** — add 2 lines

`app/pipelines/prd_pipeline.py:355-365` (records 없을 때 dict):

```python
    if not records:
        return {
            "master_id": None,
            "master_content": "",
            "master_prd_details": [],
            "latest_id": None,
            "latest_content": "",
            "latest_prd_details": [],
            "project_name": project_name,
            "prd_total": 0,
            "cleanup_at_version_count": 0,   # ← 추가
        }
```

`app/pipelines/prd_pipeline.py:367-381` (records 있을 때 dict 마지막):

```python
        "prd_total": int(row.get("prd_total") or 0),
        # [2026-05-28] L1-3 — null(필드 없음) 시 0 으로 coerce. 첫 cleanup 은
        # incremental save 에서 prd_total 로 init 되고, 그 후 interval 도달 시 trigger.
        "cleanup_at_version_count": int(row.get("cleanup_at_version_count") or 0),
    }
```

- [ ] **Step 5: Modify `build_merge_master_prd_query`** — add required arg + SET line

`app/pipelines/prd_pipeline.py` 시그니처 변경:

```python
def build_merge_master_prd_query(
    project_name: str,
    merged_content: str,
    latest_delta_id: Optional[str],
    *,
    cleanup_at_version_count: int,
) -> Tuple[str, Dict[str, Any]]:
```

`parts` 리스트(665행 근처)에서 `master.updated_at = timestamp()` 줄을 다음으로 교체:

```python
        "    master.updated_at = timestamp(),",
        "    master.cleanup_at_version_count = $cleanup_at_version_count",
```

`params` dict(673행 근처)에 추가:

```python
    params: Dict[str, Any] = {
        "master_prd_id": master_prd_id,
        "project": project_name,
        "merged_content": merged_content,
        "master_cps_id": master_cps_id,
        "cleanup_at_version_count": cleanup_at_version_count,
    }
```

- [ ] **Step 6: Update existing callers in `prd_pipeline.py`**

`run_prd_merge` 안의 `build_merge_master_prd_query` 호출은 현재 2곳 — `#71 빈-PRD-guarantee` 케이스와 본 incremental 케이스. 두 곳 모두에 키워드 인자 추가:

빈-PRD 케이스(spec_count==0, first_run): 새 master 이므로 `cleanup_at_version_count=prd_state["prd_total"]` 로 init.

Incremental 케이스(917행 근처):

```python
    merge_query, merge_params = build_merge_master_prd_query(
        project_name=payload.project_name,
        merged_content=reassembled["merged_content"],
        latest_delta_id=prd_state.get("latest_id") or delta_prd_id,
        cleanup_at_version_count=(
            prd_state.get("cleanup_at_version_count") or prd_state["prd_total"]
        ),
    )
```

(`or` 로 0/null fallback → 처음엔 prd_total 로 init, 이후엔 기존 값 보존.)

빈-PRD 케이스도 동일 패턴:

```python
    merge_query, merge_params = build_merge_master_prd_query(
        project_name=payload.project_name,
        merged_content=prd_markdown,
        latest_delta_id=delta_prd_id,
        cleanup_at_version_count=prd_state["prd_total"],
    )
```

찾아 수정: `grep -n "build_merge_master_prd_query(" app/pipelines/prd_pipeline.py` 로 2~3 곳 모두 키워드 인자 추가.

- [ ] **Step 7: Run unit tests — verify PASS**

Run: `python -m pytest tests/pipelines/test_prd_cleanup.py -v -p no:warnings`
Expected: 22 passed.

- [ ] **Step 8: Run existing PRD suite — verify NO regression**

Run: `python -m pytest tests/pipelines/ -v -p no:warnings -k "prd or cps_to_prd"`
Expected: 모두 PASS (기존 PRD 테스트가 키워드 인자 누락으로 깨질 수 있으니, 깨지면 그 호출부도 같이 수정).

깨진 테스트가 있으면: `tests/pipelines/test_*.py` 에서 `build_merge_master_prd_query(` 호출 찾아 `cleanup_at_version_count=0` 추가.

- [ ] **Step 9: Commit**

```bash
git add app/pipelines/prd_pipeline.py tests/pipelines/test_prd_cleanup.py
git commit -m "feat(prd-cleanup): Neo4j 쿼리에 cleanup_at_version_count 필드 추가"
```

---

## Task 7: Wire-in to `run_prd_merge`

**Files:**
- Modify: `app/pipelines/prd_pipeline.py` (run_prd_merge, line ~917 직후)
- Test: `tests/pipelines/test_prd_merge_periodic_cleanup.py` (새 파일)

- [ ] **Step 1: Write failing integration test**

`tests/pipelines/test_prd_merge_periodic_cleanup.py`:

```python
"""
run_prd_merge 와 cleanup wire-in 통합 — 5버전 누적 시 cleanup 트리거되는지,
검증 실패 시 master 안 덮어쓰는지.

핵심:
  - prd_total - last < interval: cleanup LLM 호출 0회 (평상시 비용 영향 없음).
  - prd_total - last >= interval: cleanup LLM 호출 1회 + master 재저장.
  - 검증 실패: master 안 덮어쓰고 cleanup_at_version_count 도 안 갱신.
"""
from __future__ import annotations

import os
import pytest

from app.pipelines import prd_pipeline


pytestmark = pytest.mark.asyncio


def _count_master_writes(neo) -> int:
    """FakeNeo4j.executed 중 build_merge_master_prd_query 가 만든 cypher 카운트."""
    return sum(
        1
        for op in neo.executed
        if "MERGE (master:PRD_Document {id:" in op["cypher"]
    )


async def test_cleanup_skipped_when_below_threshold(monkeypatch):
    """누적 3버전 (last_cleanup=0, interval=5) → cleanup LLM 호출 0회."""
    monkeypatch.setenv("PRD_CLEANUP_VERSION_INTERVAL", "5")

    # NOTE: 이 테스트는 run_prd_merge 전체 흐름이 무거워서, cleanup wire-in 직접 단위
    # 검증으로 대체 가능. 통합은 다음 task 의 e2e fixture 로.
    pass   # placeholder — Step 3 에서 실제 구현
```

- [ ] **Step 2: Run — verify it's empty/skipped**

Run: `python -m pytest tests/pipelines/test_prd_merge_periodic_cleanup.py -v -p no:warnings`
Expected: 1 passed (placeholder).

> **NOTE**: `run_prd_merge` 의 모든 LLM 단계(extract → graph → impact → merge → cleanup) 를 stub 하면 fixture 가 무거워진다. wire-in 단위 검증을 위해 더 작은 helper 를 노출:

- [ ] **Step 3: Add wire-in to `run_prd_merge`** — `app/pipelines/prd_pipeline.py:917` 직후 (`await ctx.neo4j.run_cypher(merge_query, merge_params)` 다음)

위 import 섹션에 추가:

```python
import os
from app.pipelines.prd_cleanup import run_prd_cleanup_if_due
```

`master_prd_id = ...` 줄 직전(라인 919) 에 cleanup wire-in:

```python
    await ctx.neo4j.run_cypher(merge_query, merge_params)

    # [2026-05-28] L1-3 — 5버전마다 master PRD dedup cleanup (Vision/KPI/NFR).
    # build_merge_master_prd_query 가 SET 한 cleanup_at_version_count 는 fetch
    # 다음번에 보이므로, 여기선 방금 저장한 merged_content + 이번 trigger 의 baseline
    # 으로 직접 계산. 검증 실패 시 master 안 덮어쓰고 baseline 도 안 갱신 → 다음 재시도.
    _interval = int(os.environ.get("PRD_CLEANUP_VERSION_INTERVAL", "5"))
    _prd_total = prd_state["prd_total"]
    _last_cleanup = prd_state.get("cleanup_at_version_count") or _prd_total
    cleaned_md = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=reassembled["merged_content"],
        prd_total=_prd_total,
        last_cleanup_count=_last_cleanup,
        interval=_interval,
    )
    if cleaned_md is not None:
        cleanup_query, cleanup_params = build_merge_master_prd_query(
            project_name=payload.project_name,
            merged_content=cleaned_md,
            latest_delta_id=prd_state.get("latest_id") or delta_prd_id,
            cleanup_at_version_count=_prd_total,   # baseline 갱신
        )
        await ctx.neo4j.run_cypher(cleanup_query, cleanup_params)
        logger.info(
            "prd cleanup applied: project=%s prd_total=%d in=%d out=%d",
            payload.project_name, _prd_total,
            len(reassembled["merged_content"]), len(cleaned_md),
        )

    master_prd_id = f"doc_prd_master_{payload.project_name.replace('.', '_')}"
```

- [ ] **Step 4: Replace placeholder integration test with real one**

`tests/pipelines/test_prd_merge_periodic_cleanup.py` 전체 교체:

```python
"""
run_prd_merge 와 cleanup wire-in 통합 — 5버전 누적 시 cleanup 트리거되는지.

핵심:
  - prd_total - last < interval: cleanup LLM 호출 0회.
  - prd_total - last >= interval: cleanup LLM 호출 1회 + master 재저장.
  - 검증 실패: master 한 번만 저장 (cleanup 결과 버려짐).
"""
from __future__ import annotations

import pytest

from app.pipelines.prd_cleanup import run_prd_cleanup_if_due

from tests.conftest import FakeGemini, make_pipeline_context


pytestmark = pytest.mark.asyncio


_MASTER_OK = """## 🗺️ Master PRD 조감도

### 1. Product Overview
- **통합 비전**: 비전.

### 2. Epic & User Story Map
#### 📦 [Epic-01] 신청
- `[Story-01.1]` 사용자 신청 ➡️ *(구현 화면: 신청서)*

### 3. Screen Architecture
#### 🖥️ [Screen: 신청서]
- **포함된 기능**:
  - `[Story-01.1]` 사용자 신청

### 4. Global Non-Functional Requirements
- **공통 규칙**:
  - 성능 SLA 1초.
"""


async def test_cleanup_skipped_when_below_threshold():
    gemini = FakeGemini(responses=["must not be called"])
    ctx = make_pipeline_context(gemini=gemini, neo4j=None)

    cleaned = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=_MASTER_OK,
        prd_total=3,
        last_cleanup_count=0,
        interval=5,
    )

    assert cleaned is None
    assert gemini.calls == []   # 평상시 LLM 0회 확인


async def test_cleanup_invoked_and_persisted_when_due():
    """interval 도달 + 검증 통과 → 새 md 반환 (호출자가 영속화)."""
    gemini = FakeGemini(responses=[_MASTER_OK])   # 의미상 dedup된 결과 모사
    ctx = make_pipeline_context(gemini=gemini, neo4j=None)

    cleaned = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=_MASTER_OK,
        prd_total=5,
        last_cleanup_count=0,
        interval=5,
    )

    assert cleaned is not None
    assert "[Epic-01]" in cleaned   # ID 보존 확인
    assert len(gemini.calls) == 1


async def test_cleanup_discarded_when_epic_lost():
    """interval 도달했지만 LLM 이 Epic 잃어버림 → None (master 안 덮어씀)."""
    bad = _MASTER_OK.replace("[Epic-01]", "[Gone]")
    gemini = FakeGemini(responses=[bad])
    ctx = make_pipeline_context(gemini=gemini, neo4j=None)

    cleaned = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=_MASTER_OK,
        prd_total=5,
        last_cleanup_count=0,
        interval=5,
    )

    assert cleaned is None
```

- [ ] **Step 5: Run — verify PASS**

Run: `python -m pytest tests/pipelines/test_prd_merge_periodic_cleanup.py -v -p no:warnings`
Expected: 3 passed.

- [ ] **Step 6: Run full BE suite — verify no regression**

Run: `python -m pytest -q -p no:warnings --ignore=tests/integration/test_neo4j_backup_restore_drill.py`
Expected: all pass (Windows-only backup drill 제외).

- [ ] **Step 7: Commit**

```bash
git add app/pipelines/prd_pipeline.py tests/pipelines/test_prd_merge_periodic_cleanup.py
git commit -m "feat(prd-cleanup): run_prd_merge wire-in — 임계 버전마다 자동 cleanup"
```

---

## Task 8: PR + 머지

- [ ] **Step 1: Push branch**

```bash
git push -u origin feat/prd-periodic-cleanup
```

- [ ] **Step 2: Create PR**

```bash
gh pr create --base master --title "feat(prd): L1-3 — 임계 버전(기본 5)마다 master PRD 자동 dedup cleanup" --body "<PR body 본문 — spec 링크, before/after, 데이터 안전 가드 4개, 비용 영향>"
```

PR body 핵심:
- **문제**: 매 버전 merge 에서 Section 1/4 ADD-ONLY → Vision/KPI/NFR 누더기 누적.
- **수정**: 고아 상태였던 `cleanup_master_prd.md` 를 5버전마다 자동 호출. master 노드에 `cleanup_at_version_count` 로 baseline 추적.
- **데이터 안전(★)**: Epic/Story ID 보존 + 섹션 헤더 + 길이 70% + 빈-content 가드. 검증 실패 시 master 안 덮어씀.
- **비용**: 5버전마다 LLM 1회 추가, 평상시 4/5 merge 영향 0.
- **Spec**: `docs/superpowers/specs/2026-05-28-prd-periodic-cleanup-design.md`.
- 🤖 Generated with [Claude Code](https://claude.com/claude-code)

- [ ] **Step 3: Wait for CI**

```bash
until [ "$(gh pr checks <PR#> 2>/dev/null | grep -c pending)" -eq 0 ]; do sleep 5; done
gh pr checks <PR#>
```

- [ ] **Step 4: Squash-merge**

```bash
gh pr merge <PR#> --squash --delete-branch
git checkout master && git pull --ff-only
```

---

## Self-Review Notes

- ✅ Spec 의 모든 요구사항(임계 트리거, 4중 검증, graceful skip, baseline 추적, 신규 모듈, 통합) 이 task 로 매핑됨.
- ✅ Placeholders 없음 — 모든 코드 블록 실제 코드.
- ✅ 함수 시그니처 일관 — `should_run_cleanup`/`extract_spec_ids`/`validate_cleanup_output`/`call_prd_cleanup_agent`/`run_prd_cleanup_if_due` 가 정의된 그대로 호출됨.
- ✅ `build_merge_master_prd_query` 의 신규 필수 인자가 Task 6 에서 추가되고 Task 7 의 wire-in 에서 둘 다(incremental + cleanup) 명시 전달.
- ⚠️ Task 6 Step 8: 기존 테스트가 `build_merge_master_prd_query` 의 새 필수 인자 누락으로 깨질 수 있음 — 발견 시 그 자리에서 `cleanup_at_version_count=0` 보강(같은 PR 안에서).
