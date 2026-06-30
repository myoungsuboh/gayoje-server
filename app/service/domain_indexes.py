"""
핵심 도메인 노드 인덱스 ensure (부팅 시 1회).

[배경]
지금까지 부팅 시 ensure 한 제약/인덱스:
  - User.email UNIQUE, User.github_id UNIQUE (user_repository)
  - Project.name UNIQUE (ownership_repository)
  - VibeRepo (user_email, url) composite (user_repository)
  - SubscriptionChange (user_email, created_at) (admin_repository)
  - AuditLog created_at / actor_email / target_email (audit_repository)

하지만 read hot-path 의 핵심 도메인 노드에는 인덱스가 없었다 → `MATCH (m:CPS_Document
{type: 'Master', is_latest: true}) WHERE m.project = $project` 같은 쿼리는 풀스캔
위험. 데이터셋이 커지면 응답 지연 + Neo4j 메모리 사용 ↑.

이 모듈은 도메인 read 쿼리가 자주 부딪히는 속성에 인덱스를 추가한다 — 동등성
필터(`= $project`) 면 RANGE 인덱스로 충분.

[멱등성]
모든 CREATE INDEX 는 `IF NOT EXISTS` — 부팅마다 안전.
실패 시 warning 만 로깅하고 부팅은 계속 (Neo4j 미연결 dev 환경 보호).
"""
from __future__ import annotations

import logging

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# 각 인덱스는 별도 statement — Neo4j 가 multi-statement IF NOT EXISTS 를
# 한 트랜잭션에 묶으면 일부 버전에서 conflict.
_INDEX_STATEMENTS = [
    # CPS / PRD 마스터 조회 hot-path.
    """
    CREATE INDEX cps_document_project IF NOT EXISTS
    FOR (n:CPS_Document) ON (n.project)
    """,
    """
    CREATE INDEX prd_document_project IF NOT EXISTS
    FOR (n:PRD_Document) ON (n.project)
    """,
    # Skill 검색 hot-path — 라우트, MCP search_skills, lint pipeline 이 사용.
    """
    CREATE INDEX skill_project IF NOT EXISTS
    FOR (s:Skill) ON (s.project)
    """,
    # 미팅 로그 / 업로드 / Story 도 project 단위 격리 필터를 자주 친다.
    """
    CREATE INDEX meeting_log_project IF NOT EXISTS
    FOR (m:Meeting_Log) ON (m.project)
    """,
    # [2026-05-18 Phase 1 동시접속] (project, version) composite 인덱스 —
    # gateway / v2 라우트의 사전 체크 (`meeting_log_exists`) hot-path 가속.
    # project 단일 인덱스만으로는 project 범위 내 version 비교가 in-memory scan.
    # composite 면 1-hop lookup. UNIQUE 제약은 별도 (아래) — 인덱스만으로는
    # 중복 차단 안 됨.
    """
    CREATE INDEX meeting_log_project_version IF NOT EXISTS
    FOR (m:Meeting_Log) ON (m.project, m.version)
    """,
    """
    CREATE INDEX meeting_upload_user_email IF NOT EXISTS
    FOR (u:MeetingUpload) ON (u.user_email)
    """,
    """
    CREATE INDEX story_project IF NOT EXISTS
    FOR (s:Story) ON (s.project)
    """,
    # Lint / Lineage 결과 — project 단위 read.
    """
    CREATE INDEX lint_result_project IF NOT EXISTS
    FOR (l:LintResult) ON (l.project)
    """,
    """
    CREATE INDEX lineage_result_project IF NOT EXISTS
    FOR (l:LineageResult) ON (l.project)
    """,
    # Lineage Truth (정답 라벨) — list_lineage_truth(project[, item_type])
    # hot-path. project 단일 RANGE 인덱스로 MATCH 의 첫 단계 좁힘.
    """
    CREATE INDEX lineage_truth_project IF NOT EXISTS
    FOR (t:LineageTruth) ON (t.project)
    """,
    # Trace (upstream lineage) hot-path — `/api/v2/trace` 가 한 호출에 5~7개 노드
    # 라벨을 OPTIONAL MATCH 로 거치므로 각 라벨에 project 인덱스 없으면 풀스캔.
    # PR9 의 query_repository 에서도 Epic/Problem/Aggregate 등을 자주 거친다.
    """
    CREATE INDEX epic_project IF NOT EXISTS
    FOR (n:Epic) ON (n.project)
    """,
    """
    CREATE INDEX problem_project IF NOT EXISTS
    FOR (n:Problem) ON (n.project)
    """,
    """
    CREATE INDEX solution_project IF NOT EXISTS
    FOR (n:Solution) ON (n.project)
    """,
    """
    CREATE INDEX api_project IF NOT EXISTS
    FOR (n:API) ON (n.project)
    """,
    """
    CREATE INDEX entity_project IF NOT EXISTS
    FOR (n:Entity) ON (n.project)
    """,
    """
    CREATE INDEX policy_project IF NOT EXISTS
    FOR (n:Policy) ON (n.project)
    """,
    """
    CREATE INDEX aggregate_project IF NOT EXISTS
    FOR (n:Aggregate) ON (n.project)
    """,
    """
    CREATE INDEX bounded_context_project IF NOT EXISTS
    FOR (n:BoundedContext) ON (n.project)
    """,
    """
    CREATE INDEX domain_entity_project IF NOT EXISTS
    FOR (n:DomainEntity) ON (n.project)
    """,
    """
    CREATE INDEX domain_event_project IF NOT EXISTS
    FOR (n:DomainEvent) ON (n.project)
    """,
    """
    CREATE INDEX arch_service_project IF NOT EXISTS
    FOR (n:ArchService) ON (n.project)
    """,
    """
    CREATE INDEX arch_database_project IF NOT EXISTS
    FOR (n:ArchDatabase) ON (n.project)
    """,
    # [2026-05 composite (project, id)] Phase 3 node-level inline edit hot-path:
    #   PATCH /api/v2/{cps,prd}/nodes/{id} →
    #     MATCH (n {id: $node_id, project: $project})
    # 단일 project 인덱스만으로는 project 로 narrow 한 뒤 id 로 in-memory 스캔.
    # composite 면 두 키를 한 번에 lookup → O(log n) 단일 hop.
    # Problem / Solution / Epic / Story 네 라벨이 Phase 3.1/3.2 의 whitelist.
    """
    CREATE INDEX problem_project_id IF NOT EXISTS
    FOR (n:Problem) ON (n.project, n.id)
    """,
    """
    CREATE INDEX solution_project_id IF NOT EXISTS
    FOR (n:Solution) ON (n.project, n.id)
    """,
    """
    CREATE INDEX epic_project_id IF NOT EXISTS
    FOR (n:Epic) ON (n.project, n.id)
    """,
    """
    CREATE INDEX story_project_id IF NOT EXISTS
    FOR (n:Story) ON (n.project, n.id)
    """,
    # [2026-05-18 Phase 1 동시접속] Meeting_Log (project, version) UNIQUE 제약 —
    # 사전 체크 (`meeting_log_exists`) 가 거의 다 잡지만, 두 요청이 같은 ms 에
    # 동시에 사전 체크를 통과한 race condition 의 최종 방어선. DB constraint
    # 가 두 번째 CREATE 를 거부 → worker 가 ConstraintValidationFailed 받고 job
    # 실패. 사용자 입장에선 FE polling 결과 "처리 실패" 응답.
    #
    # [기존 데이터 호환]
    # 이미 운영 중인 데이터에 중복이 있으면 제약 생성 실패. domain_indexes 의
    # 각 statement try/except 가 catch 해 warning 만 찍고 부팅 계속 → 안전.
    """
    CREATE CONSTRAINT meeting_log_project_version_unique IF NOT EXISTS
    FOR (m:Meeting_Log) REQUIRE (m.project, m.version) IS UNIQUE
    """,
    # [2026-05 MCP] spec 검색 (MCP search_spec) hot-path — 미팅 로그가 누적되면
    # project 당 spec 노드 수가 10K~100K 까지 늘어남. name + description 양쪽에
    # CONTAINS 풀스캔은 그 시점 SLA 위험. FULLTEXT INDEX 로 Lucene 기반 가속.
    #
    # Neo4j 5.x FULLTEXT 문법:
    #   CREATE FULLTEXT INDEX <name> IF NOT EXISTS
    #   FOR (n:Label1|Label2|...) ON EACH [n.prop1, n.prop2]
    #
    # 단일 fulltext 인덱스에 모든 spec 라벨 포함 — db.index.fulltext.queryNodes
    # 한 번에 cross-label 검색 가능. 호출자가 라벨 boolean 필터 (WHERE n:Story
    # OR ...) 로 조이는 패턴.
    """
    CREATE FULLTEXT INDEX spec_text_search IF NOT EXISTS
    FOR (n:Story|Epic|Aggregate|Entity|DomainEntity|ArchService|ArchDatabase|API)
    ON EACH [n.name, n.description]
    """,
]


async def ensure_domain_indexes() -> None:
    """부팅 시 1회. 실패해도 부팅 막지 않음.

    각 statement 를 개별 try/except 로 감싸 한 인덱스 실패가 다른 인덱스 생성을
    막지 않게 한다 (예: 신규 라벨 미사용 시 Neo4j 가 warning notification 만
    내고 통과).
    """
    created = 0
    for stmt in _INDEX_STATEMENTS:
        try:
            await neo4j_client.run_cypher(stmt)
            created += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "ensure_domain_indexes: 인덱스 생성 실패 (%s): %s",
                stmt.strip().split("\n")[1].strip()
                if "\n" in stmt.strip()
                else stmt[:80],
                e,
            )
    logger.info(
        "ensure_domain_indexes: %d/%d 인덱스 ensure 완료",
        created,
        len(_INDEX_STATEMENTS),
    )
