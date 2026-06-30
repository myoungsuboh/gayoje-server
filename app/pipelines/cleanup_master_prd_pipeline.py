"""
Master PRD Cleanup Pipeline — dedupe 누더기된 master PRD.

[배경]
rebuild_prd / merge_prd LLM 이 V1→V2→...→Vn 누적 통합 시 같은 의미의 Product
Vision / Epic / Story 가 dedupe 없이 누적만 되는 케이스 (AI Agent 사고).
이 pipeline 은 누적된 master PRD markdown 을 입력으로 받아 dedupe + 정리.

[설계 결정]
- 입력: 현재 master PRD 의 full_markdown (Delta 가 아님)
- 출력: dedupe 된 새 markdown
- 신정보 추가 금지 — 입력 안의 정보만 정리
- Section 헤더 (### 1~4) 구조 보존
- 저장: 기존 update_master_prd_markdown 재활용 (빈값 거부 가드 + 사용자 편집 추적)

[원자성]
- LLM 호출 → 출력 검증 (빈/너무 짧음 raise) → 그 후 단일 cypher 로 update
- LLM 실패 시 update 안 일어남 → 원본 보존 (rebuild_master 와 동일 정책)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Set, Tuple

from app.pipelines.base import (
    PipelineContext,
    strip_code_blocks,
    strip_template_placeholders,
)
from app.pipelines.design_pipeline.prd import _extract_prd_story_ids
from app.pipelines.cps_pipeline import split_master_sections
from app.service import query_repository
from app.service.query_repository import OptimisticLockConflict

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# [2026-05-26] 정리된 PRD 가 너무 줄어들어 정상 PRD 의 형태조차 잃을 risk 차단.
# 입력의 5% 이하면 LLM 이 잘못 정리한 것으로 의심 — raise.
# 일반적으로 dedupe 는 30~70% 축소. 5% 이하는 비정상.
_MIN_OUTPUT_RATIO = 0.05
# 절대 최소 — 200 자 이하는 dedupe 결과가 아닌 LLM 환각.
_MIN_OUTPUT_BYTES = 200

# [2026-05-26 P1 — Section 2/3 reconcile] cleanup 결과 검증.
# Section 3 화면에서 참조하는 Story 가 Section 2 에 모두 정의되어야 함.
# AI Agent 케이스: Section 3 에 30+ Story 참조, Section 2 엔 Story 1개만 → SPACK
# underextract. cleanup 이 reconcile 못 하면 fall-through 보다 raise 가 안전.
# 다만 raise 너무 공격적이면 정상 케이스도 막힘 — Section 3 가 비어있거나 Section 2 만
# 풍부한 케이스는 통과시킴.

# 헤더 매칭 — 1, 2, 3, 4 번호로 시작하는 ### 헤더 식별.
_SECTION_HEADER_PATTERN = re.compile(r"^###\s+(\d+)[\.\s]", re.MULTILINE)


def _split_section_by_number(markdown: str) -> Dict[int, str]:
    """### N. 헤더 기준으로 markdown 을 섹션별로 분할.

    Returns:
        {1: "Section 1 본문", 2: "Section 2 본문", ...}
        헤더 없으면 빈 dict.
    """
    if not markdown:
        return {}
    matches = list(_SECTION_HEADER_PATTERN.finditer(markdown))
    if not matches:
        return {}
    sections: Dict[int, str] = {}
    for idx, m in enumerate(matches):
        section_num = int(m.group(1))
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
        sections[section_num] = markdown[start:end]
    return sections


def _extract_story_ids_from_section(section_text: str) -> Set[str]:
    """섹션 본문에서 Story-XX.Y 패턴 추출 (PRD story ID 추출 헬퍼 재사용)."""
    return _extract_prd_story_ids(section_text)


def _check_section_reconcile(cleaned_markdown: str) -> Tuple[bool, Dict[str, Any]]:
    """Section 2 (Epic Map) 의 Story 가 Section 3 (Screens) 참조를 cover 하는지 검증.

    Returns:
        (ok, info)
        ok=False 이면 Section 3 가 참조하는 Story 중 Section 2 에 없는 게 있음 → 위험.

    Section 3 가 비어있거나 Section 2 만 풍부한 경우는 ok=True (cover 할 게 없음).
    """
    sections = _split_section_by_number(cleaned_markdown)
    section_2 = sections.get(2, "")
    section_3 = sections.get(3, "")
    s2_stories = _extract_story_ids_from_section(section_2)
    s3_stories = _extract_story_ids_from_section(section_3)
    # Section 3 만 등장하는 Story = Section 2 에 정의 누락된 것.
    missing = s3_stories - s2_stories
    info = {
        "s2_story_count": len(s2_stories),
        "s3_story_count": len(s3_stories),
        "missing_in_s2": sorted(missing),
        "missing_count": len(missing),
    }
    return len(missing) == 0, info


@dataclass(frozen=True)
class CleanupMasterPrdInput:
    project_name: str
    user_email: str
    # [2026-05-26] dry_run=True 면 update 호출 X — cleaned markdown 만 반환.
    # FE 가 ResynthDiffModal 로 diff 보여주고 사용자 확인 후 별도 PATCH 호출.
    # default False — 옛 호출자 호환 (즉시 적용).
    dry_run: bool = False
    # [멀티테넌시] 팀 컨텍스트 (빈 문자열=개인) — master PRD read/write 스코프.
    team_id: str = ""


@dataclass
class CleanupMasterPrdResult:
    project_name: str
    before_size: int
    after_size: int
    reduction_pct: int
    master_prd_id: str
    # dry_run=True 일 때 cleaned markdown 본문 (FE 가 diff 비교용).
    # dry_run=False 면 빈 string (이미 master 에 적용됨).
    cleaned_markdown: str = ""
    # 원본 markdown — FE diff 비교용 (dry_run=True 일 때만 채움).
    original_markdown: str = ""
    dry_run: bool = False
    diagnostic: Dict[str, Any] = field(default_factory=dict)


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


async def call_cleanup_llm(ctx: PipelineContext, master_prd_markdown: str) -> str:
    """Stage: LLM dedupe.

    rebuild_prd 와 동일 temperature (0.1) — 결정성 우선. 정리는 창의성 불필요.
    """
    prompt = _render(
        _load_prompt("cleanup_master_prd.md"),
        master_prd_markdown=master_prd_markdown,
    )
    result = await ctx.gemini.generate(prompt, temperature=0.1)
    return strip_template_placeholders(strip_code_blocks(result.text)).strip()


async def run_cleanup_master_prd_pipeline(
    ctx: PipelineContext,
    payload: CleanupMasterPrdInput,
) -> CleanupMasterPrdResult:
    """
    Master PRD dedupe + 정리.

    1. master PRD fetch — 없으면 raise.
    2. LLM dedupe 호출.
    3. 출력 검증 — 빈/너무 짧으면 raise (원본 보존).
    4. update_master_prd_markdown 호출 — 기존 빈값 거부 가드 통과.
    5. 결과 반환 (before/after size, 축소율).
    """
    if not payload.project_name:
        raise ValueError("project_name 필수.")

    logger.info("cleanup_master_prd start: project=%s", payload.project_name)

    # Stage 1: master PRD fetch
    master = await query_repository.get_master_prd(payload.project_name, team_id=payload.team_id or "")
    if master is None or not (master.prd_content or "").strip():
        raise ValueError(
            f"'{payload.project_name}' 에 정리할 master PRD 가 없습니다. "
            "먼저 회의록을 등록해 PRD 를 생성하세요."
        )

    before = master.prd_content
    before_size = len(before)

    # Stage 2: LLM dedupe
    cleaned = await call_cleanup_llm(ctx, before)

    # Stage 3: 출력 검증
    after_size = len(cleaned)
    if after_size < _MIN_OUTPUT_BYTES:
        raise RuntimeError(
            f"PRD 정리 실패: LLM 출력이 너무 짧습니다 ({after_size} 자, 최소 "
            f"{_MIN_OUTPUT_BYTES} 자 필요). 잠시 후 다시 시도해주세요."
        )
    if before_size > 0 and after_size < before_size * _MIN_OUTPUT_RATIO:
        # 입력의 5% 이하 = 비정상 압축. 원본 보존.
        raise RuntimeError(
            f"PRD 정리 실패: LLM 출력이 원본의 "
            f"{round(after_size / before_size * 100)}% 로 비정상적으로 작아졌습니다. "
            "의미 손실 위험이 있어 적용을 거부했습니다. 다시 시도하거나 PRD 탭에서 "
            "직접 정리해주세요."
        )

    # [2026-05-26 P1] Section 2 ↔ Section 3 reconcile 검증.
    # cleanup prompt 에 강제했지만 LLM 이 무시할 수 있음 — pipeline 단에서도 확인.
    # 입력 PRD 가 같은 mismatch 를 갖고 있어도 검증 — cleanup 의 책임은 mismatch
    # 를 줄이는 것이지 유지가 아님.
    reconcile_ok, reconcile_info = _check_section_reconcile(cleaned)
    if not reconcile_ok:
        # 입력 PRD 도 같은 mismatch 가 있었다면 cleanup 책임은 아님 → warning 만 남기고 통과.
        # 입력엔 없던 mismatch 가 cleanup 후 생겼으면 over-dedupe 의심 → raise.
        before_reconcile_ok, before_info = _check_section_reconcile(before)
        before_missing = set(before_info.get("missing_in_s2", []))
        after_missing = set(reconcile_info.get("missing_in_s2", []))
        # cleanup 이 새로 만든 mismatch = 입력에 없던 missing.
        newly_missing = after_missing - before_missing
        if newly_missing:
            raise RuntimeError(
                f"PRD 정리 실패: cleanup 후 Section 2 (Epic Map) 에서 누락된 "
                f"Story {sorted(newly_missing)} 가 Section 3 (Screen Architecture) 엔 "
                "여전히 참조됨 — cleanup 이 Epic 을 과도하게 dedupe 한 의심. "
                "의미 손실 위험으로 적용 거부. 다시 시도해주세요."
            )
        # 입력 PRD 부터 mismatch 있는 케이스 — warning 만 기록 후 통과.
        # (cleanup 이 reconcile 까지 해주면 좋지만 강제는 안 함 — 별도 PR 에서 강화).
        logger.warning(
            "cleanup_master_prd reconcile mismatch (이미 입력에 존재): "
            "project=%s s2_stories=%d s3_stories=%d missing_in_s2=%s",
            payload.project_name,
            reconcile_info["s2_story_count"],
            reconcile_info["s3_story_count"],
            reconcile_info["missing_in_s2"][:10],
        )

    reduction_pct = round((1 - after_size / before_size) * 100) if before_size > 0 else 0

    # Stage 4: update master PRD — dry_run=True 면 skip (FE 가 diff 확인 후 별도 PATCH).
    if payload.dry_run:
        logger.info(
            "cleanup_master_prd dry-run: project=%s before=%d after=%d reduction=%d%%",
            payload.project_name, before_size, after_size, reduction_pct,
        )
        return CleanupMasterPrdResult(
            project_name=payload.project_name,
            before_size=before_size,
            after_size=after_size,
            reduction_pct=reduction_pct,
            master_prd_id=master.master_prd_id,
            cleaned_markdown=cleaned,
            original_markdown=before,
            dry_run=True,
            diagnostic={
                "user_email": payload.user_email,
                "reconcile": reconcile_info,
            },
        )

    # 실 적용 path — 기존 update_master_prd_markdown 가드 재활용
    # client_updated_at=None 으로 optimistic locking 건너뜀 (관리자 정리이므로)
    # [2026-06-05] mark_design_stale=False — 정리는 markdown 압축일 뿐 의미 변경이
    # 아니므로 design 을 stale 로 재마킹하지 않는다(재생성 직후 배너 부활 방지).
    update_result = await query_repository.update_master_prd_markdown(
        project_name=payload.project_name,
        content=cleaned,
        client_updated_at=None,
        team_id=payload.team_id or "",
        mark_design_stale=False,
    )
    if update_result is None:
        # 이미 위에서 master 존재 확인했으니 None 은 드물지만 안전망
        raise RuntimeError(
            f"PRD 업데이트 실패: master 노드를 찾을 수 없습니다 "
            f"(project={payload.project_name}). 관리자에게 문의."
        )

    logger.info(
        "cleanup_master_prd applied: project=%s before=%d after=%d reduction=%d%%",
        payload.project_name, before_size, after_size, reduction_pct,
    )

    return CleanupMasterPrdResult(
        project_name=payload.project_name,
        before_size=before_size,
        after_size=after_size,
        reduction_pct=reduction_pct,
        master_prd_id=update_result["master_id"],
        cleaned_markdown="",  # 이미 적용됨 — FE 가 PRD refetch 로 확인
        original_markdown="",
        dry_run=False,
        diagnostic={
            "user_email": payload.user_email,
            "reconcile": reconcile_info,
        },
    )
