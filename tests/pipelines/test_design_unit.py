"""
Design 파이프라인 단위 테스트 — 결정적 부분만.

회귀 추적: createDesign 파이프라인 각 단계의 결정적 동작.

build_save_* 함수는 이제 (cypher_str, params_dict) 튜플을 반환. $project / $apis / ...
등의 파라메터 바인딩 사용.
"""
from __future__ import annotations

import pytest

from app.pipelines.design_pipeline import (
    _to_cypher_literal,
    build_save_architecture_query,
    build_save_ddd_query,
    build_save_spack_query,
    detect_dirty_prd,
    extract_prd_sections,
)


# ─── PRD Section Extractor ──────────────────────────────


_SAMPLE_PRD = """\
## 🗺️ Master PRD

### 1. Product Overview (통합 제품 비전)
- **통합 비전**: VISION
- **핵심 타겟**: TARGET

### 2. Epic & User Story Map (기능 계층도)
#### 📦 [Epic-01] EPIC-A
- `[Story-01.1]` story-a

### 3. Screen Architecture (화면별 구현 명세)
#### 🖥️ [Screen: Home]
- **포함된 기능**:
  - `[Story-01.1]` ...

### 4. Global Non-Functional Requirements (공통 제약 사항)
- **공통 규칙**:
  - rule1
"""


def test_extract_prd_sections_builds_three_inputs():
    inputs, diag = extract_prd_sections(_SAMPLE_PRD)

    # [2026-05-26] spack_input = Overview + EpicMap + Screens + NFR.
    # Screens 추가 이유: Section 3 만 풍부한 PRD 에서 SPACK 이 underextract 하지 않도록.
    assert "VISION" in inputs["spack_input"]
    assert "EPIC-A" in inputs["spack_input"]
    assert "rule1" in inputs["spack_input"]
    assert "Home" in inputs["spack_input"]  # Screen 포함 (2026-05-26)

    # ddd_input = Overview + EpicMap (도메인 모델 추출 — 화면 의존성 적음)
    assert "VISION" in inputs["ddd_input"]
    assert "EPIC-A" in inputs["ddd_input"]
    assert "rule1" not in inputs["ddd_input"]
    assert "Home" not in inputs["ddd_input"]

    # [B0 — 2026-05] arch_input = Overview + EpicMap + NFR + Screens.
    # Service lineage (related_story_ids) 를 위해 Epic Map 추가됨.
    assert "VISION" in inputs["arch_input"]
    assert "rule1" in inputs["arch_input"]
    assert "Home" in inputs["arch_input"]
    assert "EPIC-A" in inputs["arch_input"]

    assert diag["overview_found"]
    assert diag["epic_map_found"]
    assert diag["screens_found"]
    assert diag["nfr_found"]


def test_extract_prd_sections_spack_includes_screen_story_refs():
    """
    [2026-05-26 회귀 보호] PRD Section 2 (Epic Map) 가 빈약하고 Section 3 (Screens) 만
    풍부한 케이스 — AI Agent 프로젝트에서 발견. spack_input 은 Section 3 의 Story 참조도
    포함해야 SPACK 이 API 도출 시 충분한 근거 확보.
    """
    sparse_epic_rich_screens = """\
## 🗺️ Master PRD

### 1. Product Overview
- **통합 비전**: VISION

### 2. Epic & User Story Map
#### 📦 [Epic-01] 사용자 질의 응답
- `[Story-01.1]` 사용자 질의 응답

### 3. Screen Architecture
#### 🖥️ [Screen: 대시보드]
- **포함된 기능**:
  - `[Story-01.2]` 작업 결과 모니터링
  - `[Story-02.1]` 에이전트 설정 변경
#### 🖥️ [Screen: 작업 관리]
- **포함된 기능**:
  - `[Story-03.1]` 자동화 스케줄링
  - `[Story-04.1]` 성능 지표 조회

### 4. Global Non-Functional Requirements
- **성능**: 응답 3초
"""
    inputs, _ = extract_prd_sections(sparse_epic_rich_screens)
    # Section 2 에 Story-01.1 만 있어도 spack_input 은 Section 3 의 Story-01.2,
    # 02.1, 03.1, 04.1 까지 포함해야 함 — SPACK LLM 이 API 5개 도출할 수 있도록.
    for sid in ["Story-01.1", "Story-01.2", "Story-02.1", "Story-03.1", "Story-04.1"]:
        assert sid in inputs["spack_input"], (
            f"spack_input 에 {sid} 누락 — Section 3 의 Story 참조가 빠지면 "
            "SPACK 이 underextract."
        )


def test_extract_prd_sections_raises_on_empty():
    with pytest.raises(ValueError, match="마스터 PRD 가 비어있음"):
        extract_prd_sections("")
    with pytest.raises(ValueError, match="마스터 PRD 가 비어있음"):
        extract_prd_sections("   \n  ")


def test_extract_prd_sections_falls_back_to_full_prd_when_no_sections():
    """헤더가 하나도 없으면 모든 input 이 full PRD 로 fallback."""
    minimal = "그냥 본문 텍스트만 있는 PRD"
    inputs, diag = extract_prd_sections(minimal)
    assert inputs["spack_input"] == minimal
    assert inputs["ddd_input"] == minimal
    assert inputs["arch_input"] == minimal
    assert diag["section_count"] == 0
    # fallback diagnostic: 3개 input 모두 전체 PRD 로 떨어짐
    assert set(diag["fallback_to_full_prd"]) == {"spack", "ddd", "arch"}


def test_extract_prd_sections_source_headers_in_diagnostic():
    """매칭된 헤더의 원본 텍스트가 diagnostic 에 기록됨 (운영 디버깅용)."""
    _, diag = extract_prd_sections(_SAMPLE_PRD)
    assert diag["overview_source"] is not None
    assert "Product Overview" in diag["overview_source"]
    assert diag["epic_map_source"] is not None
    assert "Epic" in diag["epic_map_source"]
    assert diag["screens_source"] is not None
    assert diag["nfr_source"] is not None
    assert diag["fallback_to_full_prd"] == []


def test_extract_prd_sections_fuzzy_match_alternate_header_form():
    """대소문자/번호 형식이 달라도 매칭 가능 — fuzzy 정규화. (### 헤더 레벨 가정)"""
    alt = (
        "### 제품 비전\n\n비전 본문\n\n"
        "### 2-1. EPIC MAP\n\nEpic 본문\n\n"
        "### 비기능 요구사항\n\nNFR 본문\n"
    )
    inputs, diag = extract_prd_sections(alt)
    assert "비전 본문" in inputs["spack_input"]
    assert "Epic 본문" in inputs["spack_input"]
    assert "NFR 본문" in inputs["spack_input"]
    assert diag["overview_found"]
    assert diag["epic_map_found"]
    assert diag["nfr_found"]
    # fallback 없음
    assert diag["fallback_to_full_prd"] == []


# ─── detect_dirty_prd (2026-05-26 — design auto-cleanup trigger) ────────


def test_detect_dirty_prd_clean_prd_returns_not_dirty():
    """정상 PRD (Product Vision 1개 + reconcile OK) → cleanup 발동 안 함."""
    clean = """## Master PRD

### 1. Product Overview
- **통합 비전**: 식물 관리 시스템

### 2. Epic & User Story Map
#### 📦 [Epic-01] 식물 관리
- [Story 1.1] 등록

### 3. Screen Architecture
#### 🖥️ [Screen: 등록]
- 포함된 기능: [Story 1.1]

### 4. NFR
- 응답 500ms
"""
    result = detect_dirty_prd(clean)
    assert result["is_dirty"] is False
    assert result["reasons"] == []
    assert result["diagnostic"]["product_vision_count"] == 1
    assert result["diagnostic"]["s2_s3_missing_count"] == 0


def test_detect_dirty_prd_product_vision_repeats_triggers():
    """Product Vision 5+ → dirty (누적 merge 신호)."""
    pv_block = "- **Product Vision**: 자동화\n" * 6  # 6 회
    prd = f"""## Master PRD

### 1. Product Overview
- **통합 비전**: AI Agent
{pv_block}

### 2. Epic & User Story Map
#### 📦 [Epic-01] 인증
- [Story 1.1] 로그인

### 3. Screen Architecture
#### 🖥️ [Screen: 로그인]
- 포함된 기능: [Story 1.1]

### 4. NFR
- 응답 500ms
"""
    result = detect_dirty_prd(prd)
    assert result["is_dirty"] is True
    assert any("product_vision_repeats" in r for r in result["reasons"])
    # 통합 비전 1 + Product Vision 6 = 7 개
    assert result["diagnostic"]["product_vision_count"] >= 7


def test_detect_dirty_prd_s2_s3_mismatch_triggers():
    """Section 2 에 정의 안 된 Story 를 Section 3 가 3+ 참조 → dirty."""
    prd = """## Master PRD

### 1. Product Overview
- **통합 비전**: AI Agent

### 2. Epic & User Story Map
#### 📦 [Epic-01] 인증
- [Story 1.1] 로그인

### 3. Screen Architecture
#### 🖥️ [Screen: 대시보드]
- 포함된 기능: [Story 1.1], [Story 1.2], [Story 2.1]
#### 🖥️ [Screen: 작업]
- 포함된 기능: [Story 3.1]

### 4. NFR
- 응답 500ms
"""
    result = detect_dirty_prd(prd)
    assert result["is_dirty"] is True
    assert any("s2_s3_story_mismatch" in r for r in result["reasons"])
    assert result["diagnostic"]["s2_s3_missing_count"] == 3


def test_detect_dirty_prd_borderline_mismatch_not_dirty():
    """Section 3 의 누락 Story 가 2 이하면 dirty 아님 (정상 작성 중 일시적 inconsistency)."""
    prd = """## Master PRD

### 1. Product Overview
- **통합 비전**: AI Agent

### 2. Epic & User Story Map
#### 📦 [Epic-01] 인증
- [Story 1.1] 로그인

### 3. Screen Architecture
#### 🖥️ [Screen: 로그인]
- 포함된 기능: [Story 1.1], [Story 1.2]

### 4. NFR
- 응답 500ms
"""
    result = detect_dirty_prd(prd)
    # missing 1 개 — 임계 (3) 미만 → not dirty
    assert result["is_dirty"] is False
    assert result["diagnostic"]["s2_s3_missing_count"] == 1


def test_detect_dirty_prd_both_triggers_present():
    """Product Vision repeat + S2/S3 mismatch 동시 → reasons 둘 다 노출."""
    pv_block = "- **Product Vision**: 자동화\n" * 6
    prd = f"""## Master PRD

### 1. Product Overview
- **통합 비전**: AI Agent
{pv_block}

### 2. Epic & User Story Map
#### 📦 [Epic-01] 인증
- [Story 1.1] 로그인

### 3. Screen Architecture
#### 🖥️ [Screen: 대시보드]
- 포함된 기능: [Story 1.1], [Story 1.2], [Story 2.1], [Story 3.1]

### 4. NFR
- 응답 500ms
"""
    result = detect_dirty_prd(prd)
    assert result["is_dirty"] is True
    assert len(result["reasons"]) == 2


def test_detect_dirty_prd_empty_content_not_dirty():
    """빈 content → not dirty (별도 가드가 처리, cleanup 호출 의미 없음)."""
    assert detect_dirty_prd("")["is_dirty"] is False
    assert detect_dirty_prd("   ")["is_dirty"] is False
    assert detect_dirty_prd(None)["is_dirty"] is False  # type: ignore[arg-type]


# ─── _to_cypher_literal (회귀 보호) ───────────────────────────


def test_to_cypher_literal_types():
    assert _to_cypher_literal(None) == "null"
    assert _to_cypher_literal(True) == "true"
    assert _to_cypher_literal(False) == "false"
    assert _to_cypher_literal(42) == "42"
    assert _to_cypher_literal(1.5) == "1.5"
    assert _to_cypher_literal("a'b") == "'a\\'b'"
    assert _to_cypher_literal(["x", 1]) == "['x', 1]"
    assert _to_cypher_literal({"id": "X", "n": 3}) == "{id: 'X', n: 3}"


def test_to_cypher_literal_nested():
    out = _to_cypher_literal([{"id": "A", "tags": ["x", "y"]}, {"id": "B"}])
    assert out == "[{id: 'A', tags: ['x', 'y']}, {id: 'B'}]"


# ─── Spack Cypher 빌더 (parameter binding) ───────────────────────


def test_build_save_spack_query_wipes_then_creates():
    q, params = build_save_spack_query(
        "harness",
        {
            "apis": [
                {
                    "id": "API-01",
                    "name": "list",
                    "method": "GET",
                    "endpoint": "/x",
                    "description": "d",
                    "related_story_id": "Story-01.1",
                }
            ],
            "entities": [
                {
                    "id": "ENT-01",
                    "name": "Ticket",
                    "attributes": ["id"],
                    "description": "e",
                }
            ],
            "policies": [
                {
                    "id": "POL-01",
                    "category": "Security",
                    "description": "d",
                    "related_entity": "Ticket",
                }
            ],
        },
    )
    # Wipe 단계
    assert "DETACH DELETE n" in q
    # [#3 — 2026-05-25] Screen 노드도 wipe 범위에 포함.
    assert "(n:API OR n:Entity OR n:Policy OR n:Screen)" in q
    # API → Story IMPLEMENTS 관계
    assert "MERGE (api)-[:IMPLEMENTS]->(s)" in q
    # Policy → Entity GOVERNS 관계
    assert "MERGE (pol)-[:GOVERNS]->(e)" in q
    # 최종 RETURN
    assert "RETURN 'Spack Sync Completed' AS Status" in q

    # 파라메터 바인딩 (LLM 입력이 Cypher 본문에 아닌 params 에 있어야 함)
    assert "$project" in q
    assert "$apis" in q
    assert "$entities" in q
    assert "$policies" in q
    assert params["project"] == "harness"
    assert params["apis"][0]["id"] == "API-01"
    assert params["entities"][0]["id"] == "ENT-01"
    assert params["policies"][0]["id"] == "POL-01"

    # 회귀 가드: LLM 이 일으킨 값이 더 이상 Cypher 문자열에 직접 등장하지 않을 것
    assert "'API-01'" not in q
    assert "'Ticket'" not in q


def test_build_save_spack_query_empty_result_preserves_existing():
    """[2026-05-27] 빈 생성 결과는 기존 SPACK 데이터를 wipe 하면 안 됨.

    dirty PRD underextract 등으로 LLM 이 아무 노드도 못 뽑으면, 파괴적
    wipe-and-redraw 가 이전의 정상 SPACK 을 영구 삭제하던 회귀 방지.
    빈 결과 → wipe 없이 no-op (기존 데이터 보존).
    """
    q, params = build_save_spack_query(
        "p", {"apis": [], "entities": [], "policies": [], "screens": []}
    )
    # 빈 결과 → wipe 금지
    assert "DETACH DELETE" not in q
    assert "UNWIND" not in q
    # 트랜잭션 안에서 실행 가능한 유효 no-op statement 는 유지
    assert "RETURN" in q
    # $project 만, 노드 params 는 추가 안 함
    assert params["project"] == "p"
    assert "apis" not in params
    assert "entities" not in params
    assert "policies" not in params


def test_build_save_spack_query_partial_data_still_wipes():
    """일부 레이어만 채워진 정상 재생성은 여전히 wipe — 제거된 노드 정리.

    빈-결과 가드가 '정상이지만 일부 레이어가 빈' 재생성까지 막지 않도록 보장.
    """
    q, _ = build_save_spack_query(
        "p",
        {
            "apis": [
                {"id": "API-01", "name": "list", "method": "GET",
                 "endpoint": "/x", "description": "d"}
            ],
            "entities": [],
            "policies": [],
            "screens": [],
        },
    )
    assert "DETACH DELETE n" in q
    assert "$apis" in q


def test_build_save_spack_query_malicious_llm_payload_safe():
    """
    프롬프트 인젝션 → Cypher 인젝션 체인 회귀 방지.

    LLM 이 악의적 입력을 생성해도 (예: id 에 quote 포함) Cypher 본문에
    직접 인터폴레이션되지 않고 params 안에서만 조작되야 함.
    """
    payload = {
        "apis": [
            {
                "id": "API'); DETACH DELETE (n) //",
                "name": "evil",
                "method": "GET",
                "endpoint": "/x",
                "description": "d",
            }
        ],
        "entities": [],
        "policies": [],
    }
    q, params = build_save_spack_query("p", payload)
    # 악성 문자열이 Cypher 본문에 없어야 함
    assert "DETACH DELETE (n) //" not in q
    # params 에서는 원형 그대로 (driver 가 파라메터로 처리 → injection 불가)
    assert params["apis"][0]["id"] == "API'); DETACH DELETE (n) //"


# ─── DDD Cypher 빌더 (parameter binding) ─────────────────────────


def test_build_save_ddd_query_emits_all_node_types():
    q, params = build_save_ddd_query(
        "harness",
        {
            "contexts": [{"id": "CTX-01", "name": "C1", "description": "d"}],
            "aggregates": [
                {"id": "AGG-01", "name": "Ticket", "context_id": "CTX-01", "description": "d"}
            ],
            "entities": [
                {"id": "DENT-01", "name": "TT", "aggregate_id": "AGG-01", "description": "d"}
            ],
            "events": [
                {
                    "id": "EVT-01",
                    "name": "TI",
                    "description": "d",
                    "related_story_id": "Story-01.1",
                    "published_by_aggregate_id": "AGG-01",
                }
            ],
        },
    )
    assert "(n:BoundedContext OR n:Aggregate OR n:DomainEntity OR n:DomainEvent)" in q
    # Aggregate → Context BELONGS_TO
    assert "MERGE (agg)-[:BELONGS_TO]->(ctx)" in q
    # DomainEntity → Aggregate PART_OF
    assert "MERGE (dent)-[:PART_OF]->(agg)" in q
    # Aggregate → DomainEvent PUBLISHES
    assert "MERGE (agg)-[:PUBLISHES]->(evt)" in q
    # Story → DomainEvent TRIGGERS
    assert "MERGE (s)-[:TRIGGERS]->(evt)" in q
    assert "RETURN 'DDD Sync Completed' AS Status" in q

    # 파라메터 바인딩
    assert "$project" in q
    assert "$contexts" in q
    assert "$aggregates" in q
    assert "$entities" in q
    assert "$events" in q
    assert params["project"] == "harness"
    assert params["contexts"][0]["id"] == "CTX-01"
    assert params["aggregates"][0]["id"] == "AGG-01"
    assert params["events"][0]["id"] == "EVT-01"


# ─── Architecture Cypher 빌더 (parameter binding) ─────────────────────


def test_build_save_architecture_query_with_connections():
    q, params = build_save_architecture_query(
        "harness",
        {
            "services": [
                {
                    "id": "SVC-01",
                    "name": "Front",
                    "type": "Frontend",
                    "tech_stack": "Vue.js",
                    "description": "d",
                },
                {
                    "id": "SVC-02",
                    "name": "API",
                    "type": "Backend API",
                    "tech_stack": "Spring Boot",
                    "description": "d",
                    "owned_aggregates": ["Ticket"],
                },
            ],
            "databases": [
                {
                    "id": "DB-01",
                    "name": "Primary",
                    "type": "Relational",
                    "tech_stack": "PostgreSQL",
                    "description": "d",
                }
            ],
            "connections": [
                {
                    "source_id": "SVC-01",
                    "target_id": "SVC-02",
                    "protocol": "HTTPS",
                    "description": "d",
                }
            ],
        },
    )
    assert "(n:ArchService OR n:ArchDatabase)" in q
    assert "MERGE (src)-[rel:CONNECTS_TO]->(tgt)" in q
    assert "WHERE src:ArchService OR src:ArchDatabase" in q
    assert "RETURN 'Architecture Sync Completed' AS Status" in q

    # 파라메터 바인딩
    assert "$project" in q
    assert "$services" in q
    assert "$databases" in q
    assert "$connections" in q
    assert params["project"] == "harness"
    assert len(params["services"]) == 2
    assert params["databases"][0]["id"] == "DB-01"
    assert params["connections"][0]["protocol"] == "HTTPS"


def test_build_save_architecture_query_empty_result_preserves_existing():
    """[2026-05-27] 빈 Architecture 생성 결과는 기존 데이터를 wipe 하면 안 됨."""
    q, params = build_save_architecture_query(
        "p", {"services": [], "databases": [], "connections": []}
    )
    assert "DETACH DELETE" not in q
    assert "UNWIND" not in q
    assert "RETURN" in q
    assert params["project"] == "p"
    assert "services" not in params
    assert "databases" not in params
    assert "connections" not in params


def test_build_save_ddd_query_empty_result_preserves_existing():
    """[2026-05-27] 빈 DDD 생성 결과는 기존 데이터를 wipe 하면 안 됨."""
    q, params = build_save_ddd_query(
        "p", {"contexts": [], "aggregates": [], "entities": [], "events": []}
    )
    assert "DETACH DELETE" not in q
    assert "UNWIND" not in q
    assert "RETURN" in q
    assert params["project"] == "p"
    assert "contexts" not in params
    assert "aggregates" not in params
    assert "entities" not in params
    assert "events" not in params
