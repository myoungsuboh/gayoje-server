"""
PRD 파이프라인 단위 테스트 — 결정적 부분만.

회귀 추적: PRD 파이프라인 각 단계의 결정적 동작.
"""
from __future__ import annotations

import pytest

from app.pipelines.prd_pipeline import (
    PrdInput,
    build_merge_master_prd_query,
    build_save_prd_query,
    filter_affected_prd_sections,
    parse_cps_for_prd,
)


# ─── Code_CPS_Parser 동등성 ──────────────────────────────────────


def test_parse_cps_extracts_markdown_and_problems():
    graph = {
        "nodes": [
            {
                "id": "doc_cps_x_v1_1",
                "label": "CPS_Document",
                "properties": {"full_markdown": "## 📄 CPS\n- body"},
            },
            {"id": "prb_01", "label": "Problem", "properties": {"summary": "A"}},
            {"id": "prb_02", "label": "Problem", "properties": {"summary": "B"}},
            {"id": "res_01", "label": "Solution", "properties": {"summary": "Y"}},
        ],
        "relationships": [],
    }
    out = parse_cps_for_prd(graph)
    assert out["pure_markdown"] == "## 📄 CPS\n- body"
    assert "- [prb_01] A" in out["problems"]
    assert "- [prb_02] B" in out["problems"]
    assert "res_01" not in out["problems"]  # Problem 라벨만 추출


def test_parse_cps_handles_missing_doc_and_problems():
    out = parse_cps_for_prd({"nodes": [], "relationships": []})
    assert out["pure_markdown"] == "내용 없음"
    assert out["problems"] == "- 매핑된 문제 없음"


# ─── PrdInput derived id ─────────────────────────────────────────


def test_prd_input_derives_id_from_project_version():
    p = PrdInput(project_name="harness", version="v1.2", cps_graph={})
    assert p.derived_prd_id() == "doc_prd_harness_v1_2"


def test_prd_input_uses_previous_id_when_given():
    p = PrdInput(
        project_name="harness",
        version="v1.2",
        cps_graph={},
        previous_prd_id="doc_prd_master_harness",
    )
    assert p.derived_prd_id() == "doc_prd_master_harness"


# ─── build_save_prd_query (Save CPS Code 와 byte-identical 검증) ─


def test_build_save_prd_query_alias_emits_correct_cypher():
    q, params = build_save_prd_query(
        {
            "nodes": [
                {"id": "doc_prd_x_v1_1", "label": "PRD_Document", "properties": {"project": "x"}},
                {"id": "epic_01", "label": "Epic", "properties": {"summary": "E"}},
                {"id": "story_01_1", "label": "Story", "properties": {"summary": "S"}},
            ],
            "relationships": [
                {"source": "epic_01", "type": "CONTAINS", "target": "story_01_1"},
            ],
        }
    )
    # label / rtype 만 cypher 식별자 자리 (화이트리스트 통과 후 인터폴)
    assert "MERGE (n0:PRD_Document" in q
    assert "MERGE (n1:Epic" in q
    assert "MERGE (n2:Story" in q
    assert ":CONTAINS]->" in q
    # 값/id/properties 는 모두 params 로
    assert params["n_0"][0]["id"] == "doc_prd_x_v1_1"
    assert params["n_0"][0]["props"]["project"] == "x"
    assert params["n_1"][0]["id"] == "epic_01"
    assert params["n_2"][0]["id"] == "story_01_1"
    assert params["r_3"][0]["source"] == "epic_01"
    assert params["r_3"][0]["target"] == "story_01_1"


# ─── Merge master PRD Cypher ─────────────────────────────────────


def test_build_merge_master_prd_query_has_based_on_to_cps():
    """PRD 마스터는 항상 동일 프로젝트 CPS 마스터에 BASED_ON 연결."""
    q, params = build_merge_master_prd_query(
        "harness", "## doc", latest_delta_id=None, cleanup_at_version_count=0
    )
    # Cypher 본문은 $param 바인딩만. id 값은 params 로.
    assert "MERGE (master:PRD_Document {id: $master_prd_id})" in q
    assert "OPTIONAL MATCH (cps_m:CPS_Document {id: $master_cps_id})" in q
    assert "MERGE (master)-[:BASED_ON]->(cps_m)" in q
    assert params["master_prd_id"] == "doc_prd_master_harness"
    assert params["master_cps_id"] == "doc_cps_master_harness"
    assert params["project"] == "harness"
    assert params["merged_content"] == "## doc"
    assert "latest_delta_id" not in params  # first_run 분기


def test_build_merge_master_prd_query_links_latest_delta():
    q, params = build_merge_master_prd_query(
        "harness", "x", latest_delta_id="doc_prd_harness_v1_2", cleanup_at_version_count=0
    )
    assert "MATCH (latest:PRD_Document {id: $latest_delta_id})" in q
    assert "MERGE (master)-[:SYNTHESIZED_FROM]->(latest)" in q
    assert "SET latest.is_latest = false" in q
    assert params["latest_delta_id"] == "doc_prd_harness_v1_2"
    # BASED_ON 분기는 latest 유무와 독립 — 항상 포함
    assert "MERGE (master)-[:BASED_ON]->(cps_m)" in q


# ─── PRD Section Filter — PRD 전용 fallback 키워드 검증 ─────────


_PRD_MASTER = """\
## 🗺️ Master PRD

### 1. Product Overview (통합 제품 비전)
- **통합 비전**: ENV

### 2. Epic & User Story Map (기능 계층도)
#### 📦 [Epic-01] A
- `[Story-01.1]` story-a

### 3. Screen Architecture (화면별 구현 명세)
#### 🖥️ [Screen: Home]
- **포함된 기능**:
  - `[Story-01.1]` ...

### 4. Global Non-Functional Requirements (공통 제약 사항)
- **공통 규칙**:
  - rule1
"""


def test_filter_first_run_when_no_master():
    out = filter_affected_prd_sections(
        master_content="",
        latest_content="delta content",
        impact={"affected_sections": [], "removed_epic_ids": [], "removed_story_ids": []},
    )
    assert out["is_first_run"] is True
    assert out["_diagnostic"]["mode"] == "FIRST_RUN"


def test_filter_uses_prd_defaults_when_impact_empty():
    """
    impact.affected_sections 가 비고 latest_content > 50 → PRD 기본 후보 적용.

    의도된 한계:
      기본 후보 'Epic & Story Map' 은 실제 마스터 섹션 'Epic & User Story Map (...)'
      에 substring 매칭이 안 된다 ('User' 가 가운데 들어가 있어서). 따라서 첫 패스에선
      'Screen Architecture' 만 매칭. fallback ['Epic', 'Screen'] 도 affected_content
      가 이미 채워졌으므로 발동하지 않음 → Epic 섹션은 결국 매칭 안 됨.

    이 동작은 PRD Section Filter1 단계의 현재 동작과 동일. 향후 docs/tech-debt.md
    에 기록되어 별도 PR 에서 fallback 로직 개선 예정.
    """
    out = filter_affected_prd_sections(
        master_content=_PRD_MASTER,
        latest_content="x" * 60,
        impact={"affected_sections": [], "removed_epic_ids": [], "removed_story_ids": []},
    )
    keys_lower = [k.lower() for k in out["affected_section_keys"]]
    # Screen 은 잡혀야 함
    assert any("screen" in k for k in keys_lower)
    # NFR / Overview 는 안 잡혀야 함
    assert not any("non-functional" in k for k in keys_lower)
    assert not any("overview" in k for k in keys_lower)


def test_filter_matches_explicit_impact_sections():
    out = filter_affected_prd_sections(
        master_content=_PRD_MASTER,
        latest_content="delta",
        impact={
            "affected_sections": ["Global Non-Functional Requirements"],
            "removed_epic_ids": [],
            "removed_story_ids": [],
        },
    )
    keys_lower = [k.lower() for k in out["affected_section_keys"]]
    assert any("non-functional" in k or "global" in k for k in keys_lower)
    assert "rule1" in out["affected_sections_content"]


def test_filter_fallback_to_epic_screen_when_no_match():
    out = filter_affected_prd_sections(
        master_content=_PRD_MASTER,
        latest_content="x",  # < 50 chars → defaults not applied
        impact={
            "affected_sections": ["NonexistentSection"],
            "removed_epic_ids": [],
            "removed_story_ids": [],
        },
    )
    # fallback ['Epic', 'Screen'] 발동
    keys_lower = [k.lower() for k in out["affected_section_keys"]]
    assert any("epic" in k for k in keys_lower) or any("screen" in k for k in keys_lower)


# ─── PRD 빌더 인젝션 회귀 (cps 와 동일 화이트리스트 적용 확인) ──


def test_build_save_prd_query_rejects_unsafe_label_and_type():
    """LLM 이 PRD JSON 의 label/type 에 인젝션 시도해도 화이트리스트가 차단."""
    q, params = build_save_prd_query(
        {
            "nodes": [
                {"id": "good_prd", "label": "Epic", "properties": {"summary": "auth"}},
                {"id": "bad1", "label": "Bad Label", "properties": {"summary": "x"}},
                {"id": "bad2", "label": "X`)//DROP", "properties": {"summary": "x"}},
                # 빈 라벨
                {"id": "bad3", "label": "", "properties": {"summary": "x"}},
                # 비-string 라벨
                {"id": "bad4", "label": 123, "properties": {"summary": "x"}},
            ],
            "relationships": [
                {"source": "good_prd", "target": "good_prd", "type": "OK_REL"},
                {"source": "good_prd", "target": "good_prd", "type": "Bad Type"},
                {"source": "good_prd", "target": "good_prd", "type": "T;DROP DATABASE"},
                # 빈 type
                {"source": "good_prd", "target": "good_prd", "type": ""},
            ],
        }
    )
    # 위험 항목은 cypher 본문에 절대 나타나지 않음
    assert "Bad Label" not in q
    assert "DROP" not in q
    assert "Bad Type" not in q
    # 정상만 통과
    assert "MERGE (n0:Epic" in q
    assert ":OK_REL]->" in q


def test_build_save_prd_query_drops_blocked_top_level_labels():
    """PRD JSON 에 Project / User 라벨 노드가 섞여도 graph save 에서 drop —
    ownership_repository / user_repository 의 책임 영역과 충돌하지 않게."""
    q, params = build_save_prd_query(
        {
            "nodes": [
                {"id": "proj_x", "label": "Project", "properties": {"name": "x"}},
                {"id": "user_alice", "label": "User", "properties": {"email": "a@x"}},
                {"id": "doc_prd_x", "label": "PRD_Document", "properties": {"full_markdown": "# x"}},
                {"id": "epic_01", "label": "Epic", "properties": {"summary": "auth"}},
            ],
            "relationships": [
                # blocked 노드를 참조하는 관계는 함께 drop
                {"source": "proj_x", "target": "doc_prd_x", "type": "HAS_PRD"},
                {"source": "user_alice", "target": "doc_prd_x", "type": "AUTHORED"},
                # 정상 관계는 보존
                {"source": "doc_prd_x", "target": "epic_01", "type": "EXTRACTED_FROM"},
            ],
        }
    )
    assert "MERGE (n0:PRD_Document" in q
    assert "MERGE (n1:Epic" in q
    # blocked 라벨은 cypher 본문에 없음
    assert ":Project" not in q
    assert ":User " not in q
    # params 에도 drop 된 id 없음
    all_node_ids = {
        item["id"] for k, v in params.items() if k.startswith("n_") for item in v
    }
    assert "proj_x" not in all_node_ids
    assert "user_alice" not in all_node_ids
    assert "doc_prd_x" in all_node_ids
    assert "epic_01" in all_node_ids
    # blocked 노드 참조 관계도 drop
    assert ":HAS_PRD" not in q
    assert ":AUTHORED" not in q
    assert ":EXTRACTED_FROM]->" in q


def test_build_save_prd_query_uses_parameter_binding_for_values():
    """모든 사용자 입력값(id/properties)이 $param 바인딩으로만 전달되는지 — 인터폴 0."""
    q, params = build_save_prd_query(
        {
            "nodes": [
                {
                    "id": "epic_inject'; DELETE //",
                    "label": "Epic",
                    "properties": {
                        "summary": "'; DROP DATABASE; --",
                        "nested": {"a": "'; --"},
                    },
                },
            ],
            "relationships": [],
        }
    )
    # 위험 문자열은 cypher 본문에 없고 params 안에만 (driver 가 안전 escape).
    assert "DROP DATABASE" not in q
    assert "DELETE //" not in q
    # 그러나 params 의 값 안엔 그대로 (driver 가 처리)
    payloads = [item for k, v in params.items() if k.startswith("n_") for item in v]
    assert any("DROP DATABASE" in p["props"].get("summary", "") for p in payloads)
