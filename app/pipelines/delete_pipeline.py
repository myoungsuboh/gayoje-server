"""
deleteProject + deleteMeeting (rebuild 포함) 파이프라인 — PR10.

[스테이지 매핑]

deleteProject:
- ExecuteQuery graphDb6 → `delete_project` (1개 Cypher)
  루트 노드 + 5-hop 자식 노드 모두 DETACH DELETE.

deleteMeeting (11 stage):
- Stage 1 Delete Nodes Code → `_build_delete_cypher` (Meeting_Log + delta + master 삭제)
- Stage 2 Get Remaining Deltas → `_fetch_remaining_deltas`
- Stage 3 Prepare Rebuild Data → `_prepare_rebuild_data` (delta marker 로 wrap)
- Stage 4 (분기) → `has_any` 체크
- Stage 5 Rebuild CPS Agent → `call_rebuild_cps` (prompts/rebuild_cps.md)
- Stage 6 After CPS Rebuild Code → `_post_process_rebuild` (codeblock strip + skip 결정)
- Stage 7 Save Rebuilt CPS → `_save_rebuilt_master_cps`
- Stage 8 Rebuild PRD Agent → `call_rebuild_prd` (prompts/rebuild_prd.md)
- Stage 9 After PRD Rebuild Code → `_post_process_rebuild`
- Stage 10 Save Rebuilt PRD → `_save_rebuilt_master_prd`
- Stage 11 Respond → DeleteMeetingResult

[보안]
삭제 Cypher 는 LLM 결과 보간 없이 parameter binding 또는 server-controlled
escape 만 사용. id 생성 시 project_name 의 dot 만 `_` 로 변환.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.pipelines.base import (
    PipelineContext,
    strip_code_blocks,
    strip_template_placeholders,
)

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


# ─── deleteProject ──────────────────────────────────────────────


_DELETE_PROJECT_CYPHER = """\
// 1. 루트 노드 + 5-hop 자손 노드 수집
MATCH (root)
WHERE root.project = $project
OPTIONAL MATCH (root)-[*1..5]->(child)
WITH collect(DISTINCT root) AS roots, collect(DISTINCT child) AS raw_children

// 2. children 에서 null 제외 후 root + children 통합. **삭제 전에** 개수 캡처.
WITH roots,
     [c IN raw_children WHERE c IS NOT NULL] AS children
WITH roots + children AS all_nodes,
     size(roots) + size(children) AS deleted_count

// 3. UNWIND + DETACH DELETE (deleted_count 는 이미 캡처돼 안전)
UNWIND all_nodes AS n
DETACH DELETE n
RETURN deleted_count
"""

# [버그 수정 — 2026-05-18]
# _DELETE_PROJECT_CYPHER 는 `project` property 가 있는 도메인 노드(Meeting_Log /
# CPS_Document / PRD_Document / API / Entity / ArchService / Skill 등) 만 삭제한다.
# 하지만 ownership_repository.claim_project 가 만든 `:Project {name: $project}`
# 노드는 `project` property 가 아닌 `name` property 를 가지므로 이 Cypher 로는
# 지워지지 않는다. 결과적으로 도메인 데이터는 사라져도 `:Project` 노드 +
# `(:User)-[:OWNS]->(:Project)` 관계가 그대로 남아 `/auth/me/projects` 가 계속
# 같은 프로젝트를 반환했다 — 사용자 화면에는 "삭제했는데 프로젝트가 안 사라짐".
# 두 번째 Cypher 로 Project 노드를 DETACH DELETE 해 OWNS 관계까지 함께 제거.
# Phase 2D: owner_email 조건 추가로 다른 유저의 동명 프로젝트 보호.
_DELETE_PROJECT_NODE_CYPHER = """\
MATCH (p:Project {name: $project, owner_email: $email})
DETACH DELETE p
"""


async def delete_project(
    ctx: PipelineContext, project_name: str, team_id: str = ""
) -> Dict[str, Any]:
    """
    프로젝트의 모든 노드를 5-hop 으로 수집하여 삭제. deleted_count 는 root 자체 + 자손 합.

    호출자 호환 위해 응답 키는 `child_count` 유지 (실제로는 root 포함된 전체 카운트).

    [2026-05-18 버그 수정]
    이전엔 도메인 노드 (`project` property 보유) 만 삭제하고 `:Project` 노드 +
    OWNS 관계가 남아 사용자 프로젝트 목록에 좀비로 표시되던 버그. 이제 도메인
    데이터 삭제 후 Project 노드/관계도 함께 제거.

    [Phase 2D 멀티테넌시] Project 노드 매칭에 `ctx.user_email` 도 함께 사용 —
    같은 이름의 다른 유저 프로젝트는 보호. ctx.user_email 미설정 시 빈 문자열로
    매칭돼 노드 삭제 실패(좀비 재발) → 호출자는 반드시 인증된 email 로 ctx 채울 것.

    [멀티테넌시 — 도메인 노드 스코프] 도메인 노드의 project property 는 스코프 키
    (개인=이름, 팀=sentinel 합성)라, 도메인 삭제는 db_project(scoped) 로 매칭해
    동명 개인/팀 데이터를 서로 건드리지 않는다. ownership `:Project` 노드는 raw
    이름 + owner_email 로 유지되므로 개인 정리는 raw 이름으로 수행(팀 Project 노드
    정리는 ownership 레이어 소관 — 여기선 도메인 데이터만 격리 삭제).
    """
    if not project_name or not project_name.strip():
        raise ValueError("project_name 은 비어 있을 수 없습니다.")
    from app.core.project_scope import scoped_project
    db_project = scoped_project(project_name, team_id)
    # 1) 도메인 데이터 (project property = 스코프 키) 5-hop 삭제.
    records = await ctx.neo4j.run_cypher(
        _DELETE_PROJECT_CYPHER, {"project": db_project}
    )
    deleted = (records[0] or {}).get("deleted_count", 0) if records else 0
    # 2) (개인) Project 노드 + (:User)-[:OWNS]->(:Project) 관계 제거.
    # 별도 Cypher 로 순차 실행 — 두 query 가 모두 idempotent 라 부분 실패해도
    # 다음 호출로 수렴 (좀비 Project 면 1단계는 noop, 2단계가 정리).
    if not team_id:
        await ctx.neo4j.run_cypher(
            _DELETE_PROJECT_NODE_CYPHER,
            {"project": project_name, "email": ctx.user_email},
        )
    return {"status": "deleted", "project_name": project_name, "child_count": int(deleted)}


# ─── deleteMeeting ──────────────────────────────────────────────


@dataclass(frozen=True)
class DeleteMeetingInput:
    project_name: str
    version: str
    team_id: str = ""


@dataclass
class DeleteMeetingResult:
    status: str
    message: str
    project_name: str
    deleted_version: str
    remaining_cps_count: int
    remaining_prd_count: int
    cps_master_rebuilt: bool
    prd_master_rebuilt: bool


# Stage 1: Build delete Cypher (Meeting + Delta + Master)
# Delete Nodes 단계의 5-step OPTIONAL MATCH + FOREACH 체인을 parameter binding 으로.

_DELETE_MEETING_CYPHER = """\
// 1) Meeting_Log 삭제
OPTIONAL MATCH (log:Meeting_Log {id: $log_id})
WITH collect(log) AS _logs
FOREACH (n IN _logs | DETACH DELETE n)
WITH size(_logs) AS _d1

// 2) CPS Delta 삭제
OPTIONAL MATCH (cps:CPS_Document {id: $cps_delta_id})
WITH _d1, collect(cps) AS _cpss
FOREACH (n IN _cpss | DETACH DELETE n)
WITH _d1, size(_cpss) AS _d2

// 3) PRD Delta 삭제
OPTIONAL MATCH (prd:PRD_Document {id: $prd_delta_id})
WITH _d1, _d2, collect(prd) AS _prds
FOREACH (n IN _prds | DETACH DELETE n)
WITH _d1, _d2, size(_prds) AS _d3

// 4) Master CPS 삭제 (재구성 예정)
OPTIONAL MATCH (mcps:CPS_Document {id: $cps_master_id})
WITH _d1, _d2, _d3, collect(mcps) AS _mcpss
FOREACH (n IN _mcpss | DETACH DELETE n)
WITH _d1, _d2, _d3, size(_mcpss) AS _d4

// 5) Master PRD 삭제 (재구성 예정)
OPTIONAL MATCH (mprd:PRD_Document {id: $prd_master_id})
WITH _d1, _d2, _d3, _d4, collect(mprd) AS _mprds
FOREACH (n IN _mprds | DETACH DELETE n)
WITH _d1, _d2, _d3, _d4, size(_mprds) AS _d5

RETURN _d1+_d2+_d3+_d4+_d5 AS deleted_phases
"""


# [2026-06 R3] 재구성 분기 전용 삭제 — Meeting_Log + Delta 만 지우고 **Master 는 보존**.
# 이유: 재구성은 _SAVE_REBUILT_*_CYPHER 의 MERGE 가 master 를 덮어쓰므로 미리 삭제할 필요가
# 없다. 오히려 _DELETE_MEETING_CYPHER 로 master 까지 지우면, 한쪽(예: PRD)이 재구성 대상이
# 아닐 때(has_prd=False — 남은 delta 노드는 있으나 본문이 빈 손상) master 가 삭제만 되고 SAVE 가
# 없어 영구 소실된다('CPS 가득/PRD 빈'의 delete 경로 변종, 사용자 보고 버그). 따라서 재구성
# 분기에선 delta 만 삭제하고, SAVE 가 재구성 대상 master 만 덮어쓰며, 재구성 안 하는 master 는
# 기존 누적본을 그대로 보존한다. (삭제된 delta 로의 관계는 DETACH DELETE 가 정리해 dangling 없음.)
_DELETE_MEETING_DELTAS_ONLY_CYPHER = """\
// 1) Meeting_Log 삭제
OPTIONAL MATCH (log:Meeting_Log {id: $log_id})
WITH collect(log) AS _logs
FOREACH (n IN _logs | DETACH DELETE n)
WITH size(_logs) AS _d1

// 2) CPS Delta 삭제
OPTIONAL MATCH (cps:CPS_Document {id: $cps_delta_id})
WITH _d1, collect(cps) AS _cpss
FOREACH (n IN _cpss | DETACH DELETE n)
WITH _d1, size(_cpss) AS _d2

// 3) PRD Delta 삭제 (Master CPS/PRD 는 보존 — SAVE 의 MERGE 가 재구성 대상만 덮어씀)
OPTIONAL MATCH (prd:PRD_Document {id: $prd_delta_id})
WITH _d1, _d2, collect(prd) AS _prds
FOREACH (n IN _prds | DETACH DELETE n)
WITH _d1, _d2, size(_prds) AS _d3

RETURN _d1+_d2+_d3 AS deleted_phases
"""


def _derive_ids(project_key: str, version: str) -> Dict[str, str]:
    """삭제 대상 노드 id 들 — **생성 경로와 동일한 단일 빌더** 사용.

    [버그픽스] 이전엔 여기서만 (a) project 를 normalize 하고 (생성 경로는 raw),
    (b) master id 에 email_part 접두를 붙여(생성 경로는 미부착) → 운영(점 포함
    이름/ email 설정)에서 생성 id 와 mismatch → delta/master 를 못 찾아 중복·고아
    노드가 남던 잠재 버그. 이제 project_scope 빌더로 통일해 항상 일치.

    [멀티테넌시] project_key 는 이미 scoped_project() 를 거친 스코프 키
    (개인=이름, 팀=sentinel 합성).
    """
    from app.core.project_scope import (
        cps_delta_id, cps_master_id, meeting_log_id, prd_delta_id, prd_master_id,
    )
    return {
        "log_id": meeting_log_id(project_key, version),
        "cps_delta_id": cps_delta_id(project_key, version),
        "prd_delta_id": prd_delta_id(project_key, version),
        "cps_master_id": cps_master_id(project_key),
        "prd_master_id": prd_master_id(project_key),
    }


# Stage 2: 남은 delta 조회
# [2026-05] 트랜잭션 안전성 강화 — 삭제 *전* 시점에 호출.
#
# 이전 흐름: delete commit → fetch_remaining (이미 삭제된 후 조회) → LLM 호출 →
#         save_master. LLM 또는 save 실패 시 delta 삭제는 commit 됐는데 master
#         미갱신 → inconsistent state 잔존.
# 이후 흐름: 삭제 전 시점에 "이 버전이 삭제됐을 때 남을 delta" 시뮬레이션 →
#         LLM rebuild → 모두 성공한 후에야 delete + save atomic 트랜잭션 →
#         LLM 실패 시 delete 자체가 안 일어남 (사용자 재시도 가능).
#
# Cypher 변경: `cps.id <> $excluded_cps_id` 조건 추가 — 삭제 대상 delta 제외.
_FETCH_REMAINING_CYPHER = """\
// 삭제 *전* 시점에서 "이 버전 제외" 한 남은 CPS/PRD Delta 조회 (Master 제외).
// id ASC 로 정렬 (id 에 version 포함되어 시간 순서 보장).
OPTIONAL MATCH (cps:CPS_Document {project: $project})
  WHERE (cps.type IS NULL OR cps.type <> 'Master')
    AND cps.id <> $excluded_cps_id
WITH cps
ORDER BY cps.id ASC
WITH collect(cps) AS cps_nodes

OPTIONAL MATCH (prd:PRD_Document {project: $project})
  WHERE (prd.type IS NULL OR prd.type <> 'Master')
    AND prd.id <> $excluded_prd_id
WITH cps_nodes, prd
ORDER BY prd.id ASC
WITH cps_nodes, collect(prd) AS prd_nodes

RETURN
  [x IN cps_nodes WHERE x IS NOT NULL |
    {id: x.id, version: x.version, content: x.full_markdown}] AS cps_list,
  [x IN prd_nodes WHERE x IS NOT NULL |
    {id: x.id, version: x.version, content: x.full_markdown}] AS prd_list
"""


async def _fetch_remaining_deltas(
    ctx: PipelineContext,
    project_name: str,
    excluded_cps_id: str = "",
    excluded_prd_id: str = "",
) -> Dict[str, List[Dict[str, Any]]]:
    """
    삭제 전 시점에서 "특정 delta 를 제외" 한 남은 deltas 시뮬레이션 조회.

    excluded_cps_id / excluded_prd_id: 삭제 대상 delta 의 id. 빈 문자열이면 제외 안 함
    (delete cypher 실행 후 호출하던 옛 의미로 fallback).
    """
    records = await ctx.neo4j.run_cypher(
        _FETCH_REMAINING_CYPHER,
        {
            "project": project_name,
            "excluded_cps_id": excluded_cps_id,
            "excluded_prd_id": excluded_prd_id,
        },
    )
    row = records[0] if records else {}
    return {
        "cps_list": row.get("cps_list") or [],
        "prd_list": row.get("prd_list") or [],
    }


# Stage 3: Prepare Rebuild Data
def _join_deltas(deltas: List[Dict[str, Any]], label: str) -> str:
    """'Prepare Rebuild Data.joinDeltas' 헬퍼."""
    if not deltas:
        return ""
    parts: List[str] = []
    for i, d in enumerate(deltas):
        ver = d.get("version") or f"#{i + 1}"
        content = d.get("content") or ""
        parts.append(
            f"\n>>>>> {label} DELTA START (version: {ver}) >>>>>\n{content}\n"
            f"<<<<< {label} DELTA END <<<<<\n"
        )
    return "\n".join(parts)


def _prepare_rebuild_data(
    remaining: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, Any]:
    raw_cps = remaining.get("cps_list") or []
    raw_prd = remaining.get("prd_list") or []
    cps_list = [d for d in raw_cps if d and d.get("content")]
    prd_list = [d for d in raw_prd if d and d.get("content")]
    return {
        "cps_content": _join_deltas(cps_list, "CPS"),
        "prd_content": _join_deltas(prd_list, "PRD"),
        "has_cps": len(cps_list) > 0,
        "has_prd": len(prd_list) > 0,
        "has_any": (len(cps_list) > 0) or (len(prd_list) > 0),
        "remaining_cps_count": len(cps_list),
        "remaining_prd_count": len(prd_list),
        # [2026-05-27 데이터 손실 가드] 본문 필터와 무관하게 "남은 delta 노드"가
        # 실제로 존재하는지. has_any=False 이지만 has_remaining_nodes=True 면
        # "노드는 있는데 본문이 빔(데이터 손상)" → master 를 삭제하면 안 됨.
        "has_remaining_nodes": len(raw_cps) > 0 or len(raw_prd) > 0,
    }


# Stage 5/8: LLM Rebuild
def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


async def call_rebuild_cps(
    ctx: PipelineContext, cps_content: str, remaining_count: int
) -> str:
    prompt = _render(
        _load_prompt("rebuild_cps.md"),
        cps_content=cps_content,
        remaining_count=str(remaining_count),
    )
    result = await ctx.gemini.generate(prompt, temperature=0.1)
    return strip_template_placeholders(strip_code_blocks(result.text))


async def call_rebuild_prd(
    ctx: PipelineContext, prd_content: str, remaining_count: int
) -> str:
    prompt = _render(
        _load_prompt("rebuild_prd.md"),
        prd_content=prd_content,
        remaining_count=str(remaining_count),
    )
    result = await ctx.gemini.generate(prompt, temperature=0.1)
    return strip_template_placeholders(strip_code_blocks(result.text))


# Stage 6/9: post-process Agent output
def _post_process_rebuild(agent_output: str) -> str:
    """codeblock 마커 제거 + trim + placeholder leak 정리. 빈 출력은 그대로."""
    if not agent_output:
        return ""
    stripped = strip_code_blocks(agent_output).strip()
    return strip_template_placeholders(stripped)


# Stage 7/10: Save rebuilt master
_SAVE_REBUILT_CPS_CYPHER = """\
MERGE (master:CPS_Document {id: $master_id})
SET master.project = $project,
    master.version = 'Final',
    master.type = 'Master',
    master.is_latest = true,
    master.full_markdown = $content,
    master.updated_at = timestamp(),
    master.last_rebuild_reason = 'meeting_log_deletion'
RETURN master.id AS saved_id
"""

_SAVE_REBUILT_PRD_CYPHER = """\
MERGE (master:PRD_Document {id: $master_id})
SET master.project = $project,
    master.version = 'Final',
    master.type = 'Master',
    master.is_latest = true,
    master.full_markdown = $content,
    master.updated_at = timestamp(),
    master.last_rebuild_reason = 'meeting_log_deletion',
    // [2026-06] rebuild = PRD 컨텍스트 변경 → 이전 autofix 진단 무효 (merge 와 동일).
    master.autofix_needs_input = null,
    master.autofix_needs_at = null

// 마스터 CPS 와 연결 (있을 경우만) — Save Rebuilt PRD 단계와 동등.
// CPS 가 없으면 FOREACH 가 no-op (graceful).
WITH master
OPTIONAL MATCH (cps_m:CPS_Document {id: $cps_master_id})
FOREACH (ignore IN CASE WHEN cps_m IS NOT NULL THEN [1] ELSE [] END |
  MERGE (master)-[:BASED_ON]->(cps_m)
)
RETURN master.id AS saved_id
"""


async def _save_rebuilt_master_cps(
    ctx: PipelineContext, project_name: str, content: str, team_id: str = ""
) -> None:
    from app.core.project_scope import cps_master_id, scoped_project
    db_project = scoped_project(project_name, team_id)
    await ctx.neo4j.run_cypher(
        _SAVE_REBUILT_CPS_CYPHER,
        {"master_id": cps_master_id(db_project), "project": db_project, "content": content},
    )


async def _save_rebuilt_master_prd(
    ctx: PipelineContext, project_name: str, content: str, team_id: str = ""
) -> None:
    """
    재구성된 PRD master 저장 + 마스터 CPS 와 BASED_ON 관계 복원.
    'Save Rebuilt PRD' 단계의 Cypher 구현.
    """
    from app.core.project_scope import cps_master_id, prd_master_id, scoped_project
    db_project = scoped_project(project_name, team_id)
    await ctx.neo4j.run_cypher(
        _SAVE_REBUILT_PRD_CYPHER,
        {
            "master_id": prd_master_id(db_project),
            "project": db_project,
            "content": content,
            "cps_master_id": cps_master_id(db_project),
        },
    )


# ─── End-to-end orchestrator ────────────────────────────────────


async def run_delete_meeting_pipeline(
    ctx: PipelineContext, payload: DeleteMeetingInput
) -> DeleteMeetingResult:
    """
    Meeting 삭제 + (남은 delta 있으면) Master CPS/PRD 재구성.

    [2026-05 트랜잭션 안전성 — 재정렬]
    이전 흐름: delete commit → fetch remaining → LLM → save master.
        LLM 또는 save 실패 시 delta 삭제는 이미 commit 됐는데 master 미갱신 →
        inconsistent state 영구 잔존, 사용자 데이터 손실.
    이후 흐름:
      1. fetch 시뮬레이션 (삭제 전, payload.version 의 delta 제외)
      2. LLM rebuild (트랜잭션 시작 전 — 실패 시 delete 안 일어남)
      3. delete + save 를 단일 트랜잭션 안에서 atomic 실행
    효과: LLM 실패 → 사용자 재시도 가능 (delta 보존). Save Neo4j 일시 장애 →
    자동 롤백 → 동상.

    Empty 분기 (남은 delta 0):
      - delete 만 실행 (rebuild 안 함). 같은 트랜잭션이 아니어도 부분 commit 없음
        — 단일 cypher.
    """
    if not payload.project_name or not payload.version:
        raise ValueError("project_name + version 필수.")

    logger.info(
        "delete_meeting start: project=%s version=%s key=%s",
        payload.project_name,
        payload.version,
        ctx.idempotency_key,
    )

    # [멀티테넌시] 모든 노드 id/ project property 는 스코프 키 기준 (개인=이름 그대로).
    from app.core.project_scope import scoped_project
    db_project = scoped_project(payload.project_name, payload.team_id)
    ids = _derive_ids(db_project, payload.version)

    # [Step 1] 삭제 *전* 시점에서 "이 version 제외" 시뮬레이션 fetch.
    # 옛 흐름은 delete 후 fetch 였지만, 이제 LLM 실패 시 delete 안 일어나야 하므로
    # 미리 시뮬레이션. cypher 의 excluded_*_id 조건이 같은 결과 보장.
    remaining = await _fetch_remaining_deltas(
        ctx, db_project,
        excluded_cps_id=ids["cps_delta_id"],
        excluded_prd_id=ids["prd_delta_id"],
    )
    prep = _prepare_rebuild_data(remaining)

    # [Step 2] Empty 분기 — rebuild 없음.
    if not prep["has_any"]:
        # [2026-05-27 데이터 손실 가드] 남은 delta '노드'는 존재하는데 본문(full_markdown)
        # 이 모두 비어 있으면(데이터 손상 의심) → master 를 삭제하면 누적 데이터가
        # 영구 소실된다. "진짜 마지막 미팅(노드 0개)"과 구분해 삭제를 거부하고 보존.
        # (이전 버그: content 필터로 has_any=False → '남은 미팅 없음' 오판 → master 삭제.)
        if prep["has_remaining_nodes"]:
            logger.error(
                "delete_meeting BLOCKED — 남은 delta 노드 존재하나 본문 모두 빔(손상): "
                "project=%s version=%s. master 삭제 거부(보존).",
                payload.project_name, payload.version,
            )
            raise RuntimeError(
                "남은 회의록의 CPS/PRD 본문이 모두 비어 있습니다(데이터 손상 의심). "
                "이 상태로 삭제하면 누적된 마스터 데이터가 영구 소실되므로 중단합니다 — "
                "기존 데이터는 보존됩니다. 백업 복구 또는 회의록 재처리 후 다시 시도해주세요."
            )
        # 남은 delta 노드가 진짜 0개 = 마지막 미팅 삭제 → master 도 함께 삭제(정상).
        await ctx.neo4j.run_cypher(_DELETE_MEETING_CYPHER, ids)
        return DeleteMeetingResult(
            status="success",
            message="미팅 로그 삭제 완료 (남은 Delta 없음 - 마스터도 함께 삭제됨)",
            project_name=payload.project_name,
            deleted_version=payload.version,
            remaining_cps_count=0,
            remaining_prd_count=0,
            cps_master_rebuilt=False,
            prd_master_rebuilt=False,
        )

    # [Step 3] LLM rebuild — 트랜잭션 시작 전. 실패 시 delete 자체가 발생 안 함.
    # CRITICAL: LLM 이 빈 응답을 반환하면 raise — 절대 skip 하지 않음.
    #   skip 시 아래 트랜잭션에 DELETE 만 들어가고 SAVE 없이 Master 가 삭제되어
    #   데이터 영구 소실이 발생하므로, 빈 출력은 재시도 가능한 오류로 처리한다.
    cps_content_to_save: Optional[str] = None
    if prep["has_cps"]:
        cps_output = await call_rebuild_cps(
            ctx, prep["cps_content"], prep["remaining_cps_count"]
        )
        c = _post_process_rebuild(cps_output)
        if not c:
            raise RuntimeError(
                "CPS 재구성 실패: LLM 응답이 비어 있습니다. "
                "삭제를 중단합니다 — 기존 데이터는 보존됩니다. 잠시 후 다시 시도해주세요."
            )
        cps_content_to_save = c

    prd_content_to_save: Optional[str] = None
    if prep["has_prd"]:
        prd_output = await call_rebuild_prd(
            ctx, prep["prd_content"], prep["remaining_prd_count"]
        )
        c = _post_process_rebuild(prd_output)
        if not c:
            raise RuntimeError(
                "PRD 재구성 실패: LLM 응답이 비어 있습니다. "
                "삭제를 중단합니다 — 기존 데이터는 보존됩니다. 잠시 후 다시 시도해주세요."
            )
        prd_content_to_save = c

    # [Step 4] delete + save 를 atomic 트랜잭션으로 묶음.
    # LLM 모두 성공한 후에만 도달 — 실패하면 raise 되어 여기 안 옴 → delete 발생 X.
    # [멀티테넌시] master id / project property 모두 스코프 키 기준 — 생성 경로와 일치.
    # [2026-06 R3] 재구성 분기는 **delta 만 삭제**(master 보존)한다. master 는 아래 SAVE 의
    # MERGE 가 재구성 대상만 덮어쓰고, 재구성 안 하는 master(예: has_prd=False 일 때의 PRD)는
    # 기존 누적본이 보존된다 — master 까지 지우던 _DELETE_MEETING_CYPHER 는 한쪽만 SAVE 될 때
    # 반대쪽 master 를 영구 소실시켰다(사용자 보고 'CPS 가득/PRD 빈'의 delete 변종).
    from app.core.project_scope import cps_master_id, prd_master_id
    if (cps_content_to_save is None or prd_content_to_save is None) and prep["has_remaining_nodes"]:
        logger.warning(
            "delete_meeting: 한쪽 master 재구성 불가(cps=%s, prd=%s) — 해당 master 는 "
            "삭제하지 않고 기존 누적본 보존 (project=%s version=%s).",
            cps_content_to_save is not None, prd_content_to_save is not None,
            payload.project_name, payload.version,
        )
    operations: List[tuple[str, Dict[str, Any]]] = [(_DELETE_MEETING_DELTAS_ONLY_CYPHER, ids)]
    if cps_content_to_save:
        operations.append((
            _SAVE_REBUILT_CPS_CYPHER,
            {
                "master_id": cps_master_id(db_project),
                "project": db_project,
                "content": cps_content_to_save,
            },
        ))
    if prd_content_to_save:
        operations.append((
            _SAVE_REBUILT_PRD_CYPHER,
            {
                "master_id": prd_master_id(db_project),
                "project": db_project,
                "content": prd_content_to_save,
                "cps_master_id": cps_master_id(db_project),
            },
        ))

    run_tx = getattr(ctx.neo4j, "run_in_transaction", None)
    if callable(run_tx):
        await run_tx(operations)
    else:
        # 호환 fallback — run_in_transaction 미구현 ctx (옛 fake/proxy).
        # 이 경로는 atomic 보장 X 지만 옛 통합 테스트가 깨지지 않도록 유지.
        for cypher, params in operations:
            await ctx.neo4j.run_cypher(cypher, params)

    return DeleteMeetingResult(
        status="success",
        message="미팅 로그 삭제 및 마스터 재구성 완료",
        project_name=payload.project_name,
        deleted_version=payload.version,
        remaining_cps_count=prep["remaining_cps_count"],
        remaining_prd_count=prep["remaining_prd_count"],
        cps_master_rebuilt=cps_content_to_save is not None,
        prd_master_rebuilt=prd_content_to_save is not None,
    )


# ─── Master 강제 재구성 (복구용) ─────────────────────────────────────────────


@dataclass
class RebuildMasterResult:
    status: str
    project_name: str
    cps_rebuilt: bool
    prd_rebuilt: bool
    cps_delta_count: int
    prd_delta_count: int


async def run_rebuild_master_pipeline(
    ctx: PipelineContext,
    project_name: str,
    team_id: str = "",
) -> RebuildMasterResult:
    """
    기존 Delta 를 모두 병합해 Master CPS/PRD 를 재구성한다.

    삭제 파이프라인 중 LLM 빈 응답으로 Master 가 소실된 경우 복구용.
    Delta 는 보존된 채로 Master 만 새로 생성된다.

    [멀티테넌시] team_id 지정 시 해당 팀 스코프의 Delta/Master 만 대상.
    """
    if not project_name:
        raise ValueError("project_name 필수.")

    logger.info("rebuild_master start: project=%s team=%s", project_name, team_id or "-")

    from app.core.project_scope import cps_master_id, prd_master_id, scoped_project
    db_project = scoped_project(project_name, team_id)

    remaining = await _fetch_remaining_deltas(ctx, db_project)
    prep = _prepare_rebuild_data(remaining)

    if not prep["has_any"]:
        # [2026-05-26] AI Agent 사고 시나리오: CPS_Document/PRD_Document 노드는 있지만
        # full_markdown 등 본문 properties 가 모두 빈 상태. _prepare_rebuild_data 의
        # filter 가 'd.get("content")' truthy 검사로 모두 제외 → has_any=False.
        # 사용자에게 명확한 복구 안내.
        raise ValueError(
            f"'{project_name}' 프로젝트는 누적 Delta 의 본문이 모두 비어있어 "
            "자동 재구성이 불가능합니다 (데이터 손상 의심).\n\n"
            "복구 방법:\n"
            "  1) 이 프로젝트를 삭제 (홈 화면의 프로젝트 카드 메뉴)\n"
            "  2) 미팅 로그를 처음부터 다시 등록\n\n"
            "기술 정보: CPS_Document/PRD_Document 노드는 존재하지만 full_markdown 이 비어있어 "
            "LLM 재합성의 입력이 없습니다. 백업이 있다면 복구 가능 — 관리자에게 문의."
        )

    operations: List[tuple[str, Dict[str, Any]]] = []

    if prep["has_cps"]:
        cps_output = await call_rebuild_cps(
            ctx, prep["cps_content"], prep["remaining_cps_count"]
        )
        c = _post_process_rebuild(cps_output)
        if not c:
            raise RuntimeError("CPS 재구성 실패: LLM 응답이 비어 있습니다. 잠시 후 다시 시도해주세요.")
        operations.append((
            _SAVE_REBUILT_CPS_CYPHER,
            {"master_id": cps_master_id(db_project), "project": db_project, "content": c},
        ))

    if prep["has_prd"]:
        prd_output = await call_rebuild_prd(
            ctx, prep["prd_content"], prep["remaining_prd_count"]
        )
        c = _post_process_rebuild(prd_output)
        if not c:
            raise RuntimeError("PRD 재구성 실패: LLM 응답이 비어 있습니다. 잠시 후 다시 시도해주세요.")
        operations.append((
            _SAVE_REBUILT_PRD_CYPHER,
            {
                "master_id": prd_master_id(db_project),
                "project": db_project,
                "content": c,
                "cps_master_id": cps_master_id(db_project),
            },
        ))

    run_tx = getattr(ctx.neo4j, "run_in_transaction", None)
    if callable(run_tx):
        await run_tx(operations)
    else:
        for cypher, params in operations:
            await ctx.neo4j.run_cypher(cypher, params)

    logger.info(
        "rebuild_master done: project=%s cps=%s prd=%s",
        project_name, prep["has_cps"], prep["has_prd"],
    )
    return RebuildMasterResult(
        status="success",
        project_name=project_name,
        cps_rebuilt=prep["has_cps"],
        prd_rebuilt=prep["has_prd"],
        cps_delta_count=prep["remaining_cps_count"],
        prd_delta_count=prep["remaining_prd_count"],
    )
