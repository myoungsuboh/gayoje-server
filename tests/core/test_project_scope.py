"""
project_scope — 도메인 노드 멀티테넌시 스코프 키 회귀 가드.

[핵심 불변식]
- 개인(team_id 없음): 스코프 키 = 이름 그대로 (기존 데이터/마이그레이션 불필요).
- 팀: sentinel 합성으로 동명 개인/타팀 프로젝트와 격리.
- round-trip: unscope(scoped(name, tid)) == name.
- 위조 차단: 이름에 sentinel 포함 시 400.
"""
import pytest
from fastapi import HTTPException

from app.core.project_scope import (
    SCOPE_SENTINEL,
    assert_safe_project_name,
    cps_delta_id,
    cps_master_id,
    is_scoped,
    meeting_log_id,
    prd_delta_id,
    prd_master_id,
    scope_graph,
    scoped_project,
    team_id_of,
    unscope_project,
)


# ─── 개인 (team_id 없음) — 기존 동작 보존 ──────────────────

def test_personal_key_is_raw_name():
    assert scoped_project("my-app", None) == "my-app"
    assert scoped_project("my-app", "") == "my-app"
    assert is_scoped("my-app") is False
    assert unscope_project("my-app") == "my-app"
    assert team_id_of("my-app") is None


# ─── 팀 — sentinel 합성 ─────────────────────────────────────

def test_team_key_embeds_team_id():
    key = scoped_project("my-app", "team-123")
    assert key == f"{SCOPE_SENTINEL}team-123{SCOPE_SENTINEL}my-app"
    assert is_scoped(key) is True
    assert team_id_of(key) == "team-123"
    assert unscope_project(key) == "my-app"


def test_team_and_personal_same_name_do_not_collide():
    personal = scoped_project("shop", None)
    team = scoped_project("shop", "t-9")
    assert personal != team
    assert unscope_project(personal) == unscope_project(team) == "shop"


def test_name_with_inner_colons_round_trips():
    # 이름에 ':' 가 있어도 (sentinel '::team::' 전체가 아니면) 정상.
    key = scoped_project("a:b:c", "t-1")
    assert unscope_project(key) == "a:b:c"


# ─── 멱등성 (이미 스코프된 키 통과) ────────────────────────

def test_scoped_project_is_idempotent_on_scoped_key():
    # 이미 스코프된 키가 들어오면 그대로 반환 (내부 round-trip 안전, 이중스코프 X).
    key = scoped_project("secret", "t-1")
    assert scoped_project(key, None) == key
    assert scoped_project(key, "t-1") == key
    assert scoped_project(key, "other") == key  # 재스코프 안 함


# ─── 위조 차단은 claim 시점(assert_safe_project_name) ──────

def test_assert_safe_passes_normal_names():
    # 정상 이름은 통과 (예외 없음).
    for n in ["my-app", "a:b", "프로젝트1", "team_x", "", None]:
        assert_safe_project_name(n)


def test_assert_safe_rejects_sentinel_anywhere():
    with pytest.raises(HTTPException):
        assert_safe_project_name(f"pre{SCOPE_SENTINEL}post")


# ─── 방어적 동작 ────────────────────────────────────────────

def test_unscope_handles_empty_and_malformed():
    assert unscope_project("") == ""
    assert unscope_project(None) == ""
    # sentinel 1개만 (형식 불일치) — 방어적으로 원본 반환.
    malformed = f"{SCOPE_SENTINEL}only-one"
    assert unscope_project(malformed) == malformed
    assert team_id_of(malformed) is None


# ─── ID 빌더 — 개인 기존 형식 보존 ──────────────────────────

def test_id_builders_personal_preserve_existing_format():
    # 개인(스코프 키 = 이름)일 때 기존 id 형식과 동일해야 함 (마이그레이션 불필요).
    assert cps_master_id("myapp") == "doc_cps_master_myapp"
    assert prd_master_id("myapp") == "doc_prd_master_myapp"
    assert cps_delta_id("myapp", "1.0") == "doc_cps_myapp_1_0"
    assert prd_delta_id("myapp", "1.0") == "doc_prd_myapp_1_0"
    assert meeting_log_id("myapp", "1.0") == "log_myapp_1_0"


def test_id_builders_normalize_dots_everywhere():
    # 점 포함 이름: 모든 id 가 normalize (LLM ID_NORMALIZATION / 기존 저장 데이터와 일치).
    assert cps_master_id("my.app") == "doc_cps_master_my_app"
    assert cps_delta_id("my.app", "v1.2") == "doc_cps_my_app_v1_2"
    assert meeting_log_id("my.app", "v1.2") == "log_my_app_v1_2"


def test_id_builders_team_scope_distinct_from_personal():
    personal_key = scoped_project("myapp", None)
    team_key = scoped_project("myapp", "t-1")
    assert cps_master_id(personal_key) != cps_master_id(team_key)
    assert cps_delta_id(personal_key, "1") != cps_delta_id(team_key, "1")
    # 팀 키가 id 에 그대로 반영돼 격리.
    assert "t-1" in cps_delta_id(team_key, "1")


# ─── scope_graph 변환 ───────────────────────────────────────

def _sample_graph():
    return {
        "nodes": [
            {"id": "doc_cps_myapp_v1", "label": "CPS_Document",
             "properties": {"project": "myapp", "full_markdown": "# hi"}},
            {"id": "prb_01", "label": "Problem", "properties": {"summary": "p"}},
            {"id": "res_01", "label": "Solution", "properties": {"summary": "s", "project": "myapp"}},
        ],
        "relationships": [
            {"source": "prb_01", "type": "EXTRACTED_FROM", "target": "doc_cps_myapp_v1"},
            {"source": "res_01", "type": "SOLVES", "target": "prb_01"},
            {"source": "doc_cps_myapp_v1", "type": "SUPERSEDES", "target": "doc_cps_prev_v0"},
        ],
    }


def test_scope_graph_personal_is_identity():
    # 개인: project_key=이름, new_doc_id=기존 id → 무변환.
    g = _sample_graph()
    out = scope_graph(g, project_key="myapp", doc_label="CPS_Document",
                      new_doc_id="doc_cps_myapp_v1")
    doc = next(n for n in out["nodes"] if n["label"] == "CPS_Document")
    assert doc["id"] == "doc_cps_myapp_v1"
    for n in out["nodes"]:
        assert n["properties"]["project"] == "myapp"


def test_scope_graph_team_remaps_doc_id_and_project():
    g = _sample_graph()
    key = scoped_project("myapp", "t-9")
    new_id = cps_delta_id(key, "v1")
    out = scope_graph(g, project_key=key, doc_label="CPS_Document", new_doc_id=new_id)

    # 모든 노드 project = 스코프 키.
    for n in out["nodes"]:
        assert n["properties"]["project"] == key

    # doc 노드 id 재조정.
    doc = next(n for n in out["nodes"] if n["label"] == "CPS_Document")
    assert doc["id"] == new_id

    # doc id 를 가리키던 관계 endpoint 재작성.
    extracted = next(r for r in out["relationships"] if r["type"] == "EXTRACTED_FROM")
    assert extracted["target"] == new_id
    assert extracted["source"] == "prb_01"  # 비-doc id 는 그대로

    # SUPERSEDES 의 previous id(다른 delta)는 건드리지 않음.
    supersedes = next(r for r in out["relationships"] if r["type"] == "SUPERSEDES")
    assert supersedes["target"] == "doc_cps_prev_v0"
    assert supersedes["source"] == new_id

    # Problem(prb_01) id 는 유지 (기존 동작).
    assert any(n["id"] == "prb_01" for n in out["nodes"])


def test_scope_graph_handles_empty():
    # nodes 키 없음 → 그대로 반환.
    assert scope_graph({}, project_key="k", doc_label="CPS_Document", new_doc_id="x") == {}
    # nodes=[] → 빈 노드/관계 (relationships 정규화는 무해).
    out = scope_graph({"nodes": []}, project_key="k", doc_label="CPS_Document", new_doc_id="x")
    assert out["nodes"] == []
    assert out.get("relationships", []) == []
