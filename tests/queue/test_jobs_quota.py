"""
jobs.py 의 quota 토큰 적재 동작 단위 테스트.

[검증 범위]
- _tracked_ctx: TrackedGemini wrap 된 PipelineContext + TokenAccumulator pair
- _persist_token_usage: user_email + delta>0 시 add_tokens 호출
- _persist_token_usage: user_email=None 시 skip (legacy enqueue 호환)
- _persist_token_usage: delta=0 시 cypher 호출 안 함
- _persist_token_usage: add_tokens 예외 swallow (Neo4j 일시 장애가 job 결과 망치면 안 됨)
- 각 LLM job 이 finally 절에서 _persist_token_usage 호출 (성공/실패 양쪽)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import pytest

from app.clients.gemini_client import (
    GeminiResult,
    TokenAccumulator,
    TokenUsage,
    TrackedGemini,
)
from app.core import quota
from app.queue import jobs

pytestmark = pytest.mark.asyncio


# ─── _tracked_ctx ────────────────────────────────────────────


class _FakeShared:
    """worker on_startup 에서 만드는 lifeline-shared GeminiClient stub."""

    async def generate(self, prompt: str, *, temperature: float = 0.2) -> GeminiResult:
        return GeminiResult(
            text="x",
            model="fake",
            finish_reason="STOP",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=10, total_tokens=15),
        )


class _FakeNeo:
    async def run_cypher(self, *a, **kw):
        return []


async def test_tracked_ctx_wraps_gemini_with_accumulator(monkeypatch):
    """_tracked_ctx 가 (PipelineContext, TokenAccumulator) 페어를 반환하고
    PipelineContext.gemini 호출이 accumulator 에 누적되는지.

    [2026-05 변경] _tracked_ctx 가 async + user_email 인자.
    user_email 이 None 이면 subscription 조회 skip → ctx['gemini'] (legacy) fallback.
    """
    # legacy fallback 검증 — ctx['gemini'] 만 있고 등급별 key 없음
    arq_ctx = {"job_id": "j1", "gemini": _FakeShared(), "neo4j": _FakeNeo()}
    ctx, accumulator, _ = await jobs._tracked_ctx(arq_ctx, user_email=None)

    # PipelineContext.gemini 는 TrackedGemini 인스턴스
    assert isinstance(ctx.gemini, TrackedGemini)
    # idempotency_key 가 job_id 로 채워짐
    assert ctx.idempotency_key == "j1"

    # generate 호출 → accumulator 자동 누적
    await ctx.gemini.generate("test")
    assert accumulator.total.total_tokens == 15

    # 한 번 더 호출 → 누적
    await ctx.gemini.generate("test 2")
    assert accumulator.total.total_tokens == 30


# ─── _tracked_ctx 등급별 분기 회귀 가드 (2026-05 fix) ─────────
#
# 이전 버그: `subscription == SUBSCRIPTION_PRO` 단일 비교라 pro_plus / pro_max
# 사용자가 FREE 분기로 떨어져 gemini_free 인스턴스 사용. LiteLLM 에 flash-lite
# 미등록이면 무조건 400. PAID_SUBSCRIPTIONS 멤버십 체크로 fix.


@dataclass
class _StubUsageMain:
    """메인 쿼터 잔여(total_tokens=0) → resolve_quota_decision 가 main 모드."""
    subscription_type: str
    total_tokens: int = 0
    lite_tokens: int = 0
    lite_daily_tokens: int = 0
    reset_at: Optional[str] = None
    lite_daily_reset_at: Optional[str] = None


@pytest.mark.parametrize("subscription,expect_key", [
    ("pro", "gemini_pro"),
    ("pro_plus", "gemini_pro"),
    ("pro_max", "gemini_pro"),
    ("free", "gemini_free"),
    ("", "gemini_free"),       # 빈/알 수 없는 값
    ("unknown", "gemini_free"),
])
async def test_tracked_ctx_selects_instance_by_subscription(monkeypatch, subscription, expect_key):
    """[메인 모드] paid 3개 등급 → gemini_pro / free + 알 수 없는 값 → gemini_free.

    [2026-06] 라우팅이 _resolve_subscription_for_job → resolve_quota_decision 로 이동.
    메인 잔여(total_tokens=0)면 등급별 Flash/free 풀. 오버플로우는 별도 테스트.
    """
    pro_inst = _FakeShared()
    free_inst = _FakeShared()
    arq_ctx = {
        "job_id": "j-sub",
        "gemini_pro": pro_inst,
        "gemini_free": free_inst,
        "gemini": _FakeShared(),     # legacy fallback (이번 케이스에선 안 쓰여야 함)
        "neo4j": _FakeNeo(),
    }

    async def fake_get_usage(_email):
        return _StubUsageMain(subscription_type=subscription)

    monkeypatch.setattr(jobs.usage_repository, "get_usage", fake_get_usage)

    ctx, _, _ = await jobs._tracked_ctx(arq_ctx, user_email="u@b.com")
    # TrackedGemini 안의 _inner 가 기대한 인스턴스인지
    selected = ctx.gemini._inner
    if expect_key == "gemini_pro":
        assert selected is pro_inst, f"{subscription} → gemini_pro 인스턴스 사용해야"
    else:
        assert selected is free_inst, f"{subscription} → gemini_free 인스턴스 사용해야"


async def test_tracked_ctx_overflow_routes_to_lite(monkeypatch):
    """[오버플로우] 유료 등급이 메인 소진 → gemini_lite 풀 + lite 버킷."""
    pro_inst = _FakeShared()
    free_inst = _FakeShared()
    lite_inst = _FakeShared()
    arq_ctx = {
        "job_id": "j-of",
        "gemini_pro": pro_inst,
        "gemini_free": free_inst,
        "gemini_lite": lite_inst,
        "neo4j": _FakeNeo(),
    }

    async def fake_get_usage(_email):
        # pro 메인 한도(2M) 소진, 일일캡 미달 → overflow
        return _StubUsageMain(subscription_type="pro", total_tokens=2_000_000, lite_daily_tokens=0)

    monkeypatch.setattr(jobs.usage_repository, "get_usage", fake_get_usage)

    ctx, _, decision = await jobs._tracked_ctx(arq_ctx, user_email="u@b.com")
    assert ctx.gemini._inner is lite_inst
    assert decision.mode == "overflow"
    assert decision.bucket == "lite"


# ─── _tracked_ctx 가 TrackedGemini 강등/강제 상태를 모드별로 배선하는지 (2026-06) ─


def _arq_ctx_full():
    return {
        "job_id": "j",
        "gemini_pro": _FakeShared(),
        "gemini_free": _FakeShared(),
        "gemini_lite": _FakeShared(),
        "neo4j": _FakeNeo(),
    }


async def test_tracked_ctx_arms_downgrade_for_main_paid(monkeypatch):
    """main 모드 + 오버플로우 가능(Pro+) → mid-job 강등 무장 + base/limit 스냅샷 전달."""
    arq_ctx = _arq_ctx_full()

    async def fake_get_usage(_e):
        # Pro+ 메인 한도 미만(잔여) → main 모드. total_tokens 가 base_usage 로 전달돼야.
        return _StubUsageMain(subscription_type="pro_plus", total_tokens=123_456)
    monkeypatch.setattr(jobs.usage_repository, "get_usage", fake_get_usage)

    ctx, _, decision = await jobs._tracked_ctx(arq_ctx, user_email="u@b.com")
    g = ctx.gemini
    assert decision.mode == "main"
    # 메인 풀(pro)로 시작.
    assert g._inner is arq_ctx["gemini_pro"]
    # 강등 무장 — lite 풀 inner + 스냅샷.
    assert g._downgrade_lite_inner is arq_ctx["gemini_lite"]
    assert g._base_usage == 123_456
    assert g._main_limit == quota.get_limit("pro_plus", "total_tokens")
    assert g._main_limit > 0
    # main 모드라 즉시 강제는 없고, 강등 시 강제할 모델명만 준비.
    assert g._force_model is None
    assert g._downgrade_force_model is not None


async def test_tracked_ctx_no_downgrade_arm_for_free(monkeypatch):
    """free(오버플로우 불가)는 강등 무장 안 함 — lite_inner None, force 없음."""
    arq_ctx = _arq_ctx_full()

    async def fake_get_usage(_e):
        return _StubUsageMain(subscription_type="free", total_tokens=0)
    monkeypatch.setattr(jobs.usage_repository, "get_usage", fake_get_usage)

    ctx, _, decision = await jobs._tracked_ctx(arq_ctx, user_email="u@b.com")
    g = ctx.gemini
    assert decision.mode == "main"
    # 안전 게이트: lite_inner 가 None 이면 _maybe_downgrade 는 무조건 no-op.
    # (main_limit 은 free 한도로 채워지지만 lite_inner=None 이라 강등 불가 — 무해.)
    assert g._downgrade_lite_inner is None
    assert g._downgrade_force_model is None
    assert g._force_model is None
    # 실제로 한도를 넘겨도 강등 안 일어남을 직접 확인.
    g._base_usage = 0
    await ctx.gemini.generate("x")  # _FakeShared 는 15토큰; main_limit(free) 안 넘김
    g._base_usage = 10_000_000      # 강제로 넘긴 상태로 만들어도
    await ctx.gemini.generate("x")
    assert g._inner is arq_ctx["gemini_free"]  # 여전히 free 풀 (강등 없음)
    assert ctx.gemini._accumulator.main_bucket_tokens is None


async def test_tracked_ctx_overflow_sets_force_model(monkeypatch):
    """overflow 모드 → force_model=lite 즉시 강제 (비싼 모델 명시 우회 차단)."""
    arq_ctx = _arq_ctx_full()

    async def fake_get_usage(_e):
        return _StubUsageMain(subscription_type="pro", total_tokens=2_000_000, lite_daily_tokens=0)
    monkeypatch.setattr(jobs.usage_repository, "get_usage", fake_get_usage)

    ctx, _, decision = await jobs._tracked_ctx(arq_ctx, user_email="u@b.com")
    g = ctx.gemini
    assert decision.mode == "overflow"
    lite_model = quota.model_for_decision(decision)
    assert g._force_model == lite_model
    # overflow 는 이미 lite 풀이라 mid-job 강등 무장은 불필요.
    assert g._downgrade_lite_inner is None
    assert g._main_limit == 0


async def test_tracked_ctx_end_to_end_downgrade_and_split(monkeypatch):
    """통합: main 으로 시작한 Pro+ 잡이 진행 중 한도를 넘으면 lite 로 강등 + 분할 기록."""
    # Pro+ 메인 한도를 작게 override 해서 빨리 넘기게 만든다.
    quota.apply_limits_override("pro_plus", {
        "meeting_logs": 40, "summary_chars": 600_000, "total_tokens": 30,
        "library_skills": 50, "max_projects": 6, "lite_daily_cap": 1_400_000,
    })
    try:
        arq_ctx = _arq_ctx_full()

        async def fake_get_usage(_e):
            # base 0, 한도 30 → 한 번 generate(15토큰) 두 번이면 30 도달 → 강등.
            return _StubUsageMain(subscription_type="pro_plus", total_tokens=0)
        monkeypatch.setattr(jobs.usage_repository, "get_usage", fake_get_usage)

        ctx, acc, decision = await jobs._tracked_ctx(arq_ctx, user_email="u@b.com")
        assert decision.mode == "main"
        pro, lite = arq_ctx["gemini_pro"], arq_ctx["gemini_lite"]

        await ctx.gemini.generate("c1")  # 15 < 30 → main 유지
        assert ctx.gemini._inner is pro
        await ctx.gemini.generate("c2")  # 30 >= 30 → 강등
        assert ctx.gemini._inner is lite
        assert acc.main_bucket_tokens == 30  # 강등 시점 누적
        await ctx.gemini.generate("c3")     # lite 풀
        assert acc.total.total_tokens == 45
    finally:
        quota.clear_limits_override()


# ─── _persist_token_usage ────────────────────────────────────


@pytest.fixture
def fake_add_tokens(monkeypatch):
    """usage_repository.add_tokens 를 fake 로 교체. 호출 내역 반환."""

    calls: List[Tuple[str, int]] = []
    raise_on_call: List[Exception] = []

    async def fake(email: str, delta: int, *, bucket: str = "main") -> Optional[int]:
        calls.append((email, delta))
        if raise_on_call:
            raise raise_on_call.pop(0)
        return 1000 + delta  # 임의의 new total

    monkeypatch.setattr("app.queue.jobs.usage_repository.add_tokens", fake)
    return calls, raise_on_call


async def test_persist_calls_add_tokens_when_email_and_delta(fake_add_tokens):
    """정상 케이스 — user_email 있고 delta > 0 → add_tokens 호출."""
    calls, _ = fake_add_tokens
    acc = TokenAccumulator()
    acc.add(TokenUsage(total_tokens=42))
    await jobs._persist_token_usage("u@b.com", acc, job_id="j1")
    assert calls == [("u@b.com", 42)]


async def test_persist_skips_when_no_email(fake_add_tokens):
    """user_email 없음 (legacy enqueue) → add_tokens 호출 안 함."""
    calls, _ = fake_add_tokens
    acc = TokenAccumulator()
    acc.add(TokenUsage(total_tokens=42))
    await jobs._persist_token_usage(None, acc, job_id="j1")
    assert calls == []


async def test_persist_skips_when_zero_delta(fake_add_tokens):
    """누적 토큰 0 → cypher 라운드트립 회피."""
    calls, _ = fake_add_tokens
    acc = TokenAccumulator()  # 누적 안 함
    await jobs._persist_token_usage("u@b.com", acc, job_id="j1")
    assert calls == []


# ─── mid-job 강등 분할 적재 (2026-06) ─────────────────────────


@pytest.fixture
def fake_add_tokens_with_bucket(monkeypatch):
    """add_tokens 를 fake 로 교체 — (email, delta, bucket) 까지 캡처."""
    calls: List[Tuple[str, int, str]] = []

    async def fake(email: str, delta: int, *, bucket: str = "main") -> Optional[int]:
        calls.append((email, delta, bucket))
        return 1000 + delta

    monkeypatch.setattr("app.queue.jobs.usage_repository.add_tokens", fake)
    return calls


async def test_persist_splits_main_and_lite_on_downgrade(fake_add_tokens_with_bucket):
    """mid-job 강등 시 강등 시점까지는 main, 나머지는 lite 버킷으로 분할 적재."""
    calls = fake_add_tokens_with_bucket
    acc = TokenAccumulator()
    acc.add(TokenUsage(total_tokens=500_000))
    acc.main_bucket_tokens = 500_000   # 강등 시점 누적 (main 분)
    acc.add(TokenUsage(total_tokens=300_000))  # 강등 이후 (lite 분), 총 800K
    await jobs._persist_token_usage("u@b.com", acc, job_id="j1", bucket="main")
    assert calls == [
        ("u@b.com", 500_000, "main"),
        ("u@b.com", 300_000, "lite"),
    ]


async def test_persist_no_split_when_main_bucket_tokens_none(fake_add_tokens_with_bucket):
    """강등 없으면(main_bucket_tokens=None) 전량 단일 버킷."""
    calls = fake_add_tokens_with_bucket
    acc = TokenAccumulator()
    acc.add(TokenUsage(total_tokens=120_000))
    await jobs._persist_token_usage("u@b.com", acc, job_id="j1", bucket="main")
    assert calls == [("u@b.com", 120_000, "main")]


async def test_persist_no_split_when_downgrade_at_zero(fake_add_tokens_with_bucket):
    """첫 호출 전 즉시 강등(split=0)이면 전량 lite — main 0 적재 skip."""
    calls = fake_add_tokens_with_bucket
    acc = TokenAccumulator()
    acc.main_bucket_tokens = 0        # 첫 호출에서 바로 한도 초과 → 전량 lite
    acc.add(TokenUsage(total_tokens=200_000))
    await jobs._persist_token_usage("u@b.com", acc, job_id="j1", bucket="main")
    # split(0) 은 0 <= 0 < total 조건 충족 → main_part=0(skip), lite_part=200K.
    assert calls == [("u@b.com", 200_000, "lite")]


async def test_persist_swallows_add_tokens_exception(fake_add_tokens, caplog):
    """add_tokens 실패해도 예외 전파 안 함 — job 결과를 망치면 안 됨."""
    calls, raise_on_call = fake_add_tokens
    raise_on_call.append(RuntimeError("Neo4j down"))

    acc = TokenAccumulator()
    acc.add(TokenUsage(total_tokens=42))
    # 예외 없이 종료
    await jobs._persist_token_usage("u@b.com", acc, job_id="j1")
    # 호출은 됐고
    assert calls == [("u@b.com", 42)]


# ─── job 의 finally 절 — 성공 케이스에서 적재 ─────────────────


async def test_cps_pipeline_job_persists_tokens_on_success(monkeypatch):
    """cps_pipeline_job 성공 시 finally 가 _persist_token_usage 호출."""
    persist_calls: list = []

    async def fake_persist(user_email, accumulator, *, job_id, bucket="main"):
        persist_calls.append(
            {
                "user_email": user_email,
                "total_tokens": accumulator.total.total_tokens,
                "job_id": job_id,
            }
        )

    monkeypatch.setattr("app.queue.jobs._persist_token_usage", fake_persist)

    # run_cps_pipeline 도 fake — 진짜 cypher 안 부르도록
    async def fake_run_cps(ctx, payload):
        # 모의 LLM 호출 1번 (토큰 100)
        await ctx.gemini.generate("test")
        from app.pipelines.cps_pipeline import CpsResult

        return CpsResult(
            cps_graph={"nodes": [], "relationships": []},
            mode="first_run",
            meeting_log_id="ml-1",
            delta_cps_id="d-1",
            master_cps_id="m-1",
            diagnostic={},
        )

    monkeypatch.setattr("app.queue.jobs.run_cps_pipeline", fake_run_cps)

    # FakeShared 는 매 호출에 15 토큰
    arq_ctx = {"job_id": "j-cps-1", "gemini": _FakeShared(), "neo4j": _FakeNeo()}
    result = await jobs.cps_pipeline_job(
        arq_ctx,
        project_name="proj",
        version="v1",
        date="2026-05-15",
        meeting_content="hello",
        user_email="u@b.com",
    )
    # 결과는 정상
    assert result["meeting_log_id"] == "ml-1"
    # finally 가 호출됨
    assert len(persist_calls) == 1
    assert persist_calls[0]["user_email"] == "u@b.com"
    assert persist_calls[0]["total_tokens"] == 15
    assert persist_calls[0]["job_id"] == "j-cps-1"


async def test_cps_pipeline_job_persists_tokens_on_failure(monkeypatch):
    """LLM 호출 후 pipeline 안에서 예외 발생해도 finally 가 호출 — 이미 쓴 토큰은 차감."""
    persist_calls: list = []

    async def fake_persist(user_email, accumulator, *, job_id, bucket="main"):
        persist_calls.append(accumulator.total.total_tokens)

    monkeypatch.setattr("app.queue.jobs._persist_token_usage", fake_persist)

    async def fake_run_cps(ctx, payload):
        await ctx.gemini.generate("test")  # 15 토큰 소비
        raise ValueError("pipeline boom")

    monkeypatch.setattr("app.queue.jobs.run_cps_pipeline", fake_run_cps)

    arq_ctx = {"job_id": "j-cps-2", "gemini": _FakeShared(), "neo4j": _FakeNeo()}
    with pytest.raises(ValueError, match="pipeline boom"):
        await jobs.cps_pipeline_job(
            arq_ctx,
            project_name="proj",
            version="v1",
            date="2026-05-15",
            meeting_content="hi",
            user_email="u@b.com",
        )
    # finally 가 호출돼서 이미 사용된 15 토큰 적재 시도
    assert persist_calls == [15]


async def test_delete_meeting_job_persists_tokens_on_success(monkeypatch):
    """delete_meeting_job 도 Master CPS/PRD rebuild 시 LLM 호출 — 토큰 적재 확인.

    이 테스트가 빠지면 Critical Fix 2 누락 (delete 가 LLM 토큰 우회) 회귀 가드 X.
    """
    persist_calls: list = []

    async def fake_persist(user_email, accumulator, *, job_id, bucket="main"):
        persist_calls.append({
            "user_email": user_email,
            "total_tokens": accumulator.total.total_tokens,
            "job_id": job_id,
        })

    monkeypatch.setattr("app.queue.jobs._persist_token_usage", fake_persist)

    async def fake_run_delete(ctx, payload):
        # delete pipeline 안에서 LLM 2회 호출 (Master CPS/PRD rebuild)
        await ctx.gemini.generate("cps master rebuild")
        await ctx.gemini.generate("prd master rebuild")
        from app.pipelines.delete_pipeline import DeleteMeetingResult
        return DeleteMeetingResult(
            status="deleted",
            message="ok",
            project_name="proj",
            deleted_version="v1",
            remaining_cps_count=2,
            remaining_prd_count=2,
            cps_master_rebuilt=True,
            prd_master_rebuilt=True,
        )

    monkeypatch.setattr("app.queue.jobs.run_delete_meeting_pipeline", fake_run_delete)

    arq_ctx = {"job_id": "j-del-1", "gemini": _FakeShared(), "neo4j": _FakeNeo()}
    result = await jobs.delete_meeting_job(
        arq_ctx,
        project_name="proj",
        version="v1",
        user_email="u@b.com",
    )
    assert result["status"] == "deleted"
    # 2회 LLM 호출 × 15 토큰 = 30
    assert len(persist_calls) == 1
    assert persist_calls[0]["user_email"] == "u@b.com"
    assert persist_calls[0]["total_tokens"] == 30


async def test_cps_pipeline_job_legacy_call_without_user_email(monkeypatch):
    """user_email 없이 호출되는 legacy enqueue 시나리오 — job 정상 실행 + 적재 skip."""
    persist_calls: list = []

    async def fake_persist(user_email, accumulator, *, job_id, bucket="main"):
        persist_calls.append(user_email)

    monkeypatch.setattr("app.queue.jobs._persist_token_usage", fake_persist)

    async def fake_run_cps(ctx, payload):
        from app.pipelines.cps_pipeline import CpsResult
        await ctx.gemini.generate("p")
        return CpsResult(
            cps_graph={"nodes": [], "relationships": []},
            mode="first_run",
            meeting_log_id="ml",
            delta_cps_id="d",
            master_cps_id="m",
            diagnostic={},
        )

    monkeypatch.setattr("app.queue.jobs.run_cps_pipeline", fake_run_cps)

    arq_ctx = {"job_id": "j", "gemini": _FakeShared(), "neo4j": _FakeNeo()}
    # user_email 인자 안 줌 — default None
    await jobs.cps_pipeline_job(
        arq_ctx,
        project_name="proj",
        version="v1",
        date="d",
        meeting_content="m",
    )
    # finally 는 호출됐고 user_email=None 으로 전달됨
    assert persist_calls == [None]
