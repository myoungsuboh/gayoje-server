"""
Design lineage 정규화 단위 테스트 (B3).

[검증 대상]
- normalize_lineage: LLM hallucination / 형식 위반 안전 흡수
- normalize_spack / ddd / architecture 가 lineage 를 통과시키는지
"""
from __future__ import annotations

from app.pipelines.design_validator import (
    ValidationReport,
    _normalize_story_id,
    normalize_architecture,
    normalize_ddd,
    normalize_lineage,
    normalize_spack,
)


# ─── _normalize_story_id ───────────────────────────────────────


class TestNormalizeStoryId:
    def test_already_normal(self):
        assert _normalize_story_id("Story-01.1") == "Story-01.1"
        assert _normalize_story_id("Story-12.3") == "Story-12.3"

    def test_zero_pad_added(self):
        assert _normalize_story_id("Story-1.1") == "Story-01.1"

    def test_loose_space_form(self):
        assert _normalize_story_id("Story 1.1") == "Story-01.1"
        assert _normalize_story_id("Story 12.3") == "Story-12.3"

    def test_brackets_stripped(self):
        assert _normalize_story_id("[Story 1.1]") == "Story-01.1"
        assert _normalize_story_id("[Story-01.1]") == "Story-01.1"

    def test_case_insensitive(self):
        assert _normalize_story_id("story 1.1") == "Story-01.1"
        assert _normalize_story_id("STORY 1.1") == "Story-01.1"

    def test_invalid_returns_none(self):
        assert _normalize_story_id("garbage") is None
        assert _normalize_story_id("") is None
        assert _normalize_story_id(None) is None
        assert _normalize_story_id("Story") is None

    def test_neo4j_node_id_form(self):
        """[2026-06-12] 그래프 Story 노드 id('story_01_1') 흡수 — 연결 채우기가 만든
        DERIVED_FROM 엣지의 story_id 가 eval normalize 에서 drop(미연결 오판 +
        UNNORMALIZABLE warning → tier4 감점)되던 회귀 가드."""
        assert _normalize_story_id("story_01_1") == "Story-01.1"
        assert _normalize_story_id("story_12_3") == "Story-12.3"
        assert _normalize_story_id("STORY_02_5") == "Story-02.5"

    def test_prefix_word_not_overmatched(self):
        """[2026-06-13] 'story' 로 끝나는 단어를 Story id 로 날조하지 않음 — '_' 구분자
        허용(story_01_1) 후 생긴 오매칭 가드. 틀린 링크는 빈 링크보다 나쁘다."""
        assert _normalize_story_id("prehistory_5_9") is None
        assert _normalize_story_id("backstory_2_4") is None
        assert _normalize_story_id("history_3_4") is None
        assert _normalize_story_id("laboratory_9_8") is None

    def test_three_component_id_dropped_not_truncated(self):
        """[2026-06-13] 'Story-1-2-3' 같은 3+컴포넌트를 'Story-01.2' 로 잘라 잘못된
        링크를 만들지 않고 drop — 실패가 그릇된 정규화보다 안전."""
        assert _normalize_story_id("Story-1-2-3") is None
        assert _normalize_story_id("Story-1.2.3") is None


# ─── normalize_lineage ─────────────────────────────────────────


class TestNormalizeLineage:
    def _report(self):
        return ValidationReport(stage="test")

    def test_missing_returns_default(self):
        r = self._report()
        out = normalize_lineage(None, node_id="X-1", stage="spack", report=r)
        assert out == {"confidence": "none", "related_stories": []}
        assert any(v.code == "LINEAGE_MISSING" for v in r.violations)

    def test_non_dict_returns_default(self):
        r = self._report()
        out = normalize_lineage("garbage", node_id="X-1", stage="spack", report=r)
        assert out["confidence"] == "none"
        assert out["related_stories"] == []
        assert any(v.code == "LINEAGE_INVALID_SHAPE" for v in r.violations)

    def test_valid_lineage_preserved(self):
        r = self._report()
        out = normalize_lineage(
            {
                "confidence": "direct",
                "related_stories": [
                    {"story_id": "Story-01.1", "quote": "잔여금 티켓 전환"},
                ],
            },
            node_id="ENT-01", stage="spack", report=r,
        )
        assert out["confidence"] == "direct"
        assert len(out["related_stories"]) == 1
        assert out["related_stories"][0]["story_id"] == "Story-01.1"

    def test_invalid_confidence_downgraded_to_none(self):
        r = self._report()
        out = normalize_lineage(
            {"confidence": "very_sure", "related_stories": []},
            node_id="X-1", stage="spack", report=r,
        )
        assert out["confidence"] == "none"
        assert any(v.code == "LINEAGE_CONFIDENCE_INVALID" for v in r.violations)

    def test_story_id_normalized_on_intake(self):
        r = self._report()
        out = normalize_lineage(
            {
                "confidence": "direct",
                "related_stories": [
                    {"story_id": "Story 1.1", "quote": "x"},
                    {"story_id": "[Story-2.3]", "quote": "y"},
                ],
            },
            node_id="X-1", stage="ddd", report=r,
        )
        ids = [s["story_id"] for s in out["related_stories"]]
        assert ids == ["Story-01.1", "Story-02.3"]

    def test_unnormalizable_story_id_dropped(self):
        r = self._report()
        out = normalize_lineage(
            {
                "confidence": "direct",
                "related_stories": [
                    {"story_id": "Story-01.1", "quote": "ok"},
                    {"story_id": "garbage", "quote": "drop"},
                ],
            },
            node_id="X-1", stage="spack", report=r,
        )
        assert len(out["related_stories"]) == 1
        assert any(v.code == "LINEAGE_STORY_ID_UNNORMALIZABLE" for v in r.violations)

    def test_unknown_story_id_dropped_when_valid_set_given(self):
        r = self._report()
        out = normalize_lineage(
            {
                "confidence": "direct",
                "related_stories": [
                    {"story_id": "Story-01.1", "quote": "in PRD"},
                    {"story_id": "Story-99.9", "quote": "not in PRD"},
                ],
            },
            node_id="X-1", stage="arch", report=r,
            valid_story_ids={"Story-01.1"},
        )
        ids = [s["story_id"] for s in out["related_stories"]]
        assert ids == ["Story-01.1"]
        assert any(v.code == "LINEAGE_STORY_ID_UNKNOWN" for v in r.violations)

    def test_quote_truncated_if_too_long(self):
        r = self._report()
        long_quote = "x" * 200
        out = normalize_lineage(
            {
                "confidence": "direct",
                "related_stories": [{"story_id": "Story-01.1", "quote": long_quote}],
            },
            node_id="X-1", stage="spack", report=r,
        )
        q = out["related_stories"][0]["quote"]
        assert len(q) <= 81  # 80 + ellipsis
        assert q.endswith("…")
        assert any(v.code == "LINEAGE_QUOTE_TOO_LONG" for v in r.auto_fixed)

    def test_confidence_direct_with_empty_stories_downgraded(self):
        """direct/inferred 인데 stories=[] → none 으로 정직성 강등."""
        r = self._report()
        out = normalize_lineage(
            {"confidence": "direct", "related_stories": []},
            node_id="X-1", stage="spack", report=r,
        )
        assert out["confidence"] == "none"
        assert out["related_stories"] == []

    def test_confidence_none_forces_empty_stories(self):
        """none 인데 stories 가 있으면 stories 비움."""
        r = self._report()
        out = normalize_lineage(
            {
                "confidence": "none",
                "related_stories": [{"story_id": "Story-01.1", "quote": "x"}],
            },
            node_id="X-1", stage="spack", report=r,
        )
        assert out["confidence"] == "none"
        assert out["related_stories"] == []


# ─── normalize_spack/ddd/arch 와의 통합 ──────────────────────────


class TestLineageInNormalizePipeline:
    def test_spack_entity_gets_lineage_field(self):
        raw = {
            "apis": [],
            "entities": [
                {
                    "id": "ENT-1", "name": "Ticket",
                    "lineage": {
                        "confidence": "direct",
                        "related_stories": [
                            {"story_id": "Story 1.1", "quote": "티켓 발행"},
                        ],
                    },
                },
            ],
            "policies": [],
        }
        out, _ = normalize_spack(raw)
        e = out["entities"][0]
        assert "lineage" in e
        assert e["lineage"]["confidence"] == "direct"
        assert e["lineage"]["related_stories"][0]["story_id"] == "Story-01.1"

    def test_spack_entity_missing_lineage_filled_with_default(self):
        raw = {
            "apis": [],
            "entities": [{"id": "ENT-1", "name": "Ticket"}],  # lineage 없음
            "policies": [],
        }
        out, report = normalize_spack(raw)
        e = out["entities"][0]
        assert e["lineage"] == {"confidence": "none", "related_stories": []}
        assert any(v.code == "LINEAGE_MISSING" for v in report.violations)

    def test_ddd_aggregate_gets_lineage_field(self):
        raw_spack = {
            "apis": [],
            "entities": [{"id": "ENT-1", "name": "Ticket"}],
            "policies": [],
        }
        out_spack, _ = normalize_spack(raw_spack)
        raw_ddd = {
            "contexts": [{"id": "CTX-1", "name": "Ticket Context"}],
            "aggregates": [
                {
                    "id": "AGG-1", "name": "Ticket", "context_id": "CTX-1",
                    "lineage": {
                        "confidence": "direct",
                        "related_stories": [
                            {"story_id": "Story-01.1", "quote": "발행"},
                            {"story_id": "Story-02.3", "quote": "조회"},
                        ],
                    },
                },
            ],
            "entities": [],
            "events": [],
            "spack_entity_mapping": [
                {"spack_entity_id": "ENT-01", "spack_name": "Ticket",
                 "ddd_location": "AGG-01", "ddd_role": "aggregate_root"},
            ],
        }
        out, _ = normalize_ddd(raw_ddd, out_spack)
        a = out["aggregates"][0]
        assert "lineage" in a
        assert len(a["lineage"]["related_stories"]) == 2

    def test_architecture_service_gets_lineage_field(self):
        # 최소 spack/ddd 구성
        out_spack, _ = normalize_spack({"apis": [], "entities": [], "policies": []})
        out_ddd, _ = normalize_ddd(
            {"contexts": [], "aggregates": [], "entities": [], "events": [],
             "spack_entity_mapping": []},
            out_spack,
        )
        raw_arch = {
            "services": [
                {
                    "id": "SVC-1", "name": "Frontend", "type": "Frontend",
                    "tech_stack": "Vue.js",
                    "lineage": {
                        "confidence": "inferred",
                        "related_stories": [
                            {"story_id": "Story-01.1", "quote": "모바일 환경"},
                        ],
                    },
                },
            ],
            "databases": [], "connections": [], "api_service_mapping": [],
        }
        out, _ = normalize_architecture(raw_arch, out_spack, out_ddd)
        s = out["services"][0]
        assert s["lineage"]["confidence"] == "inferred"
        assert s["lineage"]["related_stories"][0]["story_id"] == "Story-01.1"
