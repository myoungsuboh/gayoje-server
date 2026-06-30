from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Dict

from app.pipelines.base import (
    PipelineContext,
    canonicalize_graph,
    canonicalize_meeting_content,
)
from app.pipelines.cps_pipeline.agents import (
    call_cps_agent,
    call_impact_analyzer,
    call_merge_agent,
    fetch_master_and_latest,
    reassemble_master,
)
from app.pipelines.cps_pipeline.cypher import (
    build_merge_master_query,
    build_save_cps_query,
    build_save_meeting_log_query,
)
from app.pipelines.cps_pipeline.sections import filter_affected_sections
from app.pipelines.cps_pipeline.types import CpsInput, CpsResult

logger = logging.getLogger(__name__)


def _canonicalize_payload(payload: CpsInput) -> CpsInput:
    """meeting_content 정규화 (CRLF→LF 등). idempotent — 어느 진입점에서 호출돼도 안전."""
    normalized_content = canonicalize_meeting_content(payload.meeting_content)
    if normalized_content != payload.meeting_content:
        payload = replace(payload, meeting_content=normalized_content)
    return payload


async def run_cps_extract(ctx: PipelineContext, payload: CpsInput) -> Dict[str, Any]:
    """CPS 추출 단계 — 순수 LLM (cps_agent). **Neo4j 쓰기 0건.**

    [batch 파이프라이닝] 이 단계 결과(graph)는 그 회의록 원문에만 의존하고 누적
    master 와 무관하다. 따라서 prefetch 로 미리 돌려 캐시해도 is_latest/master 선택
    로직에 영향 0 — 데이터 안전의 근거. **모든 DB 쓰기는 run_cps_merge 에 있다.**
    """
    payload = _canonicalize_payload(payload)
    logger.info(
        "cps extract start: project=%s version=%s key=%s",
        payload.project_name,
        payload.version,
        ctx.idempotency_key,
    )
    # [2026-05-26 perf C] sub-stage 마커 — FE 가 "지금 어디까지" 표시.
    await ctx.emit_stage("cps_extract")
    graph = await call_cps_agent(ctx, payload)
    return canonicalize_graph(graph)


async def run_cps_merge(
    ctx: PipelineContext, payload: CpsInput, graph: Dict[str, Any]
) -> CpsResult:
    """CPS 병합 단계 — 미리 계산된 graph 를 받아 DB 쓰기 + impact + merge 수행.

    **모든 Neo4j 쓰기는 이 함수 안에서만** 일어난다 (save_log → save_cps → fetch →
    merge). 순서·동작은 기존 run_cps_pipeline 과 동일. batch 에서 버전별로 순차 실행돼
    누적 master 무결성을 보존한다 (prefetch 로 앞당기는 것은 extract 뿐).
    """
    payload = _canonicalize_payload(payload)
    logger.info(
        "cps merge start: project=%s version=%s key=%s",
        payload.project_name,
        payload.version,
        ctx.idempotency_key,
    )

    # [멀티테넌시] 모든 DB project property/id 는 스코프 키 기준. LLM 프롬프트/마크다운은
    # 깨끗한 payload.project_name 을 그대로 쓴다 (sentinel 누수 방지).
    db_project = payload.project_key()
    delta_cps_id = payload.derived_cps_id()

    save_log_query, save_log_params = build_save_meeting_log_query(payload)
    # [2026-05-27] extract 분리로 기존 perf B 의 gather(cps_agent, save_log) 는 해체됨.
    # save_log 는 graph 와 무관(Meeting_Log 노드는 raw 미팅 본문만 저장)하므로 단독
    # 실행해도 DB end-state 동일 — 잃는 것은 ~50-100ms overlap 뿐.
    await ctx.neo4j.run_cypher(save_log_query, save_log_params)

    # [멀티테넌시] 저장 직전 그래프 스코프 적용 — 모든 노드 project=스코프 키,
    # LLM 생성 CPS_Document delta id 를 서버 authoritative 스코프 id 로 재조정
    # (동명 팀/개인 delta MERGE 충돌 방지). 개인은 무변환(identity).
    # 결과(result.cps_graph)는 원본 graph 유지 — 저장본만 스코프 (PRD 단계는 자체 스코프).
    from app.core.project_scope import scope_graph
    scoped_graph = scope_graph(
        graph, project_key=db_project, doc_label="CPS_Document", new_doc_id=delta_cps_id
    )

    save_cps_query, save_cps_params = build_save_cps_query(
        scoped_graph, project_name=db_project
    )
    if save_cps_query:
        await ctx.neo4j.run_cypher(save_cps_query, save_cps_params)

    cps = await fetch_master_and_latest(ctx, db_project)

    # [2026-05 데이터 손실 방지] orphan master 가드 — master 빈데 이전 CPS 존재.
    # cps_total > 1 = 이미 누적된 history 있는데 master 가 비정상 사라진 상태.
    # 그대로 진행 시 누적 데이터 덮어써짐 → raise.
    if not cps["master_content"].strip() and cps.get("cps_total", 0) > 1:
        logger.error(
            "CPS orphan master detected: project=%s, cps_total=%d, latest_id=%s. "
            "Master node missing but historical CPS documents exist — refusing "
            "to overwrite to prevent data loss.",
            payload.project_name,
            cps.get("cps_total", 0),
            cps.get("latest_id"),
        )
        raise RuntimeError(
            "이전 CPS 마스터 데이터가 비정상적으로 사라진 상태입니다. "
            "현재 회의록을 처리하면 누적된 Problem/Solution 이 모두 덮어쓰여 손실되므로 "
            "안전을 위해 중단합니다. 관리자에게 문의하거나 회의록 삭제로 마스터 "
            "재빌드를 시도해주세요."
        )

    # [2026-05-25 hotfix] perf B (first_run LLM skip) revert — 사용자 보고:
    # PRD/CPS master 본문이 비어 표시. merge_agent 가 LLM 으로 섹션 형식을 다듬는
    # 단계인데 skip 시 FE 의 섹션 split 과 출력 형식 불일치 가능성. 안전을 위해
    # 기존 흐름 복원 — first_run 에서도 impact + merge LLM 호출.
    await ctx.emit_stage("cps_impact")
    impact = await call_impact_analyzer(
        ctx, cps["master_probs"], cps["latest_content"] or payload.meeting_content
    )
    filter_data = filter_affected_sections(
        master_content=cps["master_content"],
        latest_content=cps["latest_content"] or payload.meeting_content,
        impact=impact,
    )
    await ctx.emit_stage("cps_merge")
    agent_text = await call_merge_agent(ctx, filter_data)
    reassembled = reassemble_master(filter_data, agent_text)

    merge_query, merge_params = build_merge_master_query(
        project_name=db_project,
        merged_content=reassembled["merged_content"],
        latest_delta_id=cps.get("latest_id") or delta_cps_id,
    )
    await ctx.neo4j.run_cypher(merge_query, merge_params)

    from app.core.project_scope import cps_master_id
    master_id = cps_master_id(db_project)
    # [2026-05-25] CPS Agent 의 추출 모드 — FE 가 사용자에게 표시.
    extraction_mode = str(graph.get("_extraction_mode") or "strict")
    extraction_warning = graph.get("_extraction_warning")
    return CpsResult(
        meeting_log_id=payload.log_id(),
        delta_cps_id=delta_cps_id,
        master_cps_id=master_id,
        mode="first_run" if filter_data["is_first_run"] else "incremental",
        diagnostic={
            "filter": filter_data["_diagnostic"],
            "reassemble": reassembled["_diagnostic"],
            "impact": impact,
        },
        cps_graph=graph,
        extraction_mode=extraction_mode,
        extraction_warning=extraction_warning,
    )


async def run_cps_pipeline(ctx: PipelineContext, payload: CpsInput) -> CpsResult:
    """CPS 파이프라인 (extract + merge 합성). 단일 업로드/직접 호출용 — 기존 동작 그대로.

    batch 파이프라이닝에서는 post_meeting_pipeline_job 이 extract(캐시 가능) 와 merge
    를 분리 호출하므로 이 합성 함수를 거치지 않는다.
    """
    graph = await run_cps_extract(ctx, payload)
    return await run_cps_merge(ctx, payload, graph)
