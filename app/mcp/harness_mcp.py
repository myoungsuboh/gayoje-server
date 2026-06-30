"""
Harness FastMCP 서버 — AI Agent 가 호출하는 read-only Tool 모음.

[마운트 경로]
/mcp/sse — app/api/main.py 에서 마운트.

[인증·소유권]
모든 tool 호출은 `MCPAuthMiddleware` (app/mcp/auth.py) 가 JWT Bearer 강제 →
`current_mcp_user()` 로 호출자 식별 가능. 프로젝트 단위 read 는
`require_mcp_user_and_assert_owns(project_name)` 으로 본인 소유 프로젝트만
허용 (403). `ping` 같은 사용자 비종속 tool 만 인증만 통과하면 OK.

[설계 원칙]
- MCP Tool 은 "AI Agent 의 의사결정용 검색/계산 함수"
- 데이터 mutation(CRUD) 는 노출 안 함 — Tool 이 mutation 하면 순환/혼란
- 따라서 모든 Tool 은 read-only 또는 순수 계산

[데이터 접근]
모든 Tool 은 repository 계층 (Neo4j 직접) 을 호출한다.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from app.mcp.auth import require_mcp_user_and_assert_owns

harness_mcp = FastMCP("Harness MCP Server")


# ===== 1) 헬스체크 (인증만 통과하면 OK, 프로젝트 종속 없음) =====


@harness_mcp.tool(name="ping")
async def ping() -> str:
    """MCP 연결 확인용. 정상이면 'pong' 반환.

    인증 자체는 미들웨어가 강제 — 토큰 없이는 이 함수 진입 못 함.
    """
    return "pong"


# ===== 2) Skill 검색 (read-only, 프로젝트 단위 격리) =====


@harness_mcp.tool(name="search_skills")
async def search_skills(
    project_name: str, keyword: str = ""
) -> List[Dict[str, Any]]:
    """
    프로젝트의 Skill 목록을 검색.

    Args:
        project_name: 대상 프로젝트명 (필수). 본인 소유 프로젝트만 (403 차단).
        keyword: 스킬명/태그에 포함될 부분 문자열 (빈 값이면 전체 반환)

    Returns:
        매칭된 Skill 리스트 (id, name, scope, priority, tags, rule_count, applied_services)
    """
    await require_mcp_user_and_assert_owns(project_name)

    from app.service import skill_repository  # 지연 import

    skills = await skill_repository.get_all_skills(project_name)
    items = [s.model_dump() for s in skills]
    # cat: 는 export 카테고리 분류용 내부 마커 — AI 에이전트 노출/검색 over-match 금지
    # (create_md_pipeline visible_tags · FE skillToMd 와 동일 규약).
    for it in items:
        it["tags"] = [t for t in (it.get("tags") or []) if not (isinstance(t, str) and t.startswith("cat:"))]

    if not keyword:
        return items

    kw = keyword.lower()
    return [
        s
        for s in items
        if kw in str(s.get("name", "")).lower()
        or kw in " ".join(str(t) for t in (s.get("tags") or [])).lower()
    ]


# ===== 3) PRD / CPS 조회 (read-only, 프로젝트 단위 격리) =====


@harness_mcp.tool(name="get_prd")
async def get_prd(project_name: str) -> Optional[Dict[str, Any]]:
    """
    프로젝트의 마스터 PRD 조회.

    Args:
        project_name: 대상 프로젝트명 (필수). 본인 소유 프로젝트만.

    Returns:
        { master_prd_id, prd_content, last_updated, related_master_cps_id, absorbed_prd_ids }
        또는 None (마스터 PRD 없음)
    """
    await require_mcp_user_and_assert_owns(project_name)

    from app.service import query_repository

    out = await query_repository.get_master_prd(project_name)
    return out.model_dump() if out else None


@harness_mcp.tool(name="get_cps")
async def get_cps(project_name: str) -> Optional[Dict[str, Any]]:
    """
    프로젝트의 마스터 CPS 조회.

    Args:
        project_name: 대상 프로젝트명 (필수). 본인 소유 프로젝트만.

    Returns:
        { master_id, version, content, last_updated, absorbed_cps_ids }
        또는 None (마스터 CPS 없음)
    """
    await require_mcp_user_and_assert_owns(project_name)

    from app.service import query_repository

    out = await query_repository.get_master_cps(project_name)
    return out.model_dump() if out else None


# ===== 4) 순수 계산 도구 (프로젝트 데이터 미접근 — 소유권 검증 불필요) =====


@harness_mcp.tool(name="score_skill_relevance")
async def score_skill_relevance(
    skill_tags: List[str], required_stack: List[str]
) -> Dict[str, Any]:
    """
    Skill 의 tag 와 요구되는 기술스택의 교집합 비율로 관련도 점수 (0~1).
    Agent 가 "어떤 skill 을 추천할지" 정량 판단할 때.

    Args:
        skill_tags: 스킬 태그 (예: ["python", "fastapi"])
        required_stack: 요구 기술 (예: ["python", "vue", "neo4j"])

    Returns:
        { "score": 0~1, "matched": [...] }
    """
    if not skill_tags or not required_stack:
        return {"score": 0.0, "matched": []}

    s = {t.lower() for t in skill_tags}
    r = {t.lower() for t in required_stack}
    matched = sorted(s & r)
    score = len(matched) / max(len(r), 1)
    return {"score": round(score, 3), "matched": matched}
