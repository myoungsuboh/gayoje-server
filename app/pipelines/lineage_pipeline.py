"""
analyzeLineage 파이프라인 — 프로젝트의 산출물(Story/Aggregate/API/Service) 이
등록된 GitHub Repository 의 어느 파일에 구현되어 있는지 계보(lineage) 매핑.

[스테이지 매핑]
- Parse Lineage Input → `LineageInput` 검증
- Get Stories/Aggregates/APIs/Services/Repos Lineage → `_fetch_artifacts_and_repos`
- Fetch All Repo Trees → `github_client.fetch_repo_trees_bulk`
- Build Lineage Context → `_build_lineage_result` (서버측 deterministic 매칭)
- (Lineage AI Agent → SKIP — 서버측 매칭이 deterministic 하고 충분히 정확)
- Normalize Lineage → `_finalize_stats` (verified=true 부여)
- Prepare Lineage Save + Save Lineage Neo4j → `lineage_repository.save_lineage_result`

[설계 결정 — 의도된 단순화]
deterministic 매칭 후 LLM Agent 를 한 번 더 거칠 수도 있지만, AI 환각 방지를
위해 "fileTree 에 실제로 존재하는 경로만 사용" 을 강제해야 한다. 이미 서버측
deterministic 매칭이 끝났으므로 LLM 호출이 사실상 noop + 환각 가능성만 추가.
서버측 매칭만 사용 → 토큰 절약 + 결정성 보장.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from app.clients.github_client import (
    GitHubClient,
    fetch_repo_trees_bulk,
)
from app.pipelines.base import PipelineContext
from app.service.lineage_repository import (
    LineageArtifact,
    LineageDrift,
    LineageImpl,
    LineageMissing,
    LineageResultData,
    LineageStats,
)

logger = logging.getLogger(__name__)


# ─── Domain types ───────────────────────────────────────────────


@dataclass(frozen=True)
class LineageInput:
    project_name: str
    team_id: str = ""


# ─── Stage 1: fetch artifacts from Neo4j ────────────────────────


_GET_STORIES_CYPHER = """\
MATCH (s:Story {project: $project})
RETURN collect({id: s.id, name: s.name, description: s.description}) AS stories
"""

_GET_AGGREGATES_CYPHER = """\
MATCH (a:Aggregate {project: $project})
OPTIONAL MATCH (a)<-[:BELONGS_TO]-(c:BoundedContext)
RETURN collect({
    id: a.id, name: a.name, description: a.description, context_name: c.name
}) AS aggregates
"""

_GET_APIS_CYPHER = """\
MATCH (a:API {project: $project})
RETURN collect({
    id: a.id, name: a.name, method: a.method, endpoint: a.endpoint,
    description: a.description
}) AS apis
"""

_GET_SERVICES_CYPHER = """\
MATCH (s:ArchService {project: $project})
RETURN collect({
    id: s.id, name: s.name, type: s.type, tech_stack: s.tech_stack,
    description: s.description
}) AS services
"""

_GET_REPOS_CYPHER = """\
MATCH (r:Repo {project: $project})
RETURN collect({url: r.url, role: r.role, label: r.label}) AS repos
"""


async def _fetch_artifacts_and_repos(
    ctx: PipelineContext, project: str
) -> Dict[str, List[Dict[str, Any]]]:
    stories = await ctx.neo4j.run_cypher(_GET_STORIES_CYPHER, {"project": project})
    aggregates = await ctx.neo4j.run_cypher(
        _GET_AGGREGATES_CYPHER, {"project": project}
    )
    apis = await ctx.neo4j.run_cypher(_GET_APIS_CYPHER, {"project": project})
    services = await ctx.neo4j.run_cypher(_GET_SERVICES_CYPHER, {"project": project})
    repos = await ctx.neo4j.run_cypher(_GET_REPOS_CYPHER, {"project": project})

    return {
        "stories": (stories[0].get("stories") if stories else []) or [],
        "aggregates": (aggregates[0].get("aggregates") if aggregates else []) or [],
        "apis": (apis[0].get("apis") if apis else []) or [],
        "services": (services[0].get("services") if services else []) or [],
        "repos": (repos[0].get("repos") if repos else []) or [],
    }


# ─── Stage 2: deterministic matching ────────────────────────────


# 'Build Lineage Context' 단계의 SERVICE_STOPWORDS.
_SERVICE_STOPWORDS: Set[str] = {
    "service", "services", "api", "frontend", "front", "backend", "back",
    "portal", "wrapper", "module", "engine", "platform", "system",
    "core", "common", "main", "base",
}


_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def _name_variants(name: str) -> List[str]:
    """PascalCase / snake_case / kebab-case 변형 생성."""
    lower = name.strip().lower()
    if not lower:
        return []
    variants: Set[str] = {lower}
    variants.add(re.sub(r"[\s_-]+", "", lower))
    # camelCase / PascalCase → kebab + snake
    kebab = re.sub(r"([a-z])([A-Z])", r"\1-\2", name).lower()
    snake = re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower()
    variants.add(kebab)
    variants.add(snake)
    return [v for v in variants if v]


def _match_by_name(
    name: str, repo_trees: List[Dict[str, Any]]
) -> List[LineageImpl]:
    """
    fileTree 에서 name 매칭. high > medium > low 우선순위.

    Build Lineage Context.matchByName 구현.
    """
    if not name or not name.strip():
        return []
    variants = _name_variants(name)
    matches: List[LineageImpl] = []

    for r in repo_trees:
        if r.get("error"):
            continue
        url = r.get("url") or ""
        role = r.get("role")
        for file in r.get("files") or []:
            file_lower = file.lower()
            filename = file.split("/")[-1].lower()
            filename_no_ext = re.sub(r"\.[^.]+$", "", filename)

            confidence: Optional[str] = None
            reason: Optional[str] = None

            # high: 파일명 정확 일치 또는 시작/끝
            for v in variants:
                if filename_no_ext == v:
                    confidence, reason = "high", f"파일명 정확 일치: {name}"
                    break
                if filename_no_ext.startswith(v) or filename_no_ext.endswith(v):
                    confidence, reason = "high", f"파일명 {name} 포함"
                    break
            # medium: 파일명 부분 매칭
            if not confidence:
                for v in variants:
                    if len(v) >= 4 and v in filename_no_ext:
                        confidence, reason = "medium", f"파일명 부분: {name}"
                        break
            # medium: 폴더명 매칭
            if not confidence:
                for v in variants:
                    if len(v) >= 4 and (
                        f"/{v}/" in file_lower or file_lower.endswith(f"/{v}")
                    ):
                        confidence, reason = "medium", f"폴더명 매칭: {name}"
                        break
            # low: 경로 포함
            if not confidence:
                for v in variants:
                    if len(v) >= 5 and v in file_lower:
                        confidence, reason = "low", f"경로 포함: {name}"
                        break

            if confidence:
                matches.append(
                    LineageImpl(
                        repoUrl=url,
                        role=role,
                        filePath=file,
                        confidence=confidence,
                        reason=reason,
                        verified=True,
                    )
                )

    # 정렬: high > medium > low → 상위 8개
    matches.sort(key=lambda m: _CONFIDENCE_ORDER.get(m.confidence, 3))
    return matches[:8]


def _match_by_endpoint(
    endpoint: str, repo_trees: List[Dict[str, Any]]
) -> List[LineageImpl]:
    """API endpoint 의 path segment 키워드로 추가 매칭."""
    if not endpoint:
        return []
    segments = []
    for seg in endpoint.split("/"):
        if not seg or seg.startswith(("{", ":", "?")):
            continue
        if seg.lower() == "api" or re.match(r"^v\d+$", seg, re.IGNORECASE):
            continue
        if len(seg) >= 4:
            segments.append(seg)

    out: List[LineageImpl] = []
    for s in segments:
        out.extend(_match_by_name(s, repo_trees))
    return _dedupe(out)


def _dedupe(impls: List[LineageImpl]) -> List[LineageImpl]:
    seen: Set[str] = set()
    out: List[LineageImpl] = []
    for m in impls:
        key = f"{m.repoUrl}:{m.filePath}"
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def _match_by_service_name(
    name: str, repo_trees: List[Dict[str, Any]]
) -> List[LineageImpl]:
    """
    서비스 이름은 자연어 (예: 'Governance & Subscription Service') —
    stopword 제외하고 단어별로 매칭.
    """
    if not name:
        return []
    raw_words = re.split(r"[\s&\-_/]+", name)
    words = [
        re.sub(r"[^A-Za-z]", "", w)
        for w in raw_words
    ]
    words = [
        w for w in words if len(w) >= 4 and w.lower() not in _SERVICE_STOPWORDS
    ]
    if not words:
        return []
    out: List[LineageImpl] = []
    for w in words:
        out.extend(_match_by_name(w, repo_trees))
    deduped = _dedupe(out)
    deduped.sort(key=lambda m: _CONFIDENCE_ORDER.get(m.confidence, 3))
    return deduped[:8]


# ─── Drift detection (코드 → spec 역방향 매칭) ─────────────────
#
# 명세화되지 않은 코드 후보 추출. PM/아키텍트가 review 해야 할 항목.
#
# 휴리스틱 패턴: 파일명 또는 폴더명이 다음 중 하나면 spec 후보로 본다.
#   - *Controller.* / *Handler.* / *Endpoint.*     → controller (API 후보)
#   - *Service.*                                    → service
#   - *Repository.* / *Dao.* / *Repo.*              → repository (entity 접근자 후보)
#   - *Aggregate.* / *Entity.* (도메인 코드)        → aggregate / entity
#   - *Event.* / *Listener.* / *Subscriber.*        → event
#   - routes/*.* / handlers/*.* / api/*.*           → route
#
# 매칭 정책:
#   1. 파일에서 위 패턴에 해당하는 symbol 이름 추출
#   2. 추출 이름이 PRD 의 API/Service/Aggregate/Story 이름 어느 하나와도 매칭 안 되면 drift
#   3. 매칭은 _name_variants 의 fuzzy 비교 (대소문자/구분자 무시)


_DRIFT_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    # filename-based (확장자 무시)
    ("controller", re.compile(r"([A-Za-z][A-Za-z0-9_]*?)(Controller|Handler|Endpoint)\.[a-zA-Z]+$")),
    ("service",    re.compile(r"([A-Za-z][A-Za-z0-9_]*?)Service\.[a-zA-Z]+$")),
    ("repository", re.compile(r"([A-Za-z][A-Za-z0-9_]*?)(Repository|Repo|Dao)\.[a-zA-Z]+$")),
    ("aggregate",  re.compile(r"([A-Za-z][A-Za-z0-9_]*?)(Aggregate|AggregateRoot)\.[a-zA-Z]+$")),
    ("event",      re.compile(r"([A-Za-z][A-Za-z0-9_]*?)(Event|Listener|Subscriber)\.[a-zA-Z]+$")),
]

# 폴더 기반: 이 폴더 아래의 파일은 route 후보로 본다.
_DRIFT_FOLDER_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    ("route", re.compile(r"(^|/)(routes|handlers|controllers)/([^/]+)\.[a-zA-Z]+$")),
]


def _extract_drift_candidates(
    repo_trees: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    repo_trees 에서 spec 후보 (controller/service/repository/aggregate/event/route)
    파일들을 추출. Returns: [{kind, repoUrl, role, filePath, symbol, hint}, ...]
    """
    candidates: List[Dict[str, Any]] = []
    for r in repo_trees:
        if r.get("error"):
            continue
        url = r.get("url") or ""
        role = r.get("role")
        for file in r.get("files") or []:
            # filename pattern
            for kind, pat in _DRIFT_PATTERNS:
                m = pat.search(file)
                if m:
                    symbol = m.group(1)
                    if not symbol:
                        continue
                    candidates.append({
                        "kind": kind,
                        "repoUrl": url,
                        "role": role,
                        "filePath": file,
                        "symbol": symbol,
                        "hint": f"파일명 패턴: *{m.group(2) if m.lastindex and m.lastindex >= 2 else kind.title()}*",
                    })
                    break
            else:
                # folder pattern
                for kind, pat in _DRIFT_FOLDER_PATTERNS:
                    m = pat.search(file)
                    if m:
                        symbol = m.group(3)
                        # 확장자 제거 + 스톱워드 회피
                        symbol = re.sub(r"\.[^.]+$", "", symbol)
                        if not symbol or symbol.lower() in {"index", "main", "app"}:
                            continue
                        candidates.append({
                            "kind": kind,
                            "repoUrl": url,
                            "role": role,
                            "filePath": file,
                            "symbol": symbol,
                            "hint": f"폴더 패턴: /{m.group(2)}/",
                        })
                        break
    return candidates


def _build_spec_name_index(
    sources: Dict[str, List[Dict[str, Any]]]
) -> Set[str]:
    """
    PRD/DDD/Spack 의 모든 spec 이름을 lowercase + 구분자 정규화한 집합 반환.
    drift 매칭 시 이 집합과 후보 symbol 의 variants 비교.
    """
    names: Set[str] = set()

    def _add(name: Optional[str]) -> None:
        if not name:
            return
        for v in _name_variants(name):
            names.add(v)

    for k in ("stories", "aggregates", "apis", "services"):
        for item in sources.get(k) or []:
            _add(item.get("name"))
            # API 는 endpoint segment 도 후보 매칭에 활용
            if k == "apis":
                ep = item.get("endpoint") or ""
                for seg in ep.split("/"):
                    if seg and not seg.startswith(("{", ":", "?")):
                        if seg.lower() not in ("api",) and not re.match(r"^v\d+$", seg, re.IGNORECASE):
                            _add(seg)
    return names


def _build_drift_list(
    sources: Dict[str, List[Dict[str, Any]]],
    repo_trees: List[Dict[str, Any]],
    *,
    max_items: int = 50,
) -> List[LineageDrift]:
    """
    candidates 중 spec name index 와 매칭 안 되는 것만 drift 로 분류.
    같은 (repoUrl, filePath) 중복 제거.
    """
    spec_names = _build_spec_name_index(sources)
    candidates = _extract_drift_candidates(repo_trees)

    drifts: List[LineageDrift] = []
    seen: Set[str] = set()
    for c in candidates:
        key = f"{c['repoUrl']}::{c['filePath']}"
        if key in seen:
            continue
        # symbol variants 가 spec_names 와 하나라도 겹치면 drift 아님
        sym_variants = set(_name_variants(c["symbol"]))
        if sym_variants & spec_names:
            continue
        seen.add(key)
        drifts.append(LineageDrift(
            kind=c["kind"],
            repoUrl=c["repoUrl"],
            role=c.get("role"),
            filePath=c["filePath"],
            symbol=c["symbol"],
            hint=c.get("hint"),
        ))
        if len(drifts) >= max_items:
            break
    return drifts


def _build_lineage_result(
    sources: Dict[str, List[Dict[str, Any]]],
    repo_trees: List[Dict[str, Any]],
) -> LineageResultData:
    """
    Stage: 'Build Lineage Context'.
    서버측 deterministic 매칭으로 최종 result 생성.
    """
    stories_in = sources["stories"]
    aggregates_in = sources["aggregates"]
    apis_in = sources["apis"]
    services_in = sources["services"]

    # ─ Stories ─
    enriched_stories = [
        LineageArtifact(
            id=s.get("id") or "",
            name=s.get("name") or "",
            description=s.get("description"),
            implementations=_match_by_name(s.get("name") or "", repo_trees)[:5],
        )
        for s in stories_in
    ]

    # ─ Aggregates ─
    enriched_aggregates = [
        LineageArtifact(
            id=a.get("id") or "",
            name=a.get("name") or "",
            description=a.get("description"),
            implementations=_match_by_name(a.get("name") or "", repo_trees)[:5],
        )
        for a in aggregates_in
    ]

    # ─ APIs ─ name + endpoint 모두 매칭
    enriched_apis: List[LineageArtifact] = []
    for a in apis_in:
        by_name = _match_by_name(a.get("name") or "", repo_trees)
        by_ep = _match_by_endpoint(a.get("endpoint") or "", repo_trees)
        merged = _dedupe(by_name + by_ep)
        merged.sort(key=lambda m: _CONFIDENCE_ORDER.get(m.confidence, 3))
        enriched_apis.append(
            LineageArtifact(
                id=a.get("id") or "",
                name=a.get("name") or "",
                endpoint=a.get("endpoint"),
                method=a.get("method"),
                implementations=merged[:5],
            )
        )

    # ─ Services ─ name 매칭 → 비면 stopword 제외 단어 매칭
    enriched_services: List[LineageArtifact] = []
    for s in services_in:
        name = s.get("name") or ""
        impls = _match_by_name(name, repo_trees)
        if not impls:
            impls = _match_by_service_name(name, repo_trees)
        enriched_services.append(
            LineageArtifact(
                id=s.get("id") or "",
                name=name,
                type=s.get("type"),
                tech_stack=s.get("tech_stack"),
                implementations=impls[:8],
            )
        )

    # ─ missingImpl ─ implementations 비어있는 항목
    def collect_missing(
        items: List[LineageArtifact], type_label: str
    ) -> List[LineageMissing]:
        return [
            LineageMissing(
                type=type_label,
                id=i.id,
                name=i.name,
                reason="매칭되는 파일 없음 (실제 fileTree 기준)",
            )
            for i in items
            if not i.implementations
        ]

    missing = (
        collect_missing(enriched_stories, "story")
        + collect_missing(enriched_aggregates, "aggregate")
        + collect_missing(enriched_apis, "api")
        + collect_missing(enriched_services, "service")
    )

    def count_impl(arr: List[LineageArtifact]) -> int:
        return sum(len(i.implementations) for i in arr)

    total_impls = (
        count_impl(enriched_stories)
        + count_impl(enriched_aggregates)
        + count_impl(enriched_apis)
        + count_impl(enriched_services)
    )
    total_artifacts = (
        len(stories_in) + len(aggregates_in) + len(apis_in) + len(services_in)
    )

    # ─ Drift (코드 → spec 역방향) ─
    drifts = _build_drift_list(sources, repo_trees)

    summary = (
        f"{len(aggregates_in)}개 Aggregate / "
        f"{len(apis_in)}개 API / "
        f"{len(services_in)}개 Service 중 "
        f"{total_artifacts - len(missing)}개 매칭, "
        f"{len(missing)}개 미구현, "
        f"{len(drifts)}개 drift (명세화되지 않은 코드)."
    )

    return LineageResultData(
        summary=summary,
        stories=enriched_stories,
        aggregates=enriched_aggregates,
        apis=enriched_apis,
        services=enriched_services,
        missingImpl=missing,
        drifts=drifts,
        stats=LineageStats(
            storiesCount=len(enriched_stories),
            aggregatesCount=len(enriched_aggregates),
            apisCount=len(enriched_apis),
            servicesCount=len(enriched_services),
            totalImpls=total_impls,
            verifiedImpls=total_impls,
            unverifiedImpls=0,
            missingCount=len(missing),
            driftCount=len(drifts),
        ),
    )


# ─── End-to-end orchestrator ────────────────────────────────────


async def run_lineage_pipeline(
    ctx: PipelineContext,
    payload: LineageInput,
    *,
    github_client: Optional[GitHubClient] = None,
    user_token: Optional[str] = None,
    save: bool = True,
) -> LineageResultData:
    """
    fetch artifacts + repos → fetch repo trees → deterministic matching → save.

    user_token: 사용자 OAuth access_token. private repo / rate-limit 확장에 사용.
                github_client 직접 주입 시 무시.
    """
    if not payload.project_name or not payload.project_name.strip():
        raise ValueError("projectName 이 비어 있습니다.")

    logger.info(
        "lineage pipeline start: project=%s key=%s",
        payload.project_name,
        ctx.idempotency_key,
    )

    # [멀티테넌시] 도메인 노드(Story/Aggregate/API/...) 와 LineageResult 는 project
    # property = 스코프 키로 격리. lineage 는 LLM 없는 결정적 매칭이라 이름 분리 불필요.
    from app.core.project_scope import scoped_project
    db_project = scoped_project(payload.project_name, payload.team_id)

    # Stage 1: Neo4j artifacts + repos
    # [progress] FE 진행바가 실제 단계 기반으로 차도록 각 단계 시작 시 마커 emit.
    await ctx.emit_stage("lineage:fetch")
    sources = await _fetch_artifacts_and_repos(ctx, db_project)

    # Stage 2: GitHub repo trees (failures are inline per-repo, not pipeline-fatal)
    await ctx.emit_stage("lineage:trees")
    gh = github_client or GitHubClient(user_token=user_token)
    repo_trees = await fetch_repo_trees_bulk(gh, sources["repos"])

    # Stage 3: deterministic matching
    await ctx.emit_stage("lineage:match")
    result = _build_lineage_result(sources, repo_trees)

    # Stage 4: save
    if save:
        from app.service import lineage_repository

        await ctx.emit_stage("lineage:saving")
        lineage_id = await lineage_repository.save_lineage_result(
            project=db_project, data=result
        )
        logger.info("lineage saved: id=%s", lineage_id)

    return result
