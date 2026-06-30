"""
[2026-06] _OWNERSHIP_READ — 비소유 read 가 403 대신 200-empty (핸들러 미실행).

pre-claim(아직 claim 안 된 본인 신규 프로젝트) 의 403 콘솔 노이즈 제거 + 동명 타
유저 데이터 노출(IDOR) 차단. dispatcher 는 can_access 가 False 면 핸들러를 태우지
않고 _read_empty_response 를 직접 반환한다.

핵심 회귀 가드:
- read/write 분류 무결성 (write 가 read 로 새면 비소유 403 이 사라져 보안 약화)
- _read_empty_response 의 shape 가 각 핸들러의 '데이터 없는 프로젝트' 실제 출력과
  일치(drift) — 안 그러면 FE 가 빈 상태를 오파싱.
"""
from __future__ import annotations

import pytest

from app.api import gateway_compat_routes as gw


# ─── 분류 무결성 ────────────────────────────────────────────────

def test_read_and_access_sets_disjoint():
    assert not (gw._OWNERSHIP_READ & gw._OWNERSHIP_ACCESS)


def test_all_reads_are_dispatchable():
    for action in gw._OWNERSHIP_READ:
        assert action in gw._DISPATCH, f"{action} 가 _DISPATCH 에 없음"


def test_writes_stay_in_access_not_read():
    """write/LLM 은 ACCESS 에 남아 비소유 403 유지 — read 로 새면 안 됨(보안)."""
    for w in (
        "deleteMeeting", "deleteProject", "deleteSkill", "deleteProjectRepo",
        "createDesign", "createSpack", "createMD", "createPRD",
        "runLint", "generateFixSpec", "analyzeLineage", "recommendSkillsByAI",
    ):
        assert w in gw._OWNERSHIP_ACCESS, f"{w} 가 ACCESS 에서 빠짐"
        assert w not in gw._OWNERSHIP_READ, f"{w} 가 READ 로 샘 — 비소유 403 사라짐"


# ─── 빈 응답 shape ──────────────────────────────────────────────

def test_read_empty_response_shapes():
    # None→[] 핸들러군 → {"result": []}
    for a in (
        "getCPS", "getPRD", "getMeetingLogs", "getMeetingVersions",
        "getAllSkill", "getAllSkillDetail",
    ):
        assert gw._read_empty_response(a, "p") == {"result": []}, a
    # 그래프군(DDD/Spack/Architecture) — 핸들러가 빈 그래프 '객체'를 반환 → _as_array 가
    # 단일 원소 리스트로 감싼다. {"result": []} 가 아니라 {"result": [빈그래프]}.
    for a in ("getDDD", "getSpack", "getArchitecture"):
        r = gw._read_empty_response(a, "p")
        assert isinstance(r["result"], list) and len(r["result"]) == 1, a
        graph = r["result"][0]
        assert all(v == [] for v in graph.values()), f"{a} 빈그래프 필드가 전부 [] 여야: {graph}"
    assert gw._read_empty_response("getSkill", "p") == {"result": None}
    assert gw._read_empty_response("getProjectBusy", "p") == {
        "result": {"project_name": "p", "busy": False}
    }
    assert gw._read_empty_response("getDuplicateSkill", "p") == {
        "isDuplicate": False, "existingIds": []
    }
    assert gw._read_empty_response("getProjectRepos", "p") == {"repos": [], "count": 0}
    assert gw._read_empty_response("getLastLintResult", "p") == {
        "found": False, "result": None, "savedAt": None
    }
    assert gw._read_empty_response("getLastLineage", "p") == {
        "found": False, "result": None, "savedAt": None
    }
    tl = gw._read_empty_response("getProjectTimeline", "p")
    assert tl["events"] == [] and tl["counts"] == {} and tl["project"] == "p"


def test_every_read_action_has_empty_shape():
    """모든 _OWNERSHIP_READ action 이 빈응답을 만들 수 있어야 (KeyError/None 방지)."""
    for action in gw._OWNERSHIP_READ:
        out = gw._read_empty_response(action, "p")
        assert isinstance(out, dict) and out, f"{action} 빈응답 비정상: {out}"


# ─── drift 가드 — 핸들러 실제 빈 출력 == _read_empty_response ────────

@pytest.mark.asyncio
async def test_empty_map_matches_handler_output(monkeypatch):
    """각 핸들러를 '데이터 없는 프로젝트'로 돌려, 그 출력이 _read_empty_response 와
    일치하는지 확인 — 핸들러가 빈 shape 를 바꾸면 이 테스트가 깨져 map 갱신을 강제.
    (repository 백엔드를 빈값으로 monkeypatch.)"""
    from app.service import (
        query_repository, skill_repository, lint_repository,
        lineage_repository, repo_repository,
    )

    async def _none(*a, **k):
        return None

    async def _empty_list(*a, **k):
        return []

    async def _dup_empty(*a, **k):
        return {"is_duplicate": False, "existing_ids": []}

    # result-list 계열
    monkeypatch.setattr(query_repository, "get_master_cps", _none)
    assert await gw._h_get_cps({}, {"projectName": "p"}) == gw._read_empty_response("getCPS", "p")

    monkeypatch.setattr(query_repository, "get_master_prd", _none)
    assert await gw._h_get_prd({}, {"projectName": "p"}) == gw._read_empty_response("getPRD", "p")

    monkeypatch.setattr(query_repository, "get_meeting_versions", _empty_list)
    assert await gw._h_get_meeting_versions({}, {"projectName": "p"}) == gw._read_empty_response("getMeetingVersions", "p")

    # 특수 shape
    monkeypatch.setattr(skill_repository, "get_all_skills", _empty_list)
    assert await gw._h_get_all_skill({}, {"projectName": "p"}) == gw._read_empty_response("getAllSkill", "p")

    monkeypatch.setattr(skill_repository, "get_all_skills_full", _empty_list)
    assert await gw._h_get_all_skill_detail({}, {"projectName": "p"}) == gw._read_empty_response("getAllSkillDetail", "p")

    monkeypatch.setattr(skill_repository, "get_skill", _none)
    assert await gw._h_get_skill({}, {"projectName": "p", "id": "x"}) == gw._read_empty_response("getSkill", "p")

    monkeypatch.setattr(skill_repository, "find_duplicate_skill", _dup_empty)
    assert await gw._h_get_duplicate_skill({}, {"projectName": "p", "name": "x"}) == gw._read_empty_response("getDuplicateSkill", "p")

    monkeypatch.setattr(repo_repository, "get_repos", _empty_list)
    assert await gw._h_get_project_repos({}, {"projectName": "p"}) == gw._read_empty_response("getProjectRepos", "p")

    monkeypatch.setattr(lint_repository, "get_last_lint_result", _none)
    assert await gw._h_get_last_lint({}, {"projectName": "p"}) == gw._read_empty_response("getLastLintResult", "p")

    monkeypatch.setattr(lineage_repository, "get_last_lineage", _none)
    assert await gw._h_get_last_lineage({}, {"projectName": "p"}) == gw._read_empty_response("getLastLineage", "p")

    # 그래프 read(getDDD/getSpack/getArchitecture) + getMeetingLogs 는 repository 함수를
    # None 으로 못 바꾼다(get_*_graph 가 항상 객체를 build). neo4j run_cypher 를 빈 결과로
    # 막아 '데이터 없는 프로젝트'의 실제 핸들러 출력을 재현하고 map 과 대조한다.
    # (이 그래프 3종이 이전엔 누락돼 {"result": []} drift 가 안 잡혔던 갭을 메운다.)
    async def _no_rows(*a, **k):
        return []
    monkeypatch.setattr(query_repository.neo4j_client, "run_cypher", _no_rows)
    assert await gw._h_get_ddd({}, {"projectName": "p"}) == gw._read_empty_response("getDDD", "p")
    assert await gw._h_get_spack({}, {"projectName": "p"}) == gw._read_empty_response("getSpack", "p")
    assert await gw._h_get_architecture({}, {"projectName": "p"}) == gw._read_empty_response("getArchitecture", "p")
    assert await gw._h_get_meeting_logs({}, {"projectName": "p", "version": "v1"}) == gw._read_empty_response("getMeetingLogs", "p")

    # getProjectBusy(Redis pool 의존)·getProjectTimeline(since=time.time() 기반으로 매 호출
    # 값이 달라 핸들러-실출력 정확 대조 부적합)은 여기서 제외 — 키/타입은
    # test_read_empty_response_shapes 가 핀 고정한다.
