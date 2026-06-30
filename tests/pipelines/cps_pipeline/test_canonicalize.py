"""
canonicalize_meeting_content / canonicalize_graph 단위 테스트.

LLM/Neo4j 없는 순수 함수 — 가장 빠르고 안전. LLM 비결정성 완화 정칅의
'결정성을 LLM 밖에서 확보' 하는 토대 레이어 — 그래프 적재 전/후에 일관되게 적용되어야 함.
"""
from __future__ import annotations

from app.pipelines.base import canonicalize_graph, canonicalize_meeting_content


# ─── canonicalize_meeting_content ──────────────────────────────────────


def test_normalizes_crlf_to_lf():
    assert canonicalize_meeting_content("a\r\nb\r\nc") == "a\nb\nc"


def test_normalizes_old_mac_cr_to_lf():
    assert canonicalize_meeting_content("a\rb\rc") == "a\nb\nc"


def test_strips_trailing_whitespace_per_line():
    assert canonicalize_meeting_content("a   \nb\t\nc") == "a\nb\nc"


def test_preserves_leading_indent():
    """leading 공백은 의도적 들여쓰기일 수 있어 보존."""
    inp = "  a\n\tb"
    assert canonicalize_meeting_content(inp) == "  a\n\tb"


def test_collapses_three_or_more_blank_lines():
    inp = "a\n\n\n\n\nb"
    assert canonicalize_meeting_content(inp) == "a\n\nb"


def test_strips_leading_trailing_blank_lines():
    """앞뒤 빈줄만 제거, leading 공백은 보존."""
    inp = "\n\n  a\n\n"
    assert canonicalize_meeting_content(inp) == "  a"


def test_nfd_hangeul_normalizes_to_nfc():
    """macOS 파일명 복사 시 한글이 NFD(조합형)일 수 있음."""
    nfd = "한"  # NFD: ㅎ + ㅏ + ㄴ
    nfc = "한"  # NFC: 한
    assert canonicalize_meeting_content(nfd) == nfc


def test_empty_or_none_input_returns_empty():
    assert canonicalize_meeting_content("") == ""
    assert canonicalize_meeting_content(None) == ""


def test_non_string_input_coerced():
    assert canonicalize_meeting_content(123) == "123"


# ─── canonicalize_graph ─────────────────────────────────────────────


def test_sorts_nodes_by_label_then_id():
    # canonicalize_graph 는 id 기준으로 중복 제거 — 서로 다른 label 이어도
    # id 가 같으면 먼저 등장한 것만 유지된다. 정렬 동작만 검증하려면
    # 모든 노드의 id 가 유일해야 한다.
    g = {
        "nodes": [
            {"id": "b", "label": "Problem"},
            {"id": "c", "label": "Solution"},
            {"id": "a", "label": "Problem"},
        ],
        "relationships": [],
    }
    out = canonicalize_graph(g)
    ids = [(n["label"], n["id"]) for n in out["nodes"]]
    assert ids == [("Problem", "a"), ("Problem", "b"), ("Solution", "c")]


def test_dedupes_nodes_by_id_keeping_first():
    g = {
        "nodes": [
            {"id": "a", "label": "Problem", "properties": {"summary": "first"}},
            {"id": "a", "label": "Problem", "properties": {"summary": "second"}},
        ],
        "relationships": [],
    }
    out = canonicalize_graph(g)
    assert len(out["nodes"]) == 1
    assert out["nodes"][0]["properties"]["summary"] == "first"


def test_strips_string_property_values_keeps_others():
    g = {
        "nodes": [{
            "id": "a",
            "label": "Problem",
            "properties": {"summary": "  hello  ", "year": 2026, "active": True},
        }],
        "relationships": [],
    }
    out = canonicalize_graph(g)
    p = out["nodes"][0]["properties"]
    assert p["summary"] == "hello"
    assert p["year"] == 2026
    assert p["active"] is True


def test_sorts_property_keys():
    g = {
        "nodes": [{"id": "a", "label": "P", "properties": {"z": 1, "a": 2, "m": 3}}],
        "relationships": [],
    }
    out = canonicalize_graph(g)
    keys = list(out["nodes"][0]["properties"].keys())
    assert keys == ["a", "m", "z"]


def test_sorts_relationships_by_type_source_target():
    g = {
        "nodes": [],
        "relationships": [
            {"source": "b", "target": "a", "type": "SOLVES"},
            {"source": "a", "target": "b", "type": "SOLVES"},
            {"source": "a", "target": "b", "type": "EXTRACTED_FROM"},
        ],
    }
    out = canonicalize_graph(g)
    triples = [(r["type"], r["source"], r["target"]) for r in out["relationships"]]
    assert triples == [
        ("EXTRACTED_FROM", "a", "b"),
        ("SOLVES", "a", "b"),
        ("SOLVES", "b", "a"),
    ]


def test_drops_invalid_relationships():
    g = {
        "nodes": [],
        "relationships": [
            {"source": "a", "type": "SOLVES"},  # target 없음
            {"target": "a", "type": "SOLVES"},  # source 없음
            {"source": "a", "target": "b"},  # type 없음
            {"source": "a", "target": "b", "type": "OK"},
        ],
    }
    out = canonicalize_graph(g)
    assert len(out["relationships"]) == 1
    assert out["relationships"][0]["type"] == "OK"


def test_preserves_top_level_metadata():
    g = {
        "nodes": [],
        "relationships": [],
        "_harness_metadata": {"state": "done", "verification_passed": True},
    }
    out = canonicalize_graph(g)
    assert out["_harness_metadata"] == {"state": "done", "verification_passed": True}


def test_none_returns_empty_dict():
    assert canonicalize_graph(None) == {}


def test_node_order_shuffle_produces_same_output():
    """멱등성 기초 — 입력 순서가 달라도 결과 동일."""
    g1 = {
        "nodes": [
            {"id": "b", "label": "Problem"},
            {"id": "a", "label": "Solution"},
        ],
        "relationships": [],
    }
    g2 = {
        "nodes": [
            {"id": "a", "label": "Solution"},
            {"id": "b", "label": "Problem"},
        ],
        "relationships": [],
    }
    assert canonicalize_graph(g1) == canonicalize_graph(g2)
