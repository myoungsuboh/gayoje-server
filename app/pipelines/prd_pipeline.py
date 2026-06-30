"""
postMeeting → PRD 파이프라인 (CPS 결과를 입력으로 받음).

다음 stage 시퀀스로 구성된다:
  Code_CPS_Parser
    → PRD Agent1            → PRD Agent2
    → Save PRD Code         → Save PRD ExecuteQuery
    → Get All PRD2
    → PRD Impact Analyzer1  → PRD Section Filter1
    → Merge PRD Agent2      → PRD Reassembler1
    → Merge PRD Code2       → Marge PRD ExecuteQuery2

CPS 파이프라인과 구조가 거의 동일하며 다음 헬퍼를 재사용한다:
  - `build_save_cps_query` (Save CPS Code 와 byte-identical) → `build_save_prd_query`
  - `split_master_sections` / `reassemble_master`
재사용하지 않고 PRD 전용 차이만 분리한 것 (fallback default 키워드, BASED_ON 연결).
"""
from __future__ import annotations

import json
import os
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.pipelines.base import (
    PipelineContext,
    canonicalize_graph,
    extract_json_object,
    generate_json_with_retry,
    strip_code_blocks,
    strip_template_placeholders,
)
from app.pipelines.cps_pipeline import (
    _SECTION_HEADER_RE,
    build_save_cps_query as build_save_graph_query,  # alias — graph schema 동일
    reassemble_master,
    split_master_sections,
)
from app.pipelines.spec_quality import is_meaningful_spec_node, is_placeholder_text
from app.pipelines.prd_cleanup import run_prd_cleanup_if_due

logger = logging.getLogger(__name__)


# ─── Structured Output Schemas (2026-05 결정성 강화) ─────────────────
# CPS pipeline 과 동일 정책 — Gemini responseSchema 로 LLM 출력 형식 강제.

# PRD Agent2 출력 — graph JSON (CPS Agent 와 같은 schema 형태).
PRD_GRAPH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "_harness_metadata": {"type": "object"},
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "properties": {"type": "object"},
                },
                "required": ["id", "label"],
            },
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "type": {"type": "string"},
                    "properties": {"type": "object"},
                },
                "required": ["source", "target", "type"],
            },
        },
    },
    "required": ["nodes", "relationships"],
}

PRD_IMPACT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "affected_sections": {"type": "array", "items": {"type": "string"}},
        "removed_epic_ids": {"type": "array", "items": {"type": "string"}},
        "removed_story_ids": {"type": "array", "items": {"type": "string"}},
        "analysis": {"type": "string"},
    },
}

# Temperature 통일 — CPS pipeline 과 동일 (이전 0.1/0.2 혼재 → 0.1).
_TEMPERATURE = 0.1

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# [2026-05-26 perf A] Impact analyzer 는 단순 JSON 분류 (master + latest →
# affected_sections / removed_ids). pro 모델 불필요 — flash-lite 로 충분.
# 운영 측정: pro ~6-8s / flash-lite ~1-2s. 정확도 회귀 없음 (단순 분류).
#
# [모델 선택] LiteLLM 프록시에 등록된 모델만 사용 가능 (litellm/config.yaml):
#   - gemini-2.5-flash (PRO 등급 — 기본)
#   - gemini-2.5-flash-lite (저비용 — impact 다운그레이드 + Lite 오버플로우)
# [2026-06] Google 이 gemini-2.0-flash-lite 를 폐기(404) → 2.5-flash-lite 로 이전 (litellm 등록 완료).
PRD_IMPACT_ANALYZER_MODEL = "gemini-2.5-flash-lite"


# ─── Domain types ───────────────────────────────────────────────


@dataclass(frozen=True)
class PrdInput:
    """PRD 파이프라인 입력. CPS 단계의 그래프 JSON 을 받아 시작."""

    project_name: str
    version: str
    cps_graph: Dict[str, Any]
    previous_prd_id: Optional[str] = None
    team_id: str = ""
    # [2026-06-04] raw 회의록 — CPS delta 의 full_markdown/Problem/Solution 이 모두 비어
    # parse_cps_for_prd 가 "내용 없음" 으로 떨어질 때의 최후 fallback. CPS merge 가
    # latest_content 빈값 시 payload.meeting_content 로 master 를 채우는 것과 대칭
    # (master CPS 는 raw 로 살아남는데 master PRD 만 "내용 없음" → 프로젝트명만으로
    # 환각하던 비대칭 해소). 기본 "" → 미지정 호출(직접 PRD 라우트)은 기존 동작 유지.
    meeting_content: str = ""

    def normalized_version(self) -> str:
        return self.version.replace(".", "_")

    def project_key(self) -> str:
        """도메인 노드 project property/id 용 스코프 키 (개인=이름, 팀=sentinel 합성)."""
        from app.core.project_scope import scoped_project
        return scoped_project(self.project_name, self.team_id)

    def derived_prd_id(self) -> str:
        from app.core.project_scope import prd_delta_id
        return self.previous_prd_id or prd_delta_id(self.project_key(), self.version)


@dataclass
class PrdResult:
    delta_prd_id: str
    master_prd_id: str
    # 'first_run'   : master 비어있음 + LLM 으로 새 Epic/Story 추출됨
    # 'incremental' : 기존 master 있음 + delta 통합
    # 'no_changes'  : 기존 master 있음 + 이번 회의록은 새 Epic/Story 0개
    #                 (V6 같은 보강·결정 회의록 — 환각 가드 false positive 회피)
    mode: str
    diagnostic: Dict[str, Any] = field(default_factory=dict)


# ─── Stage 1: Code_CPS_Parser ───────────────────────────────────


def parse_cps_for_prd(
    cps_graph: Dict[str, Any], meeting_content: str = ""
) -> Dict[str, str]:
    """
    Stage: `Code_CPS_Parser`.

    CPS Agent 의 그래프 JSON 에서 PRD Agent 입력으로 쓸 두 값을 추출:
      - pure_markdown: CPS_Document.full_markdown
      - problems: "- [prb_xx] summary" 라인의 join 문자열

    pure_markdown fallback 사다리 (앞이 비면 다음으로):
      1) CPS_Document.full_markdown
      2) Problem/Solution 노드로 재구성 (_synthesize_cps_markdown_from_nodes)
      3) raw 회의록 meeting_content (있으면) — CPS merge 의 raw fallback 과 대칭
      4) "내용 없음" (정말 아무 입력도 없을 때만)
    """
    pure_markdown = ""
    problems: List[str] = []
    solutions: List[str] = []

    nodes = cps_graph.get("nodes") or []
    for n in nodes:
        label = n.get("label")
        props = n.get("properties") or {}
        if label == "CPS_Document":
            md = props.get("full_markdown")
            if isinstance(md, str) and md.strip() and not pure_markdown:
                pure_markdown = md
        elif label == "Problem":
            pid = n.get("id")
            summary = props.get("summary", "")
            if pid:
                problems.append(f"- [{pid}] {summary}")
        elif label == "Solution":
            summary = props.get("summary", "")
            if isinstance(summary, str) and summary.strip():
                solutions.append(summary.strip())

    # [2026-05-28 핸드오프 fallback] cps_agent 가 full_markdown 을 누락(빈 값)해도 PRD 가
    # "내용 없음" 으로 환각하지 않도록, 안정적으로 채워지는 Problem/Solution 요약으로
    # CPS 본문을 재구성한다. (CPS merge 가 raw 회의록으로 master 를 채우는 것과 대칭.)
    # [2026-06-04] Problem/Solution 까지 비면 raw 회의록을 최후 fallback 으로 사용 —
    # CPS merge 는 latest_content 빈값 시 payload.meeting_content 로 master 를 채워
    # 살아남지만, PRD 는 이 fallback 이 없어 "내용 없음" → 프로젝트명만으로 환각했다
    # (master CPS 정상 / master PRD 엉뚱 비대칭의 근본 원인). 동일 raw 로 대칭화.
    if not pure_markdown.strip():
        pure_markdown = (
            _synthesize_cps_markdown_from_nodes(problems, solutions)
            or _synthesize_cps_markdown_from_meeting(meeting_content)
            or "내용 없음"
        )

    return {
        "pure_markdown": pure_markdown,
        "problems": "\n".join(problems) if problems else "- 매핑된 문제 없음",
    }


def _synthesize_cps_markdown_from_nodes(problems: List[str], solutions: List[str]) -> str:
    """full_markdown 누락 시 Problem/Solution 노드로 CPS 본문 재구성 (PRD 환각 방지 fallback)."""
    if not problems and not solutions:
        return ""
    parts: List[str] = ["## 📄 CPS 명세 (노드 기반 재구성)"]
    if problems:
        parts.append("\n### 핵심 문제")
        parts.extend(problems)  # 이미 "- [prb_xx] summary" 형식
    if solutions:
        parts.append("\n### 해결 방향")
        parts.extend(f"- {s}" for s in solutions)
    return "\n".join(parts)


def _synthesize_cps_markdown_from_meeting(meeting_content: str) -> str:
    """[2026-06-04] full_markdown 도 Problem/Solution 도 없을 때 raw 회의록으로 CPS 본문
    재구성 — PRD 환각 최후 방어. CPS merge 의 raw-meeting fallback 과 대칭이라, CPS 구조
    추출이 통째로 비어도 master PRD 가 프로젝트명만으로 엉뚱하게 환각하지 않고 실제 회의
    내용을 입력으로 받는다. 회의록도 비면 ""(→ 호출부에서 "내용 없음")."""
    text = (meeting_content or "").strip()
    if not text:
        return ""
    return (
        "## 📄 CPS 명세 (회의록 원문 기반 재구성)\n\n"
        "> CPS 구조 추출이 비어 회의록 원문을 PRD 입력으로 사용합니다.\n\n"
        f"{text}"
    )


# ─── [2026-05-28] Screen/IMPLEMENTED_ON reconcile (markdown ground truth) ──
# Agent2 (prd_graph LLM) 는 다음 케이스를 흔히 누락/오류 출력:
#   1. Screen 노드를 아예 안 만듦
#   2. Story-Screen IMPLEMENTED_ON 관계 미생성
#   3. relationship 의 source/target id 가 노드 id 와 불일치 → Cypher MATCH 실패로 silently drop
# 결과적으로 PRD 페이지의 Screen Relation Graph 가 비어 보임. 사용자 입장에선 markdown 에
# 화면이 있는데 그래프엔 없는 모순. 근본 해결: markdown(Agent1 출력 = 사용자가 보는 ground
# truth) 으로 Screen + IMPLEMENTED_ON 을 결정론적으로 보강.

_STORY_PAIR_RE = re.compile(r"^[^\d]*0*(\d+)[._\-\s]+0*(\d+)\s*$")


def _parse_story_pair(story_id: Optional[str]) -> Optional[Tuple[int, int]]:
    """Story id 에서 (major, minor) 정수 페어 추출. query_repository 와 동일 로직."""
    if not story_id:
        return None
    m = _STORY_PAIR_RE.match(str(story_id))
    if not m:
        return None
    try:
        return (int(m.group(1)), int(m.group(2)))
    except (ValueError, TypeError):
        return None


_SECTION_NAME_PATTERNS = (
    # 🖥️ 이모지 prefix (표준 prd_extract.md 템플릿)
    re.compile(
        r"####\s*🖥️\s*\[(?:Screen:\s*)?([^\]]+?)\](.*?)(?=####\s|^###\s|\Z)",
        flags=re.DOTALL | re.MULTILINE,
    ),
    # 🖥️ 누락하고 'Screen:' prefix 만 사용 (LLM 이 이모지 빠뜨린 케이스)
    re.compile(
        r"####\s*[^\[\n]*?\[Screen:\s*([^\]]+?)\](.*?)(?=####\s|^###\s|\Z)",
        flags=re.DOTALL | re.MULTILINE,
    ),
)

# [2026-06] '포함된 기능' 목록의 Story 참조는 '[Story 1.1]'(대괄호) 뿐 아니라
# '`Story 1.1`'(백틱) / 무괄호로도 나온다 (현 prd_extract 포맷). 여는/닫는 구분자를
# 옵션으로 만들어 세 형식 모두 흡수 — query_repository._STORY_REF_RE 와 동일.
# (대괄호만 보면 backtick 포맷에서 pairs 0건 → IMPLEMENTED_ON 미생성 버그)
_STORY_REF_RE = re.compile(r"[\[`]?Story[- ](\d+)[.\-_](\d+)[\]`]?")

# User Flow 안에 화면명이 '데이터 소스 관리' 화면 처럼 따옴표로 감싸 등장하는 케이스 흡수 —
# 화면명과 '화면' 사이 따옴표(정규/스마트)를 옵션으로 허용. query_repository 와 동일.
_SCREEN_QUOTE_CHARS = "'\"‘’“”"

_STORY_BLOCK_RE = re.compile(
    r"-\s*\*\*\[Story[- ](\d+)[.\-_](\d+)\][^\n]*\*\*"
    r"(.*?)"
    r"(?=-\s*\*\*\[Story[- ]\d+[.\-_]\d+\]|####\s|^###\s|\Z)",
    flags=re.DOTALL | re.MULTILINE,
)


def _extract_all_screens_from_markdown(markdown: str) -> List[Dict[str, Any]]:
    """markdown 에서 모든 screen 추출 + 각 screen 별 (major, minor) story 페어.

    Phase A: '#### 🖥️ [Screen: 이름]' / '#### [Screen: 이름]' 섹션 헤더
      — 섹션 본문의 [Story X.Y] refs 수집.
      LLM 이 가끔 🖥️ 이모지를 누락하므로 2개 패턴 union — query_repository.py
      `_extract_screen_story_pairs_from_markdown` 와 동일한 lenient 기준.

    Phase B: Story 블록의 User Flow inline 매칭
      (b1) Phase A 에서 알려진 화면 이름이 body 안에 '[이름] 화면' 또는 '이름 화면'
           형태로 등장 → 해당 Story 페어를 화면에 매핑.
      (b2) 알려진 이름이 없어도 '[이름] 화면' bracket 패턴이 있으면 추출.

    두 phase 결과 union. 이름 순으로 정렬해 결정론적 반환 (재실행 시 동일 결과).
    """
    if not markdown:
        return []

    screens_map: Dict[str, set] = {}

    # Phase A — 2개 section header 패턴 (🖥️ 필수 / Screen: 필수) union
    for section_re in _SECTION_NAME_PATTERNS:
        for match in section_re.finditer(markdown):
            name = (match.group(1) or "").replace("\n", " ").strip()
            if not name:
                continue
            screens_map.setdefault(name, set())
            section_body = match.group(2)
            for major, minor in _STORY_REF_RE.findall(section_body):
                try:
                    screens_map[name].add((int(major), int(minor)))
                except (ValueError, TypeError):
                    pass

    known_screen_names = list(screens_map.keys())

    # Phase B — Story block 의 User Flow inline 매칭
    inline_bracket_re = re.compile(r"\[\s*([^\[\]\n]{1,60}?)\s*\]\s*화면")
    for sm in _STORY_BLOCK_RE.finditer(markdown):
        try:
            pair = (int(sm.group(1)), int(sm.group(2)))
        except (ValueError, TypeError):
            continue
        body = sm.group(3)

        # (b1) Phase A 의 화면 이름이 body 안에 등장 — bracket 유무 무관
        # query_repository.py 의 screen_in_block_re `(\[\s*{safe}\s*\]|{safe})\s*화면`
        # 패턴과 동일 — 이미 화면 이름을 알고 있으니 bracket 없어도 매칭 가능.
        for name in known_screen_names:
            esc = re.escape(name)
            quote_cls = re.escape(_SCREEN_QUOTE_CHARS)
            if re.search(rf"(?:\[\s*{esc}\s*\]|{esc})[{quote_cls}\s]*화면", body):
                screens_map.setdefault(name, set()).add(pair)

        # (b2) 알려진 이름이 없어도 '[이름] 화면' bracket 패턴 추출 (legacy)
        for inline_match in inline_bracket_re.finditer(body):
            inline_name = (inline_match.group(1) or "").strip()
            if not inline_name:
                continue
            # Role/Story/Epic 같은 메타 토큰은 화면명으로 보지 않음
            lowered = inline_name.lower()
            if lowered.startswith(("story", "role", "epic", "user", "system")):
                continue
            screens_map.setdefault(inline_name, set()).add(pair)

    return sorted(
        [{"name": n, "pairs": sorted(p)} for n, p in screens_map.items()],
        key=lambda x: x["name"],
    )


_STORY_HEADER_RE = re.compile(
    r"-\s*\*\*\[Story[- ](\d+)[.\-_](\d+)\]\s*([^\*\n]*?)\*\*",
    flags=re.MULTILINE,
)


def _extract_all_stories_from_markdown(markdown: str) -> List[Dict[str, Any]]:
    """markdown 의 모든 [Story X.Y] 헤더 → (major, minor) + summary 추출.

    포맷: `- **[Story 1.1] 기능명**` → {pair: (1, 1), summary: '기능명'}.
    summary 가 비면 'Story X.Y' fallback.
    """
    if not markdown:
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for m in _STORY_HEADER_RE.finditer(markdown):
        try:
            pair = (int(m.group(1)), int(m.group(2)))
        except (ValueError, TypeError):
            continue
        if pair in seen:
            continue
        seen.add(pair)
        summary = (m.group(3) or "").strip()
        if not summary:
            summary = f"Story {pair[0]}.{pair[1]}"
        out.append({"pair": pair, "summary": summary})
    return out


def _reconcile_screens_in_prd_graph(
    prd_graph: Dict[str, Any], prd_markdown: str
) -> Dict[str, Any]:
    """prd_graph(Agent2) 출력에 markdown 기준으로 Story/Screen/IMPLEMENTED_ON 결정론적 보강.

    절차:
      1. markdown 의 모든 [Story X.Y] 추출 — graph 에 (major, minor) 매칭 Story 없으면
         합성 (id=story_XX_Y, summary=헤더 텍스트). Agent2 가 markdown 의 Story 를 다른
         번호로 출력한 케이스/일부 누락 케이스 흡수.
      2. markdown 에서 (screen_name, story_pairs[]) 추출.
      3. prd_graph 의 기존 Screen 노드를 name → id 인덱스화.
      4. 각 markdown 페어에 대해:
         - Screen 노드 없으면 'screen_md_<n>' id 로 신규 추가.
         - IMPLEMENTED_ON edge 없으면 추가.

    Returns: 보강된 prd_graph (in-place 갱신).
    """
    if not isinstance(prd_graph, dict):
        return prd_graph
    nodes = prd_graph.get("nodes") or []
    rels = prd_graph.get("relationships") or []

    # ─ Step 1: Story (major, minor) → id 인덱스 + markdown 누락분 합성 ─
    story_pair_to_id: Dict[Tuple[int, int], str] = {}
    for n in nodes:
        if not isinstance(n, dict) or n.get("label") != "Story":
            continue
        pair = _parse_story_pair(n.get("id"))
        if pair and pair not in story_pair_to_id:
            story_pair_to_id[pair] = str(n["id"])

    added_story = 0
    for entry in _extract_all_stories_from_markdown(prd_markdown):
        pair = entry["pair"]
        if pair in story_pair_to_id:
            continue
        # 합성 ID — Agent2 prompt 와 동일 형식: story_{major:02d}_{minor}.
        # build_save_cps_query 의 _is_meaningful_spec_node 가드 통과 위해 summary 필수.
        sid = f"story_{pair[0]:02d}_{pair[1]}"
        nodes.append({
            "id": sid,
            "label": "Story",
            "properties": {
                "summary": entry["summary"],
                "source": "markdown_reconcile",
            },
        })
        story_pair_to_id[pair] = sid
        added_story += 1

    # Screen name → id 인덱스 (기존 Agent2 출력)
    screen_name_to_id: Dict[str, str] = {}
    for n in nodes:
        if not isinstance(n, dict) or n.get("label") != "Screen":
            continue
        sname = (n.get("properties") or {}).get("name")
        if sname:
            screen_name_to_id.setdefault(str(sname).strip(), str(n.get("id") or ""))

    # 기존 IMPLEMENTED_ON edge set
    existing_edges: set = set()
    for r in rels:
        if isinstance(r, dict) and r.get("type") == "IMPLEMENTED_ON":
            existing_edges.add((str(r.get("source", "")), str(r.get("target", ""))))

    md_screens = _extract_all_screens_from_markdown(prd_markdown)

    added_screen = 0
    added_edge = 0
    next_md_idx = 1
    # 이미 'screen_md_*' id 가 있으면 충돌 방지 위해 인덱스 추적
    for n in nodes:
        if isinstance(n, dict) and isinstance(n.get("id"), str):
            m = re.match(r"^screen_md_(\d+)$", n["id"])
            if m:
                next_md_idx = max(next_md_idx, int(m.group(1)) + 1)

    screens_with_no_story_match: List[str] = []
    for entry in md_screens:
        sname = entry["name"]
        sid = screen_name_to_id.get(sname)
        if not sid:
            sid = f"screen_md_{next_md_idx}"
            next_md_idx += 1
            nodes.append({
                "id": sid,
                "label": "Screen",
                "properties": {"name": sname, "source": "markdown_reconcile"},
            })
            screen_name_to_id[sname] = sid
            added_screen += 1

        edges_added_for_this_screen = 0
        for pair in entry["pairs"]:
            story_id = story_pair_to_id.get(pair)
            if not story_id:
                continue  # Agent2 가 해당 Story 자체를 안 만든 케이스 — 합성 안 함.
            edge_key = (story_id, sid)
            if edge_key in existing_edges:
                continue
            rels.append({
                "source": story_id,
                "target": sid,
                "type": "IMPLEMENTED_ON",
                "properties": {"source": "markdown_reconcile"},
            })
            existing_edges.add(edge_key)
            added_edge += 1
            edges_added_for_this_screen += 1

        # 진단: 이 화면이 markdown 에 있고 페어도 추출됐는데 Neo4j 저장될 IMPLEMENTED_ON 이
        # 0 이면 → 사용자 'stories_match_no_data' 에러 그대로 재현. 로그로 추적.
        has_any_edge_to_this = any(
            r.get("type") == "IMPLEMENTED_ON" and str(r.get("target", "")) == sid
            for r in rels
        )
        if entry["pairs"] and not has_any_edge_to_this:
            screens_with_no_story_match.append(sname)

    if added_story or added_screen or added_edge:
        logger.info(
            "prd graph reconcile: markdown 기준 보강 — Story +%d, Screen +%d, "
            "IMPLEMENTED_ON +%d",
            added_story, added_screen, added_edge,
        )
    if screens_with_no_story_match:
        # 가능한 원인: Agent2 가 Story 노드를 markdown 의 [Story X.Y] 와 다른 번호로 출력
        # (예: markdown 1.1 vs graph 2.1) — Agent1/Agent2 의 ID 일관성 깨짐.
        logger.warning(
            "prd graph reconcile: %d 화면이 markdown 에 있지만 매칭되는 Story 가 graph 에 "
            "없어 IMPLEMENTED_ON 미생성 — screens=%r, graph_story_pairs=%r. "
            "Agent2 Story id 가 markdown [Story X.Y] 와 (major, minor) 페어 불일치 의심.",
            len(screens_with_no_story_match),
            screens_with_no_story_match,
            sorted(story_pair_to_id.keys()),
        )

    prd_graph["nodes"] = nodes
    prd_graph["relationships"] = rels
    return prd_graph


# ─── Stage 2: PRD Agent1 (markdown) ─────────────────────────────


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


async def call_prd_extract(
    ctx: PipelineContext,
    project_name: str,
    version: str,
    parsed_cps: Dict[str, str],
) -> str:
    """Stage: `PRD Agent1` — CPS → PRD markdown."""
    prompt = _render(
        _load_prompt("prd_extract.md"),
        project_name=project_name,
        version=version,
        problems=parsed_cps["problems"],
        pure_markdown=parsed_cps["pure_markdown"],
    )
    # PRD markdown 생성 — 자유 텍스트라 schema 미적용. temperature 통일.
    # template placeholder leak (prd_extract.md 의 `[도메인명 - 예: ...]` 같은
    # OUTPUT SCHEMA 흔적) 은 사후 정리 — merge 로 propagate 되기 전에 차단.
    result = await ctx.gemini.generate(prompt, temperature=_TEMPERATURE)
    return strip_template_placeholders(strip_code_blocks(result.text))


# ─── Stage 3: PRD Agent2 (graph JSON) ───────────────────────────


async def call_prd_graph(
    ctx: PipelineContext,
    payload: PrdInput,
    prd_markdown: str,
) -> Dict[str, Any]:
    """Stage: `PRD Agent2` — PRD markdown → graph JSON."""
    prompt = _render(
        _load_prompt("prd_graph.md"),
        project_name=payload.project_name,
        version=payload.version,
        version_normalized=payload.normalized_version(),
        previous_prd_id=payload.previous_prd_id or "null",
        prd_markdown=prd_markdown,
    )
    # [2026-05] structured output + strict retry. temperature 통일.
    obj, _ = await generate_json_with_retry(
        ctx.gemini, prompt,
        temperature=_TEMPERATURE,
        response_schema=PRD_GRAPH_SCHEMA,
    )
    if not obj or "nodes" not in obj:
        raise ValueError(
            f"PRD Agent2 returned unparseable JSON (idempotency_key={ctx.idempotency_key})"
        )
    # [2026-05-26] 환각 가드는 run_prd_pipeline 에서 master_content 와 함께 판단.
    # 여기서 즉시 throw 하면 V6 같은 "기존 Story 디테일 보강" 회의록도 차단됨.
    # graph 단계는 출력 그대로 반환 + spec_count 진단 정보만 로그.
    nodes = obj.get("nodes") or []
    spec_node_labels = {"Epic", "Story"}
    spec_count = sum(1 for n in nodes if (n or {}).get("label") in spec_node_labels)
    if spec_count == 0:
        logger.info(
            "prd graph: spec_count=0 (idempotency_key=%s) — master 검사 후 환각/no_changes 결정",
            ctx.idempotency_key,
        )
    return obj


# ─── Stage 4: Save PRD (graph JSON → Cypher) ────────────────────


def build_save_prd_query(
    graph: Dict[str, Any],
    project_name: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Stage: `Save PRD Code`.

    'Save PRD Code' 단계는 'Save CPS Code' 와 byte-identical 이므로
    `build_save_graph_query` (= build_save_cps_query 의 alias) 를 그대로 사용.
    Returns (cypher_string, params_dict) — UNWIND + parameter binding 패턴.

    project_name (선택): 전달되면 build_save_graph_query 에서 모든 노드 properties.project
    에 자동 주입 — LLM 누락 흡수.
    """
    return build_save_graph_query(graph, project_name=project_name)


# ─── Stage 5: Get All PRD2 ──────────────────────────────────────


_GET_ALL_PRD_QUERY = """\
// 1. 마스터 PRD 1건
OPTIONAL MATCH (m:PRD_Document {project: $project, type: 'Master', is_latest: true})
WITH m ORDER BY m.updated_at DESC LIMIT 1

// 2. 최신 Delta PRD 1건
OPTIONAL MATCH (l:PRD_Document {project: $project, is_latest: true})
WHERE l.type IS NULL OR l.type <> 'Master'
WITH m, l ORDER BY l.id DESC LIMIT 1

// 3. 마스터 하위 Epic/Story/Screen
OPTIONAL MATCH (m)<-[:EXTRACTED_FROM]-(me:Epic)
OPTIONAL MATCH (me)-[:CONTAINS]->(ms:Story)
OPTIONAL MATCH (ms)-[:IMPLEMENTED_ON]->(msc:Screen)
WITH m, l, collect(DISTINCT CASE WHEN me IS NOT NULL THEN {
    epic_id: me.id,
    epic_summary: me.summary,
    story_id: ms.id,
    story_summary: ms.summary,
    screen_name: msc.name
} ELSE NULL END) AS master_prd_details

// 4. 최신 하위 Epic/Story/Screen
OPTIONAL MATCH (l)<-[:EXTRACTED_FROM]-(le:Epic)
OPTIONAL MATCH (le)-[:CONTAINS]->(ls:Story)
OPTIONAL MATCH (ls)-[:IMPLEMENTED_ON]->(lsc:Screen)
WITH m, l, master_prd_details, collect(DISTINCT CASE WHEN le IS NOT NULL THEN {
    epic_id: le.id,
    epic_summary: le.summary,
    story_id: ls.id,
    story_summary: ls.summary,
    screen_name: lsc.name
} ELSE NULL END) AS latest_prd_details

// 5. [2026-05] 진단 — 이 프로젝트의 PRD_Document 전체 개수 (Master/Delta 무관).
//    is_first_run 판단 강화: master 비었지만 prd_total > 0 면 orphan 으로 의심.
//    아직 prd_total=0 이면 진짜 첫 실행이라 안전.
OPTIONAL MATCH (any_prd:PRD_Document {project: $project})
WITH m, l, master_prd_details, latest_prd_details, count(any_prd) AS prd_total

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


async def fetch_prd_master_and_latest(
    ctx: PipelineContext, project_name: str
) -> Dict[str, Any]:
    """Stage: `Get All PRD2`."""
    records = await ctx.neo4j.run_cypher(_GET_ALL_PRD_QUERY, {"project": project_name})
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
            "cleanup_at_version_count": 0,
        }
    row = records[0]
    return {
        "master_id": row.get("master_id"),
        "master_content": row.get("master_content") or "",
        "master_prd_details": [
            p for p in (row.get("master_prd_details") or []) if p is not None
        ],
        "latest_id": row.get("latest_id"),
        "latest_content": row.get("latest_content") or "",
        "latest_prd_details": [
            p for p in (row.get("latest_prd_details") or []) if p is not None
        ],
        "project_name": row.get("project_name") or project_name,
        # [2026-05] 진단 — 이 프로젝트의 PRD_Document 총 갯수. is_first_run 안전망용.
        "prd_total": int(row.get("prd_total") or 0),
        # [2026-05-28] L1-3 — null(필드 없음, 마이그레이션 전 master) 시 0 으로 coerce.
        # 첫 cleanup 은 incremental save 에서 prd_total 로 init 되고, 그 후 interval
        # 도달 시 trigger.
        "cleanup_at_version_count": int(row.get("cleanup_at_version_count") or 0),
    }


# ─── Stage 6: PRD Impact Analyzer + Section Filter ──────────────


async def call_prd_impact_analyzer(
    ctx: PipelineContext,
    master_prd_details: List[Dict[str, Any]],
    latest_content: str,
) -> Dict[str, Any]:
    """Stage: `PRD Impact Analyzer1`.

    [2026-05-26 perf A] flash-lite override — 단순 JSON 분류 stage 라
    pro 모델 불필요. 지연/비용 절감 (대략 -2~5초 / 호출).
    """
    prompt = _render(
        _load_prompt("prd_impact.md"),
        master_prd_details_json=json.dumps(master_prd_details, ensure_ascii=False),
        latest_content=latest_content,
    )
    # [2026-05] structured output + strict retry — 빈 dict 라도 안전 (모든 키 [] / "" 로 흡수).
    parsed, _ = await generate_json_with_retry(
        ctx.gemini, prompt,
        temperature=_TEMPERATURE,
        response_schema=PRD_IMPACT_SCHEMA,
        model=PRD_IMPACT_ANALYZER_MODEL,
    )
    return {
        "affected_sections": list(parsed.get("affected_sections") or []),
        "removed_epic_ids": list(parsed.get("removed_epic_ids") or []),
        "removed_story_ids": list(parsed.get("removed_story_ids") or []),
        "analysis": parsed.get("analysis", ""),
    }


# PRD-specific defaults ('PRD Section Filter1' 단계 기본값)
_PRD_DEFAULT_CANDIDATES = ["Epic & Story Map", "Screen Architecture"]
_PRD_FALLBACK_KEYS = ["Epic", "Screen"]


def filter_affected_prd_sections(
    master_content: str,
    latest_content: str,
    impact: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Stage: `PRD Section Filter1`.

    CPS 의 `filter_affected_sections` 와 동일 알고리즘이지만 fallback 키워드만
    PRD 전용으로 교체한다. 의도적으로 코드 중복을 허용 — stage 간 추적
    가능하도록 유지.
    """
    if not master_content.strip():
        return {
            "affected_sections_content": "",
            "full_section_map": {},
            "section_order": [],
            "affected_section_keys": [],
            "latest_content": latest_content,
            "impact": impact,
            "master_content": "",
            "is_first_run": True,
            "_diagnostic": {
                "mode": "FIRST_RUN",
                "master_size": 0,
                "affected_size": 0,
                "latest_size": len(latest_content),
            },
        }

    section_map, section_order = split_master_sections(master_content)

    candidates: List[str] = list(impact.get("affected_sections") or [])
    if not candidates and len(latest_content.strip()) > 50:
        candidates = list(_PRD_DEFAULT_CANDIDATES)

    affected_keys: List[str] = []
    affected_content = ""
    section_keys = [k for k in section_map.keys() if k != "__header__"]

    for target in candidates:
        matched = next(
            (
                k
                for k in section_keys
                if k.lower().find(target.lower()) >= 0
                or target.lower().find(k.lower()) >= 0
            ),
            None,
        )
        if matched and matched not in affected_keys:
            affected_content += ("\n\n" if affected_content else "") + section_map[matched]
            affected_keys.append(matched)

    if not affected_content.strip():
        for fb in _PRD_FALLBACK_KEYS:
            found = next(
                (k for k in section_keys if k.lower().find(fb.lower()) >= 0),
                None,
            )
            if found and found not in affected_keys:
                affected_content += (
                    "\n\n" if affected_content else ""
                ) + section_map[found]
                affected_keys.append(found)

    return {
        "affected_sections_content": affected_content.strip(),
        "full_section_map": section_map,
        "section_order": section_order,
        "affected_section_keys": affected_keys,
        "latest_content": latest_content,
        "impact": impact,
        "master_content": master_content,
        "is_first_run": False,
        "_diagnostic": {
            "mode": "INCREMENTAL",
            "master_size": len(master_content),
            "affected_size": len(affected_content),
            "latest_size": len(latest_content),
            "reduction_pct": (
                round((1 - len(affected_content) / len(master_content)) * 100)
                if master_content
                else 0
            ),
            "affected_sections": affected_keys,
            "analysis": impact.get("analysis", ""),
        },
    }


# ─── Stage 7: Merge agent (재조립은 CPS 헬퍼 재사용) ───────────


def _inventory_from_master_markdown(master_content: str) -> str:
    """[2026-05-27 R1] full_markdown 의 Epic & User Story Map 섹션을 merge agent
    인벤토리로 변환.

    graph 기반 master_prd_details 가 구조적으로 항상 비는 결함의 fallback. master
    PRD 의 누적 Epic/Story 는 full_markdown 에만 온전히 남아 있으므로, 그 Section 2
    텍스트를 통째로 인벤토리로 전달해 merge agent 가 기존 항목을 인지하고 의미가
    겹치는 새 Epic/Story/ID 남발(누더기 누적)을 피하게 한다. (파싱 정규식의 형식
    취약성을 피해 섹션 텍스트 그대로 — 견고.)
    """
    if not master_content or not master_content.strip():
        return "(첫 실행 — 기존 Epic/Story 없음. 새 ID 자유 부여 가능.)"
    section_map, _ = split_master_sections(master_content)
    epic_section = ""
    for key, body in section_map.items():
        if key == "__header__":
            continue
        if re.search(r"epic|story|기능\s*계층", key, re.IGNORECASE):
            epic_section = body
            break
    if not epic_section.strip():
        # master 는 있으나 Epic 섹션 식별 실패 — 최소한 "기존 존재" 신호로 남용 방지.
        return (
            "(기존 마스터 PRD 가 존재한다. 본문의 기존 Epic/Story 를 재사용하고, "
            "의미가 겹치면 새 ID 를 만들지 말 것.)"
        )
    return (
        "다음은 기존 마스터 PRD 의 Epic & User Story Map 이다. 새 Epic/Story 를 "
        "만들기 전에 아래 기존 항목과 의미를 매칭해 중복을 피하고, 같은 의미면 "
        "기존 ID 를 재사용하라:\n\n" + epic_section.strip()
    )


async def call_prd_merge_agent(ctx: PipelineContext, filter_data: Dict[str, Any]) -> str:
    """Stage: `Merge PRD Agent2`.

    [2026-05-26] master_prd_details 를 인벤토리 형태로 prompt 에 명시 전달 —
    LLM 이 새 Epic/Story 만들기 전에 기존 ID 와 의미 매칭 강제. 누더기 PRD 누적
    방지 (Rule 3 DEDUPLICATION).
    """
    impact = filter_data.get("impact") or {}
    details = filter_data.get("master_prd_details") or []
    if details:
        inventory = _format_epic_story_inventory(details)
    else:
        # [2026-05-27 R1] graph 기반 master_prd_details 는 구조적으로 항상 빈다
        # (Epic 이 delta 문서에만 EXTRACTED_FROM 연결되고 master 노드엔 안 매달림 →
        # _GET_ALL_PRD_QUERY 의 (m)<-[:EXTRACTED_FROM]-(Epic) 매치 0건). 그 결과
        # 인벤토리가 늘 "(첫 실행)"으로 떨어져 merge agent 가 매 회의 새 Epic/Story/
        # Product Vision 을 자유 부여 → 누더기 누적의 직접 원인이었다.
        # fallback: full_markdown 의 Epic & Story Map 섹션을 인벤토리로 사용.
        inventory = _inventory_from_master_markdown(filter_data.get("master_content") or "")
    prompt = _render(
        _load_prompt("prd_merge.md"),
        affected_sections_content=filter_data.get("affected_sections_content", ""),
        latest_content=filter_data.get("latest_content", ""),
        removed_epic_ids=json.dumps(impact.get("removed_epic_ids") or [], ensure_ascii=False),
        removed_story_ids=json.dumps(impact.get("removed_story_ids") or [], ensure_ascii=False),
        existing_epic_story_inventory=inventory,
    )
    # merge agent 는 markdown 출력 — JSON schema 미적용. temperature 통일.
    result = await ctx.gemini.generate(prompt, temperature=_TEMPERATURE)
    return strip_template_placeholders(strip_code_blocks(result.text))


def _format_epic_story_inventory(master_prd_details: List[Dict[str, Any]]) -> str:
    """master_prd_details (Epic/Story id + summary + screen) 를 LLM 친화 형식으로.

    출력 형식:
      [Epic-01] 식물 정보 관리
        - [Story-01.1] 사용자는 식물을 등록한다 (화면: Home)
        - [Story-01.2] 사용자는 식물을 조회한다 (화면: Detail)
      [Epic-02] 알림 시스템
        ...

    빈 인벤토리는 "(첫 실행 — 기존 항목 없음)" 으로 명시.
    """
    if not master_prd_details:
        return "(첫 실행 — 기존 Epic/Story 없음. 새 ID 자유 부여 가능.)"

    # epic_id 별로 group
    epic_groups: Dict[str, Dict[str, Any]] = {}
    for row in master_prd_details:
        if not isinstance(row, dict):
            continue
        epic_id = row.get("epic_id") or ""
        if not epic_id:
            continue
        if epic_id not in epic_groups:
            epic_groups[epic_id] = {
                "epic_summary": row.get("epic_summary") or "",
                "stories": [],
            }
        story_id = row.get("story_id")
        if story_id:
            epic_groups[epic_id]["stories"].append({
                "story_id": story_id,
                "story_summary": row.get("story_summary") or "",
                "screen_name": row.get("screen_name") or "",
            })

    lines: List[str] = []
    for epic_id, info in sorted(epic_groups.items()):
        lines.append(f"[{epic_id}] {info['epic_summary']}")
        for story in info["stories"]:
            screen = f" (화면: {story['screen_name']})" if story["screen_name"] else ""
            lines.append(
                f"  - [{story['story_id']}] {story['story_summary']}{screen}"
            )
    return "\n".join(lines)


# ─── Stage 8: Master PRD Cypher (with BASED_ON to master CPS) ───


def build_merge_master_prd_query(
    project_name: str,
    merged_content: str,
    latest_delta_id: Optional[str],
    *,
    cleanup_at_version_count: int,
) -> Tuple[str, Dict[str, Any]]:
    """
    Stage: `Merge PRD Code2`.

    CPS 와 다른 점: 마스터 PRD 가 항상 동일 프로젝트 마스터 CPS 에 `BASED_ON` 연결.

    모든 값 ($master_prd_id, $project, $merged_content, $master_cps_id,
    $latest_delta_id) 은 parameter binding. merged_content 는 LLM 출력 → 가장
    위험한 자리였음. 이제 인터폴 0건.

    [2026-05-26 데이터 무결성 가드] merged_content 빈 string → ValueError.
    master.full_markdown wipe 시 누적 PRD 영구 손실.
    """
    if not merged_content or not merged_content.strip():
        raise ValueError(
            f"build_merge_master_prd_query: merged_content 가 비어있음 (project={project_name}). "
            "master.full_markdown 를 wipe 하면 누적 PRD 데이터 영구 손실 — 차단."
        )

    # [멀티테넌시] project_name 은 호출자가 넘긴 *스코프 키*. id 빌더로 통일.
    # master CPS id 도 같은 스코프 키 → 올바른 팀/개인 CPS master 에 BASED_ON 연결.
    from app.core.project_scope import cps_master_id, prd_master_id
    master_prd_id = prd_master_id(project_name)
    master_cps_id = cps_master_id(project_name)

    parts = [
        "// --- 마스터 PRD 갱신 ---",
        "MERGE (master:PRD_Document {id: $master_prd_id})",
        "SET master.project = $project,",
        "    master.version = 'Final',",
        "    master.type = 'Master',",
        "    master.is_latest = true,",
        "    master.full_markdown = $merged_content,",
        "    master.updated_at = timestamp(),",
        "    master.cleanup_at_version_count = $cleanup_at_version_count,",
        # [2026-06] 회의록 merge = 새 정보 반영 → 이전 autofix 진단(인터뷰 필요
        # 항목)은 무효. 영속화된 needs_input 을 함께 소멸 (수동 편집 PATCH 는 유지).
        "    master.autofix_needs_input = null,",
        "    master.autofix_needs_at = null",
        "",
        "// --- 마스터 CPS와 연결 ---",
        "WITH master",
        "OPTIONAL MATCH (cps_m:CPS_Document {id: $master_cps_id})",
        "FOREACH (ignore IN CASE WHEN cps_m IS NOT NULL THEN [1] ELSE [] END |",
        "  MERGE (master)-[:BASED_ON]->(cps_m)",
        ")",
    ]
    params: Dict[str, Any] = {
        "master_prd_id": master_prd_id,
        "project": project_name,
        "merged_content": merged_content,
        "master_cps_id": master_cps_id,
        "cleanup_at_version_count": cleanup_at_version_count,
    }

    if latest_delta_id:
        parts.append("")
        parts.append("// --- 최신 Delta 편입 ---")
        parts.append("WITH master")
        parts.append("MATCH (latest:PRD_Document {id: $latest_delta_id})")
        parts.append("MERGE (master)-[:SYNTHESIZED_FROM]->(latest)")
        parts.append("SET latest.is_latest = false")
        params["latest_delta_id"] = latest_delta_id

    # [Phase 3.6] PRD 재합성 완료 → Design (SPACK/DDD/Arch) 은 옛 PRD 기준이라 stale.
    # 사용자가 미팅 로그 새로 올리면 이 파이프라인이 다시 돌면서 design stale=true.
    # createSpack 으로 design 재생성 시점에 false 로 reset (BE-4 에서 처리).
    # 첫 PRD 생성에서도 마킹되지만 design 이 아직 없으면 FE banner 가드로 안 보임.
    parts.append("")
    parts.append("// --- [Phase 3.6] Design source-stale 마킹 ---")
    parts.append("WITH master")
    parts.append("MERGE (p:Project {name: master.project})")
    parts.append("SET p.design_source_stale = true,")
    parts.append("    p.design_source_stale_at = timestamp()")

    return "\n".join(parts).strip(), params


# ─── End-to-end orchestrator ────────────────────────────────────


async def run_prd_extract(
    ctx: PipelineContext, payload: PrdInput
) -> Dict[str, Any]:
    """PRD 추출 단계 — Code_CPS_Parser + PRD Agent1(markdown) + PRD Agent2(graph). LLM 2회.

    [batch 파이프라이닝] **Neo4j 접근 0건** (읽기도 없음 — fetch 는 merge 단계).
    결과는 그 회의록(cps_graph)에만 의존하고 누적 master 무관 → prefetch 가능.
    모든 DB 접근은 run_prd_merge 에 있다.

    반환: {"parsed", "prd_markdown", "prd_graph"} — run_prd_merge 의 입력.
    """
    logger.info(
        "prd extract start: project=%s version=%s key=%s",
        payload.project_name,
        payload.version,
        ctx.idempotency_key,
    )
    # Stage 1: Code_CPS_Parser
    # [2026-06-04] payload.meeting_content 를 raw fallback 으로 함께 전달 — CPS delta 가
    # 통째로 비어도 PRD 가 회의 내용으로 생성되도록 (환각 방지, CPS merge 와 대칭).
    parsed = parse_cps_for_prd(payload.cps_graph, payload.meeting_content)
    # [2026-05-26 perf C] sub-stage 마커 — FE 가 "지금 어디까지" 표시.
    await ctx.emit_stage("prd_extract")
    # Stage 2: PRD Agent1 (markdown)
    prd_markdown = await call_prd_extract(
        ctx, payload.project_name, payload.version, parsed
    )
    # Stage 3: PRD Agent2 (graph JSON)
    await ctx.emit_stage("prd_graph")
    # [2026-05 결정성] PRD graph 정규화 — 노드/관계 순서 안정화.
    prd_graph = canonicalize_graph(await call_prd_graph(ctx, payload, prd_markdown))
    # [2026-05-28 크리티컬 fix] Agent2 가 누락한 Screen 노드 + IMPLEMENTED_ON 관계를
    # markdown ground truth 로 결정론적 보강 — Relation Graph 가 비어보이는 버그 방지.
    prd_graph = _reconcile_screens_in_prd_graph(prd_graph, prd_markdown)
    return {"parsed": parsed, "prd_markdown": prd_markdown, "prd_graph": prd_graph}


def _is_substantive_prd_markdown(prd_markdown: str) -> bool:
    """추출된 PRD 문서가 실질적인지 판정 — graph Epic 추출이 0이어도 이 문서를 첫 PRD
    master 로 저장(빈 PRD 방지)할 가치가 있는지.

    [2026-06 콜드스타트 수정] 이전엔 '📦' 또는 'Epic N:' 정규식을 **하드 요구**해, LLM 이
    다른 형식(예: '에픽', 숫자 없는 'Epic:', '### 기능')으로 Epic 을 써내면 실질 문서인데도
    no_changes 로 드롭됐다 → PRD master 가 영영 부트스트랩 안 되는 콜드스타트 트랩(CPS 는
    무조건 master 를 써서 누적되는데 PRD 만 빈 채 남던 'CPS 가득/PRD 빈' 비대칭의 직접 원인).
    형식 정규식 대신 **실내용 길이 + 대괄호 placeholder 밀도**로 판정 — CPS 의 '내용 있으면
    저장' 철학과 대칭화.

    [2026-06 R2] '길이 단독' 게이트의 두 구멍 차단:
    - (스켈레톤 오판) Agent1 이 prd_extract.md OUTPUT SCHEMA 뼈대([CPS의 Context...],
      [구체적인 기능명] 등)를 그대로 emit 하면 길이는 300 초과지만 내용은 placeholder 뿐.
      is_placeholder_text 는 라인이 placeholder 로 *시작*할 때만 잡아 'X: [placeholder]' 형태를
      놓치므로, 미치환 대괄호 토큰의 글자 밀도가 높으면(>35%) 비실질로 판정.
    - (짧은-실질 드롭) 진짜 Epic 을 가진 짧은 회의가 300자 floor 에 걸려 드롭되던 문제 →
      placeholder 제외 실내용 floor 를 150 으로 완화(밀도 가드가 품질 하한을 대신 보장).
    (입력이 통째로 비는 경우의 환각은 호출부 parsed CPS sentinel + meeting_content 로 가드.)
    """
    if not prd_markdown or not prd_markdown.strip():
        return False
    real = "\n".join(
        line for line in prd_markdown.splitlines()
        if line.strip() and not is_placeholder_text(line.strip())
    ).strip()
    if not real:
        return False
    # 미치환 대괄호 placeholder 밀도 — 스켈레톤 문서(대부분이 '[...작성]')를 실질로 오판 방지.
    bracket_chars = sum(len(m) for m in re.findall(r"\[[^\[\]\n]{2,}?\]", real))
    if bracket_chars / len(real) > 0.35:
        return False
    return len(real) >= 150


async def _prd_merge_compute(
    ctx: PipelineContext, payload: PrdInput, extract: Dict[str, Any]
) -> Callable[[], Awaitable[PrdResult]]:
    """PRD 병합의 **읽기 + LLM** 단계 (Neo4j 쓰기 0건). 반환: 실제 DB 쓰기를 수행하고
    PrdResult 를 돌려주는 commit 코루틴.

    [2026-06-04 perf] post_meeting 배치에서 이 compute 를 CPS 병합과 **동시 실행**해 두
    flash LLM(cps_merge·prd_merge agent)을 오버랩 → 항목당 ~7s 단축. PRD 쓰기는 commit
    에서 **CPS master 쓰기 완료 후** 수행 → BASED_ON 무결성 + 동시 쓰기 0건(데이터 안전).
    DB 쓰기 순서·내용·LLM 입출력은 기존 run_prd_merge 와 **완전히 동일**. 단일/직접 호출
    (run_prd_pipeline)은 run_prd_merge 래퍼가 compute 직후 commit 을 실행해 관측 동작 동일.
    """
    logger.info(
        "prd merge compute start: project=%s version=%s key=%s",
        payload.project_name,
        payload.version,
        ctx.idempotency_key,
    )
    parsed = extract["parsed"]
    prd_markdown = extract["prd_markdown"]
    prd_graph = extract["prd_graph"]

    # [멀티테넌시] 모든 DB project property/id 는 스코프 키 기준 (개인=이름 그대로).
    from app.core.project_scope import prd_master_id, scope_graph
    db_project = payload.project_key()

    # Stage 5~6: Get All PRD + Impact (읽기 + LLM, 쓰기 0).
    prd_state = await fetch_prd_master_and_latest(ctx, db_project)
    impact = await call_prd_impact_analyzer(
        ctx, prd_state["master_prd_details"],
        prd_state.get("latest_content") or prd_markdown,
    )

    # [2026-05-28 L1 생성 위생] 실질 본문 있는 Epic/Story 만 카운트.
    graph_nodes = prd_graph.get("nodes") or []
    spec_count = sum(
        1 for n in graph_nodes
        if (n or {}).get("label") in {"Epic", "Story"} and is_meaningful_spec_node(n)
    )
    has_existing_master = bool((prd_state.get("master_content") or "").strip())

    if spec_count == 0:
        # [빈-PRD 방지 보장 / 2026-06 콜드스타트] 기존 master 없고 + 추출 문서 실질적 +
        # CPS 입력이 실제 내용(parse sentinel '내용 없음' 아님) → 예비 first master 저장.
        # CPS 가 master 를 갖는 회의에서 PRD 만 빈 채 남던 비대칭 해소. CPS 가 통째로 비면
        # 프로젝트명만으로 PRD 환각하는 것을 막기 위해 제외.
        # [2026-06 배치 false-block 수정] 배치 경로(_prd_extract_from_cache)는 meeting_content
        # 없이 parsed 를 재구성해, strict/lenient CPS 추출이 full_markdown 을 누락하면 merge
        # 시점 pure_markdown 이 '내용 없음' 으로 잘못 떨어진다 → 실콘텐츠 부트스트랩을 false-block
        # (PR #173 콜드스타트 수정을 배치에서 무력화). payload.meeting_content(양 경로에서 신뢰
        # 가능한 회의 원문)도 함께 보아, 회의 원문이 있으면 실콘텐츠로 인정(환각 아님). 둘 다
        # 비어야 진짜 '입력 없음' → 환각 가드 발동.
        cps_has_real_content = (
            (parsed.get("pure_markdown") or "").strip() not in ("", "내용 없음")
            or bool((payload.meeting_content or "").strip())
        )
        if not has_existing_master and cps_has_real_content and _is_substantive_prd_markdown(prd_markdown):
            delta_prd_id = payload.derived_prd_id()
            merge_query, merge_params = build_merge_master_prd_query(
                project_name=db_project,
                merged_content=prd_markdown,
                latest_delta_id=prd_state.get("latest_id") or delta_prd_id,
                cleanup_at_version_count=prd_state.get("prd_total", 0),
            )
            master_prd_id = prd_master_id(db_project)
            result = PrdResult(
                delta_prd_id=delta_prd_id,
                master_prd_id=master_prd_id,
                mode="first_run",
                diagnostic={
                    "parsed_cps": {
                        "pure_markdown_size": len(parsed["pure_markdown"]),
                        "problems_count": len(parsed["problems"].split("\n")) if parsed["problems"] else 0,
                    },
                    "spec_count": 0,
                    "preliminary": True,
                    "reason": "graph Epic 0 이나 추출 문서가 실질적 → 예비 PRD 로 저장. 다음 회의에서 구체화됩니다.",
                },
            )

            async def _commit_preliminary() -> PrdResult:
                await ctx.neo4j.run_cypher(merge_query, merge_params)
                logger.info(
                    "prd nonempty-guarantee: graph Epic 0 이나 추출 문서 실질적 → 예비 first master 저장 "
                    "(project=%s version=%s)", payload.project_name, payload.version,
                )
                return result
            return _commit_preliminary

        # graceful no-op (no_changes) — 쓰기 0.
        # [2026-06 가시성] 콜드스타트(기존 master 없음)에서 PRD master 가 생성되지 않고 드롭되면
        # 경고 — '왜 PRD 가 비었는지' 운영 추적용 (기존엔 job 성공이라 완전 무음이었다).
        if not has_existing_master:
            logger.warning(
                "prd cold-start drop: project=%s version=%s — spec_count=0 & 첫 PRD master "
                "부트스트랩 미충족(cps_real=%s, substantive_md=%s) → PRD 빈 상태 유지. "
                "PRD graph Epic/Story 추출 또는 prd_extract 품질 점검 필요.",
                payload.project_name, payload.version,
                cps_has_real_content, _is_substantive_prd_markdown(prd_markdown),
            )
        reason = (
            "Epic/Story 0개 — 이 회의록만으로는 기획서(PRD) 내용이 부족합니다. "
            "다음 회의 누적 또는 회의록 보강 후 생성됩니다."
            if not has_existing_master
            else "기존 Story 보강·결정만 있어 새 Epic/Story 0개 — master 유지"
        )
        master_prd_id = prd_master_id(db_project)
        result = PrdResult(
            delta_prd_id=prd_state.get("latest_id") or "",
            master_prd_id=master_prd_id,
            mode="no_changes",
            diagnostic={
                "parsed_cps": {
                    "pure_markdown_size": len(parsed["pure_markdown"]),
                    "problems_count": len(parsed["problems"].split("\n")) if parsed["problems"] else 0,
                },
                "spec_count": 0,
                "reason": reason,
                "first_run": not has_existing_master,
            },
        )

        async def _commit_no_changes() -> PrdResult:
            logger.info(
                "prd pipeline no_changes (spec 0): project=%s version=%s has_master=%s",
                payload.project_name, payload.version, has_existing_master,
            )
            return result
        return _commit_no_changes

    # ── main path (spec_count > 0) ──
    delta_prd_id = payload.derived_prd_id()
    # Stage 4: Save PRD (쿼리만 빌드 — 쓰기는 commit).
    scoped_prd_graph = scope_graph(
        prd_graph, project_key=db_project, doc_label="PRD_Document", new_doc_id=delta_prd_id
    )
    save_prd_query, save_prd_params = build_save_prd_query(scoped_prd_graph, project_name=db_project)

    # [2026-05 데이터 손실 방지] orphan master 가드 — prd_state(저장 전 fetch) 로 결정.
    # 기존 동작 보존: orphan 이면 save_prd 만 실행하고 raise (merge agent 호출 안 함).
    is_orphan = (
        not prd_state["master_content"].strip() and prd_state.get("prd_total", 0) > 1
    )
    if is_orphan:
        # [2026-06 P0-6 / C] 자기증식 트랩 해소.
        # 기존: delta 를 **쓴 뒤** raise → prd_total 증가 → 다음 회의도 orphan 재진입 →
        # 매 회의 delta 만 쌓이고 master 영영 미생성(갇힘).
        # 수정: (a) 실질 추출 문서면 빈 master 를 그 문서로 부트스트랩 — master 가 비어
        # 있으므로 덮어써도 잃을 것이 없고(빈→채움), 과거 delta 누적은 '[마스터 재구성]'
        # 으로 통합 가능(diagnostic 안내). 갇힘에서 자동 탈출.
        # (b) 비실질이면 **delta 저장 없이** raise — 증식 차단. post_meeting(R1)이
        # prd.mode='error' 로 강등해 FE 가 재생성 안내.
        if _is_substantive_prd_markdown(prd_markdown):
            orphan_merge_query, orphan_merge_params = build_merge_master_prd_query(
                project_name=db_project,
                merged_content=prd_markdown,
                latest_delta_id=prd_state.get("latest_id") or delta_prd_id,
                cleanup_at_version_count=prd_state.get("prd_total", 0),
            )
            result = PrdResult(
                delta_prd_id=delta_prd_id,
                master_prd_id=prd_master_id(db_project),
                mode="first_run",
                diagnostic={
                    "orphan_recovered": True,
                    "spec_count": spec_count,
                    "reason": (
                        f"master 가 비정상적으로 비어 있던 상태(과거 delta {prd_state.get('prd_total', 0)}개)를 "
                        "이번 추출 문서로 복구했습니다. 과거 회의 누적을 합치려면 [마스터 재구성]을 실행하세요."
                    ),
                },
            )

            async def _commit_orphan_recover() -> PrdResult:
                if save_prd_query:
                    await ctx.neo4j.run_cypher(save_prd_query, save_prd_params)
                await ctx.neo4j.run_cypher(orphan_merge_query, orphan_merge_params)
                logger.warning(
                    "PRD orphan recovered: 빈 master 를 추출 문서로 부트스트랩 "
                    "(project=%s, prd_total=%d) — [마스터 재구성] 으로 과거 delta 통합 가능.",
                    payload.project_name, prd_state.get("prd_total", 0),
                )
                return result
            return _commit_orphan_recover

        async def _commit_orphan() -> PrdResult:
            # delta 저장 없이 raise — save-then-raise 가 prd_total 을 늘려 매 회의 orphan
            # 재진입하던 자기증식 차단.
            logger.error(
                "PRD orphan master detected: project=%s, prd_total=%d, latest_id=%s. "
                "추출 문서도 비실질이라 자동 복구 불가 — 쓰기 없이 중단.",
                payload.project_name,
                prd_state.get("prd_total", 0),
                prd_state.get("latest_id"),
            )
            raise RuntimeError(
                "이전 PRD 마스터 데이터가 비정상적으로 사라진 상태입니다. "
                "Plan 페이지의 [마스터 재구성] 버튼으로 과거 회의들에서 마스터를 복구한 뒤 "
                "다시 시도해주세요."
            )
        return _commit_orphan

    # [2026-05-25 hotfix] first_run 에서도 merge LLM 호출 (섹션 형식 재구성 보존).
    filter_data = filter_affected_prd_sections(
        master_content=prd_state["master_content"],
        latest_content=prd_state["latest_content"] or prd_markdown,
        impact=impact,
    )
    filter_data["master_prd_details"] = prd_state.get("master_prd_details") or []
    await ctx.emit_stage("prd_merge")
    agent_text = await call_prd_merge_agent(ctx, filter_data)
    reassembled = reassemble_master(filter_data, agent_text)

    # [2026-06 빈-merge 방어] main path(spec_count>0)에서 merge LLM 이 빈/공백을 토해내면
    # merged_content 가 비어 build_merge_master_prd_query 가 ValueError → job 실패 + arq 무한
    # 재시도 + PRD master 미생성(누락). spec_count==0 가지가 prd_markdown 으로 부트스트랩하는
    # 것과 대칭으로: (1) 기존 master 가 있으면 보존(빈값으로 덮어쓰기 방지), (2) 없으면(콜드
    # 스타트) 실질 prd_markdown 으로 부트스트랩. 둘 다 불가할 때만 그대로 둬 ValueError 로 차단.
    if not (reassembled.get("merged_content") or "").strip():
        if prd_state["master_content"].strip():
            logger.warning(
                "prd main-path empty merge: merge agent 빈 출력 → 기존 master 보존(no overwrite) "
                "(project=%s version=%s).", payload.project_name, payload.version,
            )
            reassembled["merged_content"] = prd_state["master_content"]
        elif _is_substantive_prd_markdown(prd_markdown):
            logger.warning(
                "prd main-path empty merge (first-run): merge agent 빈 출력 → 실질 prd_markdown "
                "으로 부트스트랩 (project=%s version=%s).", payload.project_name, payload.version,
            )
            reassembled["merged_content"] = prd_markdown

    # Stage 8: Master PRD Cypher (쿼리만 빌드).
    _last_cleanup_count = (
        prd_state.get("cleanup_at_version_count") or prd_state["prd_total"]
    )
    merge_query, merge_params = build_merge_master_prd_query(
        project_name=db_project,
        merged_content=reassembled["merged_content"],
        latest_delta_id=prd_state.get("latest_id") or delta_prd_id,
        cleanup_at_version_count=_last_cleanup_count,
    )

    # [L1-3] 임계 버전마다 cleanup (LLM — 쓰기 쿼리만 빌드).
    _cleanup_interval = int(os.environ.get("PRD_CLEANUP_VERSION_INTERVAL", "5"))
    cleaned_md = await run_prd_cleanup_if_due(
        ctx,
        current_master_md=reassembled["merged_content"],
        prd_total=prd_state["prd_total"],
        last_cleanup_count=_last_cleanup_count,
        interval=_cleanup_interval,
    )
    cleanup_query = None
    cleanup_params = None
    if cleaned_md is not None:
        cleanup_query, cleanup_params = build_merge_master_prd_query(
            project_name=db_project,
            merged_content=cleaned_md,
            latest_delta_id=prd_state.get("latest_id") or delta_prd_id,
            cleanup_at_version_count=prd_state["prd_total"],   # baseline 갱신
        )

    master_prd_id = prd_master_id(db_project)

    # [2026-05-27 R3] 생성 경로 품질 게이트 — merged_content 를 lint (순수, 쓰기 무관).
    from app.pipelines.prd_lint import lint_prd
    _lint = lint_prd(reassembled["merged_content"])
    prd_lint_diag = {
        "score": _lint.score,
        "error_count": sum(1 for i in _lint.issues if i.severity == "error"),
        "warning_count": sum(1 for i in _lint.issues if i.severity == "warning"),
        "info_count": sum(1 for i in _lint.issues if i.severity == "info"),
        "issues": [
            {"code": i.code, "severity": i.severity, "message": i.message, "hint": i.hint}
            for i in _lint.issues
        ],
    }
    if prd_lint_diag["error_count"]:
        logger.warning(
            "prd pipeline lint errors: project=%s score=%.2f errors=%d codes=%s",
            payload.project_name, _lint.score, prd_lint_diag["error_count"],
            [i["code"] for i in prd_lint_diag["issues"] if i["severity"] == "error"],
        )

    result = PrdResult(
        delta_prd_id=delta_prd_id,
        master_prd_id=master_prd_id,
        mode="first_run" if filter_data["is_first_run"] else "incremental",
        diagnostic={
            "parsed_cps": {
                "pure_markdown_size": len(parsed["pure_markdown"]),
                "problems_count": len(parsed["problems"].split("\n")) if parsed["problems"] else 0,
            },
            "filter": filter_data["_diagnostic"],
            "reassemble": reassembled["_diagnostic"],
            "impact": impact,
            "prd_lint": prd_lint_diag,
        },
    )

    async def _commit_main() -> PrdResult:
        # 쓰기 순서 = 기존 run_prd_merge 동일: save_prd → master merge → (cleanup).
        if save_prd_query:
            await ctx.neo4j.run_cypher(save_prd_query, save_prd_params)
        await ctx.neo4j.run_cypher(merge_query, merge_params)
        if cleaned_md is not None:
            await ctx.neo4j.run_cypher(cleanup_query, cleanup_params)
            logger.info(
                "prd cleanup applied: project=%s prd_total=%d in=%d out=%d",
                payload.project_name, prd_state["prd_total"],
                len(reassembled["merged_content"]), len(cleaned_md),
            )
        return result
    return _commit_main


async def run_prd_merge(
    ctx: PipelineContext, payload: PrdInput, extract: Dict[str, Any]
) -> PrdResult:
    """PRD 병합 — compute(읽기+LLM) 직후 commit(쓰기). 단일/직접 호출의 관측 동작은 기존과
    동일 (Cypher 순서 fetch → save_prd → master merge, LLM 순서 impact → merge).
    배치 오버랩은 post_meeting_pipeline_job 이 compute 를 CPS 병합과 동시 실행한다.
    """
    commit = await _prd_merge_compute(ctx, payload, extract)
    return await commit()

async def run_prd_pipeline(ctx: PipelineContext, payload: PrdInput) -> PrdResult:
    """PRD 파이프라인 (extract + merge 합성). 단일 업로드/직접 호출용 — 기존 동작 그대로.

    batch 파이프라이닝에서는 post_meeting_pipeline_job 이 extract(캐시 가능) 와 merge
    를 분리 호출하므로 이 합성 함수를 거치지 않는다.
    """
    extract = await run_prd_extract(ctx, payload)
    return await run_prd_merge(ctx, payload, extract)
