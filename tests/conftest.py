"""
공통 fixture.

테스트 정책:
  - 기본은 Gemini/Neo4j 를 fake 로 대체 → 빠르고 결정적.
  - 실제 호출을 검증하려면 `RUN_INTEGRATION=1 pytest -m integration` 으로 활성화.

[Phase A — 2026-05]
FakeGemini 가 response_schema kwarg 명시적 수용 → base.py _gemini_call 의 TypeError
fallback 에 의존하지 않고 schema 전달 자체를 검증 가능.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import pytest


# ─── LLM 키 가드 (CI 복구 — 2026-05-27) ──────────────────────────
@pytest.fixture(autouse=True)
def _ensure_llm_keys_for_tests(monkeypatch):
    """키 없는 환경(CI 등)에서 LLM 클라이언트 초기화(키 검증)만 통과시키는 더미 키.

    배경: onboard wait 경로 등은 tracked_pipeline_context 가 ctx 생성 시 GeminiClient
    를 만들고, GeminiClient.__init__ 이 GEMINI_API_KEY/GOOGLE_API_KEY 부재 시
    GeminiError 를 raise. 단위 테스트는 실제 gemini 호출을 mock 하므로 키가 필요
    없는데도, CI(키 미주입)에서 ctx 생성 단계에서 막혀 test_onboard_routes 5건이
    실패했다(#55~). 키가 이미 있으면(로컬 .env) 그대로 둬 통합 디버깅에 영향 없음.
    """
    if not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
        monkeypatch.setenv("GEMINI_API_KEY", "test-dummy-not-a-real-key")


# ─── Test markers ──────────────────────────────────────────────


def pytest_collection_modifyitems(config, items):
    """marker 별 환경변수 가드 — 외부 의존성 테스트는 명시적 opt-in."""
    run_integration = os.getenv("RUN_INTEGRATION") == "1"
    run_testcontainers = os.getenv("RUN_TESTCONTAINERS") == "1"
    skip_int = pytest.mark.skip(reason="RUN_INTEGRATION!=1 → 실제 외부 호출 테스트 스킵")
    skip_tc = pytest.mark.skip(
        reason="RUN_TESTCONTAINERS!=1 또는 Docker 데몬 없음 → testcontainers 테스트 스킵"
    )
    # get_closest_marker 로 정확히 marker 만 매칭 — `item.keywords` 는 디렉토리/모듈
    # 이름까지 잡아 false positive (tests/integration/ 디렉토리가 `integration`
    # 키워드로 잡히는 회귀).
    for item in items:
        if item.get_closest_marker("integration") and not run_integration:
            item.add_marker(skip_int)
        if item.get_closest_marker("testcontainers") and not run_testcontainers:
            item.add_marker(skip_tc)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: 실제 Gemini/Neo4j 자격증명이 필요한 e2e 테스트"
    )
    config.addinivalue_line(
        "markers",
        "testcontainers: testcontainers 로 실제 Neo4j 띄워 cypher 시맨틱 검증",
    )


# ─── Fake Gemini ──────────────────────────────────────────────────


@dataclass
class _FakeResult:
    text: str
    model: str = "fake"
    finish_reason: Optional[str] = "STOP"


class FakeGemini:
    """
    응답 전달 2가지 (택1):
      1. responder: callable[[str], str] — prompt 매칭 (backward compat)
      2. responses: list[str] — 순차 큐 (cps_pipeline 처럼 멀티 LLM 호출에 적합)

    호출 이력은 calls 리스트에 dict 로 누적:
      [{"prompt": ..., "temperature": ..., "response_schema": ...}, ...]

    response_schema 인자를 명시적으로 받아 검증 가능 — Phase A 의 핵심.
    제너릭 명시 호출 온경가이: gemini.calls[0]["response_schema"] is not None.

    구버전 호환:
      - FakeGemini(some_fn) — positional responder → 이전와 동일하게 동작
      - generate(prompt, temperature=...) — schema kwarg 없이 호출 가능
      - prompts 프로퍼티 — 이전의 calls 가 list[str] 이었다면 calls → prompts 로 이주.
    """

    def __init__(
        self,
        responder: Optional[Callable[[str], str]] = None,
        *,
        responses: Optional[List[str]] = None,
    ):
        if responder is None and responses is None:
            raise ValueError(
                "FakeGemini: responder(positional) 또는 responses(kwarg) 중 하나 필수"
            )
        if responder is not None and responses is not None:
            raise ValueError(
                "FakeGemini: responder 와 responses 를 동시에 줄 수 없음 (택1)"
            )
        self._responder = responder
        self._responses = list(responses) if responses is not None else None
        self.calls: List[Dict[str, Any]] = []

    @property
    def prompts(self) -> List[str]:
        """구버전 호환 — 이전의 calls 가 list[str] 이었다면 이 프로퍼티로 접근."""
        return [c["prompt"] for c in self.calls]

    async def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        response_schema: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
        **_kwargs: Any,
    ) -> _FakeResult:
        self.calls.append({
            "prompt": prompt,
            "temperature": temperature,
            "response_schema": response_schema,
            "model": model,
            "max_output_tokens": max_output_tokens,
        })
        if self._responses is not None:
            if not self._responses:
                return _FakeResult(text="{}")
            return _FakeResult(text=self._responses.pop(0))
        return _FakeResult(text=self._responder(prompt))

    async def generate_stream(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        model: Optional[str] = None,
    ):
        """스트리밍 fake — responses 큐에서 텍스트를 꺼내 단어 단위로 yield."""
        if self._responses is not None:
            text = self._responses.pop(0) if self._responses else ""
        else:
            text = self._responder(prompt)
        self.calls.append({"prompt": prompt, "temperature": temperature, "response_schema": None})
        # 단어 단위로 나눠 yield (실제 스트리밍과 동일한 파싱 경로 검증)
        for word in text.split(" "):
            yield word + " " if word else ""


def prompt_router(
    mapping: Dict[str, str], *, default: str = "{}"
) -> Callable[[str], str]:
    """
    prompt 의 substring 매칭으로 응답 분기.

    Example:
        FakeGemini(prompt_router({
            "통합 하네스 아키텍트": '{"nodes": [...]}',  # cps_extract.md 시그니처
            "Impact Analyzer": '{"affected_sections": []}',
        }))

    매칭 순서는 dict 입력 순서 (Python 3.7+). 모두 미매칭이면 default.
    """
    items = list(mapping.items())

    def _route(prompt: str) -> str:
        for keyword, response in items:
            if keyword in prompt:
                return response
        return default

    return _route


# ─── Fake Neo4j ───────────────────────────────────────────────────


class FakeNeo4j:
    """
    실행된 모든 Cypher 를 기록하고, 미리 정의된 응답을 반환.

    Usage:
      neo = FakeNeo4j(responses=[[{...}], []])
      # 1번째 run_cypher 호출 → 첫 응답, 2번째 → 두 번째 ...
    """

    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.executed: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def run_cypher(self, cypher: str, params: Optional[Dict[str, Any]] = None):
        self.executed.append({"cypher": cypher, "params": params or {}})
        if self._responses:
            return self._responses.pop(0)
        return []

    async def run_in_transaction(
        self,
        operations: List[tuple],
        database: Optional[str] = None,
    ) -> List[List[Dict[str, Any]]]:
        """실제 driver run_in_transaction 의 fake — 각 (cypher, params) 를 순서대로
        run_cypher 로 위임. 단일 트랜잭션 의미는 fake 에서 모사 불가능하지만 호출 contract
        는 동일 (실패 시 raise / 결과 순서 보존).
        """
        out: List[List[Dict[str, Any]]] = []
        for cypher, params in operations:
            out.append(await self.run_cypher(cypher, params))
        return out


# ─── Fake Redis ──────────────────────────────────────────────────


class FakeRedis:
    """
    arq job 의 ctx['redis'] 에 들어갈 미니멀 fake — stage marker 검증 용.
    실제 redis-py 와 동일하게 set/get 은 코루틴 (arq 가 await 함). ex 인자 보존.
    """

    def __init__(self):
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, Optional[int]] = {}

    async def set(
        self, key: str, value: Any, *, ex: Optional[int] = None, nx: bool = False
    ):
        # nx=True 면 키가 이미 있을 때 set 실패 → None 반환 (redis-py SET NX 시맨틱).
        if nx and key in self.store:
            return None
        self.store[key] = str(value)
        self.ttls[key] = ex
        return True

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    async def delete(self, key: str) -> int:
        existed = key in self.store
        self.store.pop(key, None)
        self.ttls.pop(key, None)
        return 1 if existed else 0


# ─── Fixture builders ───────────────────────────────────────────────


def make_arq_ctx(
    *,
    job_id: str = "test-job-1",
    gemini: Any = None,
    gemini_free: Any = None,
    gemini_pro: Any = None,
    neo4j: Any = None,
    redis: Any = None,
) -> Dict[str, Any]:
    """
    arq job 의 ctx dict 빌더 — _tracked_ctx / _set_job_stage 등이 필요로 하는 키만 채움.
    gemini 만 주면 free/pro 모두 같은 인스턴스로 fallback (단일 모델 운영 호환).
    free/pro 를 따로 주면 등급별 라우팅 검증 가능.
    """
    return {
        "job_id": job_id,
        "gemini": gemini,
        "gemini_free": gemini_free if gemini_free is not None else gemini,
        "gemini_pro": gemini_pro if gemini_pro is not None else gemini,
        "neo4j": neo4j,
        "redis": redis,
    }
