"""
E2E 스모크(scripts/e2e_smoke.py)의 **순수 검증기** 단위 테스트 — 네트워크 0.

[배경 — P0-2]
단위 3,096개가 그린이어도 운영에선 PRD 가 비어 있었다(무음 실패). 스모크는 실서버에
회의록 2건을 순차 처리해 CPS·PRD 생성 + 누적(V2 반영) + 조회 + error 강등을 단언한다.
이 파일은 그 스모크의 판정 로직 자체가 틀리지 않도록 고정하는 가드다 — 판정기가
무르면 스모크도 무음 통과한다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "e2e_smoke", Path(__file__).resolve().parent.parent / "scripts" / "e2e_smoke.py"
)
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


# ─── extract_task_id / read_status_info — FE(asyncJob.js) 계약 동일 ───


def test_extract_task_id_handles_all_gateway_wrappers():
    assert smoke.extract_task_id({"result": {"task_id": "a"}}) == "a"
    assert smoke.extract_task_id({"result": [{"task_id": "b"}]}) == "b"
    assert smoke.extract_task_id({"task_id": "c"}) == "c"
    assert smoke.extract_task_id({"result": []}) is None
    assert smoke.extract_task_id(None) is None
    assert smoke.extract_task_id({"result": "oops"}) is None


def test_read_status_info_unwraps_result():
    assert smoke.read_status_info({"result": {"status": "complete"}}) == {"status": "complete"}
    assert smoke.read_status_info({"result": [{"status": "queued"}]}) == {"status": "queued"}
    assert smoke.read_status_info(None) is None


# ─── judge_post_meeting — postMeeting 결과 판정 (무음 실패 감지의 핵심) ───


def _ok_result(prd_mode="first_run"):
    return {
        "cps": {"master_cps_id": "doc_cps_master_x", "mode": "first_run"},
        "prd": {"master_prd_id": "doc_prd_master_x", "mode": prd_mode, "diagnostic": {}},
    }


def test_judge_post_meeting_ok():
    assert smoke.judge_post_meeting(_ok_result()) == []
    assert smoke.judge_post_meeting(_ok_result("incremental")) == []


def test_judge_post_meeting_detects_prd_error_degrade():
    """[R1 강등 감지] prd.mode='error' 는 job 성공이어도 스모크 FAIL — diagnostic 노출."""
    r = _ok_result()
    r["prd"] = {"mode": "error", "diagnostic": {"error": "orphan", "error_type": "RuntimeError"}}
    reasons = smoke.judge_post_meeting(r)
    assert any("error" in x for x in reasons)
    assert any("orphan" in x for x in reasons)  # 원인 진단 포함


def test_judge_post_meeting_detects_missing_prd_master():
    """과거 무음 누락 그 자체 — first_run/incremental 인데 master_prd_id 빈 값."""
    r = _ok_result()
    r["prd"]["master_prd_id"] = ""
    assert smoke.judge_post_meeting(r)


def test_judge_post_meeting_detects_missing_cps():
    r = _ok_result()
    r["cps"]["master_cps_id"] = ""
    assert smoke.judge_post_meeting(r)


def test_judge_post_meeting_no_changes_allows_empty_prd_master():
    """no_changes(보강 회의)는 PRD master 미갱신이 정상 — 단 V1 골든은 first_run 이어야
    하므로 호출측에서 expect_modes 로 따로 조인다."""
    r = _ok_result("no_changes")
    r["prd"]["master_prd_id"] = ""
    assert smoke.judge_post_meeting(r) == []


def test_judge_post_meeting_expect_modes():
    r = _ok_result("no_changes")
    reasons = smoke.judge_post_meeting(r, expect_modes={"first_run"})
    assert any("no_changes" in x for x in reasons)


# ─── content_of_row — getCPS/getPRD 행에서 본문 추출 (FE fallback 동일) ───


def test_content_of_row_field_fallbacks():
    assert smoke.content_of_row({"prd_content": "a"}) == "a"
    assert smoke.content_of_row({"cps_content": "b"}) == "b"
    assert smoke.content_of_row({"output": "c"}) == "c"
    assert smoke.content_of_row({"content": "d"}) == "d"
    assert smoke.content_of_row({"full_markdown": "e"}) == "e"
    assert smoke.content_of_row({}) == ""
    assert smoke.content_of_row(None) == ""


def test_content_of_row_unescapes_newlines():
    assert "\n" in smoke.content_of_row({"prd_content": "줄1\\n줄2"})


# ─── accumulation_ok — V2 누적 판정 (D=frozen 누적 감지기) ───


def test_accumulation_ok_passes_on_growth():
    v1 = "x" * 1000
    v2 = v1 + "\n## 새 에픽: 알림\n" + ("y" * 500)
    assert smoke.accumulation_ok(v1, v2, "incremental") == []


def test_accumulation_detects_frozen_master():
    """V2 처리 후에도 PRD 가 V1 과 동일 = 누적 정지(D 증상) → FAIL."""
    v1 = "x" * 1000
    assert smoke.accumulation_ok(v1, v1, "incremental")


def test_accumulation_detects_no_changes_mode():
    """골든 V2 는 명백한 신규 에픽 포함 — no_changes 로 빠지면 누적 의심."""
    v1 = "x" * 1000
    v2 = v1 + "y" * 500
    reasons = smoke.accumulation_ok(v1, v2, "no_changes")
    assert any("no_changes" in x for x in reasons)


def test_accumulation_detects_shrink():
    """V2 후 PRD 가 크게 줄면 침식 의심 → FAIL."""
    v1 = "x" * 1000
    v2 = "x" * 300
    assert smoke.accumulation_ok(v1, v2, "incremental")


# ─── 골든 회의록 — 스펙이 뽑히도록 설계됐는지 최소 보증 ───


def test_golden_meetings_shape():
    g = smoke.GOLDEN_MEETINGS
    assert len(g) == 2
    assert g[0]["version"] == "V1" and g[1]["version"] == "V2"
    for m in g:
        assert len(m["content"]) >= 500  # 빈약 입력이면 skip-stub 으로 빠져 스모크 무의미
    # V2 는 V1 에 없는 신규 에픽 키워드를 명시 (incremental 보장 설계)
    assert "알림" in g[1]["content"] and "알림" not in g[0]["content"]


def test_smoke_project_prefix_guard():
    """스모크는 자기 prefix 프로젝트만 만들고 지운다 — 실수로 실프로젝트 삭제 불가."""
    assert smoke.SMOKE_PROJECT_PREFIX.startswith("__smoke")
    with pytest.raises(ValueError):
        smoke.assert_smoke_project("ai agent")
    smoke.assert_smoke_project(f"{smoke.SMOKE_PROJECT_PREFIX}_123")  # OK
