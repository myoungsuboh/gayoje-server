"""
api_spec_autofill_pipeline 단위 + e2e fakes.

apiSpecAutofill 동작 검증:
- error_cases/auth 가 이미 명시된 API 는 LLM 미호출 (건너뜀, 기존 값 보존)
- 빈 API 만 LLM 호출 — N개 병렬
- 생성 항목에 source=ai_draft, reviewed=False 메타가 부착되는지
- LLM 실패/빈 결과여도 전체가 안 깨짐 (generated=False, 부분 실패 격리)
- 단일 노드 부분 저장 호출 + 저장 실패 격리
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from app.pipelines.api_spec_autofill_pipeline import (
    ApiSpecInput,
    AutofillInput,
    _split_targets,
    run_api_spec_autofill_pipeline,
)
from app.pipelines.base import PipelineContext
from tests.conftest import FakeNeo4j


# ─── _split_targets (순수 동기 함수) ──────────────────────────


def test_split_targets_separates_empty_and_filled():
    apis = [
        # 둘 다 빈 → 대상
        ApiSpecInput(id="A", name="a", error_cases=[], auth={}),
        # error_cases 있음 + auth 명시 → 건너뜀
        ApiSpecInput(
            id="B", name="b",
            error_cases=[{"status": 404}],
            auth={"description": "로그인 필요"},
        ),
        # error_cases 비었지만 auth 명시 → 여전히 대상 (error_cases 빔)
        ApiSpecInput(id="C", name="c", error_cases=[], auth={"description": "x"}),
        # error_cases 있지만 auth 미명시 → 대상 (auth 빔)
        ApiSpecInput(id="D", name="d", error_cases=[{"status": 500}], auth={}),
    ]
    targets, skipped = _split_targets(apis)
    assert [a.id for a in targets] == ["A", "C", "D"]
    assert [a.id for a in skipped] == ["B"]


# ─── Fake Gemini: API 이름으로 분기 ────────────────────────────


def _spec_json(error_cases: List[Dict[str, Any]], auth: Dict[str, Any]) -> str:
    return json.dumps({"error_cases": error_cases, "auth": auth})


class _PerApiGemini:
    """프롬프트 안의 API 이름으로 분기해 spec JSON 반환. 호출 이력 기록."""

    def __init__(self, name_to_json: Dict[str, str]) -> None:
        self._map = name_to_json
        self.calls: List[str] = []

    async def generate(self, prompt: str, *, temperature: float = 0.2, response_schema=None):
        self.calls.append(prompt)
        for name, body in self._map.items():
            if name in prompt:
                from tests.conftest import _FakeResult
                return _FakeResult(text=body)
        from tests.conftest import _FakeResult
        return _FakeResult(text="{}")


def _ctx(gemini) -> PipelineContext:
    return PipelineContext(gemini=gemini, neo4j=FakeNeo4j(), idempotency_key="t")


@pytest.fixture(autouse=True)
def _stub_save(monkeypatch):
    """update_api_error_and_auth 를 stub — 호출 인자 기록, 기본 True 반환.

    실제 Neo4j 접근을 막고, 저장 호출이 단일 노드 단위로 일어나는지 검증.
    """
    from app.service import query_repository

    saved: List[Dict[str, Any]] = []

    async def _fake_save(project_name, api_id, error_cases, auth, team_id=""):
        saved.append({
            "project": project_name, "id": api_id,
            "error_cases": error_cases, "auth": auth, "team_id": team_id,
        })
        return True

    monkeypatch.setattr(query_repository, "update_api_error_and_auth", _fake_save)
    return saved


# ─── e2e ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_filled_only_calls_empty(_stub_save):
    """명시된 API 는 LLM 미호출, 빈 것만 호출 + 메타 부착 + 부분 저장."""
    apis = [
        ApiSpecInput(
            id="API-01", name="작업 생성", method="POST",
            endpoint="/api/v1/tasks", description="작업 추가",
            error_cases=[], auth={},
        ),
        ApiSpecInput(
            id="API-02", name="작업 조회", method="GET",
            endpoint="/api/v1/tasks", description="목록",
            error_cases=[{"status": 401}],            # error_cases 있음
            auth={"description": "로그인 필요"},        # auth 명시
        ),
        ApiSpecInput(
            id="API-03", name="작업 삭제", method="DELETE",
            endpoint="/api/v1/tasks/{id}", description="삭제",
            error_cases=[], auth={},
        ),
    ]
    gemini = _PerApiGemini({
        "작업 생성": _spec_json(
            [{"status": 401, "code": "AUTH_REQUIRED", "message": "로그인 필요"},
             {"status": 422, "code": "VALIDATION", "message": "검증 실패"}],
            {"required": True, "required_roles": ["owner"],
             "description": "JWT 로그인 필요"},
        ),
        "작업 삭제": _spec_json(
            [{"status": 404, "code": "NOT_FOUND", "message": "없음"}],
            {"required": True, "description": "본인만 삭제"},
        ),
        "작업 조회": _spec_json([{"status": 500}], {"description": "절대-호출되면-안됨"}),
    })
    ctx = _ctx(gemini)

    result = await run_api_spec_autofill_pipeline(
        ctx, AutofillInput(project_name="proj", apis=apis)
    )

    by_id = {f.id: f for f in result.apis}
    # 원본 순서 유지
    assert [f.id for f in result.apis] == ["API-01", "API-02", "API-03"]
    # 빈 것만 생성됨
    assert by_id["API-01"].generated is True
    assert by_id["API-03"].generated is True
    # 명시된 것은 건너뜀 + 기존 값 보존
    assert by_id["API-02"].generated is False
    assert by_id["API-02"].error_cases == [{"status": 401}]
    # LLM 은 빈 API 2개에 대해서만 호출됨 (API-02 미호출)
    assert len(gemini.calls) == 2
    # 메타 카운트
    assert result.meta["total"] == 3
    assert result.meta["targetCount"] == 2
    assert result.meta["skippedCount"] == 1
    assert result.meta["generatedCount"] == 2
    assert result.meta["savedCount"] == 2
    # 부분 저장이 생성된 2개에 대해서만 호출됨
    assert {s["id"] for s in _stub_save} == {"API-01", "API-03"}


@pytest.mark.asyncio
async def test_generated_items_marked_ai_draft(_stub_save):
    """생성된 error_cases 각 항목과 auth 에 source=ai_draft, reviewed=False 부착."""
    apis = [
        ApiSpecInput(id="API-01", name="조회", method="GET",
                     endpoint="/x/{id}", description="단건", error_cases=[], auth={}),
    ]
    gemini = _PerApiGemini({
        "조회": _spec_json(
            [{"status": 404, "code": "NOT_FOUND", "message": "없음"},
             {"status": 500, "code": "INTERNAL", "message": "오류"}],
            {"required": True, "required_roles": ["owner"], "description": "본인만"},
        ),
    })
    result = await run_api_spec_autofill_pipeline(
        _ctx(gemini), AutofillInput(project_name="proj", apis=apis)
    )
    f = result.apis[0]
    assert f.generated is True
    # error_cases 모든 항목에 메타
    assert len(f.error_cases) == 2
    for c in f.error_cases:
        assert c["source"] == "ai_draft"
        assert c["reviewed"] is False
    # auth 에도 메타
    assert f.auth["source"] == "ai_draft"
    assert f.auth["reviewed"] is False
    assert f.auth["description"] == "본인만"


@pytest.mark.asyncio
async def test_parallel_gather_structure(_stub_save):
    """대상 N개가 asyncio.gather 로 병렬 호출되는지 — 동시성 검증."""
    in_flight = 0
    max_concurrent = 0
    gate = asyncio.Event()

    class _ConcurrentGemini:
        def __init__(self) -> None:
            self.calls: List[str] = []

        async def generate(self, prompt, *, temperature=0.2, response_schema=None):
            nonlocal in_flight, max_concurrent
            self.calls.append(prompt)
            in_flight += 1
            max_concurrent = max(max_concurrent, in_flight)
            if in_flight >= 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), timeout=2.0)
            in_flight -= 1
            from tests.conftest import _FakeResult
            return _FakeResult(text=_spec_json(
                [{"status": 500}], {"description": "x"}
            ))

    apis = [
        ApiSpecInput(id="A", name="a", error_cases=[], auth={}),
        ApiSpecInput(id="B", name="b", error_cases=[], auth={}),
    ]
    gemini = _ConcurrentGemini()
    result = await run_api_spec_autofill_pipeline(
        _ctx(gemini), AutofillInput(project_name="proj", apis=apis)
    )
    assert max_concurrent == 2, "두 LLM 호출이 동시에 in-flight 여야 함 (gather 병렬)"
    assert all(f.generated for f in result.apis)


@pytest.mark.asyncio
async def test_llm_empty_result_marks_not_generated(_stub_save):
    """LLM 이 빈/무의미 결과를 주면 generated=False, 나머지는 정상 — 부분 실패 격리."""
    apis = [
        ApiSpecInput(id="A", name="good", error_cases=[], auth={}),
        ApiSpecInput(id="B", name="bad", error_cases=[], auth={}),
    ]
    gemini = _PerApiGemini({
        "good": _spec_json([{"status": 404, "code": "X"}], {"description": "정상"}),
        "bad": "{}",  # 빈 dict → 생성 실패로 간주
    })
    result = await run_api_spec_autofill_pipeline(
        _ctx(gemini), AutofillInput(project_name="proj", apis=apis)
    )
    by_id = {f.id: f for f in result.apis}
    assert by_id["A"].generated is True
    assert by_id["B"].generated is False
    assert by_id["B"].error_cases == []
    assert result.meta["generatedCount"] == 1
    # 실패한 B 는 저장 미호출
    assert {s["id"] for s in _stub_save} == {"A"}


@pytest.mark.asyncio
async def test_llm_exception_isolated(_stub_save):
    """한 API LLM 호출이 예외를 던져도 전체 배치가 안 깨짐."""
    class _FlakyGemini:
        async def generate(self, prompt, *, temperature=0.2, response_schema=None):
            if "boom" in prompt:
                raise RuntimeError("LLM down")
            from tests.conftest import _FakeResult
            return _FakeResult(text=_spec_json(
                [{"status": 500, "code": "X"}], {"description": "ok"}
            ))

    apis = [
        ApiSpecInput(id="A", name="ok-api", error_cases=[], auth={}),
        ApiSpecInput(id="B", name="boom", error_cases=[], auth={}),
    ]
    result = await run_api_spec_autofill_pipeline(
        _ctx(_FlakyGemini()), AutofillInput(project_name="proj", apis=apis)
    )
    by_id = {f.id: f for f in result.apis}
    assert by_id["A"].generated is True
    assert by_id["B"].generated is False
    assert result.meta["generatedCount"] == 1


@pytest.mark.asyncio
async def test_save_failure_isolated(monkeypatch):
    """단일 노드 저장이 False(노드 없음)/예외여도 격리 — generated 는 유지, saved=False."""
    from app.service import query_repository

    async def _save(project_name, api_id, error_cases, auth, team_id=""):
        if api_id == "B":
            return False           # 노드 없음
        if api_id == "C":
            raise RuntimeError("neo4j down")
        return True

    monkeypatch.setattr(query_repository, "update_api_error_and_auth", _save)

    apis = [
        ApiSpecInput(id="A", name="a", error_cases=[], auth={}),
        ApiSpecInput(id="B", name="b", error_cases=[], auth={}),
        ApiSpecInput(id="C", name="c", error_cases=[], auth={}),
    ]
    gemini = _PerApiGemini({
        "a": _spec_json([{"status": 500, "code": "X"}], {"description": "ok"}),
        "b": _spec_json([{"status": 500, "code": "X"}], {"description": "ok"}),
        "c": _spec_json([{"status": 500, "code": "X"}], {"description": "ok"}),
    })
    result = await run_api_spec_autofill_pipeline(
        _ctx(gemini), AutofillInput(project_name="proj", apis=apis)
    )
    by_id = {f.id: f for f in result.apis}
    # 모두 생성은 됨
    assert all(by_id[i].generated for i in ("A", "B", "C"))
    # 저장 성공은 A 만
    assert by_id["A"].saved is True
    assert by_id["B"].saved is False
    assert by_id["C"].saved is False
    assert result.meta["generatedCount"] == 3
    assert result.meta["savedCount"] == 1


@pytest.mark.asyncio
async def test_no_targets_no_llm_calls(_stub_save):
    """모든 API 가 이미 명시돼 있으면 LLM 호출 0 + 저장 0."""
    apis = [
        ApiSpecInput(id="A", name="a",
                     error_cases=[{"status": 404}], auth={"description": "x"}),
        ApiSpecInput(id="B", name="b",
                     error_cases=[{"status": 500}], auth={"required_roles": ["admin"]}),
    ]
    gemini = _PerApiGemini({"a": "{}", "b": "{}"})
    result = await run_api_spec_autofill_pipeline(
        _ctx(gemini), AutofillInput(project_name="proj", apis=apis)
    )
    assert gemini.calls == []
    assert all(not f.generated for f in result.apis)
    assert result.meta["targetCount"] == 0
    assert _stub_save == []


@pytest.mark.asyncio
async def test_empty_apis_returns_empty(_stub_save):
    gemini = _PerApiGemini({})
    result = await run_api_spec_autofill_pipeline(
        _ctx(gemini), AutofillInput(project_name="proj", apis=[])
    )
    assert result.apis == []
    assert result.meta["total"] == 0
    assert gemini.calls == []


@pytest.mark.asyncio
async def test_emits_progress_stage_markers(_stub_save):
    """[progress] FE 진행바가 작업량 기반으로 차도록 완료 API 수 + saving 단계를
    stage 마커로 emit. 대상 2건 → generating:0/2 → 2/2 (순서 무관) + saving."""
    apis = [
        ApiSpecInput(id="A", name="alpha", error_cases=[], auth={}),
        ApiSpecInput(id="B", name="beta", error_cases=[], auth={}),
    ]
    gemini = _PerApiGemini({
        "alpha": _spec_json([{"status": 404}], {"description": "x"}),
        "beta": _spec_json([{"status": 401}], {"description": "y"}),
    })
    stages: List[str] = []

    async def _record(stage: str) -> None:
        stages.append(stage)

    ctx = PipelineContext(
        gemini=gemini, neo4j=FakeNeo4j(), idempotency_key="t", stage_callback=_record
    )
    await run_api_spec_autofill_pipeline(
        ctx, AutofillInput(project_name="proj", apis=apis)
    )

    assert "autofill:generating:0/2" in stages
    assert "autofill:generating:2/2" in stages   # 마지막 완료 카운트
    assert "autofill:saving" in stages
    # saving 은 모든 generating 뒤에 와야 함
    assert stages.index("autofill:saving") > stages.index("autofill:generating:0/2")


# ─── [2026-06-01] fast-fail + 폴백(graceful degradation) ──────────────


class _ModelAwareGemini:
    """model override 를 인지하는 fake.

    primary(model=None) 에서는 GeminiError, fallback(model 지정) 에서는 정상 JSON →
    "primary 실패 시 그 API 만 경량 모델로 폴백" 경로 검증. timeout/max_retries 인자도
    수용해 _gemini_call degradation 에 의해 model 이 떨궈지지 않도록 함.
    """

    def __init__(self, *, primary_kind: str = "transient", fallback_json: str = "") -> None:
        self.calls: List = []  # 호출된 model 인자 순서 기록
        self._primary_kind = primary_kind
        self._fallback_json = fallback_json or _spec_json(
            [{"status": 503, "code": "UNAVAILABLE", "message": "일시 오류"}],
            {"required": True, "description": "fallback 초안"},
        )

    async def generate(
        self, prompt, *, temperature=0.2, response_schema=None,
        model=None, timeout=None, max_retries=None,
    ):
        self.calls.append(model)
        if model is None:
            from app.clients.gemini_client import GeminiError
            raise GeminiError("primary slow", kind=self._primary_kind)
        from tests.conftest import _FakeResult
        return _FakeResult(text=self._fallback_json)


@pytest.mark.asyncio
async def test_primary_failure_falls_back_to_lite_model(_stub_save):
    """primary(구독 모델)가 GeminiError 면 그 API 만 폴백 모델로 재시도 → degraded 초안."""
    from app.pipelines.api_spec_autofill_pipeline import call_api_spec_filler

    gemini = _ModelAwareGemini()
    api = ApiSpecInput(id="API-01", name="조회", method="GET",
                       endpoint="/x/{id}", description="단건", error_cases=[], auth={})
    f = await call_api_spec_filler(
        _ctx(gemini), "tmpl", api, fallback_model="gemini-2.0-flash-lite",
    )
    assert f.generated is True
    assert f.degraded is True                       # 폴백으로 만든 초안
    assert gemini.calls == [None, "gemini-2.0-flash-lite"]  # primary→fallback 순
    assert f.auth["description"] == "fallback 초안"
    assert f.auth["source"] == "ai_draft" and f.auth["reviewed"] is False


@pytest.mark.asyncio
async def test_primary_and_fallback_both_fail_skips_without_raise(_stub_save):
    """primary + 폴백 모두 GeminiError 면 그 API 만 generated=False — 예외 전파 없음."""
    from app.pipelines.api_spec_autofill_pipeline import call_api_spec_filler
    from app.clients.gemini_client import GeminiError

    class _AlwaysFail:
        def __init__(self): self.calls = []
        async def generate(self, prompt, *, temperature=0.2, response_schema=None,
                           model=None, timeout=None, max_retries=None):
            self.calls.append(model)
            raise GeminiError("down", kind="quota")

    gemini = _AlwaysFail()
    api = ApiSpecInput(id="A", name="a", error_cases=[], auth={})
    f = await call_api_spec_filler(_ctx(gemini), "tmpl", api, fallback_model="lite")
    assert f.generated is False                     # 격리 (raise 안 함)
    assert gemini.calls == [None, "lite"]           # primary 후 fallback 시도


@pytest.mark.asyncio
async def test_geminierror_without_fallback_skips_without_raise(_stub_save):
    """폴백 모델 미지정 + primary GeminiError → 격리(generated=False), 예외 전파 없음."""
    from app.pipelines.api_spec_autofill_pipeline import call_api_spec_filler
    from app.clients.gemini_client import GeminiError

    class _Fail:
        def __init__(self): self.calls = []
        async def generate(self, prompt, *, temperature=0.2, response_schema=None,
                           model=None, timeout=None, max_retries=None):
            self.calls.append(model)
            raise GeminiError("down", kind="transient")

    gemini = _Fail()
    api = ApiSpecInput(id="A", name="a", error_cases=[], auth={})
    f = await call_api_spec_filler(_ctx(gemini), "tmpl", api, fallback_model=None)
    assert f.generated is False
    assert gemini.calls == [None]                   # 폴백 없음 — primary 한 번만


@pytest.mark.asyncio
async def test_batch_survives_geminierror_in_one_api(_stub_save):
    """[핵심] 한 API 의 GeminiError 가 배치/잡을 깨지 않는다 (이전엔 전파 → arq 잡 재시도 폭주).

    'boom' API 는 어떤 모델로도 GeminiError → 격리. 나머지는 정상 생성.
    """
    from app.clients.gemini_client import GeminiError
    from tests.conftest import _FakeResult

    class _OneBoomGemini:
        async def generate(self, prompt, *, temperature=0.2, response_schema=None,
                           model=None, timeout=None, max_retries=None):
            if "boom" in prompt:
                raise GeminiError("down", kind="transient")
            return _FakeResult(text=_spec_json(
                [{"status": 500, "code": "X"}], {"description": "ok"}
            ))

    apis = [
        ApiSpecInput(id="A", name="ok-api", error_cases=[], auth={}),
        ApiSpecInput(id="B", name="boom", error_cases=[], auth={}),
    ]
    # run_* 가 예외 없이 완료돼야 함 (배치 비실패)
    result = await run_api_spec_autofill_pipeline(
        _ctx(_OneBoomGemini()), AutofillInput(project_name="proj", apis=apis)
    )
    by_id = {f.id: f for f in result.apis}
    assert by_id["A"].generated is True
    assert by_id["B"].generated is False
    assert result.meta["generatedCount"] == 1
    assert result.meta["failedCount"] == 1          # 대상 2 - 생성 1


@pytest.mark.asyncio
async def test_degraded_count_in_meta(monkeypatch, _stub_save):
    """primary 실패→폴백 성공 건이 meta.degradedCount 로 노출 (FE 안내용)."""
    from app.core.config import settings
    # gemini_model_for_free 는 computed property → 백킹 필드(GEMINI_MODEL_FREE)를 패치.
    monkeypatch.setattr(settings, "GEMINI_MODEL_FREE", "gemini-2.0-flash-lite")

    apis = [ApiSpecInput(id="A", name="a", error_cases=[], auth={})]
    result = await run_api_spec_autofill_pipeline(
        _ctx(_ModelAwareGemini()), AutofillInput(project_name="proj", apis=apis)
    )
    assert result.meta["generatedCount"] == 1
    assert result.meta["degradedCount"] == 1
    assert result.apis[0].degraded is True


# ─── [2026-06-10] 동시성·초안 모델 노브 ─────────────────────────


class _ModelCapturingGemini:
    """generate 에 전달된 model kwarg 를 기록 — 초안 모델 노브 검증용."""

    def __init__(self, body: str) -> None:
        self._body = body
        self.models: List[Any] = []

    async def generate(
        self, prompt: str, *, temperature: float = 0.2, response_schema=None,
        model=None, timeout=None, max_retries=None,
    ):
        self.models.append(model)
        from tests.conftest import _FakeResult
        return _FakeResult(text=self._body)


def test_llm_concurrency_reads_settings(monkeypatch):
    """AUTOFILL_LLM_CONCURRENCY env(settings) 반영 + 최소 1 보장."""
    from app.core.config import settings
    from app.pipelines.api_spec_autofill_pipeline import _llm_concurrency

    monkeypatch.setattr(settings, "AUTOFILL_LLM_CONCURRENCY", 3)
    assert _llm_concurrency() == 3
    monkeypatch.setattr(settings, "AUTOFILL_LLM_CONCURRENCY", 0)
    assert _llm_concurrency() == 5  # 0/미설정 → 보수적 fallback
    monkeypatch.setattr(settings, "AUTOFILL_LLM_CONCURRENCY", -2)
    assert _llm_concurrency() == 1  # 음수도 최소 1


@pytest.mark.asyncio
async def test_draft_model_knob_overrides_primary(monkeypatch, _stub_save):
    """AUTOFILL_DRAFT_MODEL 설정 시 1차 시도부터 그 모델로 호출.
    폴백 모델과 같으면 폴백 비활성(무의미한 동일-모델 재시도 차단).
    """
    from app.core.config import settings
    from app.pipelines.api_spec_autofill_pipeline import generate_api_spec_fills

    monkeypatch.setattr(settings, "AUTOFILL_DRAFT_MODEL", "gemini-2.5-flash-lite")

    gemini = _ModelCapturingGemini(_spec_json(
        [{"status": 404, "code": "NOT_FOUND", "message": "없음"}],
        {"required": True, "description": "로그인 필요"},
    ))
    ctx = _ctx(gemini)
    apis = [ApiSpecInput(id="API-01", name="작업 생성", method="POST",
                         endpoint="/x", description="d", error_cases=[], auth={})]

    result = await generate_api_spec_fills(
        ctx, apis, fallback_model="gemini-2.5-flash-lite", emit_progress=False,
    )

    assert result["API-01"].generated is True
    # 1차 시도가 draft model 로 호출됐고, 호출은 1회뿐(동일-모델 폴백 차단).
    assert gemini.models == ["gemini-2.5-flash-lite"]


@pytest.mark.asyncio
async def test_draft_model_unset_keeps_legacy_behavior(monkeypatch, _stub_save):
    """노브 미설정(기본) — model_override=None (구독 모델), 기존 동작 그대로."""
    from app.core.config import settings
    from app.pipelines.api_spec_autofill_pipeline import generate_api_spec_fills

    monkeypatch.setattr(settings, "AUTOFILL_DRAFT_MODEL", None)

    gemini = _ModelCapturingGemini(_spec_json(
        [{"status": 404}], {"required": True, "description": "x"},
    ))
    ctx = _ctx(gemini)
    apis = [ApiSpecInput(id="API-01", name="작업 생성", method="POST",
                         endpoint="/x", description="d", error_cases=[], auth={})]

    result = await generate_api_spec_fills(ctx, apis, emit_progress=False)

    assert result["API-01"].generated is True
    assert gemini.models == [None]


@pytest.mark.asyncio
async def test_concurrency_eight_inflight(monkeypatch, _stub_save):
    """[성능 검증] 동시성 8 — 대상 10개일 때 동시 in-flight 최대치가 8 이어야 한다
    (이전 하드코딩 5 였으면 5 에서 멈춤). sleep 중 모두 진입하므로 결정적.
    """
    from app.core.config import settings
    from app.pipelines.api_spec_autofill_pipeline import generate_api_spec_fills

    monkeypatch.setattr(settings, "AUTOFILL_LLM_CONCURRENCY", 8)

    state = {"inflight": 0, "max": 0}

    class _SlowGemini:
        async def generate(self, prompt: str, **_k):
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
            await asyncio.sleep(0.05)
            state["inflight"] -= 1
            from tests.conftest import _FakeResult
            return _FakeResult(text=_spec_json(
                [{"status": 404}], {"required": True, "description": "x"},
            ))

    ctx = _ctx(_SlowGemini())
    apis = [
        ApiSpecInput(id=f"API-{i:02d}", name=f"api{i}", method="GET",
                     endpoint=f"/x{i}", description="d", error_cases=[], auth={})
        for i in range(10)
    ]

    result = await generate_api_spec_fills(ctx, apis, emit_progress=False)

    assert len(result) == 10
    assert state["max"] == 8
