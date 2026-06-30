"""
evals/fix_targets.py — 구체적 보강 대상 추출 검증.

핵심: scorer 의 ratio 가 낮을 때 어느 개별 항목(id/name)이 빠졌는지 콕 집는다.
"""
from __future__ import annotations

from evals.fix_targets import collect_fix_targets


def _find(targets, metric_key):
    for t in targets:
        if t["metric_key"] == metric_key:
            return t
    return None


def test_collect_fix_targets_api_error_cases_names_missing_apis():
    """error_cases 없는 API 의 id/name 이 missing 에 잡힌다."""
    spack = {
        "apis": [
            {"id": "API-01", "name": "작업 생성", "method": "POST",
             "error_cases": [{"status": 401}]},
            {"id": "API-02", "name": "작업 조회", "method": "GET"},  # error_cases 없음
            {"id": "API-03", "name": "작업 삭제", "method": "DELETE"},  # 없음
        ],
        "entities": [],
    }
    targets = collect_fix_targets(spack)
    t = _find(targets, "api_error_cases_ratio")
    assert t is not None
    names = {m["name"] for m in t["missing"]}
    ids = {m["id"] for m in t["missing"]}
    assert names == {"작업 조회", "작업 삭제"}
    assert ids == {"API-02", "API-03"}
    assert t["total"] == 3
    assert t["missing_total"] == 2
    assert t["prd_section"] == "epic"
    assert t["fix"]  # 액션 문구 존재


def test_collect_fix_targets_entity_attributes():
    """attributes 없는 Entity 만 잡힌다."""
    spack = {
        "apis": [],
        "entities": [
            {"id": "ENT-01", "name": "User", "attributes": [{"name": "email"}]},
            {"id": "ENT-02", "name": "Task"},  # attributes 없음
        ],
    }
    targets = collect_fix_targets(spack)
    t = _find(targets, "entity_attributes_present_ratio")
    assert t is not None
    assert [m["name"] for m in t["missing"]] == ["Task"]


def test_collect_fix_targets_skips_fully_specified():
    """모두 채워진 항목은 fix target 에서 제외 (None drop)."""
    spack = {
        "apis": [
            {"id": "API-01", "name": "x", "method": "GET",
             "error_cases": [{"status": 404}],
             "related_story_id": "Story-01.1",
             "auth": {"description": "로그인 필요"},
             "response_body": {"fields": [{"name": "a"}]}},
        ],
        "entities": [
            {"id": "ENT-01", "name": "E", "attributes": [{"name": "a", "type": "string"}],
             "lineage": {"related_stories": [{"story_id": "Story-01.1"}]}},
        ],
    }
    targets = collect_fix_targets(spack)
    # error_cases / auth / response / api_story / entity_story 모두 충족 → 해당 target 없음
    assert _find(targets, "api_error_cases_ratio") is None
    assert _find(targets, "api_auth_specified_ratio") is None
    assert _find(targets, "api_story_mapped_ratio") is None
    assert _find(targets, "entity_attributes_present_ratio") is None


def test_collect_fix_targets_sorted_by_severity():
    """빠진 비율이 높은 target 이 앞에 온다."""
    spack = {
        "apis": [
            # error_cases: 3개 중 3개 누락 (100%)
            {"id": "API-01", "name": "a", "method": "GET", "auth": {"description": "x"},
             "related_story_id": "S-1", "response_body": {"fields": [{"name": "f"}]}},
            {"id": "API-02", "name": "b", "method": "GET", "auth": {"description": "x"},
             "related_story_id": "S-1", "response_body": {"fields": [{"name": "f"}]}},
            {"id": "API-03", "name": "c", "method": "GET", "auth": {"description": "x"},
             "related_story_id": "S-1", "response_body": {"fields": [{"name": "f"}]}},
        ],
        "entities": [
            # attributes: 2개 중 1개 누락 (50%)
            {"id": "ENT-01", "name": "E1", "attributes": [{"name": "a", "type": "string"}],
             "lineage": {"related_stories": [{"story_id": "S-1"}]}},
            {"id": "ENT-02", "name": "E2",
             "lineage": {"related_stories": [{"story_id": "S-1"}]}},
        ],
    }
    targets = collect_fix_targets(spack)
    # error_cases (100% 누락) 가 entity_attributes (50%) 보다 앞
    keys = [t["metric_key"] for t in targets]
    assert keys.index("api_error_cases_ratio") < keys.index("entity_attributes_present_ratio")


def test_collect_fix_targets_caps_item_count():
    """한 target 의 missing 항목은 최대 8개로 제한 (missing_total 은 실제 수)."""
    spack = {
        "apis": [
            {"id": f"API-{i:02d}", "name": f"api{i}", "method": "GET"}
            for i in range(1, 13)  # 12개 모두 error_cases 없음
        ],
        "entities": [],
    }
    targets = collect_fix_targets(spack)
    t = _find(targets, "api_error_cases_ratio")
    assert len(t["missing"]) == 8       # 표시는 8개로 cap
    assert t["missing_total"] == 12     # 실제 수는 12


def test_collect_fix_targets_empty_graph_safe():
    """빈 그래프 — 분모 0 이면 target 없음 (penalty/노이즈 0)."""
    targets = collect_fix_targets({"apis": [], "entities": []})
    assert targets == []
