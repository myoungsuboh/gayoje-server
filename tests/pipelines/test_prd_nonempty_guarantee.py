"""
빈-PRD 방지 보장: PRD 추출이 실질적 문서를 만들었는데 graph Epic 이 0이고 기존
master 가 없으면, 그 문서를 첫 PRD master 로 저장 → 빈 PRD 금지 (결정적, LLM 무관).

데이터 안전: **기존 master 가 있으면 절대 덮어쓰지 않음**(no_changes 유지).
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.prd_pipeline import PrdInput, run_prd_merge
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio

_IMPACT_EMPTY = json.dumps({
    "affected_sections": [], "removed_epic_ids": [], "removed_story_ids": [], "analysis": "",
})

# 실질적 4섹션 PRD 문서 (📦 Epic N: + 충분한 내용) — 단, graph 추출에선 Epic 0개로 가정.
_SUBSTANTIVE_MD = (
    "## 🚀 PRD: [ai admin]\n\n"
    "### 1. Product Overview\n"
    "- **Product Vision**: AI 도구 신청부터 사후 관리까지 일원화된 운영 체계를 구축한다.\n"
    "- **Success Metrics**: 중복 결제 제거로 비용 30% 절감, 보안 사고 0건, 활성 계정률 85%.\n\n"
    "### 2. Epic & User Story Map\n"
    "#### 📦 Epic 1: AI 도구 신청·결제 통합 관리\n"
    "- **해결 문제 매핑**: prb_01\n"
    "- **[Story 1.1] 사용자는 신청 화면에서 AI 도구를 신청한다**\n"
    "  - User Flow: 신청 화면 진입 → 항목 입력 → 제출 → 검토 대기\n"
    "#### 📦 Epic 2: AI 도구 사용 보안 가이드라인 통제\n"
    "- **해결 문제 매핑**: prb_02\n"
    "- **[Story 2.1] 사용자는 보안 서약 후 도구를 사용한다**\n\n"
    "### 3. Screen Architecture\n#### 🖥️ [Screen: 신청]\n- `[Story 1.1]` 신청 제출\n\n"
    "### 4. Global Non-Functional Requirements\n- 응답 2초 이내, OAuth 2.0, 데이터 암호화\n"
)
# graph 에 Epic 0개 (PRD_Document 만) — 추출 hiccup 또는 추상 회의 모사.
_NO_EPIC_GRAPH = {"nodes": [{"id": "doc_prd_x", "label": "PRD_Document", "properties": {}}], "relationships": []}
_PARSED = {"pure_markdown": "## CPS\n- 문제", "problems": "- [prb_01] AI 도구 현황 파악 불가"}


async def test_substantive_markdown_no_epics_saves_preliminary_first_prd():
    """graph Epic 0 + 기존 master 없음 + 실질 문서 → no_changes 대신 그 문서를 master 저장."""
    extract = {"parsed": _PARSED, "prd_markdown": _SUBSTANTIVE_MD, "prd_graph": _NO_EPIC_GRAPH}
    gemini = FakeGemini(responses=[_IMPACT_EMPTY])
    neo4j = FakeNeo4j(responses=[[]])  # fetch → master 없음(first_run); 이후 master 저장
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="prelim")

    result = await run_prd_merge(
        ctx, PrdInput(project_name="ai admin", version="v3", cps_graph={"nodes": [], "relationships": []}), extract
    )

    assert result.mode != "no_changes"             # 빈 PRD 아님
    cyphers = [e["cypher"] for e in neo4j.executed]
    assert any("MERGE (master:PRD_Document" in c for c in cyphers)   # master 저장됨
    save = [e for e in neo4j.executed if "MERGE (master:PRD_Document" in e["cypher"]][0]
    assert save["params"]["merged_content"] == _SUBSTANTIVE_MD      # 추출 문서가 그대로 저장


async def test_thin_markdown_no_epics_still_no_changes():
    """문서도 빈약(Epic 항목 없음)하면 기존대로 no_changes (예비 저장 안 함)."""
    extract = {"parsed": _PARSED, "prd_markdown": "# PRD\n## Epic\n- 인증", "prd_graph": _NO_EPIC_GRAPH}
    gemini = FakeGemini(responses=[_IMPACT_EMPTY])
    neo4j = FakeNeo4j(responses=[[]])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="thin")

    result = await run_prd_merge(
        ctx, PrdInput(project_name="x", version="v1", cps_graph={"nodes": [], "relationships": []}), extract
    )

    assert result.mode == "no_changes"
    assert not any("MERGE (master:PRD_Document" in e["cypher"] for e in neo4j.executed)


async def test_substantive_markdown_but_existing_master_does_not_overwrite():
    """실질 문서라도 **기존 master 가 있으면** 덮어쓰지 않음(no_changes) — 데이터 손실 방지."""
    extract = {"parsed": _PARSED, "prd_markdown": _SUBSTANTIVE_MD, "prd_graph": _NO_EPIC_GRAPH}
    gemini = FakeGemini(responses=[_IMPACT_EMPTY])
    fetch = [{
        "master_id": "doc_prd_master_x", "master_content": "### 1. 기존 누적 master PRD 본문",
        "master_prd_details": [], "latest_id": "doc_prd_x_v5", "latest_content": "직전",
        "latest_prd_details": [], "project_name": "x", "prd_total": 5,
    }]
    neo4j = FakeNeo4j(responses=[fetch])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="has-master")

    result = await run_prd_merge(
        ctx, PrdInput(project_name="x", version="v6", cps_graph={"nodes": [], "relationships": []}), extract
    )

    assert result.mode == "no_changes"   # 기존 master 보존, 덮어쓰기 안 함
    assert not any("MERGE (master:PRD_Document" in e["cypher"] for e in neo4j.executed)


# [2026-06 콜드스타트] 실질 PRD 문서지만 '📦'/'Epic N:' 형식이 아님 (LLM 형식 드리프트 모사).
# 기존 _is_substantive_prd_markdown 의 하드 정규식 게이트면 no_changes 로 드롭됨 → 콜드스타트 트랩
# (CPS 는 무조건 master 를 써서 누적되는데 PRD 만 빈 채 남던 비대칭의 직접 원인).
_SUBSTANTIVE_MD_NO_EMOJI = (
    "## PRD: AI 계정 관리\n\n"
    "### 1. 제품 개요\n"
    "- 제품 비전: AI 도구 신청부터 사후 관리까지 일원화된 운영 체계를 구축한다.\n"
    "- 성공 지표: 중복 결제 제거로 비용 30% 절감, 보안 사고 0건, 활성 계정률 85% 달성.\n\n"
    "### 2. 기능 계층 (에픽/스토리)\n"
    "- 에픽: AI 도구 신청·결제 통합 관리\n"
    "  - 스토리: 사용자는 신청 화면에서 도구를 신청하고 보안 서약 후 사용한다.\n"
    "  - 스토리: 관리자는 신청을 검토하고 승인 또는 반려한다.\n"
    "- 에픽: 토큰 사용량 자동 집계 및 비용 분석\n"
    "  - 스토리: 시스템은 매일 배치로 Claude 와 Gemini 토큰을 집계해 관리 대장에 표시한다.\n\n"
    "### 3. 화면 구조\n- 신청 화면, 승인 화면, 관리 대장 화면을 제공한다.\n\n"
    "### 4. 비기능 요구사항\n- 응답 2초 이내, OAuth 2.0 인증, 데이터 암호화 저장.\n"
)


async def test_cold_start_substantive_without_emoji_format_saves_preliminary():
    """[콜드스타트 트랩 회귀] graph Epic 0 + master 없음 + 실질 문서(단 '📦'/'Epic N:'
    형식이 아님) → no_changes 드롭이 아니라 예비 first master 저장.

    CPS 는 매회 master 를 무조건 써서 누적되는데, PRD 는 형식 정규식 하드 게이트 때문에
    형식만 다른 실질 문서를 통째로 드롭 → PRD master 가 영영 부트스트랩 안 되는 콜드스타트
    트랩이 'CPS 가득 / PRD 빈' 비대칭의 직접 원인이었다."""
    extract = {"parsed": _PARSED, "prd_markdown": _SUBSTANTIVE_MD_NO_EMOJI, "prd_graph": _NO_EPIC_GRAPH}
    gemini = FakeGemini(responses=[_IMPACT_EMPTY])
    neo4j = FakeNeo4j(responses=[[]])  # master 없음(first_run)
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="coldstart")

    result = await run_prd_merge(
        ctx, PrdInput(project_name="ai admin", version="v1", cps_graph={"nodes": [], "relationships": []}), extract
    )

    assert result.mode != "no_changes"             # 빈 PRD 아님 — 부트스트랩됨
    cyphers = [e["cypher"] for e in neo4j.executed]
    assert any("MERGE (master:PRD_Document" in c for c in cyphers)   # master 저장됨
    save = [e for e in neo4j.executed if "MERGE (master:PRD_Document" in e["cypher"]][0]
    assert save["params"]["merged_content"] == _SUBSTANTIVE_MD_NO_EMOJI


async def test_cold_start_empty_cps_does_not_bootstrap_even_if_markdown_long():
    """[환각 가드] 게이트 완화 후에도 CPS 입력이 통째로 빈 경우(parse sentinel '내용 없음'
    + 회의 원문도 없음)엔 예비 저장 안 함 — 프로젝트명만으로 PRD 환각하지 않도록. 실내용
    길이만으론 통과할 문서라도 CPS·회의록 둘 다 비면 막는다."""
    parsed_empty = {"pure_markdown": "내용 없음", "problems": "- 매핑된 문제 없음"}
    extract = {"parsed": parsed_empty, "prd_markdown": _SUBSTANTIVE_MD_NO_EMOJI, "prd_graph": _NO_EPIC_GRAPH}
    gemini = FakeGemini(responses=[_IMPACT_EMPTY])
    neo4j = FakeNeo4j(responses=[[]])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="empty-cps")

    result = await run_prd_merge(
        ctx,
        # meeting_content 도 비어야 진짜 '입력 없음' — 환각 가드 발동.
        PrdInput(project_name="ghost", version="v1", cps_graph={"nodes": [], "relationships": []}),
        extract,
    )

    assert result.mode == "no_changes"   # CPS·회의록 둘 다 비면 환각 안 함
    assert not any("MERGE (master:PRD_Document" in e["cypher"] for e in neo4j.executed)


async def test_batch_reparse_lost_meeting_content_still_bootstraps_when_meeting_present():
    """[배치 false-block 회귀] 배치 경로는 _prd_extract_from_cache 가 meeting_content 없이
    parsed 를 재구성(jobs.py:483)해, strict/lenient tier 가 full_markdown 을 누락하면 merge
    시점 pure_markdown 이 '내용 없음' 으로 떨어질 수 있다. 그래도 PrdInput.meeting_content(회의
    원문)가 있으면 실콘텐츠로 인정해 부트스트랩해야 한다 — 가드가 실콘텐츠를 false-block 해
    PR #173 의 콜드스타트 수정을 배치 경로에서 무력화하면 안 됨."""
    parsed_lost = {"pure_markdown": "내용 없음", "problems": "- 매핑된 문제 없음"}
    extract = {"parsed": parsed_lost, "prd_markdown": _SUBSTANTIVE_MD_NO_EMOJI, "prd_graph": _NO_EPIC_GRAPH}
    gemini = FakeGemini(responses=[_IMPACT_EMPTY])
    neo4j = FakeNeo4j(responses=[[]])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="batch-reparse")

    result = await run_prd_merge(
        ctx,
        PrdInput(
            project_name="ai admin", version="v1",
            cps_graph={"nodes": [], "relationships": []},
            meeting_content=(
                "회의 원문: AI 계정 관리 신청/승인/배정/관리 4단계 프로세스를 논의함. "
                "신청 양식, 보안 서약, 토큰 사용량 집계 등 상세 요구사항을 도출했다."
            ),
        ),
        extract,
    )

    assert result.mode != "no_changes"   # 회의 원문이 있으므로 부트스트랩 (false-block 아님)
    assert any("MERGE (master:PRD_Document" in e["cypher"] for e in neo4j.executed)


# spec_count>0 (실질 Epic/Story 보유) 그래프 — main path 진입용.
_SPEC_GRAPH = {
    "nodes": [
        {"id": "doc_prd_x", "label": "PRD_Document", "properties": {}},
        {"id": "epic_01", "label": "Epic", "properties": {"summary": "AI 도구 신청·결제 통합 관리"}},
        {"id": "story_01_1", "label": "Story", "properties": {"summary": "사용자는 도구를 신청한다"}},
    ],
    "relationships": [],
}


async def test_main_path_first_run_empty_merge_falls_back_to_markdown():
    """[main path 빈-merge 회귀] spec_count>0(풍부한 스펙=대다수 회의) + 콜드스타트에서 merge
    LLM 이 빈/공백 출력을 내면, 기존엔 build_merge_master_prd_query 가 ValueError → job 실패 +
    arq 무한 재시도 + PRD master 영영 미생성. 이제 실질 prd_markdown 으로 fallback 해 master 를
    저장해야 한다 (spec_count==0 가지의 부트스트랩과 대칭)."""
    extract = {"parsed": _PARSED, "prd_markdown": _SUBSTANTIVE_MD, "prd_graph": _SPEC_GRAPH}
    gemini = FakeGemini(responses=[_IMPACT_EMPTY, "   "])   # impact 정상, merge 는 공백
    neo4j = FakeNeo4j(responses=[[]])   # master 없음(first_run)
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="empty-merge")

    result = await run_prd_merge(
        ctx, PrdInput(project_name="ai admin", version="v1", cps_graph={"nodes": [], "relationships": []}), extract
    )

    cyphers = [e["cypher"] for e in neo4j.executed]
    assert any("MERGE (master:PRD_Document" in c for c in cyphers)   # ValueError 아님 — master 저장됨
    save = [e for e in neo4j.executed if "MERGE (master:PRD_Document" in e["cypher"]][0]
    assert save["params"]["merged_content"] == _SUBSTANTIVE_MD      # 빈 merge → prd_markdown fallback


# [R2] Agent1 이 prd_extract.md OUTPUT SCHEMA 뼈대를 그대로 emit 한 placeholder 스켈레톤 —
# 길이는 300 초과지만 대부분이 미치환 대괄호 placeholder([...작성])라 실질 내용이 없다.
_PLACEHOLDER_SKELETON_MD = (
    "## 🚀 PRD: [프로젝트명을 여기에 작성]\n\n"
    "### 1. Product Overview\n"
    "- **Product Vision**: [CPS의 Context를 기반으로 한 통합된 제품 비전을 여기에 작성하세요]\n"
    "- **Success Metrics**: [정량적 지표 또는 정성적 성공 지표를 여기에 작성하세요]\n\n"
    "### 2. Epic & User Story Map\n"
    "#### 📦 Epic 1: [핵심 기능 그룹의 이름을 여기에 작성하세요]\n"
    "- **[Story 1.1] [구체적인 사용자 스토리를 여기에 작성하세요]**\n"
    "  - User Flow: [사용자 흐름을 단계별로 여기에 작성하세요]\n"
    "#### 📦 Epic 2: [두 번째 핵심 기능 그룹의 이름을 여기에 작성하세요]\n"
    "### 3. Screen Architecture\n"
    "#### 🖥️ [Screen: 화면 이름을 여기에 작성하세요]\n\n"
    "### 4. Global Non-Functional Requirements\n"
    "- [비기능 요구사항(성능/보안/확장성)을 여기에 작성하세요]\n"
)

# [R2] 짧지만(<300자) 진짜 실내용 — placeholder 아니고 대괄호도 거의 없음.
_SHORT_REAL_MD = (
    "## PRD: AI 계정 관리\n\n"
    "### 2. 기능 계층\n"
    "- 에픽: AI 도구 신청·결제 통합 관리\n"
    "  - 스토리: 사용자는 신청 화면에서 도구를 신청하고 관리자가 검토 후 승인한다.\n"
    "  - 스토리: 시스템은 토큰 사용량을 일일 배치로 집계해 관리 대장에 표시한다.\n"
    "### 3. 화면 구조\n- 신청 화면, 승인 화면, 관리 대장 화면을 제공한다.\n"
    "### 4. 비기능\n- 응답 2초 이내, OAuth 2.0 인증, 데이터 암호화 저장.\n"
)


async def test_placeholder_skeleton_not_bootstrapped():
    """[#173 회귀 차단] Agent1 이 템플릿 뼈대를 그대로 emit 한 placeholder 스켈레톤 문서는
    길이>=300 이어도 부트스트랩하면 안 됨 — 빈 껍데기 PRD master 방지. #173 이 형식 정규식을
    제거해 '길이 단독' 게이트가 되면서 스켈레톤이 '실질적'으로 오판되던 구멍을, 대괄호
    placeholder 밀도로 차단."""
    extract = {"parsed": _PARSED, "prd_markdown": _PLACEHOLDER_SKELETON_MD, "prd_graph": _NO_EPIC_GRAPH}
    gemini = FakeGemini(responses=[_IMPACT_EMPTY])
    neo4j = FakeNeo4j(responses=[[]])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="skeleton")

    result = await run_prd_merge(
        ctx,
        PrdInput(project_name="x", version="v1", cps_graph={"nodes": [], "relationships": []},
                 meeting_content="실제 회의 원문이 있는 상태"),
        extract,
    )

    assert result.mode == "no_changes"   # placeholder 스켈레톤은 부트스트랩 안 함
    assert not any("MERGE (master:PRD_Document" in e["cypher"] for e in neo4j.executed)


async def test_short_real_markdown_bootstraps():
    """[짧지만 진짜] placeholder 아닌 실내용이 300자 미만이라도 충분하고 대괄호 밀도가 낮으면
    부트스트랩 — 진짜 Epic 을 가진 짧은 회의가 #173 의 300자 floor 에 걸려 드롭되던 구멍 차단."""
    assert 150 <= len(_SHORT_REAL_MD.strip()) < 300   # 테스트 전제: 짧은-실내용 구간
    extract = {"parsed": _PARSED, "prd_markdown": _SHORT_REAL_MD, "prd_graph": _NO_EPIC_GRAPH}
    gemini = FakeGemini(responses=[_IMPACT_EMPTY])
    neo4j = FakeNeo4j(responses=[[]])
    ctx = PipelineContext(gemini=gemini, neo4j=neo4j, idempotency_key="short-real")

    result = await run_prd_merge(
        ctx, PrdInput(project_name="ai admin", version="v1", cps_graph={"nodes": [], "relationships": []}), extract
    )

    assert result.mode != "no_changes"   # 짧아도 진짜면 부트스트랩
    assert any("MERGE (master:PRD_Document" in e["cypher"] for e in neo4j.executed)
