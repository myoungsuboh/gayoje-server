"""
잡 레벨 master 잠금 통합 — 같은 프로젝트 동시 잡의 merge 직렬화 (2026-06).

[배경] 웹 배치 + 모바일 단건이 같은 프로젝트를 동시에 처리하면 merge 의
"읽기→LLM→쓰기" 가 겹쳐 lost update. jobs.py 가 master_write_lock 으로
프로젝트(scoped key) 단위 직렬화하는지 잡 레벨에서 검증.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core import master_lock
from app.queue import jobs

pytestmark = pytest.mark.asyncio


class FakeRedis:
    """SET NX EX / GET / DELETE 인메모리 — master_lock + _set_job_stage 겸용."""

    def __init__(self):
        self.kv: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        self.kv.pop(key, None)


class _FakeGemini:
    async def generate(self, prompt: str, *, temperature: float = 0.2):
        return SimpleNamespace(text="x", usage=None)


class _FakeNeo:
    async def run_cypher(self, *a, **kw):
        return []


@pytest.fixture(autouse=True)
def _fast_lock(monkeypatch):
    monkeypatch.setattr(master_lock, "_POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(master_lock, "_WAIT_TIMEOUT_SEC", 5.0)


@pytest.fixture
def critical_tracker(monkeypatch):
    """run_cps_pipeline 을 임계구역 추적 fake 로 교체. (active, max_active) 기록."""
    state = {"active": 0, "max_active": 0}

    async def fake_run_cps(ctx, payload):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.05)  # merge 작업 시뮬레이션
        state["active"] -= 1
        from app.pipelines.cps_pipeline import CpsResult
        return CpsResult(
            cps_graph={"nodes": [], "relationships": []},
            mode="first_run", meeting_log_id="ml", delta_cps_id="d",
            master_cps_id="m", diagnostic={},
        )

    monkeypatch.setattr("app.queue.jobs.run_cps_pipeline", fake_run_cps)

    async def fake_persist(*a, **kw):
        pass
    monkeypatch.setattr("app.queue.jobs._persist_token_usage", fake_persist)
    return state


def _arq_ctx(job_id: str, redis):
    return {"job_id": job_id, "gemini": _FakeGemini(), "neo4j": _FakeNeo(),
            "redis": redis}


async def _run_cps(redis, job_id: str, project: str):
    await jobs.cps_pipeline_job(
        _arq_ctx(job_id, redis),
        project_name=project, version="v1", date="2026-06-10",
        meeting_content="hello", user_email="u@b.com",
    )


async def test_same_project_jobs_serialized(critical_tracker):
    """같은 프로젝트 cps 잡 2개 동시 실행 → 임계구역 최대 동시 1 (직렬화)."""
    r = FakeRedis()
    await asyncio.gather(
        _run_cps(r, "job-A", "projX"),
        _run_cps(r, "job-B", "projX"),
    )
    assert critical_tracker["max_active"] == 1, \
        f"같은 프로젝트는 직렬이어야: max_active={critical_tracker['max_active']}"


async def test_different_projects_run_parallel(critical_tracker):
    """다른 프로젝트끼리는 병렬 — 락 키 격리로 블록 안 함."""
    r = FakeRedis()
    await asyncio.gather(
        _run_cps(r, "job-A", "projX"),
        _run_cps(r, "job-B", "projY"),
    )
    assert critical_tracker["max_active"] == 2, \
        f"다른 프로젝트는 병렬이어야: max_active={critical_tracker['max_active']}"


async def test_no_redis_ctx_falls_open(critical_tracker):
    """redis 없는 legacy ctx → 잠금 없이 동작 (기존 테스트/워커 호환)."""
    await asyncio.gather(
        _run_cps(None, "job-A", "projX"),
        _run_cps(None, "job-B", "projX"),
    )
    # fail-open: 직렬화는 안 되지만 (max_active 2 가능) 잡 자체는 정상 완료.
    assert critical_tracker["max_active"] >= 1


async def test_lock_released_after_job_failure(monkeypatch):
    """잡 실패해도 락 해제 — 다음 잡이 즉시 진입 가능."""
    r = FakeRedis()

    async def boom(ctx, payload):
        raise RuntimeError("pipeline failed")
    monkeypatch.setattr("app.queue.jobs.run_cps_pipeline", boom)
    async def fake_persist(*a, **kw):
        pass
    monkeypatch.setattr("app.queue.jobs._persist_token_usage", fake_persist)

    with pytest.raises(RuntimeError):
        await _run_cps(r, "job-A", "projX")
    # 락이 남아있지 않음 — 실패한 잡의 락이 다음 잡을 5분 막는 사고 방지.
    assert not any(k.startswith("harness:lock:master:") for k in r.kv), \
        f"실패 후 락 잔존: {r.kv}"


async def test_team_scoped_key_used(critical_tracker, monkeypatch):
    """team_id 가 있으면 scoped key 로 잠금 — 팀 멤버 간 동시 작업도 같은 락."""
    r = FakeRedis()
    seen_keys: list[str] = []
    orig_set = r.set

    async def spy_set(key, value, **kw):
        seen_keys.append(key)
        return await orig_set(key, value, **kw)
    r.set = spy_set

    await jobs.cps_pipeline_job(
        _arq_ctx("job-A", r),
        project_name="projX", version="v1", date="2026-06-10",
        meeting_content="hello", user_email="u@b.com", team_id="team-123",
    )
    lock_keys = [k for k in seen_keys if k.startswith("harness:lock:master:")]
    assert lock_keys, "락 키가 사용되지 않음"
    # scoped_project 형식: ::team::{team_id}::{name}
    assert "team-123" in lock_keys[0] and "projX" in lock_keys[0]


async def test_post_meeting_merge_holds_lock(monkeypatch):
    """post_meeting 의 MERGE 구간이 락 안에서 실행되는지 — merge fake 가 실행되는
    동안 같은 프로젝트 락 키가 잡혀 있어야 한다."""
    r = FakeRedis()
    lock_held_during_merge: list[bool] = []

    async def fake_extract(*a, **kw):
        # EXTRACT 시점엔 락이 아직 없음 (락 밖 — prefetch 병렬 보존).
        assert "harness:lock:master:projX" not in r.kv, "extract 가 락 안에 있음"
        return {"cps_graph": {"nodes": [], "relationships": []},
                "prd_graph": {}, "prd_markdown": "# PRD"}
    monkeypatch.setattr("app.queue.jobs._get_or_compute_extract", fake_extract)

    async def fake_cps_merge(ctx, payload, cps_graph):
        lock_held_during_merge.append("harness:lock:master:projX" in r.kv)
        await asyncio.sleep(0.02)
        from app.pipelines.cps_pipeline import CpsResult
        return CpsResult(meeting_log_id="ml", delta_cps_id="d", master_cps_id="m",
                         mode="first_run")
    monkeypatch.setattr("app.queue.jobs.run_cps_merge", fake_cps_merge)

    async def fake_prd_compute(ctx, payload, extract):
        lock_held_during_merge.append("harness:lock:master:projX" in r.kv)
        async def commit():
            lock_held_during_merge.append("harness:lock:master:projX" in r.kv)
            return SimpleNamespace(delta_prd_id="dp", master_prd_id="mp",
                                   mode="incremental", diagnostic={})
        return commit
    monkeypatch.setattr("app.queue.jobs._prd_merge_compute", fake_prd_compute)

    async def noop(*a, **kw):
        pass
    monkeypatch.setattr("app.queue.jobs._maybe_trigger_auto_cleanup", noop)
    monkeypatch.setattr("app.queue.jobs._persist_token_usage", noop)

    result = await jobs.post_meeting_pipeline_job(
        _arq_ctx("job-PM", r),
        project_name="projX", version="v1", date="2026-06-10",
        meeting_content="hello", user_email="u@b.com",
    )
    # cps merge / prd compute / prd commit 세 지점 모두 락 보유 중이었어야.
    assert lock_held_during_merge == [True, True, True], lock_held_during_merge
    # 종료 후 락 해제 + 결과 정상.
    assert "harness:lock:master:projX" not in r.kv
    assert result["cps"]["meeting_log_id"] == "ml"
    assert result["prd"]["mode"] == "incremental"


async def test_job_finally_releases_project_marker(critical_tracker, monkeypatch):
    """잡 종료 시 finally 가 프로젝트 inflight 마커를 해제하는지 — 배치의 다음
    항목이 409 안 맞고 바로 enqueue 되려면 필수."""
    from app.core import concurrency
    released = []

    async def spy_release_project(redis, project_key, task_id):
        released.append((project_key, task_id))
    monkeypatch.setattr(concurrency, "release_project", spy_release_project)

    r = FakeRedis()
    await jobs.cps_pipeline_job(
        _arq_ctx("job-R", r),
        project_name="projX", version="v1", date="2026-06-10",
        meeting_content="hello", user_email="u@b.com", team_id="team-7",
    )
    # scoped key + job_id 로 해제됨.
    assert len(released) == 1
    key, task = released[0]
    assert "team-7" in key and "projX" in key and task == "job-R"


async def test_delete_meeting_job_serialized_with_lock(monkeypatch):
    """[감사 G2] delete 잡도 같은 프로젝트 락으로 직렬화 — merge 와 delete 가
    동시에 master 를 만지던 race 차단."""
    r = FakeRedis()
    lock_held: list[bool] = []

    async def fake_delete(ctx, payload):
        lock_held.append("harness:lock:master:projX" in r.kv)
        return SimpleNamespace(
            status="success", message="", project_name="projX",
            deleted_version="v1", remaining_cps_count=0, remaining_prd_count=0,
            cps_master_rebuilt=False, prd_master_rebuilt=False,
        )
    monkeypatch.setattr("app.queue.jobs.run_delete_meeting_pipeline", fake_delete)
    async def noop(*a, **kw):
        pass
    monkeypatch.setattr("app.queue.jobs._persist_token_usage", noop)

    result = await jobs.delete_meeting_job(
        _arq_ctx("job-DEL", r), project_name="projX", version="v1",
        user_email="u@b.com",
    )
    assert lock_held == [True]
    assert "harness:lock:master:projX" not in r.kv
    assert result["status"] == "success"


async def test_delete_meeting_job_releases_project_marker(monkeypatch):
    """[감사 G2] delete 잡 finally 가 프로젝트 inflight 마커 해제."""
    from app.core import concurrency
    released = []

    async def spy(redis, project_key, task_id):
        released.append((project_key, task_id))
    monkeypatch.setattr(concurrency, "release_project", spy)

    async def fake_delete(ctx, payload):
        return SimpleNamespace(
            status="success", message="", project_name="projX",
            deleted_version="v1", remaining_cps_count=0, remaining_prd_count=0,
            cps_master_rebuilt=False, prd_master_rebuilt=False,
        )
    monkeypatch.setattr("app.queue.jobs.run_delete_meeting_pipeline", fake_delete)
    async def noop(*a, **kw):
        pass
    monkeypatch.setattr("app.queue.jobs._persist_token_usage", noop)

    await jobs.delete_meeting_job(
        _arq_ctx("job-DEL2", FakeRedis()), project_name="projX", version="v1",
        user_email="u@b.com",
    )
    assert released == [("projX", "job-DEL2")]


async def test_design_job_serialized_with_lock(monkeypatch):
    """[2026-06 후속] design 도 같은 프로젝트 락 — 설계 그래프 Wipe-and-Redraw 가
    동시 실행으로 stage 혼합되는 것 차단 + merge 와의 상호 배제."""
    r = FakeRedis()
    lock_held: list[bool] = []

    async def fake_design(ctx, payload, **kw):
        lock_held.append("harness:lock:master:projX" in r.kv)
        return SimpleNamespace(
            project_name="projX", master_prd_id="mp",
            spack={}, ddd={}, architecture={}, health={}, diagnostic={},
        )
    monkeypatch.setattr("app.queue.jobs.run_design_pipeline", fake_design)

    # [2026-06-10 병렬 autofill 구조] 결과 회수(_finish_parallel_autofill — 노드
    # 저장)도 락 안에서 실행돼야 — 같은 그래프 패치.
    async def fake_finish(state, project_name, team_id):
        lock_held.append("harness:lock:master:projX" in r.kv)
        return {"total": 0, "targetCount": 0}
    monkeypatch.setattr("app.queue.jobs._finish_parallel_autofill", fake_finish)

    async def noop(*a, **kw):
        pass
    monkeypatch.setattr("app.queue.jobs._persist_token_usage", noop)

    result = await jobs.design_pipeline_job(
        _arq_ctx("job-DSN", r), project_name="projX", user_email="u@b.com",
    )
    assert lock_held == [True, True]  # design 본체 + autofill 회수 모두 락 보유
    assert "harness:lock:master:projX" not in r.kv  # 종료 후 해제
    assert result["project_name"] == "projX"


async def test_design_job_releases_project_marker(monkeypatch):
    """design 잡 finally 가 프로젝트 inflight 마커 해제 — 다음 작업이 409 안 맞게."""
    from app.core import concurrency
    released = []

    async def spy(redis, project_key, task_id):
        released.append((project_key, task_id))
    monkeypatch.setattr(concurrency, "release_project", spy)

    async def fake_design(ctx, payload, **kw):
        return SimpleNamespace(
            project_name="projX", master_prd_id="mp",
            spack={}, ddd={}, architecture={}, health={}, diagnostic={},
        )
    monkeypatch.setattr("app.queue.jobs.run_design_pipeline", fake_design)
    monkeypatch.setattr("app.queue.jobs.settings.DESIGN_AUTOFILL_API_SPECS", False)
    async def noop(*a, **kw):
        pass
    monkeypatch.setattr("app.queue.jobs._persist_token_usage", noop)

    await jobs.design_pipeline_job(
        _arq_ctx("job-DSN2", FakeRedis()), project_name="projX",
        user_email="u@b.com", team_id="team-3",
    )
    assert len(released) == 1
    key, task = released[0]
    assert "team-3" in key and "projX" in key and task == "job-DSN2"


async def test_autofill_job_serialized_with_lock(monkeypatch):
    """[2026-06 후속] autofill 도 같은 락 — design 의 wipe 와 겹쳐 패치가 유실되는
    것 차단. SPACK 읽기도 락 안 (wipe 직후 빈 그래프 읽기 방지)."""
    r = FakeRedis()
    lock_held: list[bool] = []

    async def fake_spack(project_name, team_id=""):
        lock_held.append("harness:lock:master:projX" in r.kv)
        return SimpleNamespace(apis=[])
    monkeypatch.setattr(
        "app.service.query_repository.get_spack_graph", fake_spack,
    )

    async def fake_autofill(ctx, payload):
        lock_held.append("harness:lock:master:projX" in r.kv)
        return SimpleNamespace(apis=[], meta={"total": 0})
    monkeypatch.setattr(
        "app.pipelines.api_spec_autofill_pipeline.run_api_spec_autofill_pipeline",
        fake_autofill,
    )

    async def noop(*a, **kw):
        pass
    monkeypatch.setattr("app.queue.jobs._persist_token_usage", noop)

    result = await jobs.autofill_api_specs_job(
        _arq_ctx("job-AF", r), project_name="projX", user_email="u@b.com",
    )
    assert lock_held == [True, True]  # 조회 + 파이프라인 모두 락 보유
    assert "harness:lock:master:projX" not in r.kv
    assert result["status"] == "success"


async def test_autofill_job_releases_project_marker_team_scoped(monkeypatch):
    """autofill 잡 finally 가 team scoped key 로 마커 해제 — team_id 전달이
    게이트 키와 일치하는지 (이전엔 team_id 자체가 안 넘어왔음)."""
    from app.core import concurrency
    released = []

    async def spy(redis, project_key, task_id):
        released.append((project_key, task_id))
    monkeypatch.setattr(concurrency, "release_project", spy)

    async def fake_spack(project_name, team_id=""):
        assert team_id == "team-5"  # SPACK 조회 스코프도 team — 빈 그래프 버그 픽스
        return SimpleNamespace(apis=[])
    monkeypatch.setattr(
        "app.service.query_repository.get_spack_graph", fake_spack,
    )

    async def fake_autofill(ctx, payload):
        return SimpleNamespace(apis=[], meta={})
    monkeypatch.setattr(
        "app.pipelines.api_spec_autofill_pipeline.run_api_spec_autofill_pipeline",
        fake_autofill,
    )
    async def noop(*a, **kw):
        pass
    monkeypatch.setattr("app.queue.jobs._persist_token_usage", noop)

    await jobs.autofill_api_specs_job(
        _arq_ctx("job-AF2", FakeRedis()), project_name="projX",
        user_email="u@b.com", team_id="team-5",
    )
    assert len(released) == 1
    key, task = released[0]
    assert "team-5" in key and "projX" in key and task == "job-AF2"


async def test_cleanup_job_serialized_with_lock(monkeypatch):
    """[감사 G3] cleanup(master PRD 재작성)도 같은 락 — auto-trigger 된 cleanup
    이 다음 배치 항목 merge 와 겹치는 케이스 직렬화."""
    r = FakeRedis()
    lock_held: list[bool] = []

    async def fake_cleanup(ctx, payload):
        lock_held.append("harness:lock:master:projX" in r.kv)
        return SimpleNamespace(
            project_name="projX", before_size=10, after_size=5, reduction_pct=50.0,
            master_prd_id="mp", cleaned_markdown="", original_markdown="",
            dry_run=False,
        )
    monkeypatch.setattr("app.queue.jobs.run_cleanup_master_prd_pipeline", fake_cleanup)
    async def noop(*a, **kw):
        pass
    monkeypatch.setattr("app.queue.jobs._persist_token_usage", noop)

    result = await jobs.cleanup_master_prd_job(
        _arq_ctx("job-CL", r), project_name="projX", dry_run=False,
        user_email="u@b.com",
    )
    assert lock_held == [True]
    assert "harness:lock:master:projX" not in r.kv
    assert result["status"] == "success"
