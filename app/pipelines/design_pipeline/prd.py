from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from app.pipelines.base import PipelineContext
from app.pipelines.cps_pipeline import _SECTION_HEADER_RE, split_master_sections

logger = logging.getLogger(__name__)


# ─── Stage 1: Get PRD ─────────────────────────────────────────────────────────


_GET_PRD_QUERY = """\
MATCH (m:PRD_Document {type: 'Master', is_latest: true})
WHERE m.project = $project

// 1. 이 마스터 기획의 근거가 되는 마스터 요구사항(CPS) 연결 확인
OPTIONAL MATCH (m)-[:BASED_ON]->(cps_m:CPS_Document)

// 2. 증분 병합을 통해 이 마스터에 통합된 개별 PRD 문서들의 ID 추적 (계보)
OPTIONAL MATCH (m)-[:SYNTHESIZED_FROM]->(source:PRD_Document)

RETURN
    m.id AS master_prd_id,
    m.full_markdown AS prd_content,
    m.updated_at AS last_updated,
    cps_m.id AS related_master_cps_id,
    collect(DISTINCT source.id) AS absorbed_prd_ids
"""


async def fetch_master_prd(ctx: PipelineContext, project_name: str) -> Dict[str, Any]:
    """Stage: `ExecuteQuery Get PRD`."""
    # [멀티테넌시] design 은 ctx.team_id 로 스코프 (개인=이름 그대로).
    from app.core.project_scope import scoped_project
    project_name = scoped_project(project_name, ctx.team_id)
    records = await ctx.neo4j.run_cypher(_GET_PRD_QUERY, {"project": project_name})
    if not records:
        raise ValueError(
            f"[Design] 마스터 PRD 없음: project={project_name}. postMeeting 먼저 실행 필요."
        )
    return records[0]


# ─── Stage 2: PRD Section Extractor ───────────────────────────────────────────────────────────────


def _normalize_header(s: str) -> str:
    """헤더 정규화: 소문자 + 영숫자/한글만 남김. fuzzy 매칭 안정성 ↑."""
    if not s:
        return ""
    # 숫자/마침표/하이픈/언더스코어/공백 제거 → 핵심 단어만
    return re.sub(r"[^a-zA-Z가-힙]+", "", s).lower()


def extract_prd_sections(
    prd_content: str,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """
    Stage: `PRD Section Extractor`.

    PRD 마크다운을 3개 Agent 입력으로 분할:
      - spack_input  = Overview + EpicMap + Screens + NFR
      - ddd_input    = Overview + EpicMap
      - arch_input   = Overview + EpicMap + NFR + Screens

    [강화 (2026 update)]
    - 헤더 정규화 (대소문자/번호/구분자 무시) 후 키워드 매칭
    - fallback 발생 (섹션 못 찾아서 전체 PRD 통째로 입력) 시 diagnostic 에 명시
    - 각 섹션의 출체 헤더를 diagnostic 에 기록 → 운영 디버깅 용이

    [B0 — 2026-05 lineage 작업]
    - arch_input 에 epic_map 추가. Architecture Service / Database 노드의 lineage
      (related_story_ids + evidence_quote) 를 채우려면 Service Agent 도 Story 원문에
      접근해야 함. 이전에 Screens 만으로 충분했지만 Service 가 Story 와 어떻게
      연결되는지 추적 불가능했음.

    [2026-05-26 — SPACK underextraction fix]
    spack_input 에 Screens 추가. 배경:
      - PRD Section 2 (Epic Map) 가 빈약하고 Section 3 (Screens) 만 풍부한 케이스에서
        SPACK 이 Story 1.1 류 ~1개 Epic 만 보고 API 1~2개로 underextract.
      - 화면별 `[Story-XX.Y]` 참조는 Story → 화면 → API 도출의 핵심 근거.
      - arch_input 은 이미 Screens 포함. SPACK 도 같은 정보 봐야 추출률 정상화.
    DDD 는 도메인 모델 추출이라 화면 의존성 적어 그대로 둠.
    """
    if not prd_content or not prd_content.strip():
        raise ValueError("[PRD Section Extractor] 마스터 PRD 가 비어있음")

    section_map, _ = split_master_sections(prd_content)

    # 정규화된 헤더 → 원본 키 매핑 (한 번만 계산)
    normalized_index: Dict[str, str] = {}
    for k in section_map:
        if k == "__header__":
            continue
        normalized_index[_normalize_header(k)] = k

    def find_section_with_source(*keywords: str) -> Tuple[str, Optional[str]]:
        """매칭된 (content, source_header) 반환. 없으면 ("", None)."""
        for kw in keywords:
            nk = _normalize_header(kw)
            if not nk:
                continue
            # 1) 정확 일치 (정규화 후)
            if nk in normalized_index:
                src = normalized_index[nk]
                return section_map[src], src
            # 2) substring 매칭 (정규화된 헤더 안에 키워드가 들어있는지)
            for norm_k, orig_k in normalized_index.items():
                if nk in norm_k:
                    return section_map[orig_k], orig_k
        return "", None

    overview, ov_src = find_section_with_source(
        "Product Overview", "Overview", "제품 비전", "통합 비전", "제품 소개", "비전",
    )
    epic_map, ep_src = find_section_with_source(
        "Epic & User Story Map", "Epic", "User Story Map", "기능 계층", "Epic Map", "Story Map",
    )
    screens, sc_src = find_section_with_source(
        "Screen Architecture", "Screen", "화면 구성", "화면", "UI Architecture",
    )
    nfr, nfr_src = find_section_with_source(
        "Global Non-Functional", "Non-Functional", "NFR", "공통 제약", "비기능", "Non Functional",
    )

    # fallback 시 어떤 섹션이 전체 PRD 로 떨어졌는지 기록
    fallbacks: List[str] = []

    def safe_join(label: str, *parts_with_src: Tuple[str, Optional[str]]) -> str:
        joined = "\n\n".join(p for p, _ in parts_with_src if p and p.strip()).strip()
        if joined:
            return joined
        # 모든 part 가 비었음 → 전체 PRD fallback
        fallbacks.append(label)
        return prd_content

    # [2026-05-26] spack_input 에 screens 추가 — 화면의 Story 참조도 API 도출 근거.
    # Epic Map 만 보면 PRD V1 의 단일 Epic 케이스에서 API 1~2개로 underextract.
    spack_input = safe_join(
        "spack",
        (overview, ov_src), (epic_map, ep_src), (screens, sc_src), (nfr, nfr_src),
    )
    ddd_input = safe_join("ddd", (overview, ov_src), (epic_map, ep_src))
    # [B0] arch_input 에 epic_map 추가 — Service lineage (related_story_ids) 위해.
    arch_input = safe_join(
        "arch",
        (overview, ov_src), (epic_map, ep_src), (nfr, nfr_src), (screens, sc_src),
    )

    sections_found = [k for k in section_map if k != "__header__"]

    return (
        {
            "spack_input": spack_input,
            "ddd_input": ddd_input,
            "arch_input": arch_input,
        },
        {
            "full_size": len(prd_content),
            "spack_size": len(spack_input),
            "ddd_size": len(ddd_input),
            "arch_size": len(arch_input),
            "sections_found": sections_found,
            "section_count": len(sections_found),
            "overview_found": bool(overview),
            "epic_map_found": bool(epic_map),
            "screens_found": bool(screens),
            "nfr_found": bool(nfr),
            "overview_source": ov_src,
            "epic_map_source": ep_src,
            "screens_source": sc_src,
            "nfr_source": nfr_src,
            "fallback_to_full_prd": fallbacks,  # 어떤 입력이 전체 PRD 로 떨어졌는지
        },
    )


# ─── PRD Story IDs 추출 (A — 2026-05) ───────────────────────────────────────────────────────
#
# PRD markdown 에서 Story 표기를 모두 찾아 정규화된 set 으로 반환.
# normalize_spack / ddd / architecture 의 valid_story_ids 인자로 전달 →
# LLM 이 만든 lineage 의 related_stories 가 PRD 에 실존하는 것만 유지.

_PRD_STORY_TOKEN_RE = re.compile(r"Story[-\s]?(\d+)[.\-_](\d+)", re.IGNORECASE)


# ─── Dirty PRD 감지 (2026-05-26 — design pipeline auto-cleanup trigger) ───
#
# 누적 merge 로 dirty 해진 master PRD 를 자동으로 cleanup 호출하기 위한 결정적 감지.
# trigger 가 너무 민감하면 정상 PRD 도 cleanup 발동 → LLM 비용 누수. 너무 보수적이면
# AI Agent 케이스 같은 누더기 PRD 는 못 잡음.
#
# 결정적 trigger (각각 단독으로 dirty 판정):
#   a) Product Vision 키워드 ≥ 5 회 — 누적 merge 의 가장 명확한 신호.
#      정상 PRD 는 통합 비전 1개만 등장.
#   b) Section 2 (Epic Map) 에 정의 안 된 Story 를 Section 3 (Screens) 가 ≥ 3 개
#      참조 — PRD_S2_S3_STORY_MISMATCH 룰의 임계점.
#      1~2 개는 PRD 작성 중 일시적 inconsistency 일 수 있어 그냥 두지만 3+ 는 명백히
#      디자인 단계에서 underextract 유발.
#
# 임계값 근거: AI Agent 실데이터 분석에서 Product Vision 26개 + S3 referenced
# Story 30+ 개 vs S2 정의 1개 → 임계 5/3 은 안전 마진.

_PRODUCT_VISION_KEYWORD_RE = re.compile(r"Product\s*Vision|통합\s*비전", re.IGNORECASE)
_DIRTY_PRODUCT_VISION_THRESHOLD = 5
_DIRTY_S2_S3_MISSING_THRESHOLD = 3

# Section 2/3 reconcile 검사용 헤더.
_SECTION_HEADER_PATTERN = re.compile(r"^###\s+(\d+)[\.\s]", re.MULTILINE)


def _split_sections_by_number(text: str) -> Dict[int, str]:
    """### N. 헤더 기준으로 markdown 을 섹션별로 분할.

    auto-cleanup trigger 검사용. cleanup_master_prd_pipeline 의 헬퍼와 동일 로직이지만
    순환 import 피하기 위해 design_pipeline 안에 별도 보유.
    """
    if not text:
        return {}
    matches = list(_SECTION_HEADER_PATTERN.finditer(text))
    if not matches:
        return {}
    out: Dict[int, str] = {}
    for idx, m in enumerate(matches):
        n = int(m.group(1))
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        out[n] = text[start:end]
    return out


def detect_dirty_prd(prd_content: str) -> Dict[str, Any]:
    """
    master PRD 가 cleanup 이 필요한 dirty 상태인지 결정적으로 판정.

    Returns:
        {
            "is_dirty": bool,
            "reasons": List[str],          # 사람-친화적 reason 코드 (FE 표시용)
            "diagnostic": {
                "product_vision_count": int,
                "s2_s3_missing_count": int,
                ...
            },
        }
    """
    if not prd_content or not prd_content.strip():
        return {
            "is_dirty": False,
            "reasons": [],
            "diagnostic": {"reason": "empty_content"},
        }

    pv_count = len(_PRODUCT_VISION_KEYWORD_RE.findall(prd_content))

    sections = _split_sections_by_number(prd_content)
    section_2 = sections.get(2, "")
    section_3 = sections.get(3, "")
    s2_stories = _extract_prd_story_ids(section_2)
    s3_stories = _extract_prd_story_ids(section_3)
    missing = s3_stories - s2_stories

    reasons: List[str] = []
    if pv_count >= _DIRTY_PRODUCT_VISION_THRESHOLD:
        reasons.append(f"product_vision_repeats_{pv_count}x")
    if len(missing) >= _DIRTY_S2_S3_MISSING_THRESHOLD:
        reasons.append(f"s2_s3_story_mismatch_{len(missing)}_stories")

    return {
        "is_dirty": bool(reasons),
        "reasons": reasons,
        "diagnostic": {
            "product_vision_count": pv_count,
            "s2_story_count": len(s2_stories),
            "s3_story_count": len(s3_stories),
            "s2_s3_missing_count": len(missing),
            "s2_s3_missing_preview": sorted(missing)[:5],
            "product_vision_threshold": _DIRTY_PRODUCT_VISION_THRESHOLD,
            "s2_s3_missing_threshold": _DIRTY_S2_S3_MISSING_THRESHOLD,
        },
    }


def _extract_prd_story_ids(prd_content: str) -> set:
    """
    PRD markdown 에서 모든 Story 토큰을 찾아 'Story-XX.Y' (zero-pad) set 으로.
    표기 형태: '[Story 1.1]', 'Story 1.1', '[Story-1.1]', 'Story-01.1' 모두 흥수.

    Returns:
        예: {'Story-01.1', 'Story-01.2', 'Story-02.3'}
        PRD 가 비었거나 매칭 0건이면 빈 set.
    """
    if not prd_content:
        return set()
    out: set = set()
    for m in _PRD_STORY_TOKEN_RE.finditer(prd_content):
        ep = int(m.group(1))
        st = m.group(2)
        out.add(f"Story-{ep:02d}.{st}")
    return out
