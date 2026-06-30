"""
canonicalize_meeting_content / canonicalize_graph 단위 테스트.

[정책 검증]
- 의미 동일 입력은 byte 동일 결과
- 의미 다른 입력은 다른 결과 (의도된 차이 보존)
- 위험한 변환 (lowercase, leading whitespace 제거) 은 하지 않음
"""
from __future__ import annotations

from app.pipelines.base import canonicalize_graph, canonicalize_meeting_content


# ─── canonicalize_meeting_content ──────────────────────────────────


class TestCanonicalizeMeetingContent:
    def test_empty_returns_empty(self):
        assert canonicalize_meeting_content("") == ""
        assert canonicalize_meeting_content(None) == ""

    def test_non_string_coerced(self):
        # 사용자가 실수로 dict / int 보낼 케이스 — str() 변환
        assert canonicalize_meeting_content(123) == "123"

    def test_line_endings_unified(self):
        # \r\n (Windows), \r (mac legacy), \n 모두 \n 으로
        crlf = "line1\r\nline2\r\nline3"
        cr = "line1\rline2\rline3"
        lf = "line1\nline2\nline3"
        assert canonicalize_meeting_content(crlf) == lf
        assert canonicalize_meeting_content(cr) == lf

    def test_trailing_whitespace_stripped(self):
        # 각 줄 끝 공백/탭 제거
        src = "line1   \nline2\t\t\nline3"
        assert canonicalize_meeting_content(src) == "line1\nline2\nline3"

    def test_leading_whitespace_preserved(self):
        # 들여쓰기는 의미 있을 수 있어 보존
        src = "    line1\n        line2"
        assert canonicalize_meeting_content(src) == "    line1\n        line2"

    def test_multiple_blank_lines_collapsed(self):
        # 3개 이상 연속 빈줄 → 2개
        src = "para1\n\n\n\n\npara2"
        assert canonicalize_meeting_content(src) == "para1\n\npara2"

    def test_outer_whitespace_stripped(self):
        # 전체 strip
        src = "\n\n  \nactual content\n  \n\n"
        assert canonicalize_meeting_content(src) == "actual content"

    def test_nfc_normalization(self):
        # macOS 한글 NFD (분해형) vs NFC (완성형) — 시각적 동일하지만 byte 다름
        # "한" 글자: NFC (U+D55C 1 char) vs NFD (U+1112 + U+1161 + U+11AB 3 chars)
        nfd = "한"  # NFD 한
        nfc = "한"               # NFC 한
        assert canonicalize_meeting_content(nfd) == nfc
        assert canonicalize_meeting_content(nfc) == nfc

    def test_idempotent(self):
        # canonicalize(canonicalize(x)) == canonicalize(x)
        raw = "line1\r\n  \nline2\r\n\n\n\nline3   "
        once = canonicalize_meeting_content(raw)
        twice = canonicalize_meeting_content(once)
        assert once == twice

    def test_meaning_preserved(self):
        # 의미 변경하는 변환은 하지 않음 — case, content 그대로
        src = "Login API: POST /auth/login Returns 200 with TOKEN"
        out = canonicalize_meeting_content(src)
        assert "Login" in out  # 대문자 유지
        assert "TOKEN" in out
        assert "POST /auth/login" in out


# ─── canonicalize_graph ────────────────────────────────────────────


class TestCanonicalizeGraph:
    def test_empty_returns_empty_dict(self):
        assert canonicalize_graph(None) == {}
        assert canonicalize_graph({}) == {}

    def test_nodes_sorted_by_label_then_id(self):
        graph = {
            "nodes": [
                {"id": "prb_02", "label": "Problem", "properties": {}},
                {"id": "doc_1", "label": "CPS_Document", "properties": {}},
                {"id": "prb_01", "label": "Problem", "properties": {}},
                {"id": "res_01", "label": "Solution", "properties": {}},
            ],
            "relationships": [],
        }
        out = canonicalize_graph(graph)
        ids = [n["id"] for n in out["nodes"]]
        # CPS_Document < Problem < Solution (alphabetical)
        # 같은 label 안에서는 id 정렬
        assert ids == ["doc_1", "prb_01", "prb_02", "res_01"]

    def test_relationships_sorted_by_type_then_src_then_tgt(self):
        graph = {
            "nodes": [],
            "relationships": [
                {"source": "res_02", "target": "prb_02", "type": "SOLVES"},
                {"source": "prb_01", "target": "doc_1", "type": "EXTRACTED_FROM"},
                {"source": "res_01", "target": "prb_01", "type": "SOLVES"},
            ],
        }
        out = canonicalize_graph(graph)
        rels = out["relationships"]
        # EXTRACTED_FROM < SOLVES (alphabetical type)
        assert rels[0]["type"] == "EXTRACTED_FROM"
        # 같은 type 안 source 순서
        assert rels[1]["source"] == "res_01"
        assert rels[2]["source"] == "res_02"

    def test_properties_keys_sorted(self):
        graph = {
            "nodes": [
                {"id": "n1", "label": "X", "properties": {"z": 1, "a": 2, "m": 3}},
            ],
            "relationships": [],
        }
        out = canonicalize_graph(graph)
        keys = list(out["nodes"][0]["properties"].keys())
        assert keys == ["a", "m", "z"]

    def test_property_string_values_stripped(self):
        graph = {
            "nodes": [
                {"id": "n1", "label": "X", "properties": {"summary": "  hello  "}},
            ],
            "relationships": [],
        }
        out = canonicalize_graph(graph)
        assert out["nodes"][0]["properties"]["summary"] == "hello"

    def test_duplicate_ids_first_wins(self):
        graph = {
            "nodes": [
                {"id": "x", "label": "A", "properties": {"v": 1}},
                {"id": "x", "label": "A", "properties": {"v": 2}},   # 중복 — drop
                {"id": "y", "label": "B", "properties": {"v": 3}},
            ],
            "relationships": [],
        }
        out = canonicalize_graph(graph)
        assert len(out["nodes"]) == 2
        # 첫 번째 등장한 것의 값 유지
        x_node = next(n for n in out["nodes"] if n["id"] == "x")
        assert x_node["properties"]["v"] == 1

    def test_invalid_relationships_dropped(self):
        graph = {
            "nodes": [],
            "relationships": [
                {"source": "a", "target": "b", "type": "REL"},
                {"source": "", "target": "b", "type": "REL"},        # empty source
                {"source": "a", "target": "", "type": "REL"},        # empty target
                {"source": "a", "target": "b", "type": ""},          # empty type
                {"source": "a"},                                       # missing fields
            ],
        }
        out = canonicalize_graph(graph)
        assert len(out["relationships"]) == 1

    def test_top_level_metadata_preserved(self):
        # _harness_metadata 같은 보조 필드는 그대로 통과
        graph = {
            "_harness_metadata": {"state": "recording"},
            "nodes": [],
            "relationships": [],
        }
        out = canonicalize_graph(graph)
        assert out["_harness_metadata"] == {"state": "recording"}

    def test_idempotent(self):
        # canonicalize 두 번 적용해도 결과 동일
        graph = {
            "nodes": [
                {"id": "b", "label": "Y", "properties": {"z": 1, "a": 2}},
                {"id": "a", "label": "X", "properties": {"k": "  v  "}},
            ],
            "relationships": [
                {"source": "b", "target": "a", "type": "REL"},
            ],
        }
        once = canonicalize_graph(graph)
        twice = canonicalize_graph(once)
        assert once == twice

    def test_byte_equal_for_semantically_same_inputs(self):
        # 두 입력이 노드 순서/properties 키 순서만 다르고 의미적으로 같으면
        # canonicalize 후 byte 동일
        import json
        g1 = {
            "nodes": [
                {"id": "n2", "label": "X", "properties": {"b": 1, "a": 2}},
                {"id": "n1", "label": "X", "properties": {"a": 2, "b": 1}},
            ],
            "relationships": [
                {"source": "n2", "target": "n1", "type": "REL"},
            ],
        }
        g2 = {
            "nodes": [
                {"id": "n1", "label": "X", "properties": {"b": 1, "a": 2}},
                {"id": "n2", "label": "X", "properties": {"a": 2, "b": 1}},
            ],
            "relationships": [
                {"source": "n2", "target": "n1", "type": "REL"},
            ],
        }
        s1 = json.dumps(canonicalize_graph(g1), sort_keys=False, ensure_ascii=False)
        s2 = json.dumps(canonicalize_graph(g2), sort_keys=False, ensure_ascii=False)
        assert s1 == s2

    def test_non_dict_nodes_filtered(self):
        # 방어적 — list 안에 dict 가 아닌 것 섞여도 안전
        graph = {
            "nodes": [
                {"id": "ok", "label": "X", "properties": {}},
                "garbage",
                None,
                42,
            ],
            "relationships": [],
        }
        out = canonicalize_graph(graph)
        assert len(out["nodes"]) == 1
        assert out["nodes"][0]["id"] == "ok"
