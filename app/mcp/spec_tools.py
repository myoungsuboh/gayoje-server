"""
MCP spec tools — SPACK API/Screen 계약 + Lint(코드↔설계 점검) 결과를 AI 에이전트에 노출.

[차별화 — 2026-06]
Cursor / Claude Code 가 코드를 작성할 때 가장 자주 필요한 "스펙 컨텍스트":
  - get_api_spec   : 라우트 핸들러 / fetch 호출 코드를 짤 때 — method·endpoint·
                     path/query params·request/response body·error·auth 계약.
  - get_screen_spec: 화면 컴포넌트를 짤 때 — 이 화면이 호출하는 API + 화면 전이.
  - get_lint_findings: 직전 코드↔설계 점검에서 "코드에 빠진 설계 항목" 환류.
이 셋은 코드만 읽어선 알 수 없고, 곧바로 구현 행동으로 이어지는 데이터다.

[설계 원칙 — `harness_mcp.py` / `lineage_tools.py` 와 동일]
- 모든 tool 은 read-only
- 모든 tool 진입에 `require_mcp_user_and_assert_owns(project_name)` 적용
- repository 계층만 호출 (query_repository / lint_repository)
- 개인 프로젝트 스코프 (team_id 미전달 — 기존 get_prd/get_cps 와 동일 정책)

[등록 방식]
이 모듈을 import 하면 decorator side-effect 로 `harness_mcp` 에 tool 이 등록됨.
`app/api/main.py` 가 `import app.mcp.spec_tools` 로 트리거.

[토큰 비용 정책]
list 모드(특정 id 미지정)는 compact 요약만 반환해 에이전트 컨텍스트를 아낀다.
특정 id 지정 시에만 full 계약(request/response body 등)을 펼친다.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.mcp.auth import require_mcp_user_and_assert_owns
from app.mcp.harness_mcp import harness_mcp

logger = logging.getLogger(__name__)

# list 모드 기본/최대 반환 수 — 미팅 누적으로 API/Screen 이 많아질 수 있어 상한.
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 500


def _clamp_limit(limit: int) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    if n < 1:
        return 1
    if n > _MAX_LIMIT:
        return _MAX_LIMIT
    return n


def _norm(s: Any) -> str:
    return str(s or "").lower()


# ===== 1) API 계약 조회 =====


@harness_mcp.tool(name="get_api_spec")
async def get_api_spec(
    project_name: str,
    api_id: str = "",
    keyword: str = "",
    limit: int = _DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """SPACK API 계약 조회 — method / endpoint / params / body / error / auth.

    [언제 호출하나]
    Cursor / Claude Code 가 라우트 핸들러나 클라이언트 fetch 코드를 작성할 때.
    "이 엔드포인트의 입력/출력/에러/인증 계약이 뭔지"를 코드와 정합하게 맞춤.

    [데이터 출처]
    가장 최근 Design(SPACK) 생성 결과. Design 을 한 번도 안 돌렸으면 빈 목록.

    Args:
        project_name: 본인 소유 프로젝트.
        api_id: 특정 API id (지정 시 그 API 의 full 계약 반환). 슬래시가 포함된 값은
            endpoint 로 간주해 endpoint 매칭도 시도. 미지정이면 list 모드.
        keyword: list 모드에서 endpoint/name/method/description 부분 매칭 필터.
        limit: list 모드 최대 반환 수 (기본 200, 최대 500).

    Returns:
        {
            "apis": [
                # list 모드(요약): { id, name, method, endpoint, description,
                #                   related_story_id, service }
                # 단일 모드(full): 위 + path_params, query_params, request_body,
                #                   response_body, error_cases, auth, lineage_confidence
            ],
            "count": <반환 수>,
            "total": <프로젝트 전체 API 수>,
            "mode": "single" | "list",
            "truncated": <bool — limit 으로 잘렸는지>
        }
    """
    await require_mcp_user_and_assert_owns(project_name)
    from app.service import query_repository

    spack = await query_repository.get_spack_graph(project_name)
    apis: List[Dict[str, Any]] = spack.apis or []
    total = len(apis)

    # api id → service name 매핑 (HANDLED_BY cross-rel)
    svc_by_api: Dict[str, str] = {}
    for rel in spack.api_service_rels or []:
        if rel.source_id and rel.target_name:
            svc_by_api[rel.source_id] = rel.target_name

    def _summary(a: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": a.get("id"),
            "name": a.get("name"),
            "method": a.get("method"),
            "endpoint": a.get("endpoint"),
            "description": a.get("description"),
            "related_story_id": a.get("related_story_id"),
            "service": svc_by_api.get(a.get("id")),
        }

    def _full(a: Dict[str, Any]) -> Dict[str, Any]:
        out = _summary(a)
        out.update({
            "path_params": a.get("path_params") or [],
            "query_params": a.get("query_params") or [],
            "request_body": a.get("request_body") or {},
            "response_body": a.get("response_body") or {},
            "error_cases": a.get("error_cases") or [],
            "auth": a.get("auth") or {},
            "lineage_confidence": a.get("lineage_confidence"),
        })
        return out

    # 단일 모드 — api_id 정확 매칭, 없으면 endpoint 매칭(슬래시 포함 입력 한정)
    if api_id:
        hit = next((a for a in apis if a.get("id") == api_id), None)
        if hit is None and "/" in api_id:
            hit = next((a for a in apis if _norm(a.get("endpoint")) == _norm(api_id)), None)
        return {
            "apis": [_full(hit)] if hit else [],
            "count": 1 if hit else 0,
            "total": total,
            "mode": "single",
            "truncated": False,
        }

    # list 모드 — keyword 필터 + compact 요약
    rows = apis
    if keyword:
        kw = _norm(keyword)
        rows = [
            a for a in apis
            if kw in _norm(a.get("endpoint"))
            or kw in _norm(a.get("name"))
            or kw in _norm(a.get("method"))
            or kw in _norm(a.get("description"))
        ]
    matched = len(rows)
    lim = _clamp_limit(limit)
    rows = rows[:lim]
    return {
        "apis": [_summary(a) for a in rows],
        "count": len(rows),
        "total": total,
        "mode": "list",
        "truncated": matched > len(rows),
    }


# ===== 2) Screen 계약 조회 (화면 ↔ API ↔ 전이) =====


@harness_mcp.tool(name="get_screen_spec")
async def get_screen_spec(
    project_name: str,
    screen_id: str = "",
    keyword: str = "",
    limit: int = _DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """SPACK Screen 계약 조회 — 화면이 호출하는 API + 화면 전이(next_screens).

    [언제 호출하나]
    Cursor / Claude Code 가 화면 컴포넌트(예: Vue/React 페이지)를 작성할 때.
    "이 화면이 어떤 API 를 호출하고, 어디로 전이하는지"를 코드와 맞춤.

    [데이터 출처]
    가장 최근 Design(SPACK) 생성 결과. calls_apis 는 method/endpoint 로 enrich.

    Args:
        project_name: 본인 소유 프로젝트.
        screen_id: 특정 Screen id (지정 시 그 화면 상세). 미지정이면 list 모드.
        keyword: list 모드에서 name/path/description 부분 매칭 필터.
        limit: list 모드 최대 반환 수 (기본 200, 최대 500).

    Returns:
        {
            "screens": [
                {
                    id, name, path, description, related_story_id,
                    next_screens: [...],
                    calls_apis: [{ id, method, endpoint }]   # API id 를 계약으로 enrich
                }
            ],
            "count": <반환 수>,
            "total": <프로젝트 전체 Screen 수>,
            "mode": "single" | "list",
            "truncated": <bool>
        }
    """
    await require_mcp_user_and_assert_owns(project_name)
    from app.service import query_repository

    spack = await query_repository.get_spack_graph(project_name)
    screens: List[Dict[str, Any]] = spack.screens or []
    apis: List[Dict[str, Any]] = spack.apis or []
    total = len(screens)

    # api id → {method, endpoint} 인덱스 (calls_apis enrich 용)
    api_by_id: Dict[str, Dict[str, Any]] = {
        a.get("id"): {"id": a.get("id"), "method": a.get("method"), "endpoint": a.get("endpoint")}
        for a in apis if a.get("id")
    }

    def _shape(s: Dict[str, Any]) -> Dict[str, Any]:
        called = []
        for aid in (s.get("calls_apis") or []):
            called.append(api_by_id.get(aid, {"id": aid, "method": None, "endpoint": None}))
        return {
            "id": s.get("id"),
            "name": s.get("name"),
            "path": s.get("path"),
            "description": s.get("description"),
            "related_story_id": s.get("related_story_id"),
            "next_screens": s.get("next_screens") or [],
            "calls_apis": called,
        }

    if screen_id:
        hit = next((s for s in screens if s.get("id") == screen_id), None)
        return {
            "screens": [_shape(hit)] if hit else [],
            "count": 1 if hit else 0,
            "total": total,
            "mode": "single",
            "truncated": False,
        }

    rows = screens
    if keyword:
        kw = _norm(keyword)
        rows = [
            s for s in screens
            if kw in _norm(s.get("name"))
            or kw in _norm(s.get("path"))
            or kw in _norm(s.get("description"))
        ]
    matched = len(rows)
    lim = _clamp_limit(limit)
    rows = rows[:lim]
    return {
        "screens": [_shape(s) for s in rows],
        "count": len(rows),
        "total": total,
        "mode": "list",
        "truncated": matched > len(rows),
    }


# ===== 3) Lint(코드↔설계 점검) 결과 조회 =====


@harness_mcp.tool(name="get_lint_findings")
async def get_lint_findings(
    project_name: str,
    only_unapplied: bool = True,
    limit: int = 100,
) -> Dict[str, Any]:
    """직전 Lint(코드↔설계 점검) 결과 — "코드에 빠진 설계 항목" 환류.

    [언제 호출하나]
    Cursor / Claude Code 가 "설계 대비 빠진 구현을 채워줘" 같은 작업을 할 때.
    직전 점검에서 applied=False 로 판정된 규칙(미구현 설계 항목)을 모아 보여준다.

    [주의 — 스냅샷]
    이 결과는 사용자가 마지막으로 Lint 를 실행한 시점의 스냅샷이다. 그 이후 코드가
    바뀌었으면 최신이 아닐 수 있다. status='no_lint' 면 한 번도 실행 안 한 것.
    coverage_truncated=True 면 전체 코드의 일부만 샘플 검사한 결과라 점수에 한계가 있다.

    Args:
        project_name: 본인 소유 프로젝트.
        only_unapplied: True(기본)면 applied=False 규칙만(=해야 할 일). False 면 전체.
        limit: 반환할 규칙 최대 수 (기본 100).

    Returns:
        status='no_lint' 인 경우:
            { "status": "no_lint" }
        그 외:
            {
                "status": "ok",
                "score": <0~100>,
                "violations": <미적용 규칙 수>,
                "rules_checked": <검사한 규칙 수>,
                "sampled_files": <본문 검사한 파일 수>,
                "total_code_files": <레포 전체 코드 파일 수>,
                "coverage_truncated": <bool>,
                "saved_at": <epoch ms>,
                "findings": [
                    {
                        "case": <케이스 제목>,
                        "rule": <규칙 id 예: 'api:POST /orders'>,
                        "description": <설명>,
                        "applied": <bool>,
                        "detection_method": "deterministic" | "llm" | "fallback",
                        "evidence": [{ file, line, snippet, kind }]   # applied=True 일 때 근거
                    }
                ],
                "findings_truncated": <bool>
            }
    """
    await require_mcp_user_and_assert_owns(project_name)
    from app.service import lint_repository

    res = await lint_repository.get_last_lint_for_project(project_name)
    if res is None:
        return {"status": "no_lint"}

    findings: List[Dict[str, Any]] = []
    for case in res.cases or []:
        for rule in case.rules or []:
            if only_unapplied and rule.applied:
                continue
            findings.append({
                "case": case.title,
                "rule": rule.rule,
                "description": rule.description,
                "applied": rule.applied,
                "detection_method": rule.detection_method,
                "evidence": [
                    {"file": e.file, "line": e.line, "snippet": e.snippet, "kind": e.kind}
                    for e in (rule.evidence or [])
                ],
            })

    total_findings = len(findings)
    lim = _clamp_limit(limit) if limit else total_findings
    findings = findings[:lim]

    return {
        "status": "ok",
        "score": res.score,
        "violations": res.violations,
        "rules_checked": res.rules_checked,
        "sampled_files": res.sampled_files,
        "total_code_files": res.total_code_files,
        "coverage_truncated": res.coverage_truncated,
        "saved_at": res.saved_at,
        "findings": findings,
        "findings_truncated": total_findings > len(findings),
    }
