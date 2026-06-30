"""
/api/gateway/* compat dispatcher response shape regression tests.

frontend(C:\\project\\harness\\src\\store\\harness.js, plan.vue, *Tab.vue)이
기대하는 응답 모양을 백엔드가 유지하는지 검증.

핵심 검증 포인트:
- getCPS/PRD/DDD/Spack/Architecture/MeetingLogs : `result` 가 list (frontend `.result[0]`)
- getMeetingVersions                            : `result` 가 list
- getProjectRepos                               : top-level `{repos, count}` (wrap 없음)
- getLastLintResult / getLastLineage            : top-level `{found, result, savedAt}`
"""
from __future__ import annotations

import pytest

from app.api import gateway_compat_routes as pt
from app.service.lineage_repository import LineageResult
from app.service.lint_repository import LintResult
from app.service.query_repository import (
    ArchitectureGraph,
    CpsMaster,
    DddGraph,
    MeetingLog,
    PrdMaster,
    SpackGraph,
)


@pytest.mark.asyncio
async def test_get_cps_returns_array_in_result(monkeypatch):
    monkeypatch.setattr(
        pt.query_repository,
        "get_master_cps",
        lambda p, team_id="": _async(
            CpsMaster(
                master_id="m1", version="v1", content="md", last_updated=1, absorbed_cps_ids=[]
            )
        ),
    )
    out = await pt._h_get_cps({}, {"projectName": "x"})
    assert isinstance(out["result"], list) and len(out["result"]) == 1
    assert out["result"][0]["master_id"] == "m1"


@pytest.mark.asyncio
async def test_get_cps_none_returns_empty_array(monkeypatch):
    monkeypatch.setattr(pt.query_repository, "get_master_cps", lambda p, team_id="": _async(None))
    out = await pt._h_get_cps({}, {"projectName": "x"})
    assert out == {"result": []}


@pytest.mark.asyncio
async def test_get_prd_returns_array(monkeypatch):
    monkeypatch.setattr(
        pt.query_repository,
        "get_master_prd",
        lambda p, team_id="": _async(
            PrdMaster(
                master_prd_id="p1", prd_content="x", last_updated=1,
                related_master_cps_id="c1", absorbed_prd_ids=[],
            )
        ),
    )
    out = await pt._h_get_prd({}, {"projectName": "x"})
    assert isinstance(out["result"], list)
    assert out["result"][0]["master_prd_id"] == "p1"


@pytest.mark.asyncio
async def test_get_design_graphs_return_arrays(monkeypatch):
    """SpackTab/DddTab/ArchitectureTab 가 `.result[0]` 로 읽음."""
    monkeypatch.setattr(
        pt.query_repository, "get_spack_graph",
        lambda p, team_id="": _async(SpackGraph(apis=[], entities=[], policies=[], internal_rels=[], implement_rels=[])),
    )
    monkeypatch.setattr(
        pt.query_repository, "get_ddd_graph",
        lambda p, team_id="": _async(DddGraph(contexts=[], aggregates=[], domain_entities=[], domain_events=[], internal_rels=[], trigger_rels=[])),
    )
    monkeypatch.setattr(
        pt.query_repository, "get_architecture_graph",
        lambda p, team_id="": _async(ArchitectureGraph(services=[], databases=[], connections=[])),
    )
    for handler in (pt._h_get_spack, pt._h_get_ddd, pt._h_get_architecture):
        out = await handler({}, {"projectName": "x"})
        assert isinstance(out["result"], list) and len(out["result"]) == 1, handler.__name__


@pytest.mark.asyncio
async def test_get_meeting_logs_returns_array(monkeypatch):
    monkeypatch.setattr(
        pt.query_repository, "get_meeting_log",
        lambda p, v, team_id="": _async(MeetingLog(version="v1", date="2026-01-01", meeting_content="c", created_at=1)),
    )
    out = await pt._h_get_meeting_logs({}, {"projectName": "x", "version": "v1"})
    assert isinstance(out["result"], list)
    assert out["result"][0]["version"] == "v1"


@pytest.mark.asyncio
async def test_get_meeting_versions_returns_array(monkeypatch):
    monkeypatch.setattr(
        pt.query_repository, "get_meeting_versions", lambda p, team_id="": _async([])
    )
    out = await pt._h_get_meeting_versions({}, {"projectName": "x"})
    assert out == {"result": []}


@pytest.mark.asyncio
async def test_get_project_repos_top_level_shape(monkeypatch):
    """frontend: const repos = response.data.repos — wrap 금지."""
    monkeypatch.setattr(pt.repo_repository, "get_repos", lambda p, team_id="": _async([]))
    out = await pt._h_get_project_repos({}, {"projectName": "x"})
    assert "repos" in out and "count" in out
    assert "result" not in out  # wrap 되면 안 됨
    assert out["repos"] == [] and out["count"] == 0


@pytest.mark.asyncio
async def test_get_last_lint_shape_found(monkeypatch):
    """frontend: body.found && body.result && body.savedAt."""
    monkeypatch.setattr(
        pt.lint_repository, "get_last_lint_result",
        lambda p, u, team_id="": _async(LintResult(
            id="L1", project="x", github_url="u", score=80, scanned_files=1,
            rules_checked=2, violations=0, cases=[], saved_at=12345,
        )),
    )
    out = await pt._h_get_last_lint({}, {"projectName": "x", "githubUrl": "u"})
    assert out["found"] is True
    assert isinstance(out["result"], dict)
    assert out["savedAt"] == 12345


@pytest.mark.asyncio
async def test_get_last_lint_shape_not_found(monkeypatch):
    monkeypatch.setattr(pt.lint_repository, "get_last_lint_result", lambda p, u, team_id="": _async(None))
    out = await pt._h_get_last_lint({}, {"projectName": "x", "githubUrl": "u"})
    assert out == {"found": False, "result": None, "savedAt": None}


@pytest.mark.asyncio
async def test_get_last_lineage_shape_found(monkeypatch):
    monkeypatch.setattr(
        pt.lineage_repository, "get_last_lineage",
        lambda p, team_id="": _async(LineageResult(
            id="LN1", project="x", summary="", storiesCount=0, aggregatesCount=0,
            apisCount=0, servicesCount=0, totalImpls=0, missingCount=0,
            data={}, saved_at=999,
        )),
    )
    out = await pt._h_get_last_lineage({}, {"projectName": "x"})
    assert out["found"] is True
    assert out["savedAt"] == 999
    assert isinstance(out["result"], dict)


@pytest.mark.asyncio
async def test_get_last_lineage_not_found(monkeypatch):
    monkeypatch.setattr(pt.lineage_repository, "get_last_lineage", lambda p, team_id="": _async(None))
    out = await pt._h_get_last_lineage({}, {"projectName": "x"})
    assert out == {"found": False, "result": None, "savedAt": None}


@pytest.mark.asyncio
async def test_create_md_reads_project_from_query_and_returns_flat_shape(monkeypatch):
    """
    frontend(ArchitectureTab)는 GET `/createMD?projectName=X` 로 호출 → body 비어있음.
    응답은 `{spack_md, ddd_md, arch_md, project_name}` flat (wrap 없음).
    """
    seen = {}

    async def fake_pipeline(ctx, payload):
        from app.pipelines.create_md_pipeline import CreateMdResult
        seen["project"] = payload.project_name
        return CreateMdResult(
            project_name=payload.project_name,
            spack_md="# SPACK", ddd_md="# DDD", arch_md="# ARCH",
            diagnostic={"spack_size": 1},
        )

    monkeypatch.setattr(pt, "run_create_md_pipeline", fake_pipeline)
    # 2026-05: _h_create_md 가 tracked_pipeline_context 로 변경됨 — 그것 mock.
    monkeypatch.setattr(pt, "tracked_pipeline_context", _fake_tracked_ctx)

    out = await pt._h_create_md({}, {"projectName": "myproj"}, user_email="t@b.com")
    assert seen["project"] == "myproj"  # query 에서 정확히 읽혔는지
    assert "result" not in out  # wrap 되면 안 됨
    assert out["spack_md"] == "# SPACK"
    assert out["ddd_md"] == "# DDD"
    assert out["arch_md"] == "# ARCH"
    assert out["project_name"] == "myproj"


@pytest.mark.asyncio
async def test_create_design_reads_project_from_query_and_returns_success(monkeypatch):
    """
    frontend(design.vue)는 POST `/createSpack?projectName=X` 를 body=null 로 호출.
    `response.data.result === 'success'` 체크.
    """
    seen = {}

    async def fake_pipeline(ctx, payload, **kwargs):
        # check_cancel kwarg 도 허용 (중지 지원 시그니처 변경)
        from app.pipelines.design_pipeline import DesignResult
        seen["project"] = payload.project_name
        return DesignResult(
            project_name=payload.project_name,
            master_prd_id="prd_1",
            spack={"apis": [], "entities": [], "policies": []},
            ddd={"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []},
            architecture={"services": [], "databases": [], "connections": []},
            diagnostic={},
        )

    monkeypatch.setattr(pt, "run_design_pipeline", fake_pipeline)
    # 2026-05: _h_create_design 가 tracked_pipeline_context 로 변경됨 — 그것 mock.
    monkeypatch.setattr(pt, "tracked_pipeline_context", _fake_tracked_ctx)

    # body=None (axios.post(url, null, {params:{projectName}}) 시뮬레이션)
    out = await pt._h_create_design({}, {"projectName": "myproj"}, user_email="t@b.com")
    assert seen["project"] == "myproj"
    assert out["result"] == "success"
    assert out["project_name"] == "myproj"


@pytest.mark.asyncio
async def test_create_design_returns_cancelled_when_client_disconnects(monkeypatch):
    """
    중지 기능 — pipeline 이 DesignPipelineCancelled 를 raise 하면 핸들러는
    `{result: "cancelled"}` 를 돌려준다. 최종 commit 전에 빠져나오므로 기존
    Spack/DDD/Architecture 데이터는 그대로 보존된다.
    """
    from app.pipelines.design_pipeline import DesignPipelineCancelled

    async def cancelled_pipeline(ctx, payload, *, check_cancel=None):
        # 실제 pipeline 이 stage 사이마다 check_cancel 호출하다가 True 받으면
        # raise — 여기서는 즉시 raise 로 시뮬레이션.
        raise DesignPipelineCancelled("spack_llm")

    class _FakeRequest:
        async def is_disconnected(self):
            return True

    monkeypatch.setattr(pt, "run_design_pipeline", cancelled_pipeline)
    monkeypatch.setattr(pt, "tracked_pipeline_context", _fake_tracked_ctx)

    out = await pt._h_create_design(
        {}, {"projectName": "myproj"}, user_email="t@b.com",
        request=_FakeRequest(),
    )
    assert out["result"] == "cancelled"
    assert out["project_name"] == "myproj"
    assert out["stage"] == "spack_llm"


@pytest.mark.asyncio
async def test_get_duplicate_skill_reads_query_and_returns_camelcase(monkeypatch):
    """
    frontend: GET `/getDuplicateSkill?projectName=X&newSkillId=Y`.
    응답: `{isDuplicate, existingIds}` (camelCase).
    """
    seen = {}

    async def fake_by_id(project, skill_id):
        seen["project"] = project
        seen["id"] = skill_id
        return {"is_duplicate": True, "existing_ids": ["A", "B"]}

    monkeypatch.setattr(pt.skill_repository, "find_duplicate_skill_by_id", fake_by_id)

    out = await pt._h_get_duplicate_skill(
        {}, {"projectName": "myproj", "newSkillId": "SKL-01"}
    )
    assert seen == {"project": "myproj", "id": "SKL-01"}
    assert out == {"isDuplicate": True, "existingIds": ["A", "B"]}


@pytest.mark.asyncio
async def test_get_duplicate_skill_fallback_to_name(monkeypatch):
    """`newSkillId` 가 없으면 `newSkillName` 으로 fallback."""
    seen = {}

    async def fake_by_name(project, skill_name):
        seen["called"] = "by_name"
        seen["name"] = skill_name
        return {"is_duplicate": False, "existing_ids": []}

    monkeypatch.setattr(pt.skill_repository, "find_duplicate_skill", fake_by_name)

    out = await pt._h_get_duplicate_skill(
        {"newSkillName": "Auth"}, {"projectName": "x"}
    )
    assert seen == {"called": "by_name", "name": "Auth"}
    assert out == {"isDuplicate": False, "existingIds": []}


# ─── helpers ──────────────────────────────────────────────


async def _coro(v):
    return v


def _async(v):
    return _coro(v)


# 2026-05: _h_create_md / _h_create_design 등이 tracked_pipeline_context 를 쓰므로
# 테스트가 LLM/Neo4j env 없이도 통과하도록 async context manager 를 stub.
from contextlib import asynccontextmanager
from types import SimpleNamespace


@asynccontextmanager
async def _fake_tracked_ctx(*, user_email=None, idempotency_key=None, team_id=""):
    yield SimpleNamespace(
        gemini=SimpleNamespace(),
        neo4j=SimpleNamespace(),
        idempotency_key=idempotency_key,
        team_id=team_id,
    )
