from __future__ import annotations

from typing import Any, Dict

from app.clients.github_client import GitHubClient, RepoIdentifier, parse_github_url
from app.pipelines.base import PipelineContext
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
from app.pipelines.lint_pipeline.types import LintInput


_GET_SPACK_CYPHER = """\
MATCH (n)
WHERE n.project = $project
  AND (n:API OR n:Entity OR n:Policy)
WITH
  [x IN collect(DISTINCT n) WHERE x:API | x] AS apis_n,
  [x IN collect(DISTINCT n) WHERE x:Entity | x] AS entities_n,
  [x IN collect(DISTINCT n) WHERE x:Policy | x] AS policies_n
RETURN
  // [A-2 — 2026-05-25] API payload (4개 필드) 추가. JSON string 형태로 저장돼
  // decode_apis_payload 가 객체 복원. legacy 노드는 NULL → 헬퍼가 빈 객체로.
  [a IN apis_n | {id: a.id, name: a.name, method: a.method, endpoint: a.endpoint,
                  description: a.description,
                  path_params: a.path_params, query_params: a.query_params,
                  request_body: a.request_body, response_body: a.response_body,
                  // [A-3 — 2026-05-25] error_cases + auth (JSON string, decode 시 객체)
                  error_cases: a.error_cases, auth: a.auth}] AS apis,
  [e IN entities_n | {id: e.id, name: e.name, attributes: e.attributes, description: e.description}] AS entities,
  [p IN policies_n | {id: p.id, name: p.name, category: p.category, description: p.description}] AS policies
"""


_GET_DDD_CYPHER = """\
MATCH (n)
WHERE n.project = $project
  AND (n:BoundedContext OR n:Aggregate OR n:DomainEntity OR n:DomainEvent)
WITH
  [x IN collect(DISTINCT n) WHERE x:BoundedContext | x] AS contexts_n,
  [x IN collect(DISTINCT n) WHERE x:Aggregate | x] AS aggregates_n,
  [x IN collect(DISTINCT n) WHERE x:DomainEntity | x] AS de_n,
  [x IN collect(DISTINCT n) WHERE x:DomainEvent | x] AS ev_n
RETURN
  [c IN contexts_n | {id: c.id, name: c.name, description: c.description}] AS contexts,
  // [D-1 — 2026-05-25] invariants / attributes / payload_fields (JSON string,
  // decode 시 객체). legacy 노드는 NULL → 헬퍼가 빈 list 로.
  [a IN aggregates_n | {id: a.id, name: a.name, description: a.description,
                        invariants: a.invariants}] AS aggregates,
  [d IN de_n | {id: d.id, name: d.name, description: d.description,
                attributes: d.attributes}] AS domain_entities,
  [e IN ev_n | {id: e.id, name: e.name, description: e.description,
                payload_fields: e.payload_fields}] AS domain_events
"""


_GET_ARCH_CYPHER = """\
MATCH (n)
WHERE n.project = $project
  AND (n:ArchService OR n:ArchDatabase)
WITH
  [x IN collect(DISTINCT n) WHERE x:ArchService | x] AS svc_n,
  [x IN collect(DISTINCT n) WHERE x:ArchDatabase | x] AS db_n
RETURN
  // [D-2 — 2026-05-25] deployment / external_dependencies (JSON string)
  [s IN svc_n | {id: s.id, name: s.name, type: s.type, tech_stack: s.tech_stack,
                 description: s.description,
                 deployment: s.deployment,
                 external_dependencies: s.external_dependencies}] AS services,
  [d IN db_n | {id: d.id, name: d.name, type: d.type, tech_stack: d.tech_stack, description: d.description}] AS databases
"""


_GET_SKILLS_AS_RULES_CYPHER = """\
MATCH (s:Skill {project: $project})
RETURN collect({
    id: s.id,
    name: s.name,
    description: COALESCE(s.scope, ''),
    category: s.priority,
    severity: s.priority,
    pattern: '',
    instructions: s.instructions,
    tags: s.tags
}) AS rules
"""


# [2026-06 기획 항목 자동 검증] PRD 의 Story + 설계의 Screen — 기존엔 FE 가
# 파일명 토큰 매칭만 하고 "체크리스트를 AI 도구에 붙여넣어 확인"으로 사용자에게
# 떠넘기던 영역. lint 의 5번째 카테고리로 흡수해 코드 본문 + LLM residual 로
# 자동 검증한다.
_GET_PLAN_CYPHER = """\
MATCH (n)
WHERE n.project = $project
  AND (n:Story OR n:Screen)
WITH
  [x IN collect(DISTINCT n) WHERE x:Story | x] AS stories_n,
  [x IN collect(DISTINCT n) WHERE x:Screen | x] AS screens_n
RETURN
  [s IN stories_n | {id: s.id, name: s.name, description: s.description}] AS stories,
  [sc IN screens_n | {id: sc.id, name: sc.name, path: sc.path,
                      description: sc.description}] AS screens
"""


def _parse_input(payload: LintInput) -> Dict[str, Any]:
    ident = parse_github_url(payload.github_url)
    cleaned_url = f"https://github.com/{ident.owner}/{ident.repo}"
    return {
        "project_name": payload.project_name,
        "github_url": cleaned_url,
        "owner": ident.owner,
        "repo": ident.repo,
        "_ident": ident,
    }


async def _fetch_specs(ctx: PipelineContext, project: str) -> Dict[str, Any]:
    spack_rows = await ctx.neo4j.run_cypher(_GET_SPACK_CYPHER, {"project": project})
    ddd_rows = await ctx.neo4j.run_cypher(_GET_DDD_CYPHER, {"project": project})
    arch_rows = await ctx.neo4j.run_cypher(_GET_ARCH_CYPHER, {"project": project})
    rules_rows = await ctx.neo4j.run_cypher(
        _GET_SKILLS_AS_RULES_CYPHER, {"project": project}
    )
    plan_rows = await ctx.neo4j.run_cypher(_GET_PLAN_CYPHER, {"project": project})
    spack = spack_rows[0] if spack_rows else {}
    ddd = ddd_rows[0] if ddd_rows else {}
    arch = arch_rows[0] if arch_rows else {}
    rules = (rules_rows[0] if rules_rows else {}).get("rules") or []
    plan = plan_rows[0] if plan_rows else {}
    return {
        "spack": {
            # [A-2 — 2026-05-25] API payload 4개 필드 객체 복원. lint LLM 이
            # request/response schema 까지 보고 contract 위반 탐지 가능.
            "apis": decode_apis_payload(spack.get("apis") or []),
            # [A-1 — 2026-05-25] Neo4j 의 JSON string attributes 를 객체 list 로
            # 복원. lint LLM 이 schema 디테일 (type/constraint) 까지 보고 위반 탐지 가능.
            "entities": decode_entities_attributes(spack.get("entities") or []),
            "policies": spack.get("policies") or [],
        },
        "ddd": {
            "contexts": ddd.get("contexts") or [],
            # [D-1 — 2026-05-25] DDD detail 객체 복원. lint LLM 이 도메인 규칙,
            # 엔티티 필드, 이벤트 payload 까지 보고 검증 가능.
            "aggregates": decode_aggregates_detail(ddd.get("aggregates") or []),
            "domain_entities": decode_domain_entities_detail(
                ddd.get("domain_entities") or []
            ),
            "domain_events": decode_domain_events_detail(
                ddd.get("domain_events") or []
            ),
        },
        "architecture": {
            # [D-2 — 2026-05-25] Service detail 객체 복원 (deployment 등).
            "services": decode_services_detail(arch.get("services") or []),
            "databases": arch.get("databases") or [],
        },
        "rules": rules,
        # [2026-06] 기획 항목 (PRD Story + 설계 Screen) — lint 5번째 카테고리.
        "plan": {
            "stories": plan.get("stories") or [],
            "screens": plan.get("screens") or [],
        },
    }


async def _fetch_repo_tree(
    github: GitHubClient, ident: RepoIdentifier
) -> Dict[str, Any]:
    repo_info = await github.get_repo(ident)
    branch = repo_info.get("default_branch") or "main"
    tree = await github.get_tree(ident, branch, recursive=True)
    return {
        "default_branch": branch,
        "tree": tree.get("tree") or [],
        "truncated": bool(tree.get("truncated")),
    }
