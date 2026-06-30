"""
filter_ddd_for_codegen — 코드 생성 입력에서 confidence=none DDD 제외.

[2026-05-27] "전시용 vs 코드-입력용 신뢰도 분리" — 화면엔 confidence 3단계
(direct/inferred/none) 다 보여주되, 코드 생성 입력(Architecture 에이전트 +
바이브 패키지 ddd_md)에서는 none(PRD 근거 없음)을 제외해 LLM 오염을 막는다.
inferred 는 데이터에 유지(프롬프트가 '추정-검증필요'로 해석). 옛 데이터(lineage
정보 자체 없음)는 보존 — 정보 부재를 제외 근거로 쓰지 않음.
"""
from __future__ import annotations

from app.pipelines.design_pipeline.ddd_filter import filter_ddd_for_codegen


def test_drops_none_confidence_aggregates_nested():
    """nested lineage.confidence — none 만 제외, direct/inferred 유지."""
    ddd = {
        "contexts": [{"id": "CTX-01"}],
        "aggregates": [
            {"id": "AGG-01", "lineage": {"confidence": "direct"}},
            {"id": "AGG-02", "lineage": {"confidence": "none"}},
            {"id": "AGG-03", "lineage": {"confidence": "inferred"}},
        ],
        "events": [{"id": "EVT-01"}],
    }
    out = filter_ddd_for_codegen(ddd)
    assert [a["id"] for a in out["aggregates"]] == ["AGG-01", "AGG-03"]
    # context/event 는 confidence 없음 — 그대로
    assert out["contexts"] == ddd["contexts"]
    assert out["events"] == ddd["events"]
    # 원본 불변
    assert len(ddd["aggregates"]) == 3


def test_drops_none_flat_confidence_and_domain_entities_fieldname():
    """flat lineage_confidence + DddGraph 필드명(domain_entities) 도 흡수."""
    ddd = {
        "aggregates": [{"id": "AGG-01", "lineage_confidence": "none"}],
        "domain_entities": [
            {"id": "DENT-01", "lineage_confidence": "direct"},
            {"id": "DENT-02", "lineage_confidence": "none"},
        ],
    }
    out = filter_ddd_for_codegen(ddd)
    assert out["aggregates"] == []
    assert [d["id"] for d in out["domain_entities"]] == ["DENT-01"]


def test_drops_none_for_pipeline_fieldname_entities():
    """pipeline ddd_for_llm 필드명(entities) 도 흡수."""
    ddd = {
        "entities": [
            {"id": "DENT-01", "lineage": {"confidence": "none"}},
            {"id": "DENT-02", "lineage": {"confidence": "direct"}},
        ],
    }
    out = filter_ddd_for_codegen(ddd)
    assert [d["id"] for d in out["entities"]] == ["DENT-02"]


def test_keeps_items_without_lineage_info():
    """옛 데이터(lineage 정보 자체 없음)는 보존 — 정보 부재 ≠ none."""
    ddd = {"aggregates": [{"id": "AGG-01"}]}  # lineage 없음
    out = filter_ddd_for_codegen(ddd)
    assert [a["id"] for a in out["aggregates"]] == ["AGG-01"]


def test_empty_or_missing_keys_safe():
    """빈/누락 키에도 안전."""
    assert filter_ddd_for_codegen({}) == {}
    out = filter_ddd_for_codegen({"aggregates": []})
    assert out["aggregates"] == []
