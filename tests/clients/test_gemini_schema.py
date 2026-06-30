"""
GeminiClient.generate(response_schema=...) 단위 테스트.

[검증 대상]
- schema=None 이면 기존 동작 (LiteLLM body 에 response_format 없음 / Google body 에
  responseSchema 없음 + responseMimeType=text/plain)
- schema 전달 시 LiteLLM 경로: body.response_format = {type: json_schema, ...}
- schema 전달 시 Google 경로: body.generationConfig.responseSchema + application/json
- TrackedGemini 가 schema 를 inner 로 그대로 passthrough
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.clients.gemini_client import GeminiClient, TokenAccumulator, TrackedGemini


# ─── httpx.AsyncClient mock — request body 캡처 ──────────────────


class _CapturingResponse:
    """httpx.Response 흉내 — status_code / json() / text 제공."""

    def __init__(self, status_code: int, json_data: Dict[str, Any]):
        self.status_code = status_code
        self._json = json_data
        self.text = json.dumps(json_data)

    def json(self):
        return self._json


class _CapturingClient:
    """httpx.AsyncClient 흉내 — POST 호출의 body 를 캡처."""

    def __init__(self, response_data: Dict[str, Any]):
        self.captured_bodies: List[Dict[str, Any]] = []
        self.captured_urls: List[str] = []
        self._response_data = response_data
        self.is_closed = False

    async def post(self, url: str, *, headers=None, json=None, timeout=None, **kwargs):
        # [2026-06-01] per-call timeout override 추가로 client.post(timeout=...) 가
        # 전달됨 — httpx.AsyncClient.post 처럼 수용. (**kwargs 로 향후 인자도 안전 흡수)
        self.captured_urls.append(url)
        self.captured_bodies.append(json)
        return _CapturingResponse(200, self._response_data)

    async def aclose(self):
        self.is_closed = True


LITELLM_OK_RESPONSE = {
    "choices": [{
        "message": {"content": '{"key": "value"}'},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    "model": "gemini-2.5-flash",
}

GOOGLE_OK_RESPONSE = {
    "candidates": [{
        "content": {"parts": [{"text": '{"key": "value"}'}]},
        "finishReason": "STOP",
    }],
    "usageMetadata": {
        "promptTokenCount": 10,
        "candidatesTokenCount": 5,
        "totalTokenCount": 15,
    },
}


# ─── LiteLLM 경로 schema 전달 검증 ──────────────────────────────


@pytest.mark.asyncio
async def test_litellm_no_schema_preserves_legacy_body():
    """schema 미전달 시 LiteLLM body 에 response_format 없어야 함."""
    with patch.dict(os.environ, {
        "LITELLM_PROXY_URL": "http://proxy:4000",
        "LITELLM_MASTER_KEY": "sk-test",
    }):
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(LITELLM_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test prompt", temperature=0.1)

        assert len(mock_http.captured_bodies) == 1
        body = mock_http.captured_bodies[0]
        assert "response_format" not in body  # ← 핵심
        assert body["temperature"] == 0.1
        assert body["messages"][0]["content"] == "test prompt"


@pytest.mark.asyncio
async def test_litellm_with_schema_sets_response_format():
    """schema 전달 시 LiteLLM body.response_format 에 json_schema 가 들어가야 함."""
    schema = {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    }
    with patch.dict(os.environ, {
        "LITELLM_PROXY_URL": "http://proxy:4000",
        "LITELLM_MASTER_KEY": "sk-test",
    }):
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(LITELLM_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1, response_schema=schema)

        body = mock_http.captured_bodies[0]
        assert "response_format" in body
        rf = body["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["schema"] == schema
        # strict 필드 미명시 — OpenAI spec 상 optional. default false 라
        # 우리 동적 properties schema 와 호환. 구버전 LiteLLM 도 안전.
        assert "strict" not in rf["json_schema"]


# ─── lite 계열 schema 생략 (2026-06-12 공백 폭주 장애) ──────────────


_SIMPLE_SCHEMA = {"type": "object", "properties": {"key": {"type": "string"}}}


@pytest.mark.asyncio
async def test_litellm_flash_lite_skips_schema():
    """flash-lite 는 schema 강제 시 공백 폭주(운영 실측) — response_format 미전송."""
    with patch.dict(os.environ, {
        "LITELLM_PROXY_URL": "http://proxy:4000",
        "LITELLM_MASTER_KEY": "sk-test",
    }):
        client = GeminiClient(model="gemini-2.5-flash-lite")
        mock_http = _CapturingClient(LITELLM_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1, response_schema=_SIMPLE_SCHEMA)

        body = mock_http.captured_bodies[0]
        assert "response_format" not in body  # ← 핵심: lite 는 schema 생략
        assert body["model"] == "gemini-2.5-flash-lite"


@pytest.mark.asyncio
async def test_litellm_per_call_lite_override_skips_schema():
    """인스턴스 기본이 flash 라도 per-call model 오버라이드가 lite 면 schema 생략
    (영향도 분석 등 stage-level lite 다운그레이드 경로 보호)."""
    with patch.dict(os.environ, {
        "LITELLM_PROXY_URL": "http://proxy:4000",
        "LITELLM_MASTER_KEY": "sk-test",
    }):
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(LITELLM_OK_RESPONSE)
        client._client = mock_http

        await client.generate(
            "test", temperature=0.1,
            response_schema=_SIMPLE_SCHEMA, model="gemini-2.5-flash-lite",
        )

        body = mock_http.captured_bodies[0]
        assert "response_format" not in body


@pytest.mark.asyncio
async def test_litellm_schema_skip_env_disable():
    """GEMINI_SCHEMA_SKIP_MODELS='' 이면 보호 비활성 — lite 에도 schema 전송 (이전 동작)."""
    with patch.dict(os.environ, {
        "LITELLM_PROXY_URL": "http://proxy:4000",
        "LITELLM_MASTER_KEY": "sk-test",
        "GEMINI_SCHEMA_SKIP_MODELS": "",
    }):
        client = GeminiClient(model="gemini-2.5-flash-lite")
        mock_http = _CapturingClient(LITELLM_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1, response_schema=_SIMPLE_SCHEMA)

        body = mock_http.captured_bodies[0]
        assert "response_format" in body


# ─── Google 직접 호출 경로 schema 전달 검증 ──────────────────────


@pytest.mark.asyncio
async def test_google_direct_no_schema_uses_text_plain():
    """schema 미전달 시 responseMimeType=text/plain (기존 동작)."""
    # LiteLLM env 제거 → 직접 호출 모드 강제
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False):
        # LITELLM_* 만 명시적으로 비움
        os.environ.pop("LITELLM_PROXY_URL", None)
        os.environ.pop("LITELLM_MASTER_KEY", None)
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(GOOGLE_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1)

        body = mock_http.captured_bodies[0]
        gc = body["generationConfig"]
        assert gc["responseMimeType"] == "text/plain"
        assert "responseSchema" not in gc


@pytest.mark.asyncio
async def test_google_direct_with_schema_sets_response_schema():
    """schema 전달 시 generationConfig.responseSchema + application/json."""
    schema = {
        "type": "object",
        "properties": {"nodes": {"type": "array"}},
    }
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False):
        os.environ.pop("LITELLM_PROXY_URL", None)
        os.environ.pop("LITELLM_MASTER_KEY", None)
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(GOOGLE_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1, response_schema=schema)

        body = mock_http.captured_bodies[0]
        gc = body["generationConfig"]
        assert gc["responseMimeType"] == "application/json"
        assert gc["responseSchema"] == schema


# ─── TrackedGemini schema passthrough ──────────────────────────


class _SchemaCapturingFake:
    """schema 인자가 inner 까지 전달되는지 검증용 fake."""

    def __init__(self):
        self.last_kwargs: Dict[str, Any] = {}

    async def generate(self, prompt: str, *, temperature: float = 0.2, response_schema=None):
        self.last_kwargs = {
            "prompt": prompt,
            "temperature": temperature,
            "response_schema": response_schema,
        }
        # GeminiResult 호환 — 최소 필드
        class _R:
            text = '{"k": 1}'
            model = "fake"
            finish_reason = "stop"
            usage = None
        return _R()


@pytest.mark.asyncio
async def test_tracked_gemini_passes_schema_to_inner():
    fake = _SchemaCapturingFake()
    accum = TokenAccumulator()
    tracked = TrackedGemini(fake, accum)

    schema = {"type": "object"}
    await tracked.generate("hello", temperature=0.1, response_schema=schema)

    assert fake.last_kwargs["response_schema"] == schema
    assert fake.last_kwargs["temperature"] == 0.1


@pytest.mark.asyncio
async def test_tracked_gemini_works_with_legacy_fake():
    """옛 FakeGemini (response_schema 인자 모름) — TypeError 흡수 후 fallback."""

    class _LegacyFake:
        async def generate(self, prompt: str, *, temperature: float = 0.2):
            class _R:
                text = "ok"
                usage = None
            return _R()

    fake = _LegacyFake()
    accum = TokenAccumulator()
    tracked = TrackedGemini(fake, accum)

    # response_schema 전달해도 TypeError 안 터지고 정상 동작
    result = await tracked.generate("hello", temperature=0.1, response_schema={"x": 1})
    assert result.text == "ok"


# ─── [2026-06-04] 안전필터 오탐 방지 (content_filter → 빈 응답 사고) ───────────


def _assert_block_none(settings):
    """4개 표준 카테고리가 모두 BLOCK_NONE 인지 검증."""
    cats = {s["category"]: s["threshold"] for s in settings}
    for c in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    ):
        assert cats.get(c) == "BLOCK_NONE", f"{c} not BLOCK_NONE"


@pytest.mark.asyncio
async def test_litellm_body_includes_safety_settings():
    """LiteLLM body 에 safety_settings(BLOCK_NONE) 가 포함돼 content_filter 오탐 차단."""
    with patch.dict(os.environ, {
        "LITELLM_PROXY_URL": "http://proxy:4000",
        "LITELLM_MASTER_KEY": "sk-test",
    }):
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(LITELLM_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1)

        body = mock_http.captured_bodies[0]
        assert "safety_settings" in body
        _assert_block_none(body["safety_settings"])


@pytest.mark.asyncio
async def test_google_direct_body_includes_safety_settings():
    """Google 직접 호출 body 에 safetySettings(BLOCK_NONE) 포함."""
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False):
        os.environ.pop("LITELLM_PROXY_URL", None)
        os.environ.pop("LITELLM_MASTER_KEY", None)
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(GOOGLE_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1)

        body = mock_http.captured_bodies[0]
        assert "safetySettings" in body
        _assert_block_none(body["safetySettings"])


@pytest.mark.asyncio
async def test_litellm_empty_content_raises_friendly_safety_message():
    """content_filter 로 빈 content 면 raw JSON dump 가 아닌 안전필터 안내 메시지로 raise."""
    from app.clients.gemini_client import GeminiError

    empty_resp = {
        "choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}],
        "model": "gemini-2.5-flash",
    }
    with patch.dict(os.environ, {
        "LITELLM_PROXY_URL": "http://proxy:4000",
        "LITELLM_MASTER_KEY": "sk-test",
    }):
        client = GeminiClient(model="gemini-2.5-flash")
        client._client = _CapturingClient(empty_resp)

        with pytest.raises(GeminiError) as ei:
            await client.generate("test", temperature=0.1)

        msg = str(ei.value)
        assert "안전 필터" in msg                 # 사용자 친화 안내
        assert "choices" not in msg               # raw JSON dump 노출 안 함
        assert ei.value.kind == "invalid_response"


# ─── [2026-06-10] GEMINI_THINKING_BUDGET 노브 ──────────────────────


@pytest.mark.asyncio
async def test_thinking_budget_unset_keeps_legacy_body():
    """env 미설정이면 thinking 관련 키가 body 에 없어야 함 — 기본 동작 변화 0."""
    with patch.dict(os.environ, {
        "LITELLM_PROXY_URL": "http://proxy:4000",
        "LITELLM_MASTER_KEY": "sk-test",
    }):
        os.environ.pop("GEMINI_THINKING_BUDGET", None)
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(LITELLM_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1)

        body = mock_http.captured_bodies[0]
        assert "thinking" not in body
        assert "reasoning_effort" not in body


@pytest.mark.asyncio
async def test_thinking_budget_zero_sets_reasoning_effort_disable():
    """0 = thinking 비활성 — LiteLLM 경로는 reasoning_effort='disable'."""
    with patch.dict(os.environ, {
        "LITELLM_PROXY_URL": "http://proxy:4000",
        "LITELLM_MASTER_KEY": "sk-test",
        "GEMINI_THINKING_BUDGET": "0",
    }):
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(LITELLM_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1)

        body = mock_http.captured_bodies[0]
        assert body["reasoning_effort"] == "disable"
        assert "thinking" not in body


@pytest.mark.asyncio
async def test_thinking_budget_positive_sets_thinking_param():
    """N>0 = thinking 토큰 상한 — LiteLLM 경로는 thinking.budget_tokens."""
    with patch.dict(os.environ, {
        "LITELLM_PROXY_URL": "http://proxy:4000",
        "LITELLM_MASTER_KEY": "sk-test",
        "GEMINI_THINKING_BUDGET": "2048",
    }):
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(LITELLM_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1)

        body = mock_http.captured_bodies[0]
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
        assert "reasoning_effort" not in body


@pytest.mark.asyncio
async def test_thinking_budget_direct_path_sets_thinking_config():
    """Google 직접 호출 경로 — generationConfig.thinkingConfig.thinkingBudget."""
    with patch.dict(os.environ, {
        "GEMINI_API_KEY": "test-key",
        "GEMINI_THINKING_BUDGET": "1024",
    }):
        os.environ.pop("LITELLM_PROXY_URL", None)
        os.environ.pop("LITELLM_MASTER_KEY", None)
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(GOOGLE_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1)

        body = mock_http.captured_bodies[0]
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 1024}


@pytest.mark.asyncio
async def test_thinking_budget_invalid_value_ignored():
    """정수가 아니면 무시 — 기존 동작 유지 (운영 env 오타 안전망)."""
    with patch.dict(os.environ, {
        "LITELLM_PROXY_URL": "http://proxy:4000",
        "LITELLM_MASTER_KEY": "sk-test",
        "GEMINI_THINKING_BUDGET": "abc",
    }):
        client = GeminiClient(model="gemini-2.5-flash")
        mock_http = _CapturingClient(LITELLM_OK_RESPONSE)
        client._client = mock_http

        await client.generate("test", temperature=0.1)

        body = mock_http.captured_bodies[0]
        assert "thinking" not in body
        assert "reasoning_effort" not in body
