"""
CPS→PRD 핸드오프: cps_agent 의 CPS_Document.full_markdown 이 비어도 PRD 가
'내용 없음' 으로 환각하지 않도록, Problem/Solution 노드에서 CPS 본문을 재구성(fallback).

[배경 — 2026-05-28 운영]
cps_agent(structured output)가 큰 full_markdown 필드를 종종 누락(빈 값)한다('b' 빈-
full_markdown 가드가 존재하는 이유). CPS merge 는 raw 회의록 fallback 으로 master 를
채우지만, parse_cps_for_prd 는 fallback 이 없어 PRD 가 "CPS 내용 없음" → 엉뚱한 PRD
환각. Problem/Solution 요약은 안정적으로 채워지므로 그걸로 본문을 재구성한다.
"""
from __future__ import annotations

import pytest

from app.pipelines.prd_pipeline import parse_cps_for_prd


def test_uses_full_markdown_when_present():
    graph = {
        "nodes": [
            {"id": "doc_cps_x_v1", "label": "CPS_Document",
             "properties": {"full_markdown": "# CPS 본문\n## 문제\n- 느림"}},
            {"id": "prb_01", "label": "Problem", "properties": {"summary": "느림"}},
        ],
        "relationships": [],
    }
    out = parse_cps_for_prd(graph)
    assert out["pure_markdown"] == "# CPS 본문\n## 문제\n- 느림"
    assert "[prb_01]" in out["problems"]


def test_falls_back_to_problems_and_solutions_when_full_markdown_empty():
    """full_markdown 이 비었지만 Problem/Solution 이 있으면 그걸로 본문 재구성 (환각 차단)."""
    graph = {
        "nodes": [
            {"id": "doc_cps_ai_v1", "label": "CPS_Document", "properties": {"full_markdown": ""}},
            {"id": "prb_01", "label": "Problem",
             "properties": {"summary": "AI 도구 사용 현황 파악 및 중복 결제"}},
            {"id": "prb_02", "label": "Problem",
             "properties": {"summary": "AI 도구 사용 관련 보안 취약성"}},
            {"id": "res_01", "label": "Solution",
             "properties": {"summary": "AI 도구 신청 및 결제 통합 관리"}},
        ],
        "relationships": [],
    }
    out = parse_cps_for_prd(graph)
    md = out["pure_markdown"]
    assert md != "내용 없음"                                   # 환각 방지 — 본문이 채워짐
    assert "AI 도구 신청 및 결제 통합 관리" in md              # Solution 반영
    assert "AI 도구 사용 관련 보안 취약성" in md               # Problem 반영


def test_returns_empty_when_no_content_at_all():
    """full_markdown 도 없고 Problem/Solution 도 없고 회의록도 없으면 → '내용 없음'."""
    graph = {"nodes": [{"id": "d", "label": "CPS_Document", "properties": {}}], "relationships": []}
    out = parse_cps_for_prd(graph)
    assert out["pure_markdown"] == "내용 없음"


def test_falls_back_to_raw_meeting_when_graph_completely_empty():
    """[2026-06-04] full_markdown/Problem/Solution 모두 비어도 raw 회의록이 있으면 그걸로
    본문 재구성 — master CPS 는 raw fallback 으로 살아남는데 master PRD 만 프로젝트명만으로
    환각하던 비대칭의 근본 해소."""
    graph = {"nodes": [{"id": "d", "label": "CPS_Document", "properties": {}}], "relationships": []}
    meeting = (
        "V26 회의록: 사용자가 AI 도구 신청 시 전자 서약서(PledgeDialog.vue)에 "
        "서명(signatureDataUrl)하도록 한다. 서명 없이는 결제 단계로 넘어갈 수 없다."
    )
    out = parse_cps_for_prd(graph, meeting)
    md = out["pure_markdown"]
    assert md != "내용 없음"                          # 환각 방지 — 본문이 채워짐
    assert "전자 서약서" in md                         # 회의 내용이 PRD 입력으로 들어감
    assert "signatureDataUrl" in md


def test_node_synthesis_takes_priority_over_raw_meeting():
    """Problem/Solution 노드가 있으면 (더 PRD-ready 한) 노드 재구성을 우선 — 회의록 fallback
    은 노드까지 빈 최후 케이스 전용. (구조화된 입력 우선 순서 보존.)"""
    graph = {
        "nodes": [
            {"id": "d", "label": "CPS_Document", "properties": {"full_markdown": ""}},
            {"id": "prb_01", "label": "Problem", "properties": {"summary": "중복 결제"}},
        ],
        "relationships": [],
    }
    out = parse_cps_for_prd(graph, meeting_content="회의록 원문 텍스트")
    md = out["pure_markdown"]
    assert "[prb_01]" in md                            # 노드 재구성 사용
    assert "회의록 원문 텍스트" not in md              # raw 회의록은 쓰이지 않음 (노드 우선)


def test_full_markdown_still_wins_over_meeting():
    """full_markdown 이 정상이면 회의록 fallback 은 무시 (기존 우선순위 불변)."""
    graph = {
        "nodes": [
            {"id": "d", "label": "CPS_Document",
             "properties": {"full_markdown": "# 정상 CPS 본문"}},
        ],
        "relationships": [],
    }
    out = parse_cps_for_prd(graph, meeting_content="회의록 fallback 텍스트")
    assert out["pure_markdown"] == "# 정상 CPS 본문"
