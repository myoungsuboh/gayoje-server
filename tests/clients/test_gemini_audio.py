"""
gemini_audio.transcribe_audio 단위 테스트 — httpx mock (실제 네트워크/Gemini 없음).

검증 (라우트 테스트가 mock 으로 건너뛰던 클라이언트 내부 경로):
- Files API 업로드 → ACTIVE 즉시 → generateContent → text 반환
- 업로드 직후 PROCESSING → ACTIVE 까지 폴링한 뒤 generateContent (회의 녹음 실패 버그 방지)
- 파일 처리 FAILED → 502
- finishReason=MAX_TOKENS → truncated=True (긴 회의 무음 truncation 감지)
- GEMINI_API_KEY 미설정 → 503
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
import pytest

from app.clients import gemini_audio as ga


class _FakeResp:
    def __init__(self, status_code: int, json_data: Optional[Dict[str, Any]] = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self) -> Dict[str, Any]:
        return self._json


class _FakeClient:
    """post(/upload, /generateContent) + get(poll) + delete 를 URL 패턴으로 디스패치."""

    def __init__(
        self,
        *,
        upload: _FakeResp,
        generate: _FakeResp,
        statuses: Optional[List[_FakeResp]] = None,
        delete_status: int = 200,
    ) -> None:
        self.upload = upload
        self.generate = generate
        self.statuses = list(statuses or [])
        self.delete_status = delete_status
        self.get_calls = 0
        self.post_urls: List[str] = []
        self.gen_body: Optional[Dict[str, Any]] = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def post(self, url: str, **kw):
        self.post_urls.append(url)
        if "/upload/" in url:
            return self.upload
        if "generateContent" in url:
            self.gen_body = kw.get("json")
            return self.generate
        raise AssertionError(f"unexpected POST {url}")

    async def get(self, url: str, **kw):
        self.get_calls += 1
        if self.statuses:
            return self.statuses.pop(0)
        # 큐가 비면 마지막을 ACTIVE 로 (테스트 무한루프 방지)
        return _FakeResp(200, {"state": "ACTIVE"})

    async def delete(self, url: str, **kw):
        return _FakeResp(self.delete_status)


def _gen_ok(text: str = "A: 안녕하세요", finish: str = "STOP") -> _FakeResp:
    return _FakeResp(200, {
        "candidates": [{
            "content": {"parts": [{"text": text}]},
            "finishReason": finish,
        }],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "totalTokenCount": 120},
    })


@pytest.fixture(autouse=True)
def _env_and_nosleep(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    # 폴링 sleep 제거 — 테스트 즉시 진행.
    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(ga.asyncio, "sleep", _no_sleep)


def _install(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(ga.httpx, "AsyncClient", lambda *a, **k: client)


@pytest.mark.asyncio
async def test_active_immediately_no_poll(monkeypatch):
    """업로드 응답이 이미 ACTIVE → 폴링 GET 없이 바로 전사."""
    client = _FakeClient(
        upload=_FakeResp(200, {"file": {"uri": "u://1", "name": "files/1", "state": "ACTIVE"}}),
        generate=_gen_ok(),
    )
    _install(monkeypatch, client)
    res = await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert res.text == "A: 안녕하세요"
    assert res.truncated is False
    assert res.usage.total_tokens == 120
    assert client.get_calls == 0  # ACTIVE 였으니 폴링 안 함


@pytest.mark.asyncio
async def test_polls_until_active(monkeypatch):
    """업로드 직후 PROCESSING → ACTIVE 까지 폴링한 뒤 전사 (핵심 회귀: 즉시 호출 시 400)."""
    client = _FakeClient(
        upload=_FakeResp(200, {"file": {"uri": "u://1", "name": "files/1", "state": "PROCESSING"}}),
        statuses=[
            _FakeResp(200, {"state": "PROCESSING"}),
            _FakeResp(200, {"state": "ACTIVE"}),
        ],
        generate=_gen_ok(),
    )
    _install(monkeypatch, client)
    res = await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert res.text == "A: 안녕하세요"
    assert client.get_calls == 2  # PROCESSING 1회 + ACTIVE 1회


@pytest.mark.asyncio
async def test_file_failed_state_raises_502(monkeypatch):
    from fastapi import HTTPException
    client = _FakeClient(
        upload=_FakeResp(200, {"file": {"uri": "u://1", "name": "files/1", "state": "PROCESSING"}}),
        statuses=[_FakeResp(200, {"state": "FAILED"})],
        generate=_gen_ok(),
    )
    _install(monkeypatch, client)
    with pytest.raises(HTTPException) as ei:
        await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert ei.value.status_code == 502


@pytest.mark.asyncio
async def test_truncated_flag_on_max_tokens(monkeypatch):
    """finishReason=MAX_TOKENS → truncated=True (긴 회의 무음 truncation 감지)."""
    client = _FakeClient(
        upload=_FakeResp(200, {"file": {"uri": "u://1", "name": "files/1", "state": "ACTIVE"}}),
        generate=_gen_ok(text="아주 긴 전사...", finish="MAX_TOKENS"),
    )
    _install(monkeypatch, client)
    res = await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert res.truncated is True


@pytest.mark.asyncio
async def test_missing_api_key_raises_503(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(HTTPException) as ei:
        await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert ei.value.status_code == 503


def test_default_model_is_2_5_flash_and_prompt_language_neutral():
    """모델 기본값 2.5-flash + 프롬프트가 발화 언어 그대로 전사(한국어 강제 X)."""
    assert ga.STT_MODEL == "gemini-2.5-flash"
    # 다국어 회의를 한국어로 번역하지 않도록 — 발화 언어 보존 지시
    assert "말한 언어 그대로" in ga.DEFAULT_TRANSCRIBE_PROMPT
    assert "한국어로 정확하게" not in ga.DEFAULT_TRANSCRIBE_PROMPT  # 옛 강제 문구 제거


@pytest.mark.asyncio
async def test_generate_uses_large_output_cap(monkeypatch):
    """긴 회의 truncation 완화 — maxOutputTokens 가 2.5-flash 상한(65536)으로 전송된다."""
    client = _FakeClient(
        upload=_FakeResp(200, {"file": {"uri": "u://1", "name": "files/1", "state": "ACTIVE"}}),
        generate=_gen_ok(),
    )
    _install(monkeypatch, client)
    await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert client.gen_body["generationConfig"]["maxOutputTokens"] == 65536


@pytest.mark.asyncio
async def test_thinking_disabled_for_2_5_model(monkeypatch):
    """2.5 계열은 전사에 불필요한 thinking 을 끈다 (thinkingBudget=0)."""
    client = _FakeClient(
        upload=_FakeResp(200, {"file": {"uri": "u://1", "name": "files/1", "state": "ACTIVE"}}),
        generate=_gen_ok(),
    )
    _install(monkeypatch, client)
    await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg", model="gemini-2.5-flash")
    assert client.gen_body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


@pytest.mark.asyncio
async def test_thinking_config_omitted_for_non_2_5_model(monkeypatch):
    """thinkingConfig 미지원 모델로 override 시엔 붙이지 않는다 (API 오류 방지)."""
    client = _FakeClient(
        upload=_FakeResp(200, {"file": {"uri": "u://1", "name": "files/1", "state": "ACTIVE"}}),
        generate=_gen_ok(),
    )
    _install(monkeypatch, client)
    await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg", model="gemini-1.5-flash")
    assert "thinkingConfig" not in client.gen_body["generationConfig"]


# ─── [2026-06] transient 재시도 (#24) ────────────────────────────────────

class _SeqClient:
    """generateContent 호출마다 gen_seq 를 순서대로 반환 — 재시도 검증용(업로드는 ACTIVE 고정)."""

    def __init__(self, gen_seq: List[_FakeResp]) -> None:
        self.gen_seq = list(gen_seq)
        self.gen_calls = 0
        self.upload_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def post(self, url: str, **kw):
        if "/upload/" in url:
            self.upload_calls += 1
            return _FakeResp(200, {"file": {"uri": "u://1", "name": "files/1", "state": "ACTIVE"}})
        if "generateContent" in url:
            self.gen_calls += 1
            item = self.gen_seq.pop(0) if len(self.gen_seq) > 1 else self.gen_seq[0]
            if isinstance(item, Exception):
                raise item
            return item
        raise AssertionError(f"unexpected POST {url}")

    async def get(self, url: str, **kw):
        return _FakeResp(200, {"state": "ACTIVE"})

    async def delete(self, url: str, **kw):
        return _FakeResp(200)


@pytest.mark.asyncio
async def test_retry_recovers_on_transient_5xx(monkeypatch):
    """일시적 500 → 재시도로 성공 (전사 호출 2회)."""
    monkeypatch.setattr(ga, "GEMINI_STT_MAX_RETRIES", 2)  # ambient env(롤백 0 등)에 비의존
    client = _SeqClient([_FakeResp(500, text="boom"), _gen_ok()])
    _install(monkeypatch, client)
    res = await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert res.text == "A: 안녕하세요"
    assert client.gen_calls == 2


@pytest.mark.asyncio
async def test_retry_exhausts_then_502(monkeypatch):
    """지속 5xx → 재시도 소진 후 502 (호출 = max_retries+1 = 3)."""
    from fastapi import HTTPException
    monkeypatch.setattr(ga, "GEMINI_STT_MAX_RETRIES", 2)
    client = _SeqClient([_FakeResp(503), _FakeResp(503), _FakeResp(503)])
    _install(monkeypatch, client)
    with pytest.raises(HTTPException) as ei:
        await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert ei.value.status_code == 502
    assert client.gen_calls == 3


@pytest.mark.asyncio
async def test_retry_disabled_when_max_retries_zero(monkeypatch):
    """GEMINI_STT_MAX_RETRIES=0 → 재시도 없이 즉시 502 (호출 1회) — 롤백 스위치."""
    from fastapi import HTTPException
    monkeypatch.setattr(ga, "GEMINI_STT_MAX_RETRIES", 0)
    client = _SeqClient([_FakeResp(500), _gen_ok()])
    _install(monkeypatch, client)
    with pytest.raises(HTTPException) as ei:
        await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert ei.value.status_code == 502
    assert client.gen_calls == 1


@pytest.mark.asyncio
async def test_4xx_fast_fail_no_retry(monkeypatch):
    """400 같은 비-429 4xx 는 재시도 없이 즉시 실패 (호출 1회)."""
    from fastapi import HTTPException
    monkeypatch.setattr(ga, "GEMINI_STT_MAX_RETRIES", 2)
    client = _SeqClient([_FakeResp(400, text="bad request")])
    _install(monkeypatch, client)
    with pytest.raises(HTTPException):
        await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert client.gen_calls == 1


@pytest.mark.asyncio
async def test_poll_transient_error_retried_not_optimistic(monkeypatch):
    """폴링 중 일시적 500 → 즉시 낙관통과 않고 재폴링해 ACTIVE 확인 (A5)."""
    client = _FakeClient(
        upload=_FakeResp(200, {"file": {"uri": "u://1", "name": "files/1", "state": "PROCESSING"}}),
        statuses=[_FakeResp(500), _FakeResp(200, {"state": "ACTIVE"})],
        generate=_gen_ok(),
    )
    _install(monkeypatch, client)
    res = await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert res.text == "A: 안녕하세요"
    assert client.get_calls == 2


def test_retry_delay_exponential_capped():
    """_retry_delay: 지수 백오프(2**attempt) + _RETRY_BACKOFF_CAP 상한."""
    assert ga._retry_delay(0) == 1.0
    assert ga._retry_delay(1) == 2.0
    assert ga._retry_delay(2) == 4.0
    assert ga._retry_delay(10) == 8.0  # cap


@pytest.mark.asyncio
async def test_429_not_retried_fast_fail(monkeypatch):
    """429(쿼터 한도)는 재시도 안 함 — 즉시 502(친화 메시지), generateContent 1회."""
    from fastapi import HTTPException
    monkeypatch.setattr(ga, "GEMINI_STT_MAX_RETRIES", 2)
    client = _SeqClient([_FakeResp(429), _gen_ok()])
    _install(monkeypatch, client)
    with pytest.raises(HTTPException) as ei:
        await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert ei.value.status_code == 502
    assert client.gen_calls == 1


@pytest.mark.asyncio
async def test_timeout_not_retried_fast_fail(monkeypatch):
    """타임아웃은 재시도 안 함(누적 지연→FE 상한 초과 방지) — 즉시 전파, generateContent 1회."""
    from fastapi import HTTPException
    monkeypatch.setattr(ga, "GEMINI_STT_MAX_RETRIES", 2)
    client = _SeqClient([httpx.ReadTimeout("slow"), _gen_ok()])
    _install(monkeypatch, client)
    with pytest.raises(HTTPException) as ei:
        await ga.transcribe_audio(b"\x00" * 100, mime_type="audio/mpeg")
    assert ei.value.status_code == 502
    assert client.gen_calls == 1
