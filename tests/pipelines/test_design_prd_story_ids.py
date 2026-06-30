"""
PRD Story IDs 추출 + lineage 검증 통합 단위 테스트 (A — 2026-05).

[검증 대상]
- _extract_prd_story_ids: PRD markdown 의 다양한 Story 표기를 모두 정규화 set 으로
- normalize_spack/ddd/architecture 가 valid_story_ids 받으면 PRD 부재 story_id drop
"""
from __future__ import annotations

from app.pipelines.design_pipeline import _extract_prd_story_ids
from app.pipelines.design_validator import (
    normalize_architecture,
    normalize_ddd,
    normalize_spack,
)


# ─── _extract_prd_story_ids ────────────────────────────────────


class TestExtractPrdStoryIds:
    def test_empty_returns_empty_set(self):
        assert _extract_prd_story_ids("") == set()
        assert _extract_prd_story_ids(None) == set()

    def test_bracketed_form(self):
        """`[Story 1.1]` / `[Story-1.1]` 형태."""
        md = "- **[Story 1.1] 티켓 발행**\n- **[Story 2.3] 정산**"
        assert _extract_prd_story_ids(md) == {"Story-01.1", "Story-02.3"}

    def test_unbracketed_form(self):
        md = "이 기능은 Story 3.1 와 Story 3.2 에서 도출."
        assert _extract_prd_story_ids(md) == {"Story-03.1", "Story-03.2"}

    def test_zero_padded_form(self):
        md = "Story-12.5 가 핵심."
        assert _extract_prd_story_ids(md) == {"Story-12.5"}

    def test_dedup_same_story_mentioned_multiple_times(self):
        md = "[Story 1.1] 첫 언급. ... Story 1.1 두 번째 언급."
        assert _extract_prd_story_ids(md) == {"Story-01.1"}

    def test_case_insensitive(self):
        md = "story 1.1 / STORY 2.2 / Story 3.3"
        assert _extract_prd_story_ids(md) == {
            "Story-01.1", "Story-02.2", "Story-03.3",
        }

    def test_mixed_with_garbage(self):
        md = "## Epic 1\n- [Story 1.1] 발행\n- 일반 문장\n- Story 2.10 처리"
        assert _extract_prd_story_ids(md) == {"Story-01.1", "Story-02.10"}


# ─── normalize_spack with valid_story_ids ──────────────────────


class TestNormalizeWithValidStoryIds:
    def test_spack_drops_fake_story_ids(self):
        """Entity lineage 가 PRD 부재 story_id 를 가지면 drop."""
        valid_ids = {"Story-01.1", "Story-02.3"}
        raw = {
            "apis": [],
            "entities": [{
                "id": "ENT-1", "name": "Ticket",
                "lineage": {
                    "confidence": "direct",
                    "related_stories": [
                        {"story_id": "Story-01.1", "quote": "in PRD"},     # 통과
                        {"story_id": "Story-99.9", "quote": "fake"},        # drop
                        {"story_id": "Story-02.3", "quote": "in PRD"},     # 통과
                    ],
                },
            }],
            "policies": [],
        }
        out, report = normalize_spack(raw, valid_story_ids=valid_ids)
        stories = out["entities"][0]["lineage"]["related_stories"]
        ids = {s["story_id"] for s in stories}
        assert ids == {"Story-01.1", "Story-02.3"}
        assert any(v.code == "LINEAGE_STORY_ID_UNKNOWN" for v in report.violations)

    def test_spack_all_fake_demotes_to_none(self):
        """모든 story_id 가 fake 면 stories=[] → confidence='none' 자동 강등."""
        valid_ids = {"Story-01.1"}
        raw = {
            "apis": [],
            "entities": [{
                "id": "ENT-1", "name": "Ticket",
                "lineage": {
                    "confidence": "direct",
                    "related_stories": [
                        {"story_id": "Story-99.9", "quote": "fake"},
                    ],
                },
            }],
            "policies": [],
        }
        out, _ = normalize_spack(raw, valid_story_ids=valid_ids)
        lineage = out["entities"][0]["lineage"]
        assert lineage["confidence"] == "none"
        assert lineage["related_stories"] == []

    def test_ddd_aggregate_lineage_filtered_by_valid_ids(self):
        valid_ids = {"Story-01.1"}
        out_spack, _ = normalize_spack(
            {"apis": [], "entities": [{"id": "ENT-1", "name": "Ticket"}], "policies": []},
            valid_story_ids=valid_ids,
        )
        raw_ddd = {
            "contexts": [{"id": "CTX-1", "name": "Ticket Context"}],
            "aggregates": [{
                "id": "AGG-1", "name": "Ticket", "context_id": "CTX-1",
                "lineage": {
                    "confidence": "direct",
                    "related_stories": [
                        {"story_id": "Story-01.1", "quote": "ok"},
                        {"story_id": "Story-99.9", "quote": "fake"},
                    ],
                },
            }],
            "entities": [], "events": [],
            "spack_entity_mapping": [{
                "spack_entity_id": "ENT-01", "spack_name": "Ticket",
                "ddd_location": "AGG-01", "ddd_role": "aggregate_root",
            }],
        }
        out, _ = normalize_ddd(raw_ddd, out_spack, valid_story_ids=valid_ids)
        stories = out["aggregates"][0]["lineage"]["related_stories"]
        assert [s["story_id"] for s in stories] == ["Story-01.1"]

    def test_architecture_service_lineage_filtered_by_valid_ids(self):
        valid_ids = {"Story-01.1", "Story-02.1"}
        out_spack, _ = normalize_spack(
            {"apis": [], "entities": [], "policies": []},
            valid_story_ids=valid_ids,
        )
        out_ddd, _ = normalize_ddd(
            {"contexts": [], "aggregates": [], "entities": [], "events": [],
             "spack_entity_mapping": []},
            out_spack,
            valid_story_ids=valid_ids,
        )
        raw_arch = {
            "services": [{
                "id": "SVC-1", "name": "Ticket Service", "type": "Backend API",
                "tech_stack": "Spring Boot",
                "lineage": {
                    "confidence": "inferred",
                    "related_stories": [
                        {"story_id": "Story-02.1", "quote": "ok"},
                        {"story_id": "Story-99.9", "quote": "fake"},
                    ],
                },
            }],
            "databases": [], "connections": [], "api_service_mapping": [],
        }
        out, _ = normalize_architecture(
            raw_arch, out_spack, out_ddd, valid_story_ids=valid_ids,
        )
        stories = out["services"][0]["lineage"]["related_stories"]
        assert [s["story_id"] for s in stories] == ["Story-02.1"]

    def test_valid_story_ids_none_keeps_format_only_check(self):
        """valid_story_ids=None 이면 PRD 존재 검증 skip — 형식 검증만."""
        raw = {
            "apis": [],
            "entities": [{
                "id": "ENT-1", "name": "Ticket",
                "lineage": {
                    "confidence": "direct",
                    "related_stories": [
                        {"story_id": "Story-99.9", "quote": "format-ok"},
                    ],
                },
            }],
            "policies": [],
        }
        out, _ = normalize_spack(raw, valid_story_ids=None)
        # 형식만 OK 면 통과 (PRD 검증 skip)
        assert len(out["entities"][0]["lineage"]["related_stories"]) == 1


# ─── _compute_lineage_coverage ─────────────────────────────────


class TestComputeLineageCoverage:
    def test_empty_list_zeros(self):
        from app.pipelines.design_pipeline import _compute_lineage_coverage
        out = _compute_lineage_coverage([])
        assert out == {"total": 0, "direct": 0, "inferred": 0, "none": 0, "coverage_pct": 0}

    def test_all_direct_full_coverage(self):
        from app.pipelines.design_pipeline import _compute_lineage_coverage
        items = [
            {"id": "A", "lineage": {"confidence": "direct"}},
            {"id": "B", "lineage": {"confidence": "direct"}},
        ]
        out = _compute_lineage_coverage(items)
        assert out["total"] == 2
        assert out["direct"] == 2
        assert out["coverage_pct"] == 100

    def test_mixed_distribution(self):
        from app.pipelines.design_pipeline import _compute_lineage_coverage
        items = [
            {"id": "A", "lineage": {"confidence": "direct"}},
            {"id": "B", "lineage": {"confidence": "direct"}},
            {"id": "C", "lineage": {"confidence": "inferred"}},
            {"id": "D", "lineage": {"confidence": "none"}},
            {"id": "E", "lineage": {"confidence": "none"}},
        ]
        out = _compute_lineage_coverage(items)
        assert out["total"] == 5
        assert out["direct"] == 2
        assert out["inferred"] == 1
        assert out["none"] == 2
        assert out["coverage_pct"] == 60   # (2+1)/5 = 60%

    def test_missing_lineage_treated_as_none(self):
        from app.pipelines.design_pipeline import _compute_lineage_coverage
        items = [
            {"id": "A"},  # lineage 없음
            {"id": "B", "lineage": {"confidence": "direct"}},
        ]
        out = _compute_lineage_coverage(items)
        assert out["none"] == 1
        assert out["direct"] == 1
        assert out["coverage_pct"] == 50

    def test_unknown_confidence_treated_as_none(self):
        from app.pipelines.design_pipeline import _compute_lineage_coverage
        items = [
            {"id": "A", "lineage": {"confidence": "high"}},  # invalid → none
            {"id": "B", "lineage": {"confidence": "direct"}},
        ]
        out = _compute_lineage_coverage(items)
        assert out["none"] == 1
        assert out["direct"] == 1
