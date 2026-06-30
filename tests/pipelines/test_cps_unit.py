"""
CPS 파이프라인 - 결정적 부분 단위 테스트.

외부 호출 없이 순수 함수만 검증한다:
  - Cypher 빌더 (Save Meeting Log / Save CPS / Merge Master)
  - JSON 추출, code-fence 제거
  - 섹션 분할 / affected section 매칭 / 재조립

회귀 추적: CPS 파이프라인 각 단계의 동작을 골든 fixture 로 비교.
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import (
    escape_cypher_string,
    extract_json_object,
    format_props,
    strip_code_blocks,
)
from app.pipelines.cps_pipeline import (
    CpsInput,
    build_merge_master_query,
    build_save_cps_query,
    build_save_meeting_log_query,
    filter_affected_sections,
    reassemble_master,
    split_master_sections,
)


# ─── 유틸 ───────────────────────────────────────────────────────


def test_escape_cypher_string_handles_quote_newline_backslash():
    assert escape_cypher_string("a'b\nc\\d") == "a\\'b\\nc\\\\d"
    assert escape_cypher_string(None) == ""
    assert escape_cypher_string(123) == "123"


def test_strip_code_blocks_removes_fences():
    assert strip_code_blocks("```json\n{\"a\":1}\n```") == '{"a":1}'
    assert strip_code_blocks("```markdown\n# hi\n```") == "# hi"
    assert strip_code_blocks("plain") == "plain"
    assert strip_code_blocks("") == ""
    assert strip_code_blocks(None) == ""  # type: ignore[arg-type]


def test_extract_json_object_finds_first_brace_block():
    raw = "garbage```json\n{\"x\": 1, \"y\": [1,2]}\n```extra"
    assert extract_json_object(raw) == {"x": 1, "y": [1, 2]}
    assert extract_json_object("no braces here") == {}
    assert extract_json_object("{broken json") == {}


def test_format_props_types():
    out = format_props({"a": "x'y", "b": 1, "c": True, "d": [1, "s"], "e": None})
    # 기본 형식: { a: 'x\'y', b: 1, c: true, d: [1, 's'], e: '' }
    assert "a: 'x\\'y'" in out
    assert "b: 1" in out
    assert "c: true" in out
    assert "d: [1, 's']" in out
    assert "e: ''" in out


# ─── Cypher 빌더 ──────────────────────────────────────────────


def test_build_save_meeting_log_query_uses_derived_id():
    payload = CpsInput(
        project_name="harness",
        version="v1.2",
        date="2026-05-12",
        meeting_content="It's a test\nwith newline",
        previous_cps_id=None,
    )
    q, params = build_save_meeting_log_query(payload)
    # Cypher 본문은 $param 바인딩만 사용 — 문자열 인터폴 없음
    assert "MERGE (log:Meeting_Log {id: $log_id})" in q
    assert "log.project = $project" in q
    assert "log.version = $version" in q
    assert "log.raw_content = $raw_content" in q
    assert "MATCH (doc:CPS_Document {id: $target_cps_id})" in q
    assert q.endswith("MERGE (doc)-[:EXTRACTED_FROM]->(log)")
    # 값은 params 로 — driver 가 escape 책임. 원본 그대로 전달되는지 검증
    assert params == {
        "log_id": "log_harness_v1_2",
        "project": "harness",
        "version": "v1.2",
        "date": "2026-05-12",
        "raw_content": "It's a test\nwith newline",
        "target_cps_id": "doc_cps_harness_v1_2",
    }


def test_build_save_meeting_log_query_respects_previous_cps_id():
    payload = CpsInput(
        project_name="harness",
        version="v1.3",
        date="2026-05-12",
        meeting_content="hi",
        previous_cps_id="doc_cps_master_harness",
    )
    q, params = build_save_meeting_log_query(payload)
    assert "MATCH (doc:CPS_Document {id: $target_cps_id})" in q
    assert params["target_cps_id"] == "doc_cps_master_harness"


def test_build_save_cps_query_emits_nodes_then_rels_with_with_count_blocks():
    graph = {
        "nodes": [
            {
                "id": "doc_cps_harness_v1_1",
                "label": "CPS_Document",
                "properties": {"project": "harness", "is_latest": True},
            },
            {"id": "prb_01", "label": "Problem", "properties": {"summary": "X"}},
            {"id": "res_01", "label": "Solution", "properties": {"summary": "Y"}},
        ],
        "relationships": [
            {"source": "prb_01", "type": "EXTRACTED_FROM", "target": "doc_cps_harness_v1_1"},
            {"source": "res_01", "type": "SOLVES", "target": "prb_01"},
        ],
    }
    q, params = build_save_cps_query(graph)
    # 노드 3개 라벨 → 3 블록 + 관계 2 type → 2 블록 = UNWIND 5회
    assert q.count("UNWIND") == 5
    # label 만 cypher 식별자 자리 (화이트리스트 통과 후 인터폴)
    assert "MERGE (n0:CPS_Document" in q
    assert "MERGE (n1:Problem" in q
    assert "MERGE (n2:Solution" in q
    # 2번째 블록부터 WITH count(*) 격리 (총 4번 — 5블록 사이 4개)
    assert q.count("WITH count(*) AS _dummy") == 4
    # 관계 블록: 라벨 없는 MATCH 패턴 + 화이트리스트 통과한 rtype 인터폴
    assert "MATCH (s3 {id: rItem3.source})" in q
    assert "MATCH (t3 {id: rItem3.target})" in q
    assert "MATCH (s4 {id: rItem4.source})" in q
    # 값/id 는 모두 $param 바인딩 — 인터폴 0건
    assert "$n_0" in q
    assert "$n_1" in q
    assert "$n_2" in q
    assert "$r_3" in q
    assert "$r_4" in q
    # params dict 검증: 각 블록별 list 가 그대로 들어감
    assert len(params["n_0"]) == 1
    assert params["n_0"][0]["id"] == "doc_cps_harness_v1_1"
    # is_latest=True 는 bool 그대로 (str 변환 없음)
    assert params["n_0"][0]["props"]["is_latest"] is True
    assert params["n_0"][0]["props"]["project"] == "harness"
    assert params["n_1"][0]["id"] == "prb_01"
    assert params["n_1"][0]["props"]["summary"] == "X"
    assert params["n_2"][0]["id"] == "res_01"
    # 관계 params: source/target 만 들어가고 props 는 빈 dict
    assert params["r_3"] == [
        {
            "source": "prb_01",
            "target": "doc_cps_harness_v1_1",
            "props": {},
        }
    ]
    assert params["r_4"] == [
        {"source": "res_01", "target": "prb_01", "props": {}}
    ]


def test_build_save_cps_query_scopes_node_and_rel_merge_when_project_given():
    """[멀티테넌시] project_name 제공 시 spec 노드 MERGE/관계 MATCH 가 project 로 스코프.

    prb_01 같은 project-prefix 없는 id 의 cross-project 충돌 차단 — id 만으로 MERGE
    하던 회귀를 막는다.
    """
    graph = {
        "nodes": [
            {"id": "prb_01", "label": "Problem", "properties": {"summary": "X"}},
            {"id": "res_01", "label": "Solution", "properties": {"summary": "Y"}},
        ],
        "relationships": [
            {"source": "res_01", "type": "SOLVES", "target": "prb_01"},
        ],
    }
    q, params = build_save_cps_query(graph, project_name="acme")
    # 노드 MERGE 가 {id, project} 복합 키
    assert "MERGE (n0:Problem {id: nItem0.id, project: $save_project})" in q
    assert "MERGE (n1:Solution {id: nItem1.id, project: $save_project})" in q
    # 관계 endpoint MATCH 도 project 한정
    assert "MATCH (s2 {id: rItem2.source, project: $save_project})" in q
    assert "MATCH (t2 {id: rItem2.target, project: $save_project})" in q
    # save_project 파라미터 = 전달된 스코프 키
    assert params["save_project"] == "acme"
    # _ensure_project_on_nodes 로 노드 props.project 도 채워짐
    assert params["n_0"][0]["props"]["project"] == "acme"
    # id-only 레거시 패턴은 등장하지 않음 (회귀 가드)
    assert "MERGE (n0:Problem {id: nItem0.id})" not in q
    assert "MATCH (s2 {id: rItem2.source})" not in q


def test_build_save_cps_query_legacy_id_only_when_no_project():
    """project_name 미지정(레거시/테스트) 경로는 기존 id-only 동작을 byte-identical 보존."""
    graph = {
        "nodes": [{"id": "prb_01", "label": "Problem", "properties": {"summary": "X"}}],
        "relationships": [
            {"source": "prb_01", "type": "EXTRACTED_FROM", "target": "prb_01"},
        ],
    }
    q, params = build_save_cps_query(graph)  # project_name 없음
    assert "MERGE (n0:Problem {id: nItem0.id})" in q
    assert "project: $save_project" not in q
    assert "save_project" not in params


def test_build_save_cps_query_skips_invalid_entries():
    q, params = build_save_cps_query(
        {
            "nodes": [
                {"label": "X", "properties": {}},  # id 없음 → skip
                {"id": "ok", "label": "X", "properties": {}},
            ],
            "relationships": [
                {"source": "ok", "type": "REL"},  # target 없음 → skip
                {"source": "ok", "target": "ok", "type": "REL"},
            ],
        }
    )
    assert "MERGE (n0:X" in q
    assert ":REL]->" in q
    # 유효 1개씩만 살아남음
    assert len(params["n_0"]) == 1
    assert params["n_0"][0]["id"] == "ok"
    assert len(params["r_1"]) == 1
    assert params["r_1"][0]["source"] == "ok"


def test_build_save_cps_query_rejects_unsafe_label_and_type():
    """LLM 이 라벨/관계타입에 인젝션 시도해도 화이트리스트가 차단."""
    q, params = build_save_cps_query(
        {
            "nodes": [
                {"id": "a", "label": "Good", "properties": {}},
                # 공백 + 특수문자 → silently drop
                {"id": "b", "label": "Bad Label", "properties": {}},
                {"id": "c", "label": "Bad)Drop", "properties": {}},
                # SQL/Cypher injection 시도 라벨
                {"id": "d", "label": "X`)//", "properties": {}},
            ],
            "relationships": [
                {"source": "a", "target": "a", "type": "OK_REL"},
                {"source": "a", "target": "a", "type": "Bad Type"},
                {"source": "a", "target": "a", "type": "Bad);DROP"},
            ],
        }
    )
    # 위험 항목은 cypher 본문에 절대 나타나지 않아야 함
    assert "Bad Label" not in q
    assert "Bad)Drop" not in q
    assert "DROP" not in q
    assert "Bad Type" not in q
    # 안전 항목만 통과
    assert "MERGE (n0:Good" in q
    assert ":OK_REL]->" in q


def test_build_save_cps_query_drops_blocked_top_level_labels():
    """Project / User 같은 top-level 엔티티는 ownership_repository / user_repository 가 관리.
    LLM 이 같은 라벨 노드를 새 id 로 출력해도 graph save 에서 drop — 그러지 않으면
    MERGE (id) → SET name=... 단계에서 `Project.name` UNIQUE 제약 충돌로 transaction
    전체 롤백 (실제 운영 incident: postMeeting 502 → meeting log 등록 실패)."""
    q, params = build_save_cps_query(
        {
            "nodes": [
                {"id": "proj_food", "label": "Project", "properties": {"name": "food"}},
                {"id": "user_a", "label": "User", "properties": {"email": "a@x"}},
                {"id": "epic_01", "label": "Epic", "properties": {"summary": "E"}},
                {"id": "story_01", "label": "Story", "properties": {"summary": "S"}},
            ],
            "relationships": [
                # blocked-label 노드를 참조하는 관계는 함께 drop (orphan 방지)
                {"source": "proj_food", "target": "epic_01", "type": "HAS_EPIC"},
                {"source": "user_a", "target": "epic_01", "type": "AUTHORED"},
                # 정상 노드 간 관계는 보존
                {"source": "epic_01", "target": "story_01", "type": "CONTAINS"},
            ],
        }
    )
    # 정상 라벨은 보존
    assert "MERGE (n0:Epic" in q
    assert "MERGE (n1:Story" in q
    # blocked 라벨은 cypher 본문에 없어야 함 (MERGE 절도, 주석도)
    assert ":Project" not in q
    assert ":User " not in q
    # params 에도 drop 된 id 없음
    all_node_ids = {
        item["id"] for k, v in params.items() if k.startswith("n_") for item in v
    }
    assert "proj_food" not in all_node_ids
    assert "user_a" not in all_node_ids
    assert "epic_01" in all_node_ids
    assert "story_01" in all_node_ids
    # blocked 노드를 참조하는 관계도 drop
    assert ":HAS_EPIC" not in q
    assert ":AUTHORED" not in q
    # 정상 관계는 보존
    assert ":CONTAINS]->" in q


def test_build_save_cps_query_empty_graph_returns_empty():
    """유효 노드/관계 0건 → 빈 cypher + 빈 params. 호출자는 skip 가능."""
    q, params = build_save_cps_query({"nodes": [], "relationships": []})
    assert q == ""
    assert params == {}


def test_build_save_cps_query_sanitizes_nested_props():
    """LLM 이 nested dict/list-of-dict 를 properties 로 줘도 평탄화돼야 함.
    Neo4j 노드 properties 는 primitive + list-of-primitive 만 받음."""
    q, params = build_save_cps_query(
        {
            "nodes": [
                {
                    "id": "x",
                    "label": "X",
                    "properties": {
                        "tags": ["a", "b"],            # list-of-str: 통과
                        "mixed": [1, {"k": "v"}],      # list-of-dict: dict 는 json string
                        "meta": {"nested": True},      # nested dict: json string
                        "none_val": None,              # None: ""
                        "bool_val": False,             # bool: 그대로
                    },
                }
            ],
            "relationships": [],
        }
    )
    props = params["n_0"][0]["props"]
    assert props["tags"] == ["a", "b"]
    assert props["mixed"][0] == 1
    assert json.loads(props["mixed"][1]) == {"k": "v"}
    assert json.loads(props["meta"]) == {"nested": True}
    assert props["none_val"] == ""
    assert props["bool_val"] is False


def test_build_merge_master_query_first_run_has_no_latest_match():
    q, params = build_merge_master_query(
        "harness", "## doc\n- body", latest_delta_id=None
    )
    assert "MERGE (master:CPS_Document {id: $master_id})" in q
    assert "master.full_markdown = $merged_content" in q
    assert "MATCH (latest:" not in q
    assert "SYNTHESIZED_FROM" not in q
    assert params == {
        "master_id": "doc_cps_master_harness",
        "project": "harness",
        "merged_content": "## doc\n- body",
    }
    assert "latest_delta_id" not in params


def test_build_merge_master_query_links_latest_delta_when_present():
    q, params = build_merge_master_query(
        "harness", "x", latest_delta_id="doc_cps_harness_v1_2"
    )
    assert "MATCH (latest:CPS_Document {id: $latest_delta_id})" in q
    assert "MERGE (master)-[:SYNTHESIZED_FROM]->(latest)" in q
    assert "SET latest.is_latest = false" in q
    assert params["latest_delta_id"] == "doc_cps_harness_v1_2"
    assert params["merged_content"] == "x"
    assert params["master_id"] == "doc_cps_master_harness"


# ─── [2026-05-26] 데이터 무결성 가드 — master full_markdown wipe 차단 ───


def test_build_merge_master_query_refuses_empty_content():
    """빈 string → ValueError. master 누적 데이터 보호."""
    import pytest
    with pytest.raises(ValueError, match="비어있음"):
        build_merge_master_query("harness", "", latest_delta_id=None)


def test_build_merge_master_query_refuses_whitespace_only():
    """공백/개행만 있는 content 도 wipe 효과 동일 → 차단."""
    import pytest
    with pytest.raises(ValueError, match="비어있음"):
        build_merge_master_query("harness", "   \n\t  \n", latest_delta_id=None)


# ─── [2026-05-26] LLM 환각 silent skeleton 차단 — spec 노드 빈 필드 거부 ───


def test_build_save_cps_query_drops_spec_node_with_no_fields():
    """LLM 이 Problem/Solution/Epic/Story 등 spec 노드를 id+label 만으로 출력 시 drop.
    AI Agent 프로젝트 CPS_Document 가 properties 비어있던 사고와 동일 잠재 경로."""
    from app.pipelines.cps_pipeline.cypher import build_save_cps_query
    g = {
        "nodes": [
            # 정상 spec 노드 — summary 있음
            {"id": "p1", "label": "Problem", "properties": {"summary": "느림"}},
            # 위험 spec 노드 — properties 자체가 비어있음 (LLM 환각 의심)
            {"id": "p2", "label": "Problem", "properties": {}},
            # 위험 spec 노드 — 핵심 필드 0개 (다른 메타만)
            {"id": "p3", "label": "Solution", "properties": {"_meta": "x"}},
            # 정상 — name 으로 식별
            {"id": "s1", "label": "Screen", "properties": {"name": "로그인 화면"}},
            # spec 외 라벨 — 가드 영향 없음
            {"id": "d1", "label": "CPS_Document", "properties": {"full_markdown": "# x"}},
        ],
        "relationships": [],
    }
    cypher, params = build_save_cps_query(g)
    # 정상 노드 (p1, s1, d1) 만 cypher 에 포함
    all_node_ids = []
    for v in params.values():
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict) and "id" in item:
                    all_node_ids.append(item["id"])
    assert "p1" in all_node_ids
    assert "s1" in all_node_ids
    assert "d1" in all_node_ids
    # 빈 spec 노드는 drop
    assert "p2" not in all_node_ids, "빈 properties Problem 노드가 drop 안 됨"
    assert "p3" not in all_node_ids, "핵심 필드 없는 Solution 노드가 drop 안 됨"


def test_build_save_cps_query_accepts_meaningful_spec_fields():
    """summary 외 name/description/title/body/content 도 의미 있는 필드로 인정."""
    from app.pipelines.cps_pipeline.cypher import build_save_cps_query
    g = {
        "nodes": [
            {"id": "e1", "label": "Epic", "properties": {"title": "사용자 인증"}},
            {"id": "st1", "label": "Story", "properties": {"description": "OAuth 로그인"}},
            {"id": "n1", "label": "NFR", "properties": {"content": "응답 500ms"}},
        ],
        "relationships": [],
    }
    cypher, params = build_save_cps_query(g)
    all_node_ids = []
    for v in params.values():
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict) and "id" in item:
                    all_node_ids.append(item["id"])
    assert "e1" in all_node_ids
    assert "st1" in all_node_ids
    assert "n1" in all_node_ids


# ─── 섹션 분할 / 재조립 ─────────────────────────────────────


_SAMPLE_MASTER = """\
## 📄 CPS 명세서: harness (v1.2)

### 1. Context (배경 및 상황)
- **비즈니스 환경**: alpha
- **도입 배경**: beta

### 2. Problem (핵심 문제)
- **[PRB-01] A**: a-detail
- **[PRB-02] B**: b-detail

### 3. Solution (최종 해결책 및 기획 방향)
- **목표 시스템 모델**: model
- **핵심 기능 명세**:
  - `[RES-01] f1`: [매핑: PRB-01 / x]

### 4. Pending & Action Items
- **미결정 사항**:
  - q1
- **Next Steps**:
  - [ ] `alice`: do it
"""


def test_split_master_sections_keeps_header_and_order():
    smap, order = split_master_sections(_SAMPLE_MASTER)
    assert order[0] == "__header__"
    keys = [k for k in order if k != "__header__"]
    # 섹션 분할 regex: '1. ' prefix 는 capture group 안에서 제거되지 않으므로
    # key 가 `1. Context (배경 및 상황)` 형태가 아니라 `Context (배경 및 상황)` 인지 확인.
    assert "Context (배경 및 상황)" in keys
    assert "Problem (핵심 문제)" in keys


def test_filter_affected_sections_first_run_when_no_master():
    out = filter_affected_sections(
        master_content="",
        latest_content="some delta",
        impact={"affected_sections": ["Problem"], "removed_prb_ids": [], "removed_res_ids": []},
    )
    assert out["is_first_run"] is True
    assert out["affected_sections_content"] == ""
    assert out["_diagnostic"]["mode"] == "FIRST_RUN"


def test_filter_affected_sections_matches_problem_only():
    impact = {
        "affected_sections": ["Problem"],
        "removed_prb_ids": [],
        "removed_res_ids": [],
        "analysis": "x",
    }
    out = filter_affected_sections(
        master_content=_SAMPLE_MASTER,
        latest_content="delta",
        impact=impact,
    )
    assert not out["is_first_run"]
    assert any(k.lower().startswith("problem") for k in out["affected_section_keys"])
    assert "[PRB-01]" in out["affected_sections_content"]
    assert "[RES-01]" not in out["affected_sections_content"]


def test_filter_affected_sections_falls_back_to_problem_solution():
    out = filter_affected_sections(
        master_content=_SAMPLE_MASTER,
        latest_content="x" * 60,  # > 50 chars → candidate 비어있어도 default 적용
        impact={"affected_sections": [], "removed_prb_ids": [], "removed_res_ids": []},
    )
    keys_lower = [k.lower() for k in out["affected_section_keys"]]
    assert any("problem" in k for k in keys_lower)
    assert any("solution" in k for k in keys_lower)


def test_reassemble_master_first_run_passthrough():
    filter_data = {
        "is_first_run": True,
        "section_order": [],
        "full_section_map": {},
        "affected_section_keys": [],
    }
    out = reassemble_master(filter_data, "## new content")
    assert out["merged_content"] == "## new content"
    assert out["_diagnostic"]["mode"] == "FIRST_RUN_PASSTHROUGH"


def test_reassemble_master_replaces_only_affected_sections():
    smap, order = split_master_sections(_SAMPLE_MASTER)
    affected_problem_key = next(k for k in order if k.lower().startswith("problem"))
    filter_data = {
        "is_first_run": False,
        "section_order": order,
        "full_section_map": smap,
        "affected_section_keys": [affected_problem_key],
    }
    agent_output = """\
### 2. Problem (핵심 문제)
- **[PRB-01] A**: a-detail
- **[PRB-02] B**: b-detail
- **[PRB-03] NEW**: new-detail
"""
    out = reassemble_master(filter_data, agent_output)
    merged = out["merged_content"]
    # Problem 섹션은 agent 결과로 대체 → PRB-03 포함
    assert "[PRB-03] NEW" in merged
    # 다른 섹션은 보존
    assert "[RES-01]" in merged
    assert "Next Steps" in merged
    assert out["_diagnostic"]["replaced_count"] == 1
    assert out["_diagnostic"]["preserved_count"] >= 3  # context, solution, pending + header


# ─── [b 2026-05-27] 빈 본문 Document 덮어쓰기 방지 ──────────────────
def test_save_cps_query_strips_empty_full_markdown_preserves_existing():
    """CPS_Document full_markdown 이 빈/whitespace 면 props 에서 제거 — MERGE SET 시
    기존 본문을 빈 값으로 덮어써 손상시키는 것 방지. 다른 prop 은 유지."""
    g = {
        "nodes": [
            {"id": "doc_cps_x_v1", "label": "CPS_Document",
             "properties": {"full_markdown": "   ", "project": "x"}},
        ],
        "relationships": [],
    }
    _, params = build_save_cps_query(g, project_name="x")
    props = params["n_0"][0]["props"]
    assert "full_markdown" not in props  # 빈값 → 제거 (기존 본문 덮어쓰기 방지)
    assert props.get("project") == "x"


def test_save_cps_query_keeps_nonempty_full_markdown():
    """정상 full_markdown 은 그대로 저장."""
    g = {
        "nodes": [
            {"id": "doc_cps_x_v1", "label": "CPS_Document",
             "properties": {"full_markdown": "# 실제 본문"}},
        ],
        "relationships": [],
    }
    _, params = build_save_cps_query(g, project_name="x")
    assert params["n_0"][0]["props"]["full_markdown"] == "# 실제 본문"
