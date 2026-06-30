"""
cleanup_master_prd_pipeline 회귀 가드.

[검증 범위]
1. 정상 cleanup — LLM 의 dedupe 된 markdown 으로 master PRD update
2. master PRD 없음 → ValueError (원본 보존)
3. LLM 빈/너무 짧은 응답 → raise (master 안 건드림)
4. 비정상 압축 (입력의 5% 이하) → raise (의미 손실 차단)
5. update_master_prd_markdown 의 빈값 가드와 합쳐 다중 방어

[핵심 정책]
LLM 실패 시 update 안 일어남 → 원본 보존. atomic 보장은 LLM 호출 후 단일
cypher 로 update — 트랜잭션 안 들어가는 path 차단.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.cleanup_master_prd_pipeline import (
    CleanupMasterPrdInput,
    run_cleanup_master_prd_pipeline,
)
from app.service.query_repository import PrdMaster
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio


# ─── 공통 fixture ─────────────────────────────────────────────


_DIRTY_PRD = """## 🗺️ Master PRD 조감도

### 1. Product Overview (통합 제품 비전)
- **통합 비전**: AI Agent 는 사용자 자동화 플랫폼
- **Product Vision**: AI Agent V2는 사용자 자동화
- **Product Vision**: AI Agent V7은 사용자 자동화 (CPS 명세서 부재로 상세 비전은 추가 정의 필요)
- **Product Vision**: AI Agent 는 사용자 자동화
- **Product Vision**: AI Agent V10 의 사용자 자동화

### 2. Epic & User Story Map (기능 계층도)
#### 📦 [Epic-01] 사용자 인증
- `[Story-01.1]` 로그인 ➡️ *(구현 화면: 로그인 화면)*
#### 📦 [Epic-02] 사용자 로그인 인증
- `[Story-02.1]` 로그인 처리

### 3. Screen Architecture
#### 🖥️ [Screen: 로그인 화면]
- **포함된 기능**: `[Story-01.1]` 로그인

### 4. Global Non-Functional Requirements
- **Performance**: 응답 500ms
""" * 1  # ~600자

_CLEAN_PRD = """## 🗺️ Master PRD 조감도 (정리됨)

### 1. Product Overview (통합 제품 비전)
- **통합 비전**: AI Agent 는 사용자 자동화 플랫폼
- **핵심 타겟**: 모든 사용자

### 2. Epic & User Story Map (기능 계층도)
#### 📦 [Epic-01] 사용자 인증
- `[Story-01.1]` 로그인 ➡️ *(구현 화면: 로그인 화면)*

### 3. Screen Architecture
#### 🖥️ [Screen: 로그인 화면]
- **포함된 기능**: `[Story-01.1]` 로그인

### 4. Global Non-Functional Requirements
- **Performance**: 응답 500ms
"""  # ~350자


class _FakePrdRepo:
    """query_repository 의 두 함수 (get_master_prd / update_master_prd_markdown) mock.

    monkeypatch 로 module level 함수 교체. side_effect 가능.
    """

    def __init__(
        self,
        master_content: Optional[str] = _DIRTY_PRD,
        update_returns: Optional[Dict[str, Any]] = None,
    ):
        if master_content is None:
            self.master = None
        else:
            self.master = PrdMaster(
                master_prd_id="doc_prd_master_test",
                prd_content=master_content,
                last_updated=1700000000000,
                markdown_stale=False,
                related_master_cps_id="doc_cps_master_test",
                absorbed_prd_ids=[],
            )
        self.update_returns = update_returns or {
            "master_id": "doc_prd_master_test", "last_updated": 1700000001000,
        }
        self.update_calls: List[Dict[str, Any]] = []

    async def get_master_prd(self, project_name: str, team_id: str = ""):
        return self.master

    async def update_master_prd_markdown(
        self, project_name: str, content: str,
        *, client_updated_at: Optional[int] = None, team_id: str = "",
        mark_design_stale: bool = True,
    ):
        self.update_calls.append({
            "project_name": project_name, "content": content,
            "client_updated_at": client_updated_at,
            "mark_design_stale": mark_design_stale,
        })
        return self.update_returns


@pytest.fixture
def fake_repo(monkeypatch):
    def _setup(**kwargs):
        repo = _FakePrdRepo(**kwargs)
        monkeypatch.setattr(
            "app.pipelines.cleanup_master_prd_pipeline.query_repository.get_master_prd",
            repo.get_master_prd,
        )
        monkeypatch.setattr(
            "app.pipelines.cleanup_master_prd_pipeline.query_repository.update_master_prd_markdown",
            repo.update_master_prd_markdown,
        )
        return repo
    return _setup


def _ctx(gemini_responses: List[str]) -> PipelineContext:
    gemini = FakeGemini(responses=gemini_responses)
    neo4j = FakeNeo4j()
    return PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="cleanup-test")


# ─── 정상 흐름 ──────────────────────────────────────────────


async def test_normal_cleanup_returns_size_diff(fake_repo):
    repo = fake_repo(master_content=_DIRTY_PRD)
    ctx = _ctx([_CLEAN_PRD])

    result = await run_cleanup_master_prd_pipeline(
        ctx, CleanupMasterPrdInput(project_name="proj_x", user_email="u@x"),
    )

    assert result.project_name == "proj_x"
    assert result.before_size == len(_DIRTY_PRD)
    assert result.after_size == len(_CLEAN_PRD.strip())
    assert result.reduction_pct > 0
    assert result.master_prd_id == "doc_prd_master_test"

    # update 가 정리된 content 로 호출됐는지
    assert len(repo.update_calls) == 1
    call = repo.update_calls[0]
    assert call["project_name"] == "proj_x"
    assert call["content"] == _CLEAN_PRD.strip()
    # optimistic locking skip (관리자 정리)
    assert call["client_updated_at"] is None
    # [2026-06-05] cleanup 은 design 을 stale 로 재마킹하면 안 됨 (배너 부활 방지).
    assert call["mark_design_stale"] is False


async def test_normal_cleanup_strips_code_blocks_and_placeholders(fake_repo):
    """LLM 출력에 ```markdown ... ``` wrap 이 있어도 strip 됨."""
    repo = fake_repo(master_content=_DIRTY_PRD)
    wrapped = "```markdown\n" + _CLEAN_PRD + "\n```"
    ctx = _ctx([wrapped])

    result = await run_cleanup_master_prd_pipeline(
        ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
    )

    saved = repo.update_calls[0]["content"]
    assert "```" not in saved
    assert saved == _CLEAN_PRD.strip()
    assert result.after_size == len(_CLEAN_PRD.strip())


# ─── 가드: master 없음 ──────────────────────────────────────


async def test_no_master_raises_value_error(fake_repo):
    """master PRD 가 없으면 LLM 호출 없이 즉시 raise."""
    repo = fake_repo(master_content=None)
    ctx = _ctx([])  # LLM 호출 안 됨

    with pytest.raises(ValueError, match="master PRD 가 없습니다"):
        await run_cleanup_master_prd_pipeline(
            ctx, CleanupMasterPrdInput(project_name="ghost", user_email="u@x"),
        )

    # LLM 호출 안 됨
    assert ctx.gemini.calls == []
    # update 호출 안 됨 — 원본 보존
    assert repo.update_calls == []


async def test_empty_master_content_raises(fake_repo):
    """master 노드는 있지만 full_markdown 이 빈 string → raise."""
    repo = fake_repo(master_content="   \n\n   ")
    ctx = _ctx([])

    with pytest.raises(ValueError, match="master PRD 가 없습니다"):
        await run_cleanup_master_prd_pipeline(
            ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
        )

    assert repo.update_calls == []


# ─── 가드: LLM 빈/짧은 응답 ─────────────────────────────────


async def test_empty_llm_response_raises_and_preserves_original(fake_repo):
    """LLM 이 빈 응답 → raise. master 안 건드림."""
    repo = fake_repo(master_content=_DIRTY_PRD)
    ctx = _ctx([""])

    with pytest.raises(RuntimeError, match="너무 짧"):
        await run_cleanup_master_prd_pipeline(
            ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
        )

    # update 호출 안 됨 — 원본 보존
    assert repo.update_calls == []


async def test_too_short_llm_response_raises(fake_repo):
    """200자 미만 → raise. wiping 차단."""
    repo = fake_repo(master_content=_DIRTY_PRD)
    ctx = _ctx(["짧은 응답"])  # < 200 bytes

    with pytest.raises(RuntimeError, match="너무 짧"):
        await run_cleanup_master_prd_pipeline(
            ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
        )

    assert repo.update_calls == []


# ─── 가드: 비정상 압축 ───────────────────────────────────────


async def test_abnormal_compression_below_5pct_raises(fake_repo):
    """입력의 5% 이하로 줄어든 출력 → raise (의미 손실 risk)."""
    # 큰 입력 (~50KB) — 그래야 5% 가드가 명확히 발동
    big_dirty = _DIRTY_PRD * 80  # ~50KB
    # _MIN_OUTPUT_BYTES (200) 통과하되 입력의 < 5% 인 응답 (~600자, 입력의 ~1%)
    tiny_output = (
        "## 🗺️ Master PRD 정리됨\n\n"
        "### 1. Product Overview\n- **통합 비전**: AI 자동화\n- **핵심 타겟**: 사용자\n\n"
        "### 2. Epic & User Story Map\n#### 📦 [Epic-01] 사용자 인증\n- `[Story-01.1]` 로그인\n\n"
        "### 3. Screen Architecture\n#### 🖥️ [Screen: 로그인 화면]\n- 로그인 기능\n\n"
        "### 4. Global Non-Functional Requirements\n- **Performance**: 응답 500ms 이내\n- **Security**: OAuth\n"
    )
    assert len(tiny_output) > 200, f"실제 길이 {len(tiny_output)}"
    assert len(tiny_output) < len(big_dirty) * 0.05, \
        f"tiny={len(tiny_output)}, 5%={len(big_dirty) * 0.05}"

    repo = fake_repo(master_content=big_dirty)
    ctx = _ctx([tiny_output])

    with pytest.raises(RuntimeError, match="비정상적으로 작아"):
        await run_cleanup_master_prd_pipeline(
            ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
        )

    # update 호출 안 됨 — 원본 보존 (의미 손실 차단)
    assert repo.update_calls == []


async def test_normal_compression_30pct_is_allowed(fake_repo):
    """30% 축소는 정상 (5% 이상이면 통과)."""
    big_dirty = _DIRTY_PRD * 10  # ~6KB
    cleaned = _CLEAN_PRD * 2  # ~700자 = 12% 정도 (5% 이상)
    assert len(cleaned) > len(big_dirty) * 0.05

    repo = fake_repo(master_content=big_dirty)
    ctx = _ctx([cleaned])

    result = await run_cleanup_master_prd_pipeline(
        ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
    )

    assert result.after_size > 0
    assert len(repo.update_calls) == 1


# ─── 가드: update 단계 실패 ─────────────────────────────────


async def test_update_returns_none_raises(fake_repo):
    """update_master_prd_markdown 이 None 반환 → 안전망 raise."""
    repo = fake_repo(master_content=_DIRTY_PRD, update_returns=None)
    # 단, None 을 반환하려면 _FakePrdRepo 가 직접 그렇게.
    # 아래 trick: update_returns 를 sentinel 로 두고 함수에서 None 반환.
    # 간단히 monkeypatch 로 update 만 None 반환하게 재설정.

    async def _update_none(*args, **kwargs):
        return None
    import app.pipelines.cleanup_master_prd_pipeline as mod
    mod.query_repository.update_master_prd_markdown = _update_none

    ctx = _ctx([_CLEAN_PRD])

    with pytest.raises(RuntimeError, match="master 노드를 찾을 수 없"):
        await run_cleanup_master_prd_pipeline(
            ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
        )


# ─── prompt 정상 전달 ────────────────────────────────────────


# ─── dry_run ────────────────────────────────────────────────


async def test_dry_run_does_not_update_master(fake_repo):
    """dry_run=True 면 LLM 호출은 하되 update_master_prd_markdown 안 부름.

    FE 가 cleaned markdown 받아 diff 모달 + 사용자 확인 후 별도 PATCH 호출.
    """
    repo = fake_repo(master_content=_DIRTY_PRD)
    ctx = _ctx([_CLEAN_PRD])

    result = await run_cleanup_master_prd_pipeline(
        ctx, CleanupMasterPrdInput(
            project_name="p", user_email="u@x", dry_run=True,
        ),
    )

    # LLM 은 호출됨 (cleanup 수행)
    assert len(ctx.gemini.calls) == 1
    # update 는 호출 안 됨 (dry_run 정책)
    assert repo.update_calls == []
    # 결과에 cleaned + original markdown 둘 다 (FE diff 비교용)
    assert result.dry_run is True
    assert result.cleaned_markdown == _CLEAN_PRD.strip()
    assert result.original_markdown == _DIRTY_PRD
    assert result.reduction_pct > 0


async def test_apply_mode_excludes_markdown_from_response(fake_repo):
    """dry_run=False (default) 면 cleaned_markdown 빈 string (이미 적용됨).

    FE 가 PRD refetch 로 변경 확인 — 응답에 본문 중복 노출 안 함 (payload size 절약).
    """
    repo = fake_repo(master_content=_DIRTY_PRD)
    ctx = _ctx([_CLEAN_PRD])

    result = await run_cleanup_master_prd_pipeline(
        ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
    )

    # update 호출됨
    assert len(repo.update_calls) == 1
    # 응답에 markdown 본문 비어있음
    assert result.dry_run is False
    assert result.cleaned_markdown == ""
    assert result.original_markdown == ""
    # 다만 size 정보는 있음 (사용자에게 결과 토스트 표시)
    assert result.before_size == len(_DIRTY_PRD)
    assert result.reduction_pct > 0


async def test_dry_run_still_applies_guards(fake_repo):
    """dry_run 도 가드 (빈 응답 / 짧은 응답 / 비정상 압축) 동일 적용."""
    repo = fake_repo(master_content=_DIRTY_PRD)
    ctx = _ctx([""])  # 빈 LLM 응답

    with pytest.raises(RuntimeError, match="너무 짧"):
        await run_cleanup_master_prd_pipeline(
            ctx, CleanupMasterPrdInput(
                project_name="p", user_email="u@x", dry_run=True,
            ),
        )

    # dry_run 이라도 빈 응답이면 raise. update 안 됨 (당연).
    assert repo.update_calls == []


# ─── 가드: Section 2/3 reconcile ─────────────────────────────


_DIRTY_PRD_WITH_FULL_RECONCILE = """## 🗺️ Master PRD 조감도

### 1. Product Overview
- **통합 비전**: AI Agent 자동화 플랫폼
- **Product Vision**: AI Agent V2 자동화
- **Product Vision**: AI Agent V7 자동화

### 2. Epic & User Story Map
#### 📦 [Epic-01] 사용자 인증
- `[Story-01.1]` 로그인
- `[Story-01.2]` 로그아웃
#### 📦 [Epic-02] 작업 관리
- `[Story-02.1]` 작업 생성

### 3. Screen Architecture
#### 🖥️ [Screen: 로그인]
- **포함된 기능**: `[Story-01.1]` 로그인, `[Story-01.2]` 로그아웃
#### 🖥️ [Screen: 대시보드]
- **포함된 기능**: `[Story-02.1]` 작업 생성

### 4. Global Non-Functional Requirements
- **Performance**: 응답 500ms
"""


async def test_cleanup_raises_when_cleanup_creates_new_section_2_3_mismatch(fake_repo):
    """
    cleanup 이 Epic 을 과도하게 dedupe 해서 Section 3 가 참조하는 Story 가
    Section 2 에서 사라진 경우 → raise (over-dedupe 차단).

    입력: Section 2 에 Epic-01/Epic-02 둘 다, Section 3 도 둘 다 참조 (reconcile OK)
    LLM 출력: Section 2 에서 Epic-02 사라짐, Section 3 는 Story-02.1 여전히 참조
    → newly_missing = {Story-02.1} 발생 → raise.
    """
    bad_cleanup = """## 🗺️ Master PRD 조감도 (정리됨)

### 1. Product Overview
- **통합 비전**: AI Agent 자동화 플랫폼
- **핵심 타겟**: 사용자

### 2. Epic & User Story Map
#### 📦 [Epic-01] 사용자 인증
- `[Story-01.1]` 로그인
- `[Story-01.2]` 로그아웃

### 3. Screen Architecture
#### 🖥️ [Screen: 로그인]
- **포함된 기능**: `[Story-01.1]` 로그인, `[Story-01.2]` 로그아웃
#### 🖥️ [Screen: 대시보드]
- **포함된 기능**: `[Story-02.1]` 작업 생성

### 4. Global Non-Functional Requirements
- **Performance**: 응답 500ms 이내
- **Security**: OAuth 2.0
- **Availability**: 99.9%
"""
    repo = fake_repo(master_content=_DIRTY_PRD_WITH_FULL_RECONCILE)
    ctx = _ctx([bad_cleanup])

    with pytest.raises(RuntimeError, match="Story.*Section 3"):
        await run_cleanup_master_prd_pipeline(
            ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
        )
    # update 안 일어남 — 원본 보존
    assert repo.update_calls == []


async def test_cleanup_passes_when_input_already_has_mismatch(fake_repo):
    """
    입력 PRD 부터 Section 2/3 mismatch 가 있는 케이스 (AI Agent 실데이터 케이스):
    cleanup 이 mismatch 를 줄이지 못해도 raise 하지 않고 warning 만 (cleanup 책임은
    mismatch 를 만드는 것이 아니라 유지하는 것이라 raise 너무 공격적).
    """
    dirty_with_mismatch = """## 🗺️ Master PRD 조감도

### 1. Product Overview
- **통합 비전**: AI Agent
- **Product Vision**: AI Agent V2
- **Product Vision**: AI Agent V7

### 2. Epic & User Story Map
#### 📦 [Epic-01] 사용자 인증
- `[Story-01.1]` 로그인

### 3. Screen Architecture
#### 🖥️ [Screen: 로그인]
- **포함된 기능**: `[Story-01.1]` 로그인
#### 🖥️ [Screen: 대시보드]
- **포함된 기능**: `[Story-02.1]` 작업 생성

### 4. Global Non-Functional Requirements
- **Performance**: 응답 500ms
"""
    # cleanup 출력도 동일한 mismatch 유지 (LLM 이 reconcile 못 함)
    cleaned_keeping_mismatch = """## 🗺️ Master PRD 조감도 (정리됨)

### 1. Product Overview
- **통합 비전**: AI Agent 자동화 플랫폼
- **핵심 타겟**: 사용자

### 2. Epic & User Story Map
#### 📦 [Epic-01] 사용자 인증
- `[Story-01.1]` 로그인

### 3. Screen Architecture
#### 🖥️ [Screen: 로그인]
- **포함된 기능**: `[Story-01.1]` 로그인
#### 🖥️ [Screen: 대시보드]
- **포함된 기능**: `[Story-02.1]` 작업 생성

### 4. Global Non-Functional Requirements
- **Performance**: 응답 500ms 이내
- **Security**: OAuth 2.0
"""
    repo = fake_repo(master_content=dirty_with_mismatch)
    ctx = _ctx([cleaned_keeping_mismatch])

    # raise 하지 않음 — 입력에 이미 같은 mismatch 있었음
    result = await run_cleanup_master_prd_pipeline(
        ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
    )

    assert result.project_name == "p"
    # diagnostic 에 reconcile 정보 포함 (운영 visibility)
    assert "reconcile" in result.diagnostic
    rec = result.diagnostic["reconcile"]
    assert rec["missing_count"] >= 1  # Story-02.1 여전히 missing
    assert "Story-02.1" in rec["missing_in_s2"]
    # update 진행됨 — over-dedupe 아님
    assert len(repo.update_calls) == 1


async def test_cleanup_passes_when_reconcile_fully_resolved(fake_repo):
    """
    cleanup 이 Section 3 의 Story 참조를 모두 Section 2 에 정의시킨 케이스 (이상적):
    raise 하지 않고 정상 통과 + diagnostic 에 missing_count=0.
    """
    dirty_with_mismatch = """## 🗺️ Master PRD 조감도

### 1. Product Overview
- **통합 비전**: AI Agent
- **Product Vision**: AI Agent V2

### 2. Epic & User Story Map
#### 📦 [Epic-01] 사용자 인증
- `[Story-01.1]` 로그인

### 3. Screen Architecture
#### 🖥️ [Screen: 로그인]
- **포함된 기능**: `[Story-01.1]` 로그인
#### 🖥️ [Screen: 대시보드]
- **포함된 기능**: `[Story-02.1]` 작업 생성

### 4. Global Non-Functional Requirements
- **Performance**: 응답 500ms
"""
    # cleanup 출력 — Section 2 에 Story-02.1 추가 (reconcile 완료)
    cleaned_reconciled = """## 🗺️ Master PRD 조감도 (정리됨)

### 1. Product Overview
- **통합 비전**: AI Agent 자동화 플랫폼
- **핵심 타겟**: 사용자

### 2. Epic & User Story Map
#### 📦 [Epic-01] 사용자 인증
- `[Story-01.1]` 로그인
#### 📦 [Epic-02] 작업 관리
- `[Story-02.1]` 작업 생성

### 3. Screen Architecture
#### 🖥️ [Screen: 로그인]
- **포함된 기능**: `[Story-01.1]` 로그인
#### 🖥️ [Screen: 대시보드]
- **포함된 기능**: `[Story-02.1]` 작업 생성

### 4. Global Non-Functional Requirements
- **Performance**: 응답 500ms 이내
- **Security**: OAuth 2.0
"""
    repo = fake_repo(master_content=dirty_with_mismatch)
    ctx = _ctx([cleaned_reconciled])

    result = await run_cleanup_master_prd_pipeline(
        ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
    )

    assert result.diagnostic["reconcile"]["missing_count"] == 0
    assert len(repo.update_calls) == 1


# ─── prompt 정상 전달 ─────────────────────────────────────────


async def test_master_markdown_passed_to_prompt(fake_repo):
    """LLM 호출 시 master markdown 이 prompt 에 정확히 포함."""
    repo = fake_repo(master_content=_DIRTY_PRD)
    ctx = _ctx([_CLEAN_PRD])

    await run_cleanup_master_prd_pipeline(
        ctx, CleanupMasterPrdInput(project_name="p", user_email="u@x"),
    )

    # FakeGemini.calls — list of dict
    assert len(ctx.gemini.calls) == 1
    prompt = ctx.gemini.calls[0]["prompt"]
    assert "Product Vision" in prompt  # _DIRTY_PRD 내용
    # cleanup prompt 의 핵심 instruction 포함
    assert "DEDUPLICATION" in prompt or "dedupe" in prompt.lower()
    # temperature 0.1 (결정성 우선)
    assert ctx.gemini.calls[0]["temperature"] == 0.1
