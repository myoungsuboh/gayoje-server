"""
조회 라우트용 Cypher 모음 — PR9.

[엔드포인트 매핑]
- getCPS              → `get_master_cps`
- getPRD              → `get_master_prd`
- getDDD              → `get_ddd_graph`
- getSpack            → `get_spack_graph`
- getArchitecture     → `get_architecture_graph`
- getMeetingLogs      → `get_meeting_log`
- getMeetingVersions  → `get_meeting_versions`

모든 Cypher 는 문자열 보간 대신 `$param` 바인딩을 사용 (injection 안전).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from app.clients import neo4j_client
from app.core.project_scope import scoped_project
from app.pipelines.design_validator.api_payload import decode_apis_payload
from app.pipelines.design_validator.arch_detail import (
    decode_connections_auth,
    decode_services_detail,
)
from app.pipelines.design_validator.attributes import decode_entities_attributes
from app.pipelines.design_validator.ddd_detail import (
    decode_aggregates_detail,
    decode_domain_entities_detail,
    decode_domain_events_detail,
)

logger = logging.getLogger(__name__)


# ===== Cypher =====


_GET_CPS_CYPHER = """\
MATCH (m:CPS_Document {type: 'Master', is_latest: true})
WHERE m.project = $project

// 증분 병합 로직에 의해 이 마스터에 흡수된 하위 CPS 문서들의 ID 추적
OPTIONAL MATCH (m)-[:SYNTHESIZED_FROM]->(source:CPS_Document)

RETURN
    m.id AS master_id,
    m.version AS version,
    m.full_markdown AS content,
    m.updated_at AS last_updated,
    COALESCE(m.markdown_stale, false) AS markdown_stale,
    collect(DISTINCT source.id) AS absorbed_cps_ids
"""


# [2026-05] 사용자 직접 편집 — Master CPS 의 full_markdown 만 덮어쓰기.
# 검수 게이트 (auto_progress=false) 모드에서 사용자가 LLM 결과를 수정하고 다음
# stage 로 진행. is_latest=true 인 master 만 매칭 → 동일 project 의 단일 노드만 영향.
# user_edited_at 필드도 함께 갱신 — 운영 추적 (얼마나 자주 수정되는지).
#
# [2026-05-18 Phase 2 Optimistic Locking]
# $client_updated_at 이 주어지면 m.updated_at 과 일치하는 경우에만 갱신 →
# 동시 편집 시 두 번째 요청은 일치 실패로 빈 결과 → 호출자가 409 응답.
# $client_updated_at = NULL 이면 (legacy / FE 미갱신 케이스) 조건 skip — 기존 동작.
_UPDATE_CPS_MARKDOWN_CYPHER = """\
MATCH (m:CPS_Document {project: $project, type: 'Master', is_latest: true})
WHERE $client_updated_at IS NULL OR m.updated_at = $client_updated_at
SET m.full_markdown = $content,
    m.updated_at = timestamp(),
    m.user_edited_at = timestamp(),
    // [Phase 3.5a] 사용자가 markdown 을 직접 편집했으므로 graph 와의 desync 해소 의도.
    // FE banner 사라짐. 실제 graph 노드 summary 와는 다를 수 있지만 그건 사용자 선택.
    m.markdown_stale = false
RETURN m.id AS master_id, m.updated_at AS last_updated
"""


# [2026-05] PRD 도 동일 패턴 — Master PRD full_markdown 만 덮어쓰기.
# Epic/Story 그래프 노드는 그대로 (markdown 은 display only).
#
# [Phase 3.6] PRD 변경 → Design (SPACK/DDD/Arch) 은 옛 PRD 기준이라 stale.
# 같은 트랜잭션에 Project.design_source_stale=true 마킹 (원자성 보장).
#
# [2026-05-18 Phase 2 Optimistic Locking] CPS 와 동일 정책.
_UPDATE_PRD_MARKDOWN_CYPHER = """\
MATCH (m:PRD_Document {project: $project, type: 'Master', is_latest: true})
WHERE $client_updated_at IS NULL OR m.updated_at = $client_updated_at
SET m.full_markdown = $content,
    m.updated_at = timestamp(),
    m.user_edited_at = timestamp(),
    m.markdown_stale = false
WITH m
MERGE (p:Project {name: m.project})
SET p.design_source_stale = true,
    p.design_source_stale_at = timestamp()
RETURN m.id AS master_id, m.updated_at AS last_updated
"""

# [2026-06-05] stale 마킹 없는 변형 — 자동 정리(cleanup_master_prd_job) 전용.
# cleanup 은 PRD 를 의미적으로 바꾸는 게 아니라 누더기 markdown 을 압축·정리만 한다.
# 따라서 design_source_stale 를 재마킹하면 안 된다(사용자가 막 설계를 재생성했는데
# 백그라운드 cleanup 이 또 '옛 PRD 기준' 배너를 띄우는 모순 방지). user_edited_at 도
# 안 찍는다 — cleanup 은 사용자 편집이 아니다.
_UPDATE_PRD_MARKDOWN_NO_STALE_CYPHER = """\
MATCH (m:PRD_Document {project: $project, type: 'Master', is_latest: true})
WHERE $client_updated_at IS NULL OR m.updated_at = $client_updated_at
SET m.full_markdown = $content,
    m.updated_at = timestamp(),
    m.markdown_stale = false
RETURN m.id AS master_id, m.updated_at AS last_updated
"""


# [Phase 3.5a] markdown_stale 플래그 dismiss — 사용자가 banner 무시.
# markdown 은 변경 안 됨 (graph 와 desync 상태 유지) — 사용자가 명시적으로 OK 함.
_DISMISS_CPS_STALE_CYPHER = """\
MATCH (m:CPS_Document {project: $project, type: 'Master', is_latest: true})
SET m.markdown_stale = false
RETURN m.id AS master_id
"""

_DISMISS_PRD_STALE_CYPHER = """\
MATCH (m:PRD_Document {project: $project, type: 'Master', is_latest: true})
SET m.markdown_stale = false
RETURN m.id AS master_id
"""


# ─── [2026-05 Phase 3.6] Design source-stale ───────────────────────
#
# [목적] PRD 가 갱신됐는데 Design (SPACK/DDD/Architecture) 은 아직 옛 PRD 기준.
# CPS/PRD 의 markdown_stale 과 같은 desync 알림을 Design 단계에도 확장.
#
# [스키마 결정] CPS/PRD 는 Master 노드(:CPS_Document / :PRD_Document {type:'Master'})
# 에 플래그를 두지만, Design 은 Master 문서 노드가 없고 API/Entity/Aggregate/
# ArchService 등 그래프 노드 컬렉션으로만 존재. 또한 createSpack 이 SPACK/DDD/
# Architecture 3개를 한 번에 재생성 → 카테고리별 플래그가 아닌 단일 플래그면 충분.
# 따라서 (:Project {name}) 노드에 design_source_stale 한 쌍을 둔다.
#
# [생명주기]
#   PRD 갱신/노드수정    → MERGE Project + design_source_stale = true
#   사용자 dismiss      → false (FE banner 닫기, 실제 design 재생성 안 함)
#   createSpack 성공    → false (실제로 최신 PRD 반영했으므로 자동 reset)
_MARK_DESIGN_STALE_CYPHER = """\
MERGE (p:Project {name: $project})
SET p.design_source_stale = true,
    p.design_source_stale_at = timestamp()
RETURN p.name AS project
"""

_DISMISS_DESIGN_STALE_CYPHER = """\
MATCH (p:Project {name: $project})
SET p.design_source_stale = false
RETURN p.name AS project
"""

# [Phase 3.6] FE 가 design 페이지 진입 시 한 번 호출 — banner 표시 여부 결정.
# Project 노드가 없으면 (= 아직 아무 산출물도 없음) stale=false 로 응답.
_GET_DESIGN_STALE_CYPHER = """\
OPTIONAL MATCH (p:Project {name: $project})
RETURN
    COALESCE(p.design_source_stale, false) AS design_source_stale,
    p.design_source_stale_at AS design_source_stale_at,
    p.design_last_generated_at AS design_last_generated_at
"""


# [2026-06] Design 품질 체크 — MAPPED_TO.role 제약 검증.
#
# LLM 프롬프트(design_ddd.md)는 spack_entity_mapping 에서 각 Aggregate 에 대해
# aggregate_root 를 정확히 1개 지정하도록 지시. 실제로 2개 이상이거나 0개인 경우
# 도메인 모델 설계 오류. 이 쿼리는 그 위반을 직접 Neo4j 에서 탐지.
_GET_DESIGN_QUALITY_CYPHER = """\
MATCH (agg:Aggregate {project: $project})
OPTIONAL MATCH (e:Entity)-[r:MAPPED_TO {role: 'aggregate_root'}]->(agg)
WITH agg, collect(e.id) AS roots
WHERE size(roots) <> 1
RETURN
    agg.id AS aggregate_id,
    COALESCE(agg.name, agg.id) AS aggregate_name,
    size(roots) AS root_count,
    roots AS root_entity_ids,
    CASE WHEN size(roots) = 0 THEN 'missing_aggregate_root'
         ELSE 'multiple_aggregate_roots' END AS violation_type
ORDER BY agg.id
"""


# [B — 2026-06] 그래프 임팩트 분석 — DERIVED_FROM / IMPLEMENTS 엣지 traversal.
#
# design 파이프라인은 설계 노드에 "어떤 Story 에서 파생됐는지" 를 엣지로 기록하는데,
# 노드 종류마다 엣지 타입이 다르다:
#   - API    : (API)-[:IMPLEMENTS]->(Story)
#   - Entity / ArchService / ArchDatabase : (node)-[:DERIVED_FROM]->(Story)
#   - Screen : (Screen)-[:RENDERS]->(Story)  → screen_layer(L4a)가 별도 담당
# 이 쿼리는 그 그래프를 역방향으로 읽어, "design 재생성 이후 수정된 Epic/Story →
# 영향받는 design 노드" 를 추출한다.
#
# [핵심 파라미터]
# $project : 도메인 스코프 키 (n.project 와 동일 — 개인=이름, 팀=sentinel 합성)
# $since   : design_last_generated_at (ms) — 이 시각 이후에 user_edited_at 있는 노드만.
#            0 이면 전체 이력(최초 or design 미생성 프로젝트).
#
# [방향] (design_node)-[:DERIVED_FROM|IMPLEMENTS]->(Story)
#        → MATCH (story)<-[:DERIVED_FROM|IMPLEMENTS]-(d) 로 역순 매칭.
_GET_DESIGN_IMPACT_CYPHER = """\
// [2026-06 Impact Cascade v2]
// [Fix-A] Epic 편집 시 CONTAINS→Story 경로 추가 (기존: Epic의 DERIVED_FROM = 항상 0건).
// [Fix-B] API 는 DERIVED_FROM 이 아니라 IMPLEMENTS 로 Story 에 연결됨. L1 traversal 에
//   IMPLEMENTS 를 추가하지 않으면 API 가 design_layer 에 절대 안 잡히고, 이에 의존하는
//   L3(HANDLED_BY)/L4b(CALLS_API)/L5(api_chain)/error_cases 가 전부 dead 가 됨.
//   Screen 은 screen_layer(L4a, RENDERS)가 담당하므로 L1 화이트리스트에서 제외.
// Layer1: Story/Epic → Design (DERIVED_FROM|IMPLEMENTS, confidence + quote 포함)
// Layer2: Design → DDD (MAPPED_TO Aggregate|DomainEntity, BELONGS_TO BoundedContext)
// Layer3: Design → Arch (HANDLED_BY ArchService, CONNECTS_TO ArchService|ArchDatabase)
// Layer4: Story/API → Screen (RENDERS, CALLS_API)
// Layer5: API Chain (HANDLED_BY → CONNECTS_TO → HANDLED_BY 역방향 peer API)
MATCH (n {project: $project})
WHERE (n:Epic OR n:Story)
  AND n.user_edited_at IS NOT NULL
  AND n.user_edited_at > $since

// Story 경로: 자기 자신 ← DERIVED_FROM(Entity/Service/DB) | IMPLEMENTS(API)
OPTIONAL MATCH (n)<-[df_s:DERIVED_FROM|IMPLEMENTS]-(d_s)
WHERE n:Story
  AND (d_s:API OR d_s:Entity OR d_s:ArchService OR d_s:ArchDatabase)

// Epic 경로: CONTAINS → Story ← DERIVED_FROM(Entity/Service/DB) | IMPLEMENTS(API)
OPTIONAL MATCH (n)-[:CONTAINS]->(cs:Story)<-[df_e:DERIVED_FROM|IMPLEMENTS]-(d_e)
WHERE n:Epic
  AND (d_e:API OR d_e:Entity OR d_e:ArchService OR d_e:ArchDatabase)

WITH n,
     COALESCE(d_s, d_e) AS d,
     COALESCE(df_s, df_e) AS df,
     CASE WHEN n:Story THEN n ELSE cs END AS via_story

// [L2] DDD: MAPPED_TO → Aggregate | DomainEntity
OPTIONAL MATCH (d)-[:MAPPED_TO]->(ddd_node)
WHERE ddd_node:Aggregate OR ddd_node:DomainEntity

// [L2b] BoundedContext: MAPPED_TO → Aggregate → BELONGS_TO
OPTIONAL MATCH (d)-[:MAPPED_TO]->(ctx_agg:Aggregate)-[:BELONGS_TO]->(ctx:BoundedContext)

// [L3] Arch: HANDLED_BY → ArchService
OPTIONAL MATCH (d)-[:HANDLED_BY]->(svc:ArchService)

// [L3b] 연결 서비스: ArchService → CONNECTS_TO
OPTIONAL MATCH (svc)-[:CONNECTS_TO]->(conn)
WHERE conn:ArchService OR conn:ArchDatabase

// [L4a] Screen: Story 를 렌더링하는 Screen (RENDERS 역방향)
OPTIONAL MATCH (screen_r:Screen)-[:RENDERS]->(via_story)
WHERE via_story IS NOT NULL

// [L4b] Screen: 변경된 API 를 호출하는 Screen (CALLS_API 역방향)
OPTIONAL MATCH (screen_c:Screen)-[:CALLS_API]->(d)
WHERE d:API

// [L5] API Chain: 연결 서비스가 처리하는 다른 API (regression 영향범위)
OPTIONAL MATCH (conn)<-[:HANDLED_BY]-(peer:API)
WHERE conn:ArchService

// [L6] DomainEvent: Story가 트리거하는 도메인 이벤트 (TRIGGERS)
OPTIONAL MATCH (via_story)-[:TRIGGERS]->(evt:DomainEvent)
WHERE via_story IS NOT NULL

// [L6b] 발행 Aggregate: 해당 이벤트를 PUBLISHES 하는 Aggregate (역방향)
OPTIONAL MATCH (evt_pub:Aggregate)-[:PUBLISHES]->(evt)
WHERE evt IS NOT NULL

RETURN
    n.id AS node_id,
    labels(n)[0] AS node_label,
    n.summary AS summary,
    n.user_edited_at AS changed_at,
    [item IN collect(DISTINCT CASE WHEN d IS NOT NULL THEN {
        id: d.id,
        label: labels(d)[0],
        via_story: via_story.id,
        rel_type: type(df),
        confidence: COALESCE(df.confidence, 'none'),
        quote: COALESCE(df.quote, ''),
        error_cases: COALESCE(d.error_cases, '')
    } ELSE null END) WHERE item IS NOT NULL] AS design_layer,
    [item IN collect(DISTINCT CASE WHEN ddd_node IS NOT NULL THEN {
        id: ddd_node.id, label: labels(ddd_node)[0]
    } ELSE null END) WHERE item IS NOT NULL] +
    [item IN collect(DISTINCT CASE WHEN ctx IS NOT NULL THEN {
        id: ctx.id, label: 'BoundedContext'
    } ELSE null END) WHERE item IS NOT NULL] +
    [item IN collect(DISTINCT CASE WHEN evt_pub IS NOT NULL THEN {
        id: evt_pub.id, label: 'Aggregate'
    } ELSE null END) WHERE item IS NOT NULL] AS ddd_layer,
    [item IN collect(DISTINCT CASE WHEN svc IS NOT NULL THEN {
        id: svc.id, label: 'ArchService'
    } ELSE null END) WHERE item IS NOT NULL] +
    [item IN collect(DISTINCT CASE WHEN conn IS NOT NULL THEN {
        id: conn.id, label: labels(conn)[0]
    } ELSE null END) WHERE item IS NOT NULL] AS arch_layer,
    [item IN collect(DISTINCT CASE WHEN screen_r IS NOT NULL THEN {
        id: screen_r.id, name: screen_r.name, via: 'story'
    } ELSE null END) WHERE item IS NOT NULL] +
    [item IN collect(DISTINCT CASE WHEN screen_c IS NOT NULL THEN {
        id: screen_c.id, name: screen_c.name, via: 'api'
    } ELSE null END) WHERE item IS NOT NULL] AS screen_layer,
    [item IN collect(DISTINCT CASE WHEN peer IS NOT NULL THEN {
        id: peer.id, endpoint: peer.endpoint, method: peer.method
    } ELSE null END) WHERE item IS NOT NULL] AS api_chain_layer,
    [item IN collect(DISTINCT CASE WHEN evt IS NOT NULL THEN {
        id: evt.id, label: 'DomainEvent'
    } ELSE null END) WHERE item IS NOT NULL] AS event_layer
ORDER BY n.user_edited_at DESC
"""


# [2026-05 검수 게이트 Phase 3.1] CPS 노드 (Problem / Solution) 단일 update.
#
# [정책]
# - project 필터 + label whitelist (Problem | Solution) — 다른 라벨 노드는 매칭 안 됨.
#   IDOR 방어: project_name 일치 + 라우트 단 ownership 검증.
# - summary 필드 갱신. description 은 옵션 (없으면 그대로).
# - markdown 재합성은 별도 stage (Phase 3.5+) — 이번엔 graph 노드만 수정.
# - user_edited_at 추적.
#
# [라벨 통일 — 2026-05] 코드베이스 전반에 :Solution / :Resolution 두 라벨이
# 혼재했음. 실제 생성 노드는 LLM 프롬프트 (cps_extract.md) 기준 :Solution 이고
# cps_pipeline 의 build_save_cps_query 가 그대로 MERGE. 그러나 cps_pipeline 의
# Master rebuild + graph_repository 의 trace 는 :Resolution 으로 lookup 하고
# 있어 silent fail 상태였음. Solution 으로 통일.
_UPDATE_CPS_NODE_CYPHER = """\
MATCH (n {id: $node_id, project: $project})
WHERE (n:Problem OR n:Solution)
SET n.summary = $summary,
    n.updated_at = timestamp(),
    n.user_edited_at = timestamp()
WITH n
// [Phase 3.5a] Master CPS 에 markdown_stale 마킹 — graph 만 수정됐고 markdown 은
// 아직 옛 summary. FE 가 banner 띄우고 사용자가 Phase 2 markdown 편집으로 sync.
OPTIONAL MATCH (m:CPS_Document {project: n.project, type: 'Master', is_latest: true})
FOREACH (_ IN CASE WHEN m IS NOT NULL THEN [1] ELSE [] END |
    SET m.markdown_stale = true,
        m.markdown_stale_at = timestamp()
)
RETURN n.id AS id, labels(n)[0] AS label, n.summary AS summary
"""


# [2026-05 검수 게이트 Phase 3.2] PRD 노드 (Epic / Story) 단일 update.
# CPS 와 동일 정책 + label whitelist 만 Epic/Story 로.
# Screen 노드는 schema 가 다름 (name 필드) — 별도 PR.
_UPDATE_PRD_NODE_CYPHER = """\
MATCH (n {id: $node_id, project: $project})
WHERE (n:Epic OR n:Story)
SET n.summary = $summary,
    n.updated_at = timestamp(),
    n.user_edited_at = timestamp()
WITH n
// [Phase 3.5a] Master PRD 에 markdown_stale 마킹.
OPTIONAL MATCH (m:PRD_Document {project: n.project, type: 'Master', is_latest: true})
FOREACH (_ IN CASE WHEN m IS NOT NULL THEN [1] ELSE [] END |
    SET m.markdown_stale = true,
        m.markdown_stale_at = timestamp()
)
// [Phase 3.6] PRD 노드 변경 → Design 도 옛 PRD 기준이라 stale.
// MERGE 로 Project 노드 없으면 생성. 같은 트랜잭션 = 원자성.
WITH n
MERGE (p:Project {name: n.project})
SET p.design_source_stale = true,
    p.design_source_stale_at = timestamp()
RETURN n.id AS id, labels(n)[0] AS label, n.summary AS summary
"""


# [2026-05 검수 게이트 Phase 3.3] CPS / PRD 노드 listing — FE 사이드바가 실제 그래프 ID 로 PATCH 호출하기 위함.
# markdown 파싱 의존 제거 (display ID 'PRB-01' → 실제 graph ID 'prb_01_1' 매핑 불필요).
_LIST_CPS_NODES_CYPHER = """\
MATCH (n {project: $project})
WHERE (n:Problem OR n:Solution)
RETURN n.id AS id, labels(n)[0] AS label, n.summary AS summary
ORDER BY label, id
"""

_LIST_PRD_NODES_CYPHER = """\
MATCH (n {project: $project})
WHERE (n:Epic OR n:Story)
RETURN n.id AS id, labels(n)[0] AS label, n.summary AS summary
ORDER BY label, id
"""


_GET_PRD_CYPHER = """\
MATCH (m:PRD_Document {type: 'Master', is_latest: true})
WHERE m.project = $project

// 1. 이 마스터 기획의 근거가 되는 마스터 요구사항(CPS) 연결 확인
OPTIONAL MATCH (m)-[:BASED_ON]->(cps_m:CPS_Document)

// 2. 증분 병합을 통해 이 마스터에 통합된 개별 PRD 문서들의 ID 추적
OPTIONAL MATCH (m)-[:SYNTHESIZED_FROM]->(source:PRD_Document)

RETURN
    m.id AS master_prd_id,
    m.full_markdown AS prd_content,
    m.updated_at AS last_updated,
    COALESCE(m.markdown_stale, false) AS markdown_stale,
    cps_m.id AS related_master_cps_id,
    collect(DISTINCT source.id) AS absorbed_prd_ids,
    m.autofix_needs_input AS autofix_needs_input
"""


# ─── [2026-06] autofix needs_input 영속화 ──────────────────────────
#
# [배경] '/prd/autofix' 가 반환하는 needs_input(AI 가 근거 부족으로 못 채운
# 항목)이 FE in-memory store 에만 있어 새로고침/다른 기기에서 증발 — 사용자가
# 보완하기를 다시 눌러 같은 진단에 토큰을 재지출했다. master PRD 노드에 JSON
# string 으로 저장(Neo4j 는 중첩 map property 불가)하고 getPRD 가 동봉한다.
#
# [수명] 회의록 merge(prd_pipeline._synthesize) / delete rebuild 가 명시적으로
# null 처리 — "새 정보가 PRD 에 반영되기 전까지 유지"가 의미론. 수동 편집·diff
# 적용(PATCH, 같은 노드 SET)은 건드리지 않음 — gap 은 인터뷰로 채워지는 정보라
# 보통 그대로 남아 있고, 아니면 사용자가 X(dismiss)로 닫는다.
_SET_PRD_AUTOFIX_NEEDS_CYPHER = """\
MATCH (m:PRD_Document {project: $project, type: 'Master', is_latest: true})
SET m.autofix_needs_input = $needs_json,
    m.autofix_needs_at = timestamp()
RETURN m.id AS master_id
"""

_CLEAR_PRD_AUTOFIX_NEEDS_CYPHER = """\
MATCH (m:PRD_Document {project: $project, type: 'Master', is_latest: true})
SET m.autofix_needs_input = null,
    m.autofix_needs_at = null
RETURN m.id AS master_id
"""


_GET_DDD_CYPHER = """\
MATCH (n)
WHERE n.project = $project
  AND (n:BoundedContext OR n:Aggregate OR n:DomainEntity OR n:DomainEvent)
WITH
  [x IN collect(DISTINCT n) WHERE x:BoundedContext | x] AS contexts,
  // [2026-06 연결 점수 fix] Aggregate lineage 를 스코어러가 기대하는 노드 모양으로 복원.
  // (SPACK entity 와 동일 — lineage_confidence(평탄) + DERIVED_FROM 엣지 → lineage 객체)
  [x IN collect(DISTINCT n) WHERE x:Aggregate |
    x { .*, lineage: {
      confidence: CASE
        WHEN size([(x)-[:DERIVED_FROM]->(:Story) | 1]) = 0 THEN 'none'
        WHEN x.lineage_confidence IN ['direct', 'inferred'] THEN x.lineage_confidence
        ELSE 'none' END,
      related_stories: [(x)-[df:DERIVED_FROM]->(s:Story) | {story_id: s.id, quote: COALESCE(df.quote, '')}]
    } }] AS aggregates,
  // [2026-06 lineage 상세 fix] DomainEntity 도 Aggregate/Entity 와 동일하게 nested
  // lineage(confidence + related_stories{story_id, quote}) 복원. 저장 시 flat
  // lineage_confidence + DERIVED_FROM(quote) 엣지가 들어가므로(cypher.py) 동일 패턴.
  [x IN collect(DISTINCT n) WHERE x:DomainEntity |
    x { .*, lineage: {
      confidence: CASE
        WHEN size([(x)-[:DERIVED_FROM]->(:Story) | 1]) = 0 THEN 'none'
        WHEN x.lineage_confidence IN ['direct', 'inferred'] THEN x.lineage_confidence
        ELSE 'none' END,
      related_stories: [(x)-[df:DERIVED_FROM]->(s:Story) | {story_id: s.id, quote: COALESCE(df.quote, '')}]
    } }] AS domain_entities,
  [x IN collect(DISTINCT n) WHERE x:DomainEvent | x] AS domain_events

// 내부 관계 (BELONGS_TO, PART_OF, PUBLISHES)
OPTIONAL MATCH (src)-[r:BELONGS_TO|PART_OF|PUBLISHES]->(tgt)
WHERE src.project = $project AND tgt.project = $project
WITH contexts, aggregates, domain_entities, domain_events,
     [rel IN collect(DISTINCT r) WHERE rel IS NOT NULL | {
         source_id: startNode(rel).id,
         target_id: endNode(rel).id,
         type: type(rel)
     }] AS internal_rels

// Story → DomainEvent TRIGGERS (외부 관계)
OPTIONAL MATCH (s:Story {project: $project})-[t:TRIGGERS]->(evt:DomainEvent {project: $project})
WITH contexts, aggregates, domain_entities, domain_events, internal_rels,
     [rel IN collect(DISTINCT t) WHERE rel IS NOT NULL | {
         source_id: startNode(rel).id,
         target_id: endNode(rel).id,
         type: type(rel)
     }] AS trigger_rels

// [2026-05-19] cross-jump — Aggregate → ArchService 매핑 (OWNED_BY 관계)
OPTIONAL MATCH (agg:Aggregate {project: $project})-[obr:OWNED_BY]->(svc:ArchService {project: $project})
WITH contexts, aggregates, domain_entities, domain_events, internal_rels, trigger_rels,
     [rel IN collect(DISTINCT obr) WHERE rel IS NOT NULL | {
         source_id: startNode(rel).id,
         target_id: endNode(rel).id,
         target_name: endNode(rel).name,
         type: type(rel)
     }] AS aggregate_service_rels

RETURN contexts, aggregates, domain_entities, domain_events,
       internal_rels, trigger_rels, aggregate_service_rels
"""


_GET_SPACK_CYPHER = """\
MATCH (n)
WHERE n.project = $project
  AND (n:API OR n:Entity OR n:Policy OR n:Screen)
WITH
  // [2026-06 연결 점수 fix] API/Entity 의 PRD 연결을 스코어러가 기대하는 노드 모양으로 복원.
  // 저장 시 related_story_id 는 노드 속성(2026-06 추가) + IMPLEMENTS 엣지, entity lineage 는
  // DERIVED_FROM 엣지 + lineage_confidence(평탄) 로 들어간다. 스코어러(evals.scorer tier3)는
  // a.related_story_id / e.lineage.* 를 노드 모양으로 읽으므로, 속성 우선 + 엣지 fallback 로 맞춘다.
  // (속성은 신규 저장분, 엣지 fallback 은 구 데이터 호환.)
  // [2026-06-12 연결 채우기] API 의 lineage_confidence 파생 — API 노드엔 설계 저장이
  // confidence 를 안 쓰므로(FE 연결 상세가 항상 0% 로 보이던 원인), 연결(속성/엣지)이
  // 있으면 'direct'(생성이 직접 도출) 또는 저장된 값('inferred'=AI 매칭)을 노출한다.
  [x IN collect(DISTINCT n) WHERE x:API |
    x { .*, related_story_id: coalesce(x.related_story_id,
        [(x)-[:IMPLEMENTS]->(s:Story) | s.id][0]),
      lineage_confidence: CASE
        WHEN x.lineage_confidence IN ['direct', 'inferred'] THEN x.lineage_confidence
        WHEN coalesce(x.related_story_id,
             [(x)-[:IMPLEMENTS]->(s:Story) | s.id][0]) IS NOT NULL THEN 'direct'
        ELSE 'none' END }] AS apis,
  [x IN collect(DISTINCT n) WHERE x:Entity |
    x { .*, lineage: {
      confidence: CASE
        WHEN size([(x)-[:DERIVED_FROM]->(:Story) | 1]) = 0 THEN 'none'
        WHEN x.lineage_confidence IN ['direct', 'inferred'] THEN x.lineage_confidence
        ELSE 'none' END,
      related_stories: [(x)-[df:DERIVED_FROM]->(s:Story) | {story_id: s.id, quote: COALESCE(df.quote, '')}]
    } }] AS entities,
  [x IN collect(DISTINCT n) WHERE x:Policy | x] AS policies,
  // [#3 — 2026-05-25] Screen 노드 + 호출 API id list (CALLS_API 관계로부터)
  [x IN collect(DISTINCT n) WHERE x:Screen |
    {id: x.id, name: x.name, path: x.path, description: x.description,
     next_screens: x.next_screens,
     calls_apis: [(x)-[:CALLS_API]->(api:API) | api.id],
     related_story_id: coalesce(x.related_story_id,
        [(x)-[:RENDERS]->(s:Story) | s.id][0])
    }] AS screens

// 내부 관계 (GOVERNS)
OPTIONAL MATCH (src)-[r:GOVERNS]->(tgt)
WHERE src.project = $project AND tgt.project = $project
WITH apis, entities, policies, screens,
     [rel IN collect(DISTINCT r) WHERE rel IS NOT NULL | {
         source_id: startNode(rel).id,
         target_id: endNode(rel).id,
         type: type(rel)
     }] AS internal_rels

// 외부 관계 API → Story (IMPLEMENTS)
OPTIONAL MATCH (src:API {project: $project})-[t:IMPLEMENTS]->(s:Story {project: $project})
WITH apis, entities, policies, screens, internal_rels,
     [rel IN collect(DISTINCT t) WHERE rel IS NOT NULL | {
         source_id: startNode(rel).id,
         target_id: endNode(rel).id,
         type: type(rel)
     }] AS implement_rels

// [2026-05-19] cross-jump 매핑 — Entity → DDD location (Aggregate/DomainEntity)
OPTIONAL MATCH (e:Entity {project: $project})-[mr:MAPPED_TO]->(t)
WHERE t.project = $project AND (t:Aggregate OR t:DomainEntity)
WITH apis, entities, policies, screens, internal_rels, implement_rels,
     [rel IN collect(DISTINCT mr) WHERE rel IS NOT NULL | {
         source_id: startNode(rel).id,
         target_id: endNode(rel).id,
         target_name: endNode(rel).name,
         target_kind: CASE WHEN endNode(rel):Aggregate THEN 'aggregate' ELSE 'domain_entity' END,
         role: rel.role,
         type: type(rel)
     }] AS entity_mapping_rels

// [2026-05-19] cross-jump 매핑 — API → ArchService (HANDLED_BY)
OPTIONAL MATCH (api:API {project: $project})-[hr:HANDLED_BY]->(svc:ArchService {project: $project})
WITH apis, entities, policies, screens, internal_rels, implement_rels, entity_mapping_rels,
     [rel IN collect(DISTINCT hr) WHERE rel IS NOT NULL | {
         source_id: startNode(rel).id,
         target_id: endNode(rel).id,
         target_name: endNode(rel).name,
         reason: rel.reason,
         type: type(rel)
     }] AS api_service_rels

RETURN apis, entities, policies, screens, internal_rels, implement_rels,
       entity_mapping_rels, api_service_rels
"""


_GET_ARCHITECTURE_CYPHER = """\
MATCH (n)
WHERE n.project = $project
  AND (n:ArchService OR n:ArchDatabase)
WITH
  // [2026-06 lineage 상세 fix] ArchService/ArchDatabase 도 SPACK Entity 와 동일하게
  // nested lineage(confidence + related_stories{story_id, quote}) 로 복원한다.
  // 기존엔 raw 노드(flat lineage_confidence/story_count)만 반환해 FE 가 story_id/quote
  // 상세를 못 받고 "상세 항목 0건 제공" placeholder 로 표기되던 문제.
  [x IN collect(DISTINCT n) WHERE x:ArchService |
    x { .*, lineage: {
      confidence: CASE
        WHEN size([(x)-[:DERIVED_FROM]->(:Story) | 1]) = 0 THEN 'none'
        WHEN x.lineage_confidence IN ['direct', 'inferred'] THEN x.lineage_confidence
        ELSE 'none' END,
      related_stories: [(x)-[df:DERIVED_FROM]->(s:Story) | {story_id: s.id, quote: COALESCE(df.quote, '')}]
    } }] AS services,
  [x IN collect(DISTINCT n) WHERE x:ArchDatabase |
    x { .*, lineage: {
      confidence: CASE
        WHEN size([(x)-[:DERIVED_FROM]->(:Story) | 1]) = 0 THEN 'none'
        WHEN x.lineage_confidence IN ['direct', 'inferred'] THEN x.lineage_confidence
        ELSE 'none' END,
      related_stories: [(x)-[df:DERIVED_FROM]->(s:Story) | {story_id: s.id, quote: COALESCE(df.quote, '')}]
    } }] AS databases

// 통신 관계(CONNECTS_TO)
OPTIONAL MATCH (src)-[r:CONNECTS_TO]->(tgt)
WHERE src.project = $project AND tgt.project = $project
  AND (src:ArchService OR src:ArchDatabase)
  AND (tgt:ArchService OR tgt:ArchDatabase)
WITH services, databases,
     [rel IN collect(DISTINCT r) WHERE rel IS NOT NULL | {
         source_id: startNode(rel).id,
         target_id: endNode(rel).id,
         type: type(rel),
         protocol: rel.protocol,
         description: rel.description,
         // [D-2 — 2026-05-25] Connection auth (mTLS/bearer/basic/api-key/none)
         auth: rel.auth
     }] AS connections

RETURN services, databases, connections
"""


_GET_MEETING_LOG_CYPHER = """\
MATCH (log:Meeting_Log)
WHERE log.project = $project AND log.version = $version
RETURN
    log.version AS version,
    log.date AS date,
    log.raw_content AS meeting_content,
    log.created_at AS created_at
"""


_GET_MEETING_VERSIONS_CYPHER = """\
MATCH (log:Meeting_Log)
WHERE log.project = $project
RETURN
    log.id AS log_id,
    log.version AS version,
    log.date AS date
ORDER BY log.version ASC
"""


# 그래프 스냅샷 — project 단위 격리.
# embedding/full_markdown 같은 무거운 속성은 응답에서 제외(별도 endpoint 로 조회).
# 노드 수가 폭발하지 않도록 LIMIT 적용.
_GET_PROJECT_GRAPH_CYPHER = """\
MATCH (n)
WHERE n.project = $project
WITH collect(DISTINCT n) AS ns
OPTIONAL MATCH (s)-[r]->(t)
WHERE s.project = $project AND t.project = $project
WITH ns, collect(DISTINCT r) AS rs
RETURN
    [x IN ns | {
        id: x.id,
        label: labels(x)[0],
        properties: apoc.map.removeKeys(properties(x), ['embedding','full_markdown','raw_content'])
    }] AS nodes,
    [y IN rs | {
        source_id: startNode(y).id,
        target_id: endNode(y).id,
        type: type(y)
    }] AS edges
"""


# APOC 가 없는 환경(테스트/로컬) 대비 fallback — 모든 속성 포함하되 호출자가 무거운 필드 strip.
_GET_PROJECT_GRAPH_FALLBACK_CYPHER = """\
MATCH (n)
WHERE n.project = $project
WITH collect(DISTINCT n) AS ns
OPTIONAL MATCH (s)-[r]->(t)
WHERE s.project = $project AND t.project = $project
WITH ns, collect(DISTINCT r) AS rs
RETURN
    [x IN ns | {id: x.id, label: labels(x)[0], properties: properties(x)}] AS nodes,
    [y IN rs | {
        source_id: startNode(y).id,
        target_id: endNode(y).id,
        type: type(y)
    }] AS edges
"""


# ─── Screen 단위 서브그래프 ────────────────────────────────────
# 한 화면(:Screen {name})에 연결된 Story/Epic 만 추출.
# 기존 전체 그래프는 프로젝트의 모든 노드를 반환 → 화면별 뷰엔 노이즈가 큼.
#
# [스키마]
#   (:Epic)-[:CONTAINS]->(:Story)-[:IMPLEMENTED_ON]->(:Screen {name})
#
# [노드 ID 정책]
#   Screen 은 `id` 필드가 없고 `name` 만 가짐 → response 에서는 'screen:<name>' 으로
#   합성 ID 를 사용. Epic/Story 는 기존 id 그대로.
#
# [anchor 정책]
#   prd_graph.md 프롬프트가 Screen 노드에 `project` 속성을 요구하지 않아 Screen
#   에 project 필터를 걸면 매치 0건. 대신 project 가 보장된 Story 를 anchor 로
#   IMPLEMENTED_ON 따라 Screen 으로 들어감 → 프로젝트 격리 유지.
_GET_SCREEN_SUBGRAPH_CYPHER = """\
MATCH (story:Story {project: $project})-[:IMPLEMENTED_ON]->(screen:Screen {name: $screen_name})
OPTIONAL MATCH (epic:Epic {project: $project})-[:CONTAINS]->(story)
WITH collect(DISTINCT screen) + collect(DISTINCT story) + collect(DISTINCT epic) AS raw
UNWIND raw AS n
WITH collect(DISTINCT n) AS ns
OPTIONAL MATCH (a)-[r]->(b)
WHERE a IN ns AND b IN ns
WITH ns, collect(DISTINCT r) AS rs
RETURN
    [x IN ns | {
        id: coalesce(x.id, 'screen:' + x.name),
        label: labels(x)[0],
        properties: apoc.map.removeKeys(properties(x), ['embedding','full_markdown','raw_content'])
    }] AS nodes,
    [y IN rs | {
        source_id: coalesce(startNode(y).id, 'screen:' + startNode(y).name),
        target_id: coalesce(endNode(y).id, 'screen:' + endNode(y).name),
        type: type(y)
    }] AS edges
"""


_GET_SCREEN_SUBGRAPH_FALLBACK_CYPHER = """\
MATCH (story:Story {project: $project})-[:IMPLEMENTED_ON]->(screen:Screen {name: $screen_name})
OPTIONAL MATCH (epic:Epic {project: $project})-[:CONTAINS]->(story)
WITH collect(DISTINCT screen) + collect(DISTINCT story) + collect(DISTINCT epic) AS raw
UNWIND raw AS n
WITH collect(DISTINCT n) AS ns
OPTIONAL MATCH (a)-[r]->(b)
WHERE a IN ns AND b IN ns
WITH ns, collect(DISTINCT r) AS rs
RETURN
    [x IN ns | {
        id: coalesce(x.id, 'screen:' + x.name),
        label: labels(x)[0],
        properties: properties(x)
    }] AS nodes,
    [y IN rs | {
        source_id: coalesce(startNode(y).id, 'screen:' + startNode(y).name),
        target_id: coalesce(endNode(y).id, 'screen:' + endNode(y).name),
        type: type(y)
    }] AS edges
"""


# 마크다운에서 Screen → Story 매핑 추출용 fallback Cypher.
# Story.summary 의 일부가 markdown 의 Story 설명과 매치할 가능성을 활용.
# 화면 단위 서브그래프가 비었을 때 (IMPLEMENTED_ON 엣지 미존재) markdown 으로 보강.
_GET_STORIES_BY_IDS_CYPHER = """\
MATCH (story:Story {project: $project})
WHERE story.id IN $story_ids
OPTIONAL MATCH (epic:Epic {project: $project})-[:CONTAINS]->(story)
WITH collect(DISTINCT story) + collect(DISTINCT epic) AS raw
UNWIND raw AS n
WITH collect(DISTINCT n) AS ns
OPTIONAL MATCH (a)-[r]->(b)
WHERE a IN ns AND b IN ns
WITH ns, collect(DISTINCT r) AS rs
RETURN
    [x IN ns | {
        id: x.id,
        label: labels(x)[0],
        properties: apoc.map.removeKeys(properties(x), ['embedding','full_markdown','raw_content'])
    }] AS nodes,
    [y IN rs | {
        source_id: startNode(y).id,
        target_id: endNode(y).id,
        type: type(y)
    }] AS edges
"""

_GET_STORIES_BY_IDS_FALLBACK_CYPHER = """\
MATCH (story:Story {project: $project})
WHERE story.id IN $story_ids
OPTIONAL MATCH (epic:Epic {project: $project})-[:CONTAINS]->(story)
WITH collect(DISTINCT story) + collect(DISTINCT epic) AS raw
UNWIND raw AS n
WITH collect(DISTINCT n) AS ns
OPTIONAL MATCH (a)-[r]->(b)
WHERE a IN ns AND b IN ns
WITH ns, collect(DISTINCT r) AS rs
RETURN
    [x IN ns | {id: x.id, label: labels(x)[0], properties: properties(x)}] AS nodes,
    [y IN rs | {source_id: startNode(y).id, target_id: endNode(y).id, type: type(y)}] AS edges
"""


# 그래프 응답에서 strip 할 무거운 속성 키들 (fallback 경로용).
_HEAVY_PROP_KEYS = {"embedding", "full_markdown", "raw_content"}


# 한 응답당 최대 노드/엣지 수 — 4GB 호스트 보호.
_MAX_GRAPH_NODES = 500
_MAX_GRAPH_EDGES = 2000


# ===== Pydantic 응답 모델 =====


class CpsMaster(BaseModel):
    master_id: Optional[str] = None
    version: Optional[str] = None
    content: Optional[str] = None
    last_updated: Optional[int] = None
    # [Phase 3.5a] true 면 graph 노드가 수정됐는데 markdown 은 아직 옛 summary.
    markdown_stale: bool = False
    absorbed_cps_ids: List[str] = []


class PrdMaster(BaseModel):
    master_prd_id: Optional[str] = None
    prd_content: Optional[str] = None
    last_updated: Optional[int] = None
    markdown_stale: bool = False
    related_master_cps_id: Optional[str] = None
    absorbed_prd_ids: List[str] = []
    # [2026-06] autofix 가 채우지 못해 인터뷰가 필요한 항목 [{topic, question}].
    # 새로고침/다른 기기에서도 '인터뷰로 채우기' 상태를 복원 — 같은 진단을 얻으려고
    # LLM 을 다시 돌리는 토큰 낭비 방지. 회의록 merge/delete rebuild 시 자동 소멸.
    autofix_needs_input: List[Dict[str, str]] = []


class GraphRel(BaseModel):
    source_id: str
    target_id: str
    type: str
    protocol: Optional[str] = None
    description: Optional[str] = None
    # [D-2 — 2026-05-25] Architecture Connection 의 auth (mTLS/bearer/basic/...).
    # 다른 관계 타입엔 부재 (Optional). FE 가 Connection Map 의 auth 컬럼 노출.
    auth: Optional[str] = None


class CrossMappingRel(BaseModel):
    """[2026-05-19] cross-jump 용 매핑 관계 한 줄.

    source/target 양쪽 모두 id 와 name 을 제공해 FE 가 별도 lookup 없이
    chip 에 바로 그릴 수 있게 함. role / reason 등 보조 메타 같이.
    """
    source_id: str
    target_id: str
    target_name: Optional[str] = None
    target_kind: Optional[str] = None  # 'aggregate' | 'domain_entity' (Spack→DDD 전용)
    role: Optional[str] = None          # 'aggregate_root' | 'entity' 등 (Spack→DDD)
    reason: Optional[str] = None        # API→Service 매핑 사유 (LLM 출력)
    type: str


class DddGraph(BaseModel):
    contexts: List[Dict[str, Any]] = []
    aggregates: List[Dict[str, Any]] = []
    domain_entities: List[Dict[str, Any]] = []
    domain_events: List[Dict[str, Any]] = []
    internal_rels: List[GraphRel] = []
    trigger_rels: List[GraphRel] = []
    # [2026-05-19] cross-jump — Aggregate → ArchService 매핑 (역방향 OWNED_BY)
    aggregate_service_rels: List[CrossMappingRel] = []


class SpackGraph(BaseModel):
    apis: List[Dict[str, Any]] = []
    entities: List[Dict[str, Any]] = []
    policies: List[Dict[str, Any]] = []
    # [#3 — 2026-05-25] Screen 노드 (화면 ↔ API 매핑).
    # FE 코드 생성에 필요 — 어떤 path 에서 어떤 API 호출하는지.
    screens: List[Dict[str, Any]] = []
    internal_rels: List[GraphRel] = []
    implement_rels: List[GraphRel] = []
    # [2026-05-19] cross-jump 매핑 — FE 에서 SPACK 카드 옆 chip 으로 노출.
    entity_mapping_rels: List[CrossMappingRel] = []  # Entity → DDD location
    api_service_rels: List[CrossMappingRel] = []     # API → ArchService


class ArchitectureGraph(BaseModel):
    services: List[Dict[str, Any]] = []
    databases: List[Dict[str, Any]] = []
    connections: List[GraphRel] = []


class MeetingLog(BaseModel):
    version: Optional[str] = None
    date: Optional[str] = None
    meeting_content: Optional[str] = None
    created_at: Optional[int] = None


class MeetingVersion(BaseModel):
    log_id: Optional[str] = None
    version: Optional[str] = None
    date: Optional[str] = None


class TimelineEvent(BaseModel):
    """프로젝트 타임라인 이벤트 — Deliverables Hero 영역 strip 용."""
    kind: str            # 'meeting' | 'cps_update' | 'prd_update' | 'lint' | 'lineage' | 'repo_add'
    occurred_at: int     # epoch ms
    label: str           # 'v1.3 미팅', 'PRD 갱신', 'Lint 78점' 등
    detail: Optional[str] = None


class ProjectTimeline(BaseModel):
    project: str
    since: int           # 조회 윈도우 시작 (epoch ms)
    events: List[TimelineEvent] = []
    counts: Dict[str, int] = {}  # kind 별 카운트


class GraphNode(BaseModel):
    id: str
    label: str
    properties: Dict[str, Any] = {}


class GraphEdge(BaseModel):
    source_id: str
    target_id: str
    type: str
    # [D — 2026-05] DERIVED_FROM 같은 lineage 관계는 confidence + quote 보유.
    # 다른 관계 type 은 빈 dict — backward compat.
    properties: Dict[str, Any] = {}


class ProjectGraph(BaseModel):
    """프론트 그래프 시각화용 — project 단위 격리된 노드/관계 스냅샷."""
    project: str
    nodes: List[GraphNode] = []
    edges: List[GraphEdge] = []
    # [2026-05] 빈 결과 진단 — FE 가 정확한 안내 메시지 띄울 수 있도록.
    # 값: 'ok' | 'screen_not_found' | 'no_implemented_on' | 'stories_match_no_data'
    #     | 'no_lineage' | 'no_design'
    # None 이면 옛 응답 (호환). FE 는 nodes 가 비어있을 때만 reason 참고.
    reason: Optional[str] = None
    # [2026-05] 운영 디버깅용 진단 정보 — 빈 결과일 때만 채움.
    # - debug.attempted_story_ids: markdown 에서 추출한 후보 Story IDs (Neo4j 매칭 시도)
    # - debug.matched_story_ids: 그 중 Neo4j 에 실제로 존재하는 IDs (보통 빈 list 가 문제)
    # - debug.screen_node_count: 해당 project 의 Screen 노드 총 수
    # FE 는 표시 안 함 — 사용자가 응답 body 직접 봐서 운영 디버깅용.
    debug: Dict[str, Any] = {}


# ===== 헬퍼 =====


def _first_row(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return records[0] if records else None


def _clean_node_props(node: Any) -> Dict[str, Any]:
    """
    Neo4j 가 반환한 노드를 dict 로 변환. Pydantic 모델이 받을 수 있는 형태.
    이미 dict (또는 dict 호환) 이면 그대로.
    """
    if isinstance(node, dict):
        return node
    # neo4j driver 의 Node 객체는 mapping 처럼 동작
    try:
        return dict(node)  # type: ignore[arg-type]
    except Exception:
        return {}


def _clean_node_list(nodes: Any) -> List[Dict[str, Any]]:
    if not isinstance(nodes, list):
        return []
    return [_clean_node_props(n) for n in nodes if n is not None]


def _clean_rel_list(rels: Any) -> List[GraphRel]:
    if not isinstance(rels, list):
        return []
    out: List[GraphRel] = []
    for r in rels:
        if not isinstance(r, dict):
            continue
        if not r.get("source_id") or not r.get("target_id") or not r.get("type"):
            continue
        out.append(
            GraphRel(
                source_id=str(r["source_id"]),
                target_id=str(r["target_id"]),
                type=str(r["type"]),
                protocol=r.get("protocol"),
                description=r.get("description"),
                # [D-2 — 2026-05-25] Connection auth — Architecture 만 사용.
                # 다른 rel 은 None (Optional default).
                auth=r.get("auth"),
            )
        )
    return out


def _clean_cross_rel_list(rels: Any) -> List[CrossMappingRel]:
    """[2026-05-19] cross-jump 매핑 rel list 정규화 — Pydantic 변환."""
    if not isinstance(rels, list):
        return []
    out: List[CrossMappingRel] = []
    for r in rels:
        if not isinstance(r, dict):
            continue
        if not r.get("source_id") or not r.get("target_id") or not r.get("type"):
            continue
        out.append(
            CrossMappingRel(
                source_id=str(r["source_id"]),
                target_id=str(r["target_id"]),
                target_name=r.get("target_name"),
                target_kind=r.get("target_kind"),
                role=r.get("role"),
                reason=r.get("reason"),
                type=str(r["type"]),
            )
        )
    return out


# ===== Query 함수 =====


async def get_master_cps(project_name: str, team_id: str = "") -> Optional[CpsMaster]:
    project_name = scoped_project(project_name, team_id)
    records = await neo4j_client.run_cypher(
        _GET_CPS_CYPHER, {"project": project_name}
    )
    row = _first_row(records)
    if not row or not row.get("master_id"):
        return None
    return CpsMaster(
        master_id=row.get("master_id"),
        version=row.get("version"),
        content=row.get("content"),
        last_updated=int(row["last_updated"])
        if row.get("last_updated") is not None
        else None,
        markdown_stale=bool(row.get("markdown_stale") or False),
        absorbed_cps_ids=[i for i in (row.get("absorbed_cps_ids") or []) if i],
    )


class OptimisticLockConflict(Exception):
    """[2026-05-18 Phase 2] 동시 편집 충돌 — client_updated_at 가 DB 의 updated_at 과 불일치.

    호출자(라우트)가 catch 해서 409 응답 + "다른 디바이스에서 변경됨" 안내.
    """


async def _check_master_exists_cps(project_name: str, team_id: str = "") -> bool:
    """Optimistic lock 충돌과 'master 없음' (404) 을 구분하기 위한 보조 조회."""
    project_name = scoped_project(project_name, team_id)
    out = await neo4j_client.run_cypher(
        "MATCH (m:CPS_Document {project: $project, type: 'Master', is_latest: true}) "
        "RETURN m.id AS id LIMIT 1",
        {"project": project_name},
    )
    return bool(out and out[0].get("id"))


async def _check_master_exists_prd(project_name: str, team_id: str = "") -> bool:
    project_name = scoped_project(project_name, team_id)
    out = await neo4j_client.run_cypher(
        "MATCH (m:PRD_Document {project: $project, type: 'Master', is_latest: true}) "
        "RETURN m.id AS id LIMIT 1",
        {"project": project_name},
    )
    return bool(out and out[0].get("id"))


async def update_master_cps_markdown(
    project_name: str,
    content: str,
    *,
    client_updated_at: Optional[int] = None,
    team_id: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Master CPS 의 full_markdown 직접 덮어쓰기 — 검수 게이트 모드용.

    [정책]
    - is_latest=true 인 단일 master 만 갱신
    - master 가 없으면 None 반환 (404 매핑)
    - SYNTHESIZED_FROM 관계 / Problem / Solution 하위 노드는 그대로 (markdown 만 변경)
    - 사용자가 markdown 안 구조를 바꿔도 그래프 정합성 깨지지 않음 — markdown 은 display only

    [2026-05-18 Phase 2 — Optimistic Locking]
    client_updated_at 이 주어지면 DB updated_at 과 일치 시에만 갱신.
    불일치 (다른 디바이스에서 먼저 변경됨) → OptimisticLockConflict raise.
    master 자체가 없으면 None 반환 (기존 404 매핑 유지).
    client_updated_at = None 이면 (legacy / FE 미갱신) 조건 skip — 호환.

    Returns:
        { master_id, last_updated } | None (master 없음)
    Raises:
        OptimisticLockConflict: client_updated_at 불일치 (동시 편집)
    """
    project_name = scoped_project(project_name, team_id)
    # [2026-05-26 데이터 무결성 가드] 빈 content 거부.
    # FE 가 차단해도 API 직접 호출 / MCP / 다른 client 경유 가능 — Defense in Depth.
    if not content or not content.strip():
        raise ValueError(
            "CPS markdown 내용이 비어있습니다. master 데이터 손실 방지를 위해 거부합니다."
        )
    records = await neo4j_client.run_cypher(
        _UPDATE_CPS_MARKDOWN_CYPHER,
        {
            "project": project_name,
            "content": content,
            "client_updated_at": client_updated_at,
        },
    )
    row = _first_row(records)
    if not row or not row.get("master_id"):
        # 빈 결과 — master 가 아예 없는 경우 vs optimistic lock 충돌 구분.
        if client_updated_at is not None and await _check_master_exists_cps(project_name):
            raise OptimisticLockConflict(
                "다른 디바이스에서 먼저 편집됐습니다. 새로고침 후 변경사항을 다시 적용해주세요."
            )
        return None
    return {
        "master_id": row["master_id"],
        "last_updated": int(row["last_updated"]) if row.get("last_updated") is not None else None,
    }


async def get_master_prd(project_name: str, team_id: str = "") -> Optional[PrdMaster]:
    project_name = scoped_project(project_name, team_id)
    records = await neo4j_client.run_cypher(
        _GET_PRD_CYPHER, {"project": project_name}
    )
    row = _first_row(records)
    if not row or not row.get("master_prd_id"):
        return None
    return PrdMaster(
        master_prd_id=row.get("master_prd_id"),
        prd_content=row.get("prd_content"),
        last_updated=int(row["last_updated"])
        if row.get("last_updated") is not None
        else None,
        markdown_stale=bool(row.get("markdown_stale") or False),
        related_master_cps_id=row.get("related_master_cps_id"),
        absorbed_prd_ids=[i for i in (row.get("absorbed_prd_ids") or []) if i],
        autofix_needs_input=_parse_autofix_needs(row.get("autofix_needs_input")),
    )


def _parse_autofix_needs(raw: Any) -> List[Dict[str, str]]:
    """노드의 JSON string → [{topic, question}]. 손상/형식 불일치 → [] (표시 생략)."""
    if not raw or not isinstance(raw, str):
        return []
    try:
        items = json.loads(raw)
        return [
            {"topic": str(i.get("topic") or ""), "question": str(i.get("question") or "")}
            for i in items
            if isinstance(i, dict) and (i.get("topic") or i.get("question"))
        ]
    except Exception:  # noqa: BLE001 — 손상된 값이 getPRD 자체를 막으면 안 됨
        logger.warning("autofix_needs_input JSON 파싱 실패 — 무시")
        return []


async def set_prd_autofix_needs_input(
    project_name: str, needs: List[Dict[str, str]], team_id: str = ""
) -> bool:
    """autofix 진단의 needs_input 저장. 빈 리스트면 해제(진단상 gap 없음).

    Returns: master 존재해 반영됐으면 True.
    """
    if not needs:
        return await clear_prd_autofix_needs_input(project_name, team_id=team_id)
    project_name = scoped_project(project_name, team_id)
    records = await neo4j_client.run_cypher(
        _SET_PRD_AUTOFIX_NEEDS_CYPHER,
        {
            "project": project_name,
            "needs_json": json.dumps(
                [{"topic": n.get("topic") or "", "question": n.get("question") or ""}
                 for n in needs],
                ensure_ascii=False,
            ),
        },
    )
    row = _first_row(records)
    return bool(row and row.get("master_id"))


async def clear_prd_autofix_needs_input(project_name: str, team_id: str = "") -> bool:
    """needs_input 해제 — 사용자 dismiss(X) 또는 빈 진단."""
    project_name = scoped_project(project_name, team_id)
    records = await neo4j_client.run_cypher(
        _CLEAR_PRD_AUTOFIX_NEEDS_CYPHER, {"project": project_name}
    )
    row = _first_row(records)
    return bool(row and row.get("master_id"))


async def update_master_prd_markdown(
    project_name: str,
    content: str,
    *,
    client_updated_at: Optional[int] = None,
    team_id: str = "",
    mark_design_stale: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Master PRD 의 full_markdown 직접 덮어쓰기 — 검수 게이트 모드.

    [정책 — update_master_cps_markdown 와 동일]
    - is_latest=true Master PRD 만 갱신
    - Epic/Story 그래프 노드는 그대로 (markdown 은 display only)
    - master 없으면 None (404 매핑)

    [2026-05-18 Phase 2 — Optimistic Locking]
    client_updated_at 매개변수 동작은 CPS 와 동일 (충돌 시 OptimisticLockConflict).

    mark_design_stale (2026-06-05):
        True  — 사용자 직접 편집. PRD 가 바뀌었으니 design_source_stale=true 로 마킹.
        False — 자동 정리(cleanup) 전용. 누더기 압축·정리는 의미 변경이 아니므로
                stale 재마킹 안 함(재생성 직후 cleanup 이 배너를 부활시키는 모순 방지).

    Returns:
        { master_id, last_updated } | None
    Raises:
        OptimisticLockConflict: client_updated_at 불일치
    """
    project_name = scoped_project(project_name, team_id)
    # [2026-05-26 데이터 무결성 가드] 빈 content 거부 — update_master_cps_markdown 동일 정책.
    if not content or not content.strip():
        raise ValueError(
            "PRD markdown 내용이 비어있습니다. master 데이터 손실 방지를 위해 거부합니다."
        )
    cypher = (
        _UPDATE_PRD_MARKDOWN_CYPHER if mark_design_stale
        else _UPDATE_PRD_MARKDOWN_NO_STALE_CYPHER
    )
    records = await neo4j_client.run_cypher(
        cypher,
        {
            "project": project_name,
            "content": content,
            "client_updated_at": client_updated_at,
        },
    )
    row = _first_row(records)
    if not row or not row.get("master_id"):
        if client_updated_at is not None and await _check_master_exists_prd(project_name):
            raise OptimisticLockConflict(
                "다른 디바이스에서 먼저 편집됐습니다. 새로고침 후 변경사항을 다시 적용해주세요."
            )
        return None
    return {
        "master_id": row["master_id"],
        "last_updated": int(row["last_updated"]) if row.get("last_updated") is not None else None,
    }


async def update_cps_node(
    project_name: str, node_id: str, summary: str, team_id: str = ""
) -> Optional[Dict[str, Any]]:
    """
    CPS 그래프의 단일 노드 (Problem 또는 Solution) 의 summary 수정.

    [Phase 3.1 — 노드 단위 inline edit]
    이전 phase 2 의 markdown 통째 편집보다 정교 — 특정 Problem 1개만 수정 가능.
    다만 markdown 재합성은 별도 (Phase 3.5+). 이번엔 graph 만 수정.

    [정책]
    - Problem / Solution 라벨만 매칭 (cypher whitelist) — Project/User/CPS_Document
      같은 다른 라벨은 매칭 안 됨.
    - project 필터 + 라우트 단 ownership 검증으로 IDOR 방어 이중망.
    - description 필드는 schema 에 없어 일단 summary 만 지원.

    Returns:
        { id, label, summary } or None (노드 없음 / 라벨 불일치 / project 불일치)
    """
    project_name = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        _UPDATE_CPS_NODE_CYPHER,
        {"project": project_name, "node_id": node_id, "summary": summary},
    )
    row = _first_row(rows)
    if not row or not row.get("id"):
        return None
    return {
        "id": row["id"],
        "label": row.get("label"),
        "summary": row.get("summary"),
    }


async def update_prd_node(
    project_name: str, node_id: str, summary: str, team_id: str = ""
) -> Optional[Dict[str, Any]]:
    """
    PRD 그래프의 단일 노드 (Epic 또는 Story) 의 summary 수정.

    [Phase 3.2 — CPS 와 동일 패턴]
    - cypher label whitelist: Epic | Story
    - project + node_id 필터 (이중망 IDOR 방어)
    - Screen 노드는 schema 가 다름 (name 필드) — 별도 PR
    - markdown 재합성 별도 (Phase 3.5+)

    Returns:
        { id, label, summary } or None
    """
    project_name = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        _UPDATE_PRD_NODE_CYPHER,
        {"project": project_name, "node_id": node_id, "summary": summary},
    )
    row = _first_row(rows)
    if not row or not row.get("id"):
        return None
    return {
        "id": row["id"],
        "label": row.get("label"),
        "summary": row.get("summary"),
    }


async def dismiss_cps_markdown_stale(project_name: str, team_id: str = "") -> bool:
    """
    [Phase 3.5a] CPS markdown_stale=false 로 set — banner dismiss 용.

    Returns:
        True 면 master 있음 + 업데이트 완료, False 면 master 없음 (404).
    """
    project_name = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        _DISMISS_CPS_STALE_CYPHER, {"project": project_name}
    )
    return bool(rows and rows[0].get("master_id"))


async def dismiss_prd_markdown_stale(project_name: str, team_id: str = "") -> bool:
    """[Phase 3.5a] PRD markdown_stale=false. CPS 와 동일 패턴."""
    project_name = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        _DISMISS_PRD_STALE_CYPHER, {"project": project_name}
    )
    return bool(rows and rows[0].get("master_id"))


# ─── [2026-05 Phase 3.6] Design source-stale helpers ─────────────────


async def mark_design_source_stale(project_name: str, team_id: str = "") -> None:
    """
    [Phase 3.6] PRD 갱신 시 Design (SPACK/DDD/Arch) 도 옛 PRD 기준이라 표시.

    호출 위치 (예정):
      - PRD 노드(Epic/Story) 수정 직후
      - 미팅 로그 재등록으로 PRD 가 재합성된 직후
      - PRD markdown 직접 편집 직후

    [정책]
    - Project 노드가 없으면 MERGE 로 생성 (이름만, 다른 속성은 안 건드림).
    - 이 함수는 fire-and-forget 성격 — 실패해도 호출자 흐름 깨면 안 됨.
      현 구현은 예외를 그대로 전파하지만 호출 측에서 try/except 로 감싸는 것을 권장.
    """
    project_name = scoped_project(project_name, team_id)
    await neo4j_client.run_cypher(
        _MARK_DESIGN_STALE_CYPHER, {"project": project_name}
    )


async def dismiss_design_source_stale(project_name: str, team_id: str = "") -> bool:
    """
    [Phase 3.6] FE banner 무시 — flag 만 false 로. design 자체는 재생성 안 함.

    Returns:
        True 면 Project 노드 존재 + 업데이트 완료, False 면 Project 없음 (404).
    """
    project_name = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        _DISMISS_DESIGN_STALE_CYPHER, {"project": project_name}
    )
    return bool(rows and rows[0].get("project"))


async def get_design_stale_status(project_name: str, team_id: str = "") -> Dict[str, Any]:
    """
    [Phase 3.6] FE 가 design 페이지 진입 시 호출 — banner 표시 여부 + 마킹 시각.

    Returns:
        {"design_source_stale": bool, "design_source_stale_at": Optional[int],
         "design_last_generated_at": Optional[int]}

    Project 노드가 없으면 (아직 산출물 0 단계) stale=false / at=None 반환.
    """
    project_name = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        _GET_DESIGN_STALE_CYPHER, {"project": project_name}
    )
    row = _first_row(rows) or {}
    at = row.get("design_source_stale_at")
    gen_at = row.get("design_last_generated_at")
    return {
        "design_source_stale": bool(row.get("design_source_stale") or False),
        "design_source_stale_at": int(at) if at is not None else None,
        "design_last_generated_at": int(gen_at) if gen_at is not None else None,
    }


async def get_design_impact(project_name: str, team_id: str = "") -> Dict[str, Any]:
    """
    [B — 2026-06] PRD 변경 → Design 영향 노드 분석.

    design 재생성 이후 수정된 Epic/Story 를 찾고, DERIVED_FROM|IMPLEMENTS → MAPPED_TO →
    BELONGS_TO / HANDLED_BY → CONNECTS_TO 경로를 cascade 로 탐색해 영향 레이어별 노드를 반환한다.
    (API 는 IMPLEMENTS, Entity/Service/DB 는 DERIVED_FROM 으로 Story 에 연결됨.)

    [Fix-A] Epic 편집 → CONTAINS → Story 경로를 통해 산하 설계 영향을 집계.
    [Tier]  design_layer 의 tier: IMPLEMENTS(API) 또는 confidence∈{direct,inferred}
            → "confirmed", 그 외(confidence none/null) → "review".
    [#2] design_layer id 기준 dedup (Epic 산하 Story 다중 경유 시 중복 방지).

    Returns:
        {
            "design_last_generated_at": Optional[int],
            "changed_nodes": [
                {
                    "node_id": str, "node_label": str, "summary": str,
                    "changed_at": int,
                    "impact_layers": {
                        "design": [{"id", "label", "via_story", "tier", "quote"}, ...],
                        "ddd":    [{"id", "label", "tier"}, ...],
                        "arch":   [{"id", "label", "tier"}, ...],
                    }
                },
                ...
            ],
            "total_affected_design_count": int,
        }
    """
    scoped = scoped_project(project_name, team_id)
    # design_last_generated_at 을 stale_status 와 같은 Project 노드에서 가져옴.
    stale = await get_design_stale_status(project_name, team_id=team_id)
    since = stale.get("design_last_generated_at") or 0

    rows = await neo4j_client.run_cypher(
        _GET_DESIGN_IMPACT_CYPHER, {"project": scoped, "since": since}
    )

    changed_nodes: List[Dict[str, Any]] = []
    affected_ids: set = set()
    for row in rows:
        if not row.get("node_id"):
            continue
        # [#2] design_layer dedup by id: Epic 산하 여러 Story가 같은 design을 공유할 때
        # collect(DISTINCT {…, via_story}) 가 via_story 차이로 중복 수집되는 문제 해소.
        # confirmed > review 우선순위로 유지.
        seen_design: Dict[str, Dict[str, Any]] = {}
        for d in (row.get("design_layer") or []):
            if not (isinstance(d, dict) and d.get("id")):
                continue
            # tier 결정:
            #  - IMPLEMENTS (API→Story): 명시적·구조적 구현 링크 → 항상 confirmed
            #  - DERIVED_FROM: lineage confidence 가 direct/inferred 면 confirmed,
            #    none/null 이면 review(근거 없음 → 사람 검토 필요)
            rel_type = str(d.get("rel_type") or "")
            confidence = str(d.get("confidence") or "")
            is_confirmed = rel_type == "IMPLEMENTS" or confidence in ("direct", "inferred")
            item = {
                "id": d["id"],
                "label": d["label"],
                "tier": "confirmed" if is_confirmed else "review",
                "quote": d.get("quote") or "",
                "error_cases": d.get("error_cases") or "",
            }
            existing = seen_design.get(item["id"])
            if existing is None or (item["tier"] == "confirmed" and existing["tier"] == "review"):
                seen_design[item["id"]] = item
        design_layer = list(seen_design.values())

        # [L6b] ddd_layer에 evt_pub(발행 Aggregate)가 합산되므로 id 기준 dedup
        seen_ddd: Dict[str, Dict[str, Any]] = {}
        for x in (row.get("ddd_layer") or []):
            if isinstance(x, dict) and x.get("id") and x["id"] not in seen_ddd:
                seen_ddd[x["id"]] = {"id": x["id"], "label": x["label"], "tier": "estimated"}
        ddd_layer = list(seen_ddd.values())
        arch_layer = [
            {"id": x["id"], "label": x["label"], "tier": "estimated"}
            for x in (row.get("arch_layer") or [])
            if isinstance(x, dict) and x.get("id")
        ]
        # [L4] Screen dedup by id (RENDERS + CALLS_API 두 경로 합산 시 중복 방지)
        seen_screens: Dict[str, Dict[str, Any]] = {}
        for s in (row.get("screen_layer") or []):
            if isinstance(s, dict) and s.get("id") and s["id"] not in seen_screens:
                seen_screens[s["id"]] = {"id": s["id"], "name": s.get("name") or "", "tier": "direct"}
        screen_layer = list(seen_screens.values())
        # [L5] API chain dedup by id + 원본 design API 제외 (이미 design_layer에 있음)
        design_ids = {d["id"] for d in design_layer}
        seen_peers: Dict[str, Dict[str, Any]] = {}
        for p in (row.get("api_chain_layer") or []):
            if isinstance(p, dict) and p.get("id") and p["id"] not in design_ids:
                seen_peers[p["id"]] = {
                    "id": p["id"],
                    "endpoint": p.get("endpoint") or "",
                    "method": p.get("method") or "",
                    "tier": "estimated",
                }
        api_chain_layer = list(seen_peers.values())
        # [L6] DomainEvent dedup by id
        seen_events: Dict[str, Dict[str, Any]] = {}
        for e in (row.get("event_layer") or []):
            if isinstance(e, dict) and e.get("id") and e["id"] not in seen_events:
                seen_events[e["id"]] = {"id": e["id"], "label": "DomainEvent", "tier": "estimated"}
        event_layer = list(seen_events.values())
        for d in design_layer:
            affected_ids.add(d["id"])
        changed_nodes.append({
            "node_id": str(row["node_id"]),
            "node_label": str(row.get("node_label") or ""),
            "summary": str(row.get("summary") or ""),
            "changed_at": int(row["changed_at"]),
            "impact_layers": {
                "design": design_layer,
                "ddd": ddd_layer,
                "arch": arch_layer,
                "events": event_layer,
                "screens": screen_layer,
                "api_chain": api_chain_layer,
            },
        })

    return {
        "design_last_generated_at": stale.get("design_last_generated_at"),
        "changed_nodes": changed_nodes,
        "total_affected_design_count": len(affected_ids),
    }


async def get_design_quality(project_name: str, team_id: str = "") -> Dict[str, Any]:
    """
    [2026-06] MAPPED_TO.role 품질 체크 — aggregate_root 제약 위반 탐지.

    LLM 프롬프트는 각 Aggregate 에 aggregate_root 엔티티를 정확히 1개 지정하도록 지시.
    이 함수는 Neo4j 그래프에서 그 제약이 지켜지는지 검증한다.

    Returns:
        {"violation_count": int, "violations": [{aggregate_id, aggregate_name,
          root_count, root_entity_ids, violation_type}, ...]}
    """
    scoped = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        _GET_DESIGN_QUALITY_CYPHER, {"project": scoped}
    )
    violations = [
        {
            "aggregate_id": r["aggregate_id"],
            "aggregate_name": r.get("aggregate_name") or r["aggregate_id"],
            "root_count": int(r["root_count"]),
            "root_entity_ids": list(r.get("root_entity_ids") or []),
            "violation_type": str(r["violation_type"]),
        }
        for r in (rows or [])
        if r.get("aggregate_id")
    ]
    return {"violation_count": len(violations), "violations": violations}


async def list_cps_nodes(project_name: str, team_id: str = "") -> List[Dict[str, Any]]:
    """
    CPS 그래프의 Problem / Solution 노드 전체 listing.

    [Phase 3.3 — FE 사이드바 inline edit 대응]
    FE 가 markdown 파싱 (display ID 'PRB-01') 대신 실제 그래프 ID 로 PATCH 호출.
    """
    project_name = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        _LIST_CPS_NODES_CYPHER, {"project": project_name}
    )
    return [
        {
            "id": r.get("id"),
            "label": r.get("label"),
            "summary": r.get("summary"),
        }
        for r in (rows or [])
        if r.get("id")
    ]


async def list_prd_nodes(project_name: str, team_id: str = "") -> List[Dict[str, Any]]:
    """
    PRD 그래프의 Epic / Story 노드 전체 listing.

    [Phase 3.3 — CPS 와 동일 패턴]
    """
    project_name = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        _LIST_PRD_NODES_CYPHER, {"project": project_name}
    )
    return [
        {
            "id": r.get("id"),
            "label": r.get("label"),
            "summary": r.get("summary"),
        }
        for r in (rows or [])
        if r.get("id")
    ]


# ─── [Phase 3.5b] LLM 기반 graph → markdown 재합성 ───────────────


# 출력이 markdown 인지 확인 — LLM 이 가끔 빈 문자열 / 단순 텍스트 반환 가드.
_CPS_MD_HEADER_HINTS = ("## 📄 CPS", "## CPS", "# CPS")
_PRD_MD_HEADER_HINTS = ("# PRD", "## PRD", "## 1.", "## Product Overview")


def _looks_like_cps_markdown(s: str) -> bool:
    if not s or len(s) < 50:
        return False
    head = s.lstrip()[:200]
    return any(h in head for h in _CPS_MD_HEADER_HINTS)


def _looks_like_prd_markdown(s: str) -> bool:
    if not s or len(s) < 50:
        return False
    head = s.lstrip()[:300]
    return any(h in head for h in _PRD_MD_HEADER_HINTS)


def _format_nodes_for_prompt(nodes: List[Dict[str, Any]]) -> str:
    """그래프 노드 리스트 → LLM 입력용 plain text. label 별 그룹."""
    if not nodes:
        return "(노드 없음)"
    by_label: Dict[str, List[Dict[str, Any]]] = {}
    for n in nodes:
        by_label.setdefault(n.get("label") or "Unknown", []).append(n)
    parts: List[str] = []
    for label in sorted(by_label.keys()):
        parts.append(f"## {label}")
        for n in by_label[label]:
            nid = n.get("id") or ""
            summary = (n.get("summary") or "").strip()
            parts.append(f"- id={nid}\n  summary: {summary}")
    return "\n".join(parts)


def _prd_has_nested_epic_story(s: str, node_count: Dict[str, int]) -> bool:
    """
    [Phase 3.5c] PRD 재합성 출력의 nested Epic↔Story 계층 보존 검증.

    PRD markdown 은 Epic 아래 Story 들이 nested 되는 구조여야 함:
        #### 📦 Epic N: ...
        - 📝 Story N.M: ...

    Returns True if:
    - 출력에 Epic 갯수가 graph 의 Epic 갯수 +/- 1 범위 (LLM 이 1개 정도 머지하는 건 허용)
    - graph 에 Story 가 1개 이상이면 출력에 Story 마커도 있어야 함
    - Epic 마커 (#### 📦) 가 Story 마커 (Story N.M 또는 -) 보다 먼저 등장하는 일관성

    완벽한 구조 파싱은 안 함 — 명확한 violation 만 거부.
    """
    epic_count_in_graph = node_count.get("Epic", 0)
    story_count_in_graph = node_count.get("Story", 0)

    # graph 자체에 Epic 이 없으면 검증 의미 없음 — 통과.
    if epic_count_in_graph == 0:
        return True

    # 출력의 Epic 마커 — 굳이 '#### 📦' 정확 매칭 안 함, 'Epic ' 단어 등장 수로 근사.
    import re
    epic_matches = re.findall(r"(?im)^(?:####?\s*)?(?:📦\s*)?Epic\s*\d+", s)
    output_epic_count = len(epic_matches)

    # 그래프와 너무 차이 나면 거부 (절반 이하 또는 두 배 초과).
    if output_epic_count == 0:
        return False
    if output_epic_count < max(1, epic_count_in_graph // 2):
        return False
    if output_epic_count > epic_count_in_graph * 2:
        return False

    # graph 에 Story 가 있으면 출력에도 'Story' 단어 등장해야 함.
    if story_count_in_graph > 0 and "Story" not in s:
        return False

    return True


async def resync_cps_markdown_from_graph(
    ctx: Any, project_name: str, team_id: str = ""
) -> Optional[str]:
    """
    [Phase 3.5b/c] CPS markdown 을 그래프 상태에 맞게 LLM 으로 재합성 — preview 만.

    [흐름]
      1. master CPS markdown + 그래프의 Problem/Solution 노드 fetch
      2. resync_cps_from_graph.md 프롬프트 렌더링
      3. ctx.gemini.generate() — TrackedGemini 가 토큰 누적
      4. 출력이 CPS markdown 형식인지 검증 — 실패 시 None
      5. **save 안 함** (Phase 3.5c: preview 패턴) — caller (FE) 가 diff 보고
         PATCH /api/v2/cps 로 명시적 저장.

    Args:
      ctx: tracked_pipeline_context() yield 결과 (gemini + neo4j + key)
      project_name: 대상 프로젝트

    Returns:
      새 markdown 문자열 (preview), 또는 None.

    Raises:
      GeminiError: LLM 호출 실패
    """
    project_name = scoped_project(project_name, team_id)
    master = await get_master_cps(project_name)
    if master is None:
        return None
    nodes = await list_cps_nodes(project_name)
    if not nodes:
        return None

    # 지연 import — pipelines 의존성을 service 레이어에 끌어오지 않게.
    from app.pipelines.cps_pipeline import _load_prompt
    from app.pipelines.base import strip_code_blocks

    prompt = _load_prompt("resync_cps_from_graph.md")
    prompt = prompt.replace("<<current_markdown>>", master.content or "")
    prompt = prompt.replace("<<graph_nodes>>", _format_nodes_for_prompt(nodes))

    result = await ctx.gemini.generate(prompt, temperature=0.1)
    new_md = strip_code_blocks(result.text or "")

    if not _looks_like_cps_markdown(new_md):
        # LLM 이 형식 못 지킴 — preview 반환 안 함.
        return None
    return new_md


async def resync_prd_markdown_from_graph(
    ctx: Any, project_name: str, team_id: str = ""
) -> Optional[str]:
    """
    [Phase 3.5b/c] PRD markdown 재합성 — preview 만 (CPS 와 동일 패턴).

    추가로 nested Epic↔Story 계층 보존 검증 — _prd_has_nested_epic_story.

    Returns: 새 markdown (preview) 또는 None.
    Raises: GeminiError
    """
    project_name = scoped_project(project_name, team_id)
    master = await get_master_prd(project_name)
    if master is None:
        return None
    nodes = await list_prd_nodes(project_name)
    if not nodes:
        return None

    # 라벨별 카운트 — 구조 검증용.
    node_count: Dict[str, int] = {}
    for n in nodes:
        label = n.get("label") or "Unknown"
        node_count[label] = node_count.get(label, 0) + 1

    from app.pipelines.cps_pipeline import _load_prompt
    from app.pipelines.base import strip_code_blocks

    prompt = _load_prompt("resync_prd_from_graph.md")
    prompt = prompt.replace("<<current_markdown>>", master.prd_content or "")
    prompt = prompt.replace("<<graph_nodes>>", _format_nodes_for_prompt(nodes))

    result = await ctx.gemini.generate(prompt, temperature=0.1)
    new_md = strip_code_blocks(result.text or "")

    if not _looks_like_prd_markdown(new_md):
        return None
    # [Phase 3.5c] nested 계층 검증 — Epic 개수가 비정상이면 거부.
    if not _prd_has_nested_epic_story(new_md, node_count):
        return None
    return new_md


async def get_ddd_graph(project_name: str, team_id: str = "") -> DddGraph:
    """DDD 노드는 없을 수도 있으므로 항상 (비어있는) DddGraph 반환."""
    project_name = scoped_project(project_name, team_id)
    records = await neo4j_client.run_cypher(
        _GET_DDD_CYPHER, {"project": project_name}
    )
    row = _first_row(records) or {}
    return DddGraph(
        contexts=_clean_node_list(row.get("contexts")),
        # [D-1 — 2026-05-25] invariants / attributes / payload_fields 가 JSON
        # string 으로 저장됨. decode_* 가 객체 복원. legacy 데이터도 안전.
        aggregates=decode_aggregates_detail(_clean_node_list(row.get("aggregates"))),
        domain_entities=decode_domain_entities_detail(
            _clean_node_list(row.get("domain_entities"))
        ),
        domain_events=decode_domain_events_detail(
            _clean_node_list(row.get("domain_events"))
        ),
        internal_rels=_clean_rel_list(row.get("internal_rels")),
        trigger_rels=_clean_rel_list(row.get("trigger_rels")),
        aggregate_service_rels=_clean_cross_rel_list(row.get("aggregate_service_rels")),
    )


async def get_spack_graph(project_name: str, team_id: str = "") -> SpackGraph:
    project_name = scoped_project(project_name, team_id)
    records = await neo4j_client.run_cypher(
        _GET_SPACK_CYPHER, {"project": project_name}
    )
    row = _first_row(records) or {}
    return SpackGraph(
        # [A-2 — 2026-05-25] API 노드의 path_params/query_params/request_body/
        # response_body 가 JSON string 으로 저장됨. decode_apis_payload 가 객체
        # 복원. legacy API (4개 필드 미존재) 도 빈 객체로 정규화.
        apis=decode_apis_payload(_clean_node_list(row.get("apis"))),
        # [A-1 — 2026-05-25] attributes 가 Neo4j 에 JSON string 으로 저장됨.
        # decode_entities_attributes 가 객체 list 로 복원 + legacy string list
        # 데이터도 자동 마이그레이트.
        entities=decode_entities_attributes(_clean_node_list(row.get("entities"))),
        policies=_clean_node_list(row.get("policies")),
        # [#3 — 2026-05-25] Screen 노드 (cypher 에서 calls_apis 이미 추출).
        screens=_clean_node_list(row.get("screens")),
        internal_rels=_clean_rel_list(row.get("internal_rels")),
        implement_rels=_clean_rel_list(row.get("implement_rels")),
        entity_mapping_rels=_clean_cross_rel_list(row.get("entity_mapping_rels")),
        api_service_rels=_clean_cross_rel_list(row.get("api_service_rels")),
    )


# [AI 초안 보완 — 2026-05-29] 단일 API 노드의 error_cases/auth 만 부분 갱신.
# ★ Wipe-and-Redraw (build_save_spack_query) 와 절대 분리. 그 쿼리는 project
#   단위 DETACH DELETE 라서 autofill 용으로 쓰면 다른 노드를 전부 날린다.
#   여기서는 MATCH 한 단일 API 노드의 두 속성만 SET — 다른 데이터 무손상.
# 저장 형식은 기존 SPACK 저장과 동일하게 JSON string 직렬화 (Neo4j primitive
# 제약 우회, decode_apis_payload 가 read 시 복원). source/reviewed 메타는
# normalize 단계에서 보존되므로 직렬화 결과에도 포함된다.
_UPDATE_API_SPECS_CYPHER = """\
MATCH (a:API {id: $id, project: $project})
SET a.error_cases = $error_cases,
    a.auth = $auth,
    a.updated_at = timestamp()
RETURN a.id AS id
"""


async def update_api_error_and_auth(
    project_name: str,
    api_id: str,
    error_cases: List[Dict[str, Any]],
    auth: Dict[str, Any],
    team_id: str = "",
) -> bool:
    """단일 API 노드의 error_cases/auth 만 SET. 갱신 성공 여부 반환.

    error_cases (list) / auth (dict) 는 객체 형태로 받아 JSON string 직렬화 후 저장.
    ★ 정규화(normalize_*)를 거치지 않고 그대로 직렬화 — 호출자(autofill 파이프라인)가
      이미 정규화 + 메타(source/reviewed) 부착을 마친 결과를 넘긴다. 여기서 다시
      normalize 를 돌리면 메타가 보존되긴 하나 책임 경계를 흐리므로 직렬화만 수행.
    노드가 없으면 (잘못된 id 등) False — 호출자가 부분 실패로 처리.
    """
    project_name = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        _UPDATE_API_SPECS_CYPHER,
        {
            "project": project_name,
            "id": api_id,
            "error_cases": json.dumps(error_cases or [], ensure_ascii=False),
            "auth": json.dumps(auth or {}, ensure_ascii=False),
        },
    )
    row = _first_row(rows)
    return bool(row and row.get("id"))


# [연결 채우기 — 2026-06-12] PRD 연결(스토리 추적)이 끊긴 노드의 부분 패치.
# ★ api_spec_autofill 의 update_api_error_and_auth 와 같은 정책: Wipe-and-Redraw 와
#   절대 분리 — MATCH 한 단일 노드의 연결 속성 + 엣지만 갱신, 다른 데이터 무손상.
# Story 노드도 MATCH(OPTIONAL 아님) — 파이프라인이 whitelist 검증을 하지만, 혹시
# 그 사이 PRD 재생성으로 스토리가 사라졌으면 no-row → False (dangling 엣지 방지).
# AI 매칭 결과는 confidence='inferred' + link_source='ai_autofill' 로 정직하게 마킹.
_UPDATE_STORY_LINK_CYPHER: Dict[str, str] = {
    "API": """\
MATCH (n:API {id: $id, project: $project})
MATCH (s:Story {id: $story_id, project: $project})
MERGE (n)-[:IMPLEMENTS]->(s)
SET n.related_story_id = $story_id,
    n.lineage_confidence = 'inferred',
    n.link_source = 'ai_autofill',
    n.updated_at = timestamp()
RETURN n.id AS id
""",
    "Entity": """\
MATCH (n:Entity {id: $id, project: $project})
MATCH (s:Story {id: $story_id, project: $project})
MERGE (n)-[r:DERIVED_FROM]->(s)
SET r.confidence = 'inferred',
    n.lineage_confidence = CASE
      WHEN n.lineage_confidence IN ['direct', 'inferred'] THEN n.lineage_confidence
      ELSE 'inferred' END,
    n.lineage_story_count = size([(n)-[:DERIVED_FROM]->(:Story) | 1]),
    n.link_source = 'ai_autofill',
    n.updated_at = timestamp()
RETURN n.id AS id
""",
    "Policy": """\
MATCH (n:Policy {id: $id, project: $project})
MATCH (s:Story {id: $story_id, project: $project})
MERGE (n)-[r:DERIVED_FROM]->(s)
SET r.confidence = 'inferred',
    n.related_story_id = $story_id,
    n.lineage_confidence = 'inferred',
    n.link_source = 'ai_autofill',
    n.updated_at = timestamp()
RETURN n.id AS id
""",
}


async def update_node_story_link(
    project_name: str,
    node_label: str,
    node_id: str,
    story_id: str,
    team_id: str = "",
) -> bool:
    """단일 API/Entity/Policy 노드의 PRD 연결만 SET + 엣지 MERGE. 성공 여부 반환.

    node_label 은 whitelist(_UPDATE_STORY_LINK_CYPHER 키) 외엔 False — Cypher 라벨은
    파라미터 바인딩이 불가하므로 사전 정의 쿼리로만 실행 (주입 차단).
    """
    cypher = _UPDATE_STORY_LINK_CYPHER.get(node_label)
    if not cypher or not node_id or not story_id:
        return False
    project_name = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        cypher, {"project": project_name, "id": node_id, "story_id": story_id},
    )
    row = _first_row(rows)
    return bool(row and row.get("id"))


# [완성도 어시스턴트 — 2026-06-06] AI 초안 검토 완료 → 만점 반영.
# 스코어러 _item_review_weight: source=='ai_draft' & reviewed!=True → 0.5, 그 외 1.0.
# autofill 이 생성한 초안에 reviewed=True 를 부착해 완성도 점수를 정직하게 끌어올림.
_GET_API_SPECS_CYPHER = """\
MATCH (a:API {id: $id, project: $project})
RETURN a.error_cases AS error_cases, a.auth AS auth
"""

_GET_ALL_API_SPECS_CYPHER = """\
MATCH (a:API {project: $project})
RETURN a.id AS id, a.error_cases AS error_cases, a.auth AS auth
"""


def _decode_json_prop(value: Any, default: Any) -> Any:
    """Neo4j 에 JSON string 으로 저장된 error_cases(list)/auth(dict) 복원.
    이미 객체면 그대로, 파싱 실패/None 이면 default."""
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
    return default


async def mark_api_reviewed(project_name: str, api_id: str, team_id: str = "") -> bool:
    """단일 API 의 error_cases[*]/auth 에 reviewed=True 부착(검토 완료 → 만점).

    사용자가 해당 API 의 '검토 완료'를 누른 것이므로 항목 전체에 reviewed=True.
    (이미 1.0 인 비-초안 항목엔 무해 — 점수 불변.) 노드 없으면 False.
    """
    scoped = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(
        _GET_API_SPECS_CYPHER, {"project": scoped, "id": api_id}
    )
    row = _first_row(rows)
    if not row:
        return False
    error_cases = _decode_json_prop(row.get("error_cases"), [])
    auth = _decode_json_prop(row.get("auth"), {})
    for ec in error_cases:
        if isinstance(ec, dict):
            ec["reviewed"] = True
    if isinstance(auth, dict) and auth:
        auth["reviewed"] = True
    # update_api_error_and_auth 가 scoped_project 를 다시 적용 → 원본 project_name 전달.
    return await update_api_error_and_auth(
        project_name, api_id, error_cases, auth, team_id=team_id
    )


async def mark_all_apis_reviewed(project_name: str, team_id: str = "") -> int:
    """프로젝트의 'AI 초안 미검토'(source=='ai_draft' & reviewed!=True) 항목을 가진
    모든 API 를 일괄 검토 완료. 실제로 바뀐 API 수 반환."""
    scoped = scoped_project(project_name, team_id)
    rows = await neo4j_client.run_cypher(_GET_ALL_API_SPECS_CYPHER, {"project": scoped})
    count = 0
    for row in rows or []:
        api_id = row.get("id")
        if not api_id:
            continue
        error_cases = _decode_json_prop(row.get("error_cases"), [])
        auth = _decode_json_prop(row.get("auth"), {})
        changed = False
        for ec in error_cases:
            if (
                isinstance(ec, dict)
                and ec.get("source") == "ai_draft"
                and ec.get("reviewed") is not True
            ):
                ec["reviewed"] = True
                changed = True
        if (
            isinstance(auth, dict)
            and auth.get("source") == "ai_draft"
            and auth.get("reviewed") is not True
        ):
            auth["reviewed"] = True
            changed = True
        if changed:
            ok = await update_api_error_and_auth(
                project_name, api_id, error_cases, auth, team_id=team_id
            )
            if ok:
                count += 1
    return count


async def get_architecture_graph(project_name: str, team_id: str = "") -> ArchitectureGraph:
    project_name = scoped_project(project_name, team_id)
    records = await neo4j_client.run_cypher(
        _GET_ARCHITECTURE_CYPHER, {"project": project_name}
    )
    row = _first_row(records) or {}
    # [D-2 — 2026-05-25] services 의 deployment/external_dependencies (JSON
    # string), connections 의 auth (enum) 객체 복원. legacy 노드 안전.
    # connections 의 protocol/description 외 새 auth 필드 통합은
    # _clean_rel_list 가 GraphRel pydantic 모델로 변환 — auth 가 GraphRel 에
    # 없으면 무시되므로, raw row 의 dict 형태에서 별도 처리.
    raw_connections = row.get("connections") or []
    # _clean_rel_list 는 GraphRel(BaseModel) 로 변환 — 우리 auth 는 모델 외부.
    # 일단 기존 그대로 (GraphRel 확장은 별 작업); 다만 lint/fix 경로는 dict 라 OK.
    return ArchitectureGraph(
        services=decode_services_detail(_clean_node_list(row.get("services"))),
        databases=_clean_node_list(row.get("databases")),
        connections=_clean_rel_list(raw_connections),
    )


def _strip_heavy(props: Any) -> Dict[str, Any]:
    """fallback 경로 후 호출 — embedding/full_markdown/raw_content 제거."""
    if not isinstance(props, dict):
        return {}
    return {k: v for k, v in props.items() if k not in _HEAVY_PROP_KEYS}


async def get_project_graph(project_name: str, team_id: str = "") -> ProjectGraph:
    """
    프로젝트 단위 read-only 그래프 스냅샷.

    프론트가 Neo4j 에 직접 접근하지 않도록 BE 가 project 격리된 결과만 반환.
    노드/엣지 수가 cap 을 넘으면 잘라서 반환 — 응답 크기/메모리 보호.

    APOC 미설치 환경 대비 fallback Cypher 자동 시도.
    """
    project_name = scoped_project(project_name, team_id)
    records: List[Dict[str, Any]] = []
    try:
        records = await neo4j_client.run_cypher(
            _GET_PROJECT_GRAPH_CYPHER, {"project": project_name}
        )
    except Exception as e:  # noqa: BLE001
        # APOC procedure 미설치 등 — fallback 으로 재시도.
        logger.warning(
            "get_project_graph: primary cypher failed (%s) — falling back", e
        )
        records = await neo4j_client.run_cypher(
            _GET_PROJECT_GRAPH_FALLBACK_CYPHER, {"project": project_name}
        )

    row = _first_row(records) or {}
    raw_nodes = row.get("nodes") or []
    raw_edges = row.get("edges") or []

    nodes: List[GraphNode] = []
    for n in raw_nodes[:_MAX_GRAPH_NODES]:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        nlabel = n.get("label")
        if not nid or not nlabel:
            continue
        nodes.append(
            GraphNode(
                id=str(nid),
                label=str(nlabel),
                properties=_strip_heavy(n.get("properties")),
            )
        )

    valid_ids = {n.id for n in nodes}
    edges: List[GraphEdge] = []
    for e in raw_edges[:_MAX_GRAPH_EDGES]:
        if not isinstance(e, dict):
            continue
        src, tgt, etype = e.get("source_id"), e.get("target_id"), e.get("type")
        if not (src and tgt and etype):
            continue
        # 노드 cap 으로 인해 잘려나간 엣지는 dangling 방지 — 양쪽 다 포함된 경우만.
        if str(src) not in valid_ids or str(tgt) not in valid_ids:
            continue
        # [D — 2026-05] edge properties 보존 — lineage 관계 (confidence/quote) 표시용
        eprops = e.get("properties") if isinstance(e.get("properties"), dict) else {}
        edges.append(
            GraphEdge(
                source_id=str(src), target_id=str(tgt), type=str(etype),
                properties=eprops,
            )
        )

    return ProjectGraph(project=project_name, nodes=nodes, edges=edges)


def _story_id_candidates(major: str, minor: str) -> List[str]:
    """
    하나의 (major, minor) 페어에 대해 가능한 Neo4j story id 후보 모두 생성.

    [2026-05] prd_graph.md 의 ID_NORMALIZATION 규칙은 zero-pad 예시 (story_01_1) 를
    보이지만 LLM 이 실제 출력하는 id 형식이 항상 zero-pad 라고 보장 안 됨.
    PRD markdown 의 표기 ('[Story 2.1]', '[Story-02.1]') 와 prd_graph 가 만든
    Neo4j id 사이 zero-pad 불일치 시 매칭 실패 → 빈 그래프.

    이 헬퍼는 zero-pad / non-zero-pad / minor zero-pad 등 가능한 변형 모두 후보로
    반환해 'WHERE story.id IN $ids' 에서 어느 형식이든 매칭되도록 한다.

    Returns: ['story_2_1', 'story_02_1'] 같은 후보 list (dedupe + 안정 순서)
    """
    candidates: List[str] = []
    forms = {
        f"story_{int(major)}_{int(minor)}",            # raw → 'story_2_1'
        f"story_{int(major):02d}_{int(minor)}",        # major zero-pad → 'story_02_1'
        f"story_{int(major)}_{int(minor):02d}",        # minor zero-pad → 'story_2_01'
        f"story_{int(major):02d}_{int(minor):02d}",    # 둘 다 zero-pad → 'story_02_01'
    }
    return sorted(forms)


# [2026-05-28] Story id 에서 (major, minor) 정수 페어 추출 — Phase 2 fallback fuzzy 매칭용.
# LLM 이 만드는 Story id 형식은 prd_graph.md 의 ID_NORMALIZATION 예시 ('story_01_1') 와
# 다를 수 있음 — 3-digit zero-pad, 다른 separator, 다른 prefix 등. Cypher IN 매칭이
# 변형을 모두 흡수하지 못해 그래프가 비어 보이는 'stories_match_no_data' 케이스 다수.
# 따라서 ID 의 문자열 형식이 무엇이든 (major, minor) 페어로 정규화해 비교.
#
# 매칭 규칙: 임의 prefix → 첫 정수(major) → separator (`_`, `-`, `.`, ` `) → 두번째 정수(minor)
# 예) 'story_2_1', 'story_02_1', 'story_001_001', 'story-1-1', 's_1_1', 'Story 1.1'
_STORY_ID_PAIR_RE = re.compile(r"^[^\d]*0*(\d+)[._\-\s]+0*(\d+)\s*$")

# [2026-06] PRD 포맷 호환 강화 — Screen Architecture 섹션의 Story 참조가
# '[Story 1.1]'(대괄호) 뿐 아니라 '`Story 1.1`'(백틱) 또는 무괄호로도 나온다.
# 현 prd_extract 는 '포함된 기능' 목록에 '`Story 1.1`'(백틱) 형태로 적어서,
# 대괄호만 보던 기존 regex 가 0건 매칭 → 'no_implemented_on' 빈 그래프 버그가 났다.
# 여는/닫는 구분자(대괄호·백틱)를 옵션으로 만들어 세 형식을 모두 흡수한다.
_STORY_REF_RE = re.compile(r"[\[`]?Story[- ](\d+)[.\-_](\d+)[\]`]?")

# User Flow 안에 화면명이 '데이터 소스 관리' 화면 처럼 따옴표로 감싸 등장하는 케이스 흡수 —
# 화면명과 '화면' 사이의 따옴표(' " ‘ ’ “ ”)를 옵션으로 허용. (정규/스마트 따옴표 모두)
_SCREEN_QUOTE_CHARS = "'\"‘’“”"


def _screen_in_block_re(safe: str) -> "re.Pattern[str]":
    """Story block 본문에서 '<화면명> 화면' / '[<화면명>] 화면' (+따옴표) 매칭 regex."""
    return re.compile(
        r"(\[\s*" + safe + r"\s*\]|" + safe + r")["
        + re.escape(_SCREEN_QUOTE_CHARS) + r"\s]*화면"
    )


def _parse_story_major_minor(story_id: Optional[str]) -> Optional[Tuple[int, int]]:
    """Neo4j Story id 에서 (major, minor) 정수 페어 추출. 매칭 실패 시 None."""
    if not story_id:
        return None
    m = _STORY_ID_PAIR_RE.match(str(story_id))
    if not m:
        return None
    try:
        return (int(m.group(1)), int(m.group(2)))
    except (ValueError, TypeError):
        return None


def _extract_screen_story_pairs_from_markdown(
    markdown: str, screen_name: str
) -> List[Tuple[int, int]]:
    """
    PRD 마크다운에서 주어진 screen 에 속한 Story 의 (major, minor) 페어 목록 추출.

    `_extract_screen_story_ids_from_markdown` 와 같은 2단계 매칭(섹션/inline)을 쓰되,
    ID 후보 문자열 대신 정수 페어를 반환 — Phase 2 fallback 에서 Neo4j 의 모든 Story 와
    페어 비교로 fuzzy 매칭하기 위함.
    """
    if not markdown or not screen_name:
        return []

    safe = re.escape(screen_name)
    pairs: List[Tuple[int, int]] = []

    # Phase 1: Screen Architecture 섹션
    pattern = rf"####\s*[^\[]*\[Screen:\s*{safe}\s*\](.*?)(?=####\s|^###\s|\Z)"
    m = re.search(pattern, markdown, flags=re.DOTALL | re.MULTILINE)
    if m:
        section = m.group(1)
        story_refs = _STORY_REF_RE.findall(section)
        for major, minor in story_refs:
            try:
                pairs.append((int(major), int(minor)))
            except (ValueError, TypeError):
                pass
        if pairs:
            return list(dict.fromkeys(pairs))

    # Phase 2: Story block inline 매칭
    story_block_re = re.compile(
        r"-\s*\*\*\[Story[- ](\d+)[.\-_](\d+)\][^\n]*\*\*"
        r"(.*?)"
        r"(?=-\s*\*\*\[Story[- ]\d+[.\-_]\d+\]|"
        r"####\s|"
        r"^###\s|"
        r"\Z)",
        flags=re.DOTALL | re.MULTILINE,
    )
    screen_in_block_re = _screen_in_block_re(safe)
    for match in story_block_re.finditer(markdown):
        major, minor, body = match.group(1), match.group(2), match.group(3)
        if screen_in_block_re.search(body):
            try:
                pairs.append((int(major), int(minor)))
            except (ValueError, TypeError):
                pass

    return list(dict.fromkeys(pairs))


def _extract_screen_story_ids_from_markdown(
    markdown: str, screen_name: str
) -> List[str]:
    """
    PRD 마크다운에서 주어진 screen 에 속한 Story id 목록 추출 (Neo4j 포맷: story_XX_X).

    [2단계 매칭 — 2026-05 강화]

    1차 — '#### 🖥️ [Screen: 화면명]' 별도 섹션 매칭 (옛 PRD 호환)
       옛 prd_extract 출력 또는 외부 source 에 Screen Architecture 가 있을 때.

    2차 — Story block 안의 User Flow 텍스트 매칭 (★ 신규 — 현 prd_extract 형식)
       현 prd_extract.md 는 'Screen Architecture' 섹션을 만들지 않고 (TEMPLATE_STRICT_MATCH
       규칙) 화면 이름을 각 Story 의 User Flow 안에 인라인으로 적음:
         - **[Story 1.1] [기능명]**
           - **User Flow**: 1. 사용자가 [메인] 화면에서 ... → 2. ...
       각 Story block 내부에 화면명이 '[<name>] 화면' 또는 '<name> 화면' 패턴으로
       포함된지 검사.

    [zero-pad 변형 흡수]
      LLM 이 만든 Neo4j Story id 가 'story_2_1' / 'story_02_1' 등 어느 형식이든
      매칭되도록 각 (major, minor) 페어마다 후보 4개 (raw / major-pad / minor-pad /
      both-pad) 를 모두 반환.
    """
    if not markdown or not screen_name:
        return []

    safe = re.escape(screen_name)
    ids: List[str] = []

    # ─── 1차: 기존 Screen Architecture 섹션 (옛 PRD 호환) ───
    pattern = rf"####\s*[^\[]*\[Screen:\s*{safe}\s*\](.*?)(?=####\s|^###\s|\Z)"
    m = re.search(pattern, markdown, flags=re.DOTALL | re.MULTILINE)
    if m:
        section = m.group(1)
        story_refs = _STORY_REF_RE.findall(section)
        for major, minor in story_refs:
            ids.extend(_story_id_candidates(major, minor))
        if ids:
            return list(dict.fromkeys(ids))

    # ─── 2차: Story block 안의 화면명 매칭 (현 prd_extract 형식) ───
    story_block_re = re.compile(
        r"-\s*\*\*\[Story[- ](\d+)[.\-_](\d+)\][^\n]*\*\*"
        r"(.*?)"
        r"(?=-\s*\*\*\[Story[- ]\d+[.\-_]\d+\]|"
        r"####\s|"
        r"^###\s|"
        r"\Z)",
        flags=re.DOTALL | re.MULTILINE,
    )
    screen_in_block_re = _screen_in_block_re(safe)
    for match in story_block_re.finditer(markdown):
        major, minor, body = match.group(1), match.group(2), match.group(3)
        if screen_in_block_re.search(body):
            ids.extend(_story_id_candidates(major, minor))

    return list(dict.fromkeys(ids))


async def get_screen_subgraph(
    project_name: str, screen_name: str, team_id: str = ""
) -> ProjectGraph:
    """
    한 화면(:Screen {name})에 연결된 PRD 계층 서브그래프 (Screen + Story + Epic).

    전체 프로젝트 그래프에서 시각적 노이즈를 제거하기 위해 PRD 의 한 화면 단위로만
    추출. 호출자(FE) 가 모달 제목으로 표시한 화면 이름과 일치하는 노드를 anchor 로 함.

    [2단계 해석]
      1) Cypher 1차: Story-[:IMPLEMENTED_ON]->Screen 직접 traverse
      2) 1차 결과 비면 PRD 마크다운의 Screen Architecture 섹션을 파싱해 Story id 추출 후
         해당 Story + Epic 만 별도 Cypher 로 조회. LLM 이 IMPLEMENTED_ON 엣지를 안 만든
         케이스를 흡수.

    Returns:
        ProjectGraph (nodes/edges 가 비어있을 수 있음 — Screen 미존재 또는 미연결).
        404 분기는 호출자(라우트)가 nodes 가 0개 인지를 보고 판단.

    APOC 미설치 환경 대비 fallback Cypher 자동 시도 — get_project_graph 와 동일 패턴.
    """
    project_name = scoped_project(project_name, team_id)
    # ── 1차: 그래프 직접 traverse ────────────────────────────
    records: List[Dict[str, Any]] = []
    params = {"project": project_name, "screen_name": screen_name}
    try:
        records = await neo4j_client.run_cypher(
            _GET_SCREEN_SUBGRAPH_CYPHER, params
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "get_screen_subgraph: primary cypher failed (%s) — falling back", e
        )
        records = await neo4j_client.run_cypher(
            _GET_SCREEN_SUBGRAPH_FALLBACK_CYPHER, params
        )

    graph = _build_graph_from_records(project_name, records)
    if graph.nodes:
        graph.reason = "ok"
        return graph

    # ── 2차: PRD 마크다운에서 Screen→Story 매핑 추출 후 보강 ──
    # IMPLEMENTED_ON 엣지가 LLM 출력에서 누락된 경우 흡수.
    # [2026-05] reason 정확 분기:
    #   - PRD 자체 없음 → 'no_prd'
    #   - markdown 매칭 0개 → 'no_implemented_on' (PRD 재실행 권장)
    #   - 매칭은 됐지만 Story 가 Neo4j 에 없음 → 'stories_match_no_data'
    master = await get_master_prd(project_name)
    if not master or not master.prd_content:
        graph.reason = "no_prd"
        return graph

    # [2026-05-28] markdown 의 [Story X.Y] 참조를 (major, minor) 정수 페어로 추출.
    # 이전엔 zero-pad 4 변형(story_X_Y / story_0X_Y / story_X_0Y / story_0X_0Y) 만 시도해
    # LLM 이 다른 형식(3-digit pad, 'story-1-1', 다른 prefix 등) 으로 저장한 케이스에서
    # 매칭 실패 → 'stories_match_no_data' 빈 그래프. 페어 기반 fuzzy 매칭으로 흡수.
    target_pairs = _extract_screen_story_pairs_from_markdown(
        master.prd_content, screen_name
    )
    if not target_pairs:
        graph.reason = "no_implemented_on"
        # [2026-05] 운영 디버깅: Screen 노드 / Story 노드 카운트 + PRD markdown 길이.
        try:
            counts = await neo4j_client.run_cypher(
                "MATCH (sc:Screen {project: $project}) "
                "WITH count(sc) AS n_screen "
                "MATCH (st:Story {project: $project}) "
                "RETURN n_screen, count(st) AS n_story",
                {"project": project_name},
            )
            row = counts[0] if counts else {}
        except Exception:  # noqa: BLE001
            row = {}
        graph.debug = {
            "screen_node_count": row.get("n_screen", 0),
            "story_node_count": row.get("n_story", 0),
            "prd_content_length": len(master.prd_content),
            "requested_screen": screen_name,
            "hint": (
                "PRD markdown 에서 화면명 매칭 0건. PRD 의 User Flow 안에 "
                "'<screen_name> 화면' 또는 '[<screen_name>] 화면' 형태로 화면 이름이 "
                "들어있는지 확인. 또는 prd_graph 가 IMPLEMENTED_ON 엣지를 생성했는지."
            ),
        }
        return graph

    # ── Phase 2 fuzzy 매칭: 프로젝트 전체 Story 를 가져와 (major, minor) 페어로 비교 ──
    # markdown 은 ground truth — ID 문자열 형식이 무엇이든 같은 (major, minor) 면 같은 Story.
    try:
        all_story_rows = await neo4j_client.run_cypher(
            "MATCH (s:Story {project: $project}) RETURN s.id AS id ORDER BY s.id",
            {"project": project_name},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "get_screen_subgraph: list all stories failed (%s) — aborting fallback", e
        )
        all_story_rows = []

    target_set = set(target_pairs)
    matched_ids: List[str] = []
    unmatched_existing: List[Dict[str, Any]] = []  # 디버깅: (id, parsed_pair)
    for r in all_story_rows:
        sid = r.get("id")
        pair = _parse_story_major_minor(sid)
        if pair and pair in target_set:
            matched_ids.append(str(sid))
        else:
            unmatched_existing.append({"id": sid, "parsed": list(pair) if pair else None})

    if not matched_ids:
        # markdown 매칭은 했지만 어느 Story 도 (major, minor) 페어 일치 안 됨 → 진짜 동기화 불일치.
        graph2 = ProjectGraph(project=project_name, nodes=[], edges=[])
        graph2.reason = "stories_match_no_data"
        graph2.debug = {
            "attempted_pairs": [list(p) for p in target_pairs],
            "existing_story_ids_in_neo4j": [
                r.get("id") for r in all_story_rows if r.get("id")
            ],
            "unparseable_or_unmatched_examples": unmatched_existing[:10],
            "hint": (
                "PRD markdown 의 Story 참조 (major.minor) 와 Neo4j Story id 의 "
                "(major, minor) 정수 페어 일치 0건. attempted_pairs 와 "
                "existing_story_ids_in_neo4j 비교 후 PRD 재실행 필요."
            ),
        }
        return graph2

    logger.info(
        "get_screen_subgraph: fuzzy 매칭 — markdown 페어 %d 개 중 Neo4j Story %d 개 일치 → 보강 조회",
        len(target_pairs), len(matched_ids),
    )

    fallback_params = {"project": project_name, "story_ids": matched_ids}
    try:
        records = await neo4j_client.run_cypher(
            _GET_STORIES_BY_IDS_CYPHER, fallback_params
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "get_screen_subgraph: by-ids primary failed (%s) — falling back", e
        )
        records = await neo4j_client.run_cypher(
            _GET_STORIES_BY_IDS_FALLBACK_CYPHER, fallback_params
        )

    graph2 = _build_graph_from_records(project_name, records)
    # markdown 기반으로 찾았으면 Screen 노드는 합성해서 추가 + IMPLEMENTED_ON 합성 엣지.
    if graph2.nodes:
        synthetic_screen_id = f"screen:{screen_name}"
        graph2.nodes.append(
            GraphNode(
                id=synthetic_screen_id,
                label="Screen",
                properties={"name": screen_name, "source": "markdown_fallback"},
            )
        )
        for n in list(graph2.nodes):
            if n.label == "Story":
                graph2.edges.append(
                    GraphEdge(
                        source_id=n.id,
                        target_id=synthetic_screen_id,
                        type="IMPLEMENTED_ON",
                    )
                )
        graph2.reason = "ok"   # markdown fuzzy fallback 성공
    else:
        # 이 분기는 사실상 도달 불가 (matched_ids 가 있으면 by-ids 결과도 있어야 함).
        # 방어적으로 stories_match_no_data 유지.
        graph2.reason = "stories_match_no_data"
        graph2.debug = {
            "matched_ids": matched_ids,
            "hint": "fuzzy 매칭 ID 는 있지만 by-ids cypher 결과 비어있음 — 데이터 정합성 점검 필요.",
        }
    return graph2


def _build_graph_from_records(
    project_name: str, records: List[Dict[str, Any]]
) -> ProjectGraph:
    """records → ProjectGraph 변환 (strip + dangling drop + cap). 공통 정규화."""
    row = _first_row(records) or {}
    raw_nodes = row.get("nodes") or []
    raw_edges = row.get("edges") or []

    nodes: List[GraphNode] = []
    for n in raw_nodes[:_MAX_GRAPH_NODES]:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        nlabel = n.get("label")
        if not nid or not nlabel:
            continue
        nodes.append(
            GraphNode(
                id=str(nid),
                label=str(nlabel),
                properties=_strip_heavy(n.get("properties")),
            )
        )

    valid_ids = {n.id for n in nodes}
    edges: List[GraphEdge] = []
    for e in raw_edges[:_MAX_GRAPH_EDGES]:
        if not isinstance(e, dict):
            continue
        src, tgt, etype = e.get("source_id"), e.get("target_id"), e.get("type")
        if not (src and tgt and etype):
            continue
        if str(src) not in valid_ids or str(tgt) not in valid_ids:
            continue
        # [D — 2026-05] edge properties 보존 — lineage 관계 (confidence/quote) 표시용
        eprops = e.get("properties") if isinstance(e.get("properties"), dict) else {}
        edges.append(
            GraphEdge(
                source_id=str(src), target_id=str(tgt), type=str(etype),
                properties=eprops,
            )
        )

    return ProjectGraph(project=project_name, nodes=nodes, edges=edges)


async def get_meeting_log(
    project_name: str, version: str, team_id: str = ""
) -> Optional[MeetingLog]:
    project_name = scoped_project(project_name, team_id)
    records = await neo4j_client.run_cypher(
        _GET_MEETING_LOG_CYPHER,
        {"project": project_name, "version": version},
    )
    row = _first_row(records)
    if not row or not row.get("version"):
        return None
    return MeetingLog(
        version=row.get("version"),
        date=row.get("date"),
        meeting_content=row.get("meeting_content"),
        created_at=int(row["created_at"])
        if row.get("created_at") is not None
        else None,
    )


_GET_ALL_MEETING_CONTENT_CYPHER = """\
MATCH (log:Meeting_Log {project: $project})
RETURN log.raw_content AS content
ORDER BY log.created_at ASC
"""


async def get_all_meeting_content(project_name: str, team_id: str = "") -> str:
    """프로젝트의 모든 회의록 raw_content 를 시간순으로 이어붙여 반환.

    PRD 정확성 검증(원본 ↔ PRD 대조)의 '원본' 으로 사용 — PRD 는 여러 회의록
    버전에서 생성되므로 전체를 합쳐야 누락/환각을 정확히 판정할 수 있다.
    """
    project_name = scoped_project(project_name, team_id)
    records = await neo4j_client.run_cypher(
        _GET_ALL_MEETING_CONTENT_CYPHER, {"project": project_name}
    )
    return "\n\n".join(
        str(r.get("content") or "") for r in records if r.get("content")
    )


# [2026-05-18 Phase 1] 동시 접속 차단 — (project, version) 조합 사전 체크.
#
# [Why]
# Meeting_Log 의 id 는 `log_{project}_{version}` 패턴. 같은 사용자가 PC + 모바일
# 에서 동시에 같은 v1.1 저장 시 두 디바이스 모두 같은 log_id 생성 → MERGE 패턴
# 으로 인해 나중에 도착한 raw_content 가 덮어씀 (데이터 손실) + LLM 호출 2번
# (비용 2배) + 미팅 카운트 2배 차감.
#
# 이 helper 는 라우트 진입 시점 (LLM 호출 전, quota 차감 전) 호출하면
# (project, version) 충돌 시 즉시 409 응답 → 사용자에게 "다른 곳에서 먼저
# 저장됐습니다" 안내. UNIQUE 제약 (domain_indexes) 이 race 까지 잡고, 이
# 사전 체크가 친절한 에러 메시지 제공.
#
# [TOCTOU 한계]
# 사전 체크 직후 다른 디바이스가 같은 version 저장 시 race 가능. UNIQUE
# 제약이 그 경우 BE 단에서 막음 (Neo4j ConstraintValidationFailed).
_MEETING_LOG_EXISTS_CYPHER = """\
MATCH (log:Meeting_Log {project: $project, version: $version})
RETURN log.id AS id LIMIT 1
"""


async def meeting_log_exists(project_name: str, version: str, team_id: str = "") -> bool:
    """주어진 (project, version) 으로 이미 Meeting_Log 가 존재하는지.

    [용도]
    - 동시 저장 (cross-device race) 사전 차단 — quota 차감 / LLM 호출 전.
    - 같은 사용자의 PC + 모바일 동시 작업 시 한 쪽 즉시 409 응답.

    [성능]
    Story / Meeting_Log 의 project 인덱스 활용 — 점프 1회. ms 이하.

    Returns:
        True: 이미 존재 (저장하면 충돌 / 덮어쓰기 위험)
        False: 신규 version — 안전하게 진행 가능
    """
    project_name = scoped_project(project_name, team_id)
    if not project_name or not version:
        return False
    records = await neo4j_client.run_cypher(
        _MEETING_LOG_EXISTS_CYPHER,
        {"project": project_name, "version": version},
    )
    return bool(records and records[0].get("id"))


_CPS_DELTA_MD_CYPHER = """\
MATCH (c:CPS_Document {project: $project, version: $version})
WHERE c.type IS NULL OR c.type <> 'Master'
RETURN c.full_markdown AS md
ORDER BY c.updated_at DESC
LIMIT 1
"""


async def get_cps_delta_markdown(
    project_name: str, version: str, team_id: str = ""
) -> Optional[str]:
    """해당 버전 delta CPS 의 full_markdown — 검수 모드 createPRD(PRD 단독 실행)의 입력.

    없거나 본문이 비어 있으면 None (호출측 404 — 'CPS 먼저 생성' 안내).
    """
    project_name = scoped_project(project_name, team_id)
    if not project_name or not version:
        return None
    records = await neo4j_client.run_cypher(
        _CPS_DELTA_MD_CYPHER, {"project": project_name, "version": version}
    )
    md = records[0].get("md") if records else None
    return md if isinstance(md, str) and md.strip() else None


async def get_meeting_versions(project_name: str, team_id: str = "") -> List[MeetingVersion]:
    project_name = scoped_project(project_name, team_id)
    records = await neo4j_client.run_cypher(
        _GET_MEETING_VERSIONS_CYPHER, {"project": project_name}
    )
    out: List[MeetingVersion] = []
    for r in records:
        if not r.get("version"):
            continue
        out.append(
            MeetingVersion(
                log_id=r.get("log_id"),
                version=r.get("version"),
                date=r.get("date"),
            )
        )
    return out


# ─── Timeline ────────────────────────────────────────────────────


# 한 쿼리에 너무 많이 담지 말고 source 별로 분리. event 합산은 Python 에서.
_TIMELINE_MEETINGS_CYPHER = """\
MATCH (log:Meeting_Log {project: $project})
WHERE log.created_at >= $since
RETURN log.version AS version, log.date AS date, log.created_at AS ts
ORDER BY log.created_at DESC
LIMIT 50
"""

_TIMELINE_CPS_PRD_CYPHER = """\
MATCH (d)
WHERE d.project = $project
  AND d.updated_at >= $since
  AND (d:CPS_Document OR d:PRD_Document)
  AND d.type = 'Master'
RETURN labels(d)[0] AS label, d.version AS version, d.updated_at AS ts
ORDER BY d.updated_at DESC
LIMIT 50
"""

_TIMELINE_LINT_CYPHER = """\
MATCH (l:LintResult {project: $project})
WHERE l.saved_at >= $since
RETURN l.score AS score, l.scanned_files AS files, l.saved_at AS ts
ORDER BY l.saved_at DESC
LIMIT 50
"""

_TIMELINE_LINEAGE_CYPHER = """\
MATCH (l:LineageResult {project: $project})
WHERE l.savedAt >= $since
RETURN
    coalesce(l.totalImpls, 0) AS total_impls,
    coalesce(l.missingCount, 0) AS missing,
    coalesce(l.driftCount, 0) AS drift,
    l.savedAt AS ts
ORDER BY l.savedAt DESC
LIMIT 50
"""

_TIMELINE_REPO_CYPHER = """\
MATCH (r:ProjectRepo)
WHERE r.project_name = $project
  AND coalesce(r.added_at, r.updated_at, 0) >= $since
RETURN r.url AS url, r.role AS role, coalesce(r.added_at, r.updated_at) AS ts
ORDER BY ts DESC
LIMIT 50
"""


def _to_epoch_ms(v: Any) -> Optional[int]:
    """Neo4j timestamp() (ms) 또는 ISO string 모두 ms epoch 로 정규화."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        # ISO 포맷 시도
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return None
    # neo4j DateTime 등
    try:
        return int(v.to_native().timestamp() * 1000)  # type: ignore[attr-defined]
    except Exception:
        return None


async def get_project_timeline(
    project_name: str, *, since_ms: int, limit: int = 30, team_id: str = ""
) -> ProjectTimeline:
    """
    `since_ms` 이후 발생한 이벤트들을 합쳐서 최신 순으로 반환.
    Deliverables Hero strip 의 '지난 7일 변경사항' 표시용.
    """
    project_name = scoped_project(project_name, team_id)
    events: List[TimelineEvent] = []

    # 1) 미팅 로그
    rows = await neo4j_client.run_cypher(
        _TIMELINE_MEETINGS_CYPHER, {"project": project_name, "since": since_ms}
    )
    for r in rows:
        ts = _to_epoch_ms(r.get("ts"))
        if ts is None:
            continue
        events.append(TimelineEvent(
            kind="meeting",
            occurred_at=ts,
            label=f"{r.get('version') or '미팅'} 등록",
            detail=r.get("date"),
        ))

    # 2) CPS/PRD master update
    rows = await neo4j_client.run_cypher(
        _TIMELINE_CPS_PRD_CYPHER, {"project": project_name, "since": since_ms}
    )
    for r in rows:
        ts = _to_epoch_ms(r.get("ts"))
        if ts is None:
            continue
        label = r.get("label") or ""
        kind = "cps_update" if "CPS" in label else "prd_update"
        title = "CPS Master 갱신" if "CPS" in label else "PRD Master 갱신"
        events.append(TimelineEvent(
            kind=kind, occurred_at=ts, label=title, detail=r.get("version"),
        ))

    # 3) Lint
    rows = await neo4j_client.run_cypher(
        _TIMELINE_LINT_CYPHER, {"project": project_name, "since": since_ms}
    )
    for r in rows:
        ts = _to_epoch_ms(r.get("ts"))
        if ts is None:
            continue
        score = r.get("score")
        events.append(TimelineEvent(
            kind="lint",
            occurred_at=ts,
            label=f"Lint {score}점" if score is not None else "Lint 실행",
            detail=f"{r.get('files') or 0} files 분석",
        ))

    # 4) Lineage
    rows = await neo4j_client.run_cypher(
        _TIMELINE_LINEAGE_CYPHER, {"project": project_name, "since": since_ms}
    )
    for r in rows:
        ts = _to_epoch_ms(r.get("ts"))
        if ts is None:
            continue
        events.append(TimelineEvent(
            kind="lineage",
            occurred_at=ts,
            label=f"Lineage 분석 (drift {r.get('drift') or 0})",
            detail=f"매칭 {r.get('total_impls') or 0} / 미구현 {r.get('missing') or 0}",
        ))

    # 5) Repo add
    rows = await neo4j_client.run_cypher(
        _TIMELINE_REPO_CYPHER, {"project": project_name, "since": since_ms}
    )
    for r in rows:
        ts = _to_epoch_ms(r.get("ts"))
        if ts is None:
            continue
        events.append(TimelineEvent(
            kind="repo_add",
            occurred_at=ts,
            label=f"Repo 추가 ({r.get('role') or 'other'})",
            detail=r.get("url"),
        ))

    # 합산 정렬 + slice
    events.sort(key=lambda e: e.occurred_at, reverse=True)
    events = events[:limit]

    # 카운트
    counts: Dict[str, int] = {}
    for e in events:
        counts[e.kind] = counts.get(e.kind, 0) + 1

    return ProjectTimeline(
        project=project_name, since=since_ms, events=events, counts=counts,
    )


# ─── [D — 2026-05] Lineage Graph (Design ↔ PRD Story) ─────────
#
# Design 노드 (Entity / Aggregate / ArchService) 와 PRD Story 사이의
# DERIVED_FROM 관계를 그래프로 반환. 사용자가 "이 Story 가 영향 주는 design 노드"
# 또는 "이 Service 가 어느 Story 에서 왔나" 같은 분석을 한 화면에서 볼 수 있음.
#
# Cypher 전략:
#   - focus_story_id 지정: 해당 Story + 모든 DERIVED_FROM source 노드 + Epic
#   - focus_node_id 지정: 해당 노드 + 모든 DERIVED_FROM target Story + Epic
#   - 미지정 (전체): project 의 모든 DERIVED_FROM 엣지 (성능 한도 _MAX_GRAPH_EDGES)

_LINEAGE_GRAPH_ALL_CYPHER = """\
// [2026-06 fix] 버그 2건:
//  (1) API 는 IMPLEMENTS 로 Story 에 연결 → DERIVED_FROM 만 매치하면 API lineage 엣지 누락
//      (2026-06-13 선반영). DERIVED_FROM|IMPLEMENTS 둘 다 매치.
//  (2) [이번] 이전엔 `UNWIND rels AS r` + `WITH ... r ... collect()` 라 그룹키에 r 이 들어가
//      rel 1개당 1행(각 행 엣지 1개)을 반환했고, _build_graph_from_records 의 _first_row 가
//      첫 행만 읽어 '노드 다수 + 엣지 단 1개' 로 깨졌다(화면 그대로). 엣지를 검증된
//      _GET_PROJECT_GRAPH_CYPHER 와 동일하게 list comprehension 으로 한 행에 집계.
MATCH (src)-[r:DERIVED_FROM|IMPLEMENTS]->(story:Story {project: $project})
WITH collect(DISTINCT src) AS src_nodes,
     collect(DISTINCT story) AS stories,
     collect(DISTINCT r) AS rels

OPTIONAL MATCH (epic:Epic)-[:CONTAINS]->(cstory:Story)
WHERE cstory IN stories
WITH src_nodes, stories, rels,
     collect(DISTINCT epic) AS epics,
     collect(DISTINCT {
        source_id: epic.id, target_id: cstory.id, type: 'CONTAINS', properties: {}
     }) AS contains_edges

RETURN [n IN (src_nodes + stories + epics) | {
           id: n.id, label: labels(n)[0], properties: properties(n)
       }] AS nodes,
       [r IN rels | {
           source_id: startNode(r).id,
           target_id: endNode(r).id,
           type: type(r),
           properties: properties(r)
       }] + contains_edges AS edges
"""

_LINEAGE_GRAPH_BY_STORY_CYPHER = """\
// [2026-06 fix] ALL cypher 와 동일 — API 의 IMPLEMENTS 포함 + 엣지를 list comprehension 으로
// 한 행에 집계 (이전 `UNWIND rels AS r` 다중행 → _first_row 가 엣지 1개만 읽던 버그 차단).
MATCH (story:Story {project: $project, id: $focus_story_id})
// API(IMPLEMENTS) 포함 — ALL 쿼리와 동일 (DERIVED_FROM 만 보면 API 누락).
OPTIONAL MATCH (src)-[r:DERIVED_FROM|IMPLEMENTS]->(story)
OPTIONAL MATCH (epic:Epic)-[c:CONTAINS]->(story)

WITH story,
     collect(DISTINCT src) AS srcs, collect(DISTINCT r) AS rels,
     collect(DISTINCT epic) AS epics, collect(DISTINCT c) AS c_rels

RETURN [n IN ([story] + srcs + epics) | {
           id: n.id, label: labels(n)[0], properties: properties(n)
       }] AS nodes,
       [r IN rels | {
           source_id: startNode(r).id, target_id: endNode(r).id,
           type: type(r), properties: properties(r)
       }] +
       [c IN c_rels | {
           source_id: startNode(c).id, target_id: endNode(c).id,
           type: type(c), properties: {}
       }] AS edges
"""


async def get_design_lineage_graph(
    project_name: str, focus_story_id: Optional[str] = None, team_id: str = "",
) -> ProjectGraph:
    """
    Design ↔ PRD lineage 서브그래프.

    [Args]
    - project_name: 격리된 프로젝트 — assert_owns 는 호출자 (라우트) 에서.
    - focus_story_id: 특정 Story 만 보기 (예: 'story_01_1'). None 이면 전체 lineage.

    [Returns]
    ProjectGraph — nodes (Entity/Aggregate/ArchService/Story/Epic) + edges
    ([:DERIVED_FROM] + [:CONTAINS]). DERIVED_FROM 엣지는 properties 에
    confidence + quote 보유.

    [성능]
    전체 모드는 _MAX_GRAPH_EDGES (200) 으로 cap. focus 모드는 항상 작음.
    """
    project_name = scoped_project(project_name, team_id)
    if focus_story_id:
        records = await neo4j_client.run_cypher(
            _LINEAGE_GRAPH_BY_STORY_CYPHER,
            {"project": project_name, "focus_story_id": focus_story_id},
        )
    else:
        records = await neo4j_client.run_cypher(
            _LINEAGE_GRAPH_ALL_CYPHER, {"project": project_name},
        )
    graph = _build_graph_from_records(project_name, records)

    # [2026-05] reason 분기 — FE 가 빈 결과 시 정확한 안내 메시지 띄울 수 있도록.
    if graph.nodes:
        graph.reason = "ok"
        # [2026-06-13 관측성] 노드는 있는데 엣지가 0 이면 '고립 노드만' 상태 —
        # 이전엔 조용히 ok 라 "관계선이 왜 없지?" 진단 단서가 없었다. debug 에 노출.
        if not graph.edges:
            graph.debug = {
                "node_count": len(graph.nodes),
                "edge_count": 0,
                "hint": (
                    "노드는 있으나 lineage 엣지 0개 — DERIVED_FROM/IMPLEMENTS 미생성. "
                    "옛 design 데이터일 수 있으니 design 재실행으로 lineage 재생성 권장."
                ),
            }
        return graph

    # 빈 결과 분기 — design 자체가 없는지 vs lineage 만 없는지 구분.
    design_check = await neo4j_client.run_cypher(
        "MATCH (n) WHERE n.project = $project AND "
        # [2026-06-13] API 추가 — API-only 프로젝트(IMPLEMENTS 누락)가 no_design 으로
        # 오분류되던 경계. API 도 설계 노드이므로 'design 있음'으로 본다.
        "(n:Entity OR n:Aggregate OR n:ArchService OR n:ArchDatabase OR n:API) "
        "RETURN count(n) AS n_design",
        {"project": project_name},
    )
    n_design = (design_check[0].get("n_design") if design_check else 0) or 0
    if n_design == 0:
        graph.reason = "no_design"
        graph.debug = {
            "design_node_count": 0,
            "hint": "Entity/Aggregate/ArchService/ArchDatabase 노드가 0개. Design 미생성.",
        }
    else:
        # design 노드는 있지만 DERIVED_FROM 엣지 0개 → 옛 design 데이터, 재실행 필요
        graph.reason = "no_lineage"
        graph.debug = {
            "design_node_count": n_design,
            "hint": (
                f"design 노드 {n_design}개 있지만 DERIVED_FROM 엣지 0개. "
                "옛 design 데이터 — design 재실행해서 lineage 생성 필요."
            ),
        }
    return graph
