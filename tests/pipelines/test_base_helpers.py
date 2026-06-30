"""
base.py 헬퍼 단위 테스트 — 새로 추가된 generate_json_with_retry + TokenUsage.

[정책 검증]
- 첫 시도가 valid JSON 이면 단 1회 호출 (재시도 없음)
- 첫 시도 빈 응답 → strict prefix 부착하고 temperature 절반으로 재시도
- 두 번째도 실패하면 빈 dict 반환 (예외 던지지 않음 — 호출자가 결정)
- 재시도 시 더 엄격한 시스템 메시지가 프롬프트에 부착되는지
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import pytest

from app.pipelines.base import generate_json_with_retry


@dataclass
class _FakeResult:
    text: str
    model: str = "fake"
    finish_reason: Optional[str] = "STOP"


class _Recording:
    """generate 호출 인자/temperature 기록 + 미리 정해둔 응답 시퀀스 반환."""

    def __init__(self, responses: List[str]):
        self._responses = list(responses)
        self.calls: List[dict] = []

    async def generate(self, prompt: str, *, temperature: float = 0.2):
        self.calls.append({"prompt": prompt, "temperature": temperature})
        if self._responses:
            return _FakeResult(text=self._responses.pop(0))
        return _FakeResult(text="")


@pytest.mark.asyncio
async def test_first_attempt_valid_json_returns_immediately():
    g = _Recording(['{"ok": 1, "x": "value"}'])
    parsed, result = await generate_json_with_retry(g, "give me json")
    assert parsed == {"ok": 1, "x": "value"}
    assert result.text == '{"ok": 1, "x": "value"}'
    # 재시도 없음
    assert len(g.calls) == 1
    assert g.calls[0]["prompt"] == "give me json"


@pytest.mark.asyncio
async def test_first_attempt_empty_triggers_retry():
    """첫 응답이 fence 만 / no JSON 이면 재시도."""
    g = _Recording(["no json here at all", '{"ok": 1}'])
    parsed, result = await generate_json_with_retry(
        g, "user prompt", temperature=0.4
    )
    assert parsed == {"ok": 1}
    assert len(g.calls) == 2
    # 두 번째 프롬프트엔 strict prefix 부착
    assert "JSON" in g.calls[1]["prompt"]
    assert "user prompt" in g.calls[1]["prompt"]
    # temperature 절반
    assert g.calls[0]["temperature"] == 0.4
    assert g.calls[1]["temperature"] == 0.2


@pytest.mark.asyncio
async def test_both_attempts_fail_returns_empty_dict():
    """두 번째도 실패면 빈 dict + 마지막 result. 예외 안 던짐."""
    g = _Recording(["garbage 1", "garbage 2"])
    parsed, result = await generate_json_with_retry(g, "p")
    assert parsed == {}
    assert result.text == "garbage 2"
    assert len(g.calls) == 2


@pytest.mark.asyncio
async def test_malformed_json_triggers_retry():
    """JSON 비슷한데 파싱 실패 → 재시도."""
    g = _Recording(["{ not valid json", '{"good": true}'])
    parsed, _ = await generate_json_with_retry(g, "p")
    assert parsed == {"good": True}
    assert len(g.calls) == 2


@pytest.mark.asyncio
async def test_fenced_json_in_first_attempt_succeeds():
    """fence 안에 들어있어도 strip_code_blocks 가 처리 — 재시도 없음."""
    g = _Recording(['```json\n{"a": 1}\n```'])
    parsed, _ = await generate_json_with_retry(g, "p")
    assert parsed == {"a": 1}
    assert len(g.calls) == 1


@pytest.mark.asyncio
async def test_custom_strict_prefix_used():
    """strict_prefix 인자 커스터마이즈 가능."""
    g = _Recording(["nope", '{"x": 2}'])
    parsed, _ = await generate_json_with_retry(
        g, "p", strict_prefix="[CUSTOM PREFIX]\n"
    )
    assert parsed == {"x": 2}
    assert g.calls[1]["prompt"].startswith("[CUSTOM PREFIX]")


# ─── TokenUsage 동작 ─────────────────────────────────────────


def test_token_usage_addition():
    from app.clients.gemini_client import TokenUsage
    u1 = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    u2 = TokenUsage(prompt_tokens=200, completion_tokens=80, total_tokens=280)
    total = u1 + u2
    assert total.prompt_tokens == 300
    assert total.completion_tokens == 130
    assert total.total_tokens == 430


def test_token_usage_default_zero():
    from app.clients.gemini_client import TokenUsage
    u = TokenUsage()
    assert u.prompt_tokens == 0
    assert u.completion_tokens == 0
    assert u.total_tokens == 0


# ─── _gemini_call fallback 검증 (2026-05 운영 안전망) ────────────────


class _SchemaFailingFake:
    """schema 받으면 GeminiError(invalid_response) 던지고, schema 없으면 정상."""

    def __init__(self):
        self.calls = []

    async def generate(self, prompt, *, temperature=0.2, response_schema=None):
        self.calls.append({
            "temperature": temperature,
            "has_schema": response_schema is not None,
        })
        if response_schema is not None:
            from app.clients.gemini_client import GeminiError
            raise GeminiError("schema not supported by backend", kind="invalid_response")
        return _FakeResult(text='{"ok": true}')


class _AuthFailingFake:
    """quota/auth 류 에러는 fallback 안 함 (schema 무관) — 그대로 raise."""

    async def generate(self, prompt, *, temperature=0.2, response_schema=None):
        from app.clients.gemini_client import GeminiError
        raise GeminiError("quota exhausted", kind="quota")


@pytest.mark.asyncio
async def test_schema_failure_falls_back_to_schema_free():
    """schema 거부 시 schema 없이 재시도해 정상 응답 받음."""
    from app.pipelines.base import _gemini_call

    fake = _SchemaFailingFake()
    result = await _gemini_call(
        fake, "test", temperature=0.1,
        response_schema={"type": "object"},
    )
    assert result.text == '{"ok": true}'
    # 첫 호출은 schema 있음, 두 번째 (fallback) 는 schema 없음
    assert len(fake.calls) == 2
    assert fake.calls[0]["has_schema"] is True
    assert fake.calls[1]["has_schema"] is False


@pytest.mark.asyncio
async def test_quota_error_does_not_fallback():
    """quota/auth 류는 schema 무관 — fallback 시도하지 않고 그대로 raise."""
    from app.clients.gemini_client import GeminiError
    from app.pipelines.base import _gemini_call

    fake = _AuthFailingFake()
    with pytest.raises(GeminiError) as exc_info:
        await _gemini_call(
            fake, "test", temperature=0.1,
            response_schema={"type": "object"},
        )
    assert exc_info.value.kind == "quota"


@pytest.mark.asyncio
async def test_no_schema_no_fallback_attempt():
    """schema=None 이면 단순 passthrough — 예외도 그대로 raise."""
    from app.clients.gemini_client import GeminiError
    from app.pipelines.base import _gemini_call

    class _AlwaysFailing:
        async def generate(self, prompt, *, temperature=0.2):
            raise GeminiError("transient", kind="transient")

    fake = _AlwaysFailing()
    with pytest.raises(GeminiError):
        await _gemini_call(fake, "test", temperature=0.1, response_schema=None)


# ─── strip_code_blocks / extract_json_object — 퇴행성 출력 방어 (2026-06-12) ──
#
# 운영 장애 회귀 테스트: flash-lite 가 구조화 출력 모드에서 간헐적으로 뱉는
# 퇴행성 출력(수십만 자 공백/줄바꿈 블럭)이 후행 fence 정규식 `\n?\s*```\s*$` 의
# O(n²) 백트래킹을 격발 → 워커 이벤트 루프가 수십 분 동결 (py-spy 로 확인).
# 선형 구현 교체 후 아래 테스트가 그 회귀를 막는다.


def test_strip_code_blocks_basic_fences():
    """기존 동작 보존 — fence 제거 의미는 그대로."""
    from app.pipelines.base import strip_code_blocks

    assert strip_code_blocks('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_blocks('```markdown\n# 제목\n```') == "# 제목"
    assert strip_code_blocks('```\n{"a": 1}\n```') == '{"a": 1}'
    # fence 없음 — strip 만
    assert strip_code_blocks('  {"a": 1}  ') == '{"a": 1}'
    # 후행 fence 뒤 공백/줄바꿈 허용
    assert strip_code_blocks('```json\n{"a": 1}\n```   \n\n') == '{"a": 1}'
    # 비문자열/빈 입력 방어
    assert strip_code_blocks("") == ""
    assert strip_code_blocks(None) == ""  # type: ignore[arg-type]


def test_strip_code_blocks_degenerate_whitespace_is_linear():
    """[회귀] 공백 ~30만 자 꼬리 — 구 정규식은 수십 분 (O(n²)), 선형 구현은 즉시."""
    import time

    from app.pipelines.base import strip_code_blocks

    bomb = '{"a": 1}' + ("\n" + " " * 99) * 3000  # fence 없는 공백 폭탄
    t0 = time.perf_counter()
    out = strip_code_blocks(bomb)
    elapsed = time.perf_counter() - t0
    assert out == '{"a": 1}'
    assert elapsed < 2.0, f"strip_code_blocks O(n²) 회귀 의심: {elapsed:.1f}s"


def test_extract_json_object_whitespace_bomb_is_linear():
    """[회귀] 동일 폭탄을 extract_json_object 전체 경로로 — 워커 동결 장애의 입력 형상."""
    import time

    from app.pipelines.base import extract_json_object

    bomb = '```json\n{"nodes": [], "relationships": []}\n' + " \n" * 150_000
    t0 = time.perf_counter()
    parsed = extract_json_object(bomb)
    elapsed = time.perf_counter() - t0
    assert parsed == {"nodes": [], "relationships": []}
    assert elapsed < 2.0, f"extract_json_object O(n²) 회귀 의심: {elapsed:.1f}s"


def test_extract_json_object_first_to_last_brace():
    """구 그리디 정규식 `\\{[\\s\\S]*\\}` 과 동일 의미: 첫 `{` ~ 마지막 `}`."""
    from app.pipelines.base import extract_json_object

    assert extract_json_object('머리말 {"a": {"b": 2}} 꼬리') == {"a": {"b": 2}}
    assert extract_json_object("{}") == {}
    assert extract_json_object("브레이스 없음") == {}
    assert extract_json_object("} 역순 {") == {}
    # 첫 { ~ 마지막 } 구간이 invalid JSON 이면 방어적으로 빈 dict (구 동작 동일)
    assert extract_json_object('{"a": 1} 그리고 {"b": 2}') == {}


@pytest.mark.asyncio
async def test_strict_retry_drops_response_schema():
    """[2026-06-12] schema 강제 첫 시도가 깡통이면 재시도는 schema 없이 —
    responseSchema 제약에 막힌 모델(flash-lite 운영 실측) 구제 경로 검증."""
    from app.pipelines.base import generate_json_with_retry

    class _SchemaRecording:
        def __init__(self):
            self.schema_flags: List[bool] = []

        async def generate(self, prompt, *, temperature=0.2, response_schema=None):
            self.schema_flags.append(response_schema is not None)
            if len(self.schema_flags) == 1:
                return _FakeResult(text="깡통 출력 (브레이스 없음)")
            return _FakeResult(text='{"ok": 1}')

    g = _SchemaRecording()
    parsed, _ = await generate_json_with_retry(
        g, "p", response_schema={"type": "object"}
    )
    assert parsed == {"ok": 1}
    # 첫 호출은 schema 있음, strict retry 는 schema 없음
    assert g.schema_flags == [True, False]
