"""
/api/gateway/transcribeAudio 라우트 회귀 테스트.

검증:
- multipart upload → mock 전사 함수 → 응답 shape `{result:"success", text, model, tokens_used}`
- MIME 검증 (415): 지원 안 하는 형식 거부
- 크기 검증 (413): 30MB 초과 거부
- 빈 파일 (400): 0 byte 거부
- Ownership: projectName 있고 권한 없으면 403 (assert_owns 직접 raise)
- Token 누적: 응답 정상 시 add_tokens 호출됨
"""
from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import gateway_compat_routes as pt
from app.clients.gemini_audio import TranscribeResult
from app.clients.gemini_client import TokenUsage
from app.service.user_repository import UserPublic


def _make_app() -> FastAPI:
    """라우트만 mount 한 미니 앱 — 미들웨어/DB 무관."""
    app = FastAPI()
    # current_user dependency override
    app.dependency_overrides[pt.get_current_user] = lambda: UserPublic(
        id="test-user", email="t@b.com", name="tester"
    )
    app.include_router(pt.router)
    return app


def _fake_audio() -> bytes:
    """가짜 audio 본문 — 길이만 의미 있음. MIME 검증을 위해 별도 header 필요."""
    return b"\x00" * 1024  # 1KB


@pytest.fixture(autouse=True)
def _stub_quota_and_ownership(monkeypatch):
    """test 별로 quota / ownership 가드를 no-op 으로 치환."""
    monkeypatch.setattr(
        pt.quota, "assert_tokens_within_limit",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        pt.ownership_repository, "assert_owns",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        pt.usage_repository, "add_tokens",
        AsyncMock(return_value=100),
    )


def test_transcribe_success_returns_text(monkeypatch):
    """정상 경로 — mock 전사 함수가 텍스트 반환 → 라우트가 응답 shape 유지."""
    async def fake_transcribe(audio_bytes, mime_type, **kwargs):
        return TranscribeResult(
            text="안녕하세요. 회의를 시작합니다.",
            usage=TokenUsage(prompt_tokens=200, completion_tokens=50, total_tokens=250),
            model="gemini-1.5-flash",
        )
    monkeypatch.setattr(pt, "transcribe_audio", fake_transcribe)

    client = TestClient(_make_app())
    resp = client.post(
        "/api/gateway/transcribeAudio",
        files={"file": ("meeting.mp3", _fake_audio(), "audio/mpeg")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["result"] == "success"
    assert data["text"] == "안녕하세요. 회의를 시작합니다."
    assert data["model"] == "gemini-1.5-flash"
    assert data["tokens_used"] == 250
    # add_tokens 가 호출됐는지 확인 (best-effort 누적)
    pt.usage_repository.add_tokens.assert_awaited_once_with("t@b.com", 250)


def test_transcribe_rejects_unsupported_mime():
    """415 — 지원 안 하는 MIME 거부."""
    client = TestClient(_make_app())
    resp = client.post(
        "/api/gateway/transcribeAudio",
        files={"file": ("doc.pdf", _fake_audio(), "application/pdf")},
    )
    assert resp.status_code == 415
    assert "지원하지 않는" in resp.json()["detail"]


def test_transcribe_rejects_empty_file(monkeypatch):
    """400 — 0 byte 파일 거부."""
    monkeypatch.setattr(pt, "transcribe_audio", AsyncMock())
    client = TestClient(_make_app())
    resp = client.post(
        "/api/gateway/transcribeAudio",
        files={"file": ("empty.mp3", b"", "audio/mpeg")},
    )
    assert resp.status_code == 400
    assert "빈 파일" in resp.json()["detail"]


def test_transcribe_rejects_oversize_file(monkeypatch):
    """413 — 30MB 초과 거부."""
    monkeypatch.setattr(pt, "transcribe_audio", AsyncMock())
    client = TestClient(_make_app())
    big = b"\x00" * (31 * 1024 * 1024)
    resp = client.post(
        "/api/gateway/transcribeAudio",
        files={"file": ("big.mp3", big, "audio/mpeg")},
    )
    assert resp.status_code == 413
    assert "30MB" in resp.json()["detail"]


def test_transcribe_accepts_all_common_audio_mimes(monkeypatch):
    """일반적 회의 녹음 포맷 — mp3/m4a/wav/webm/mp4 모두 통과해야."""
    async def fake_transcribe(audio_bytes, mime_type, **kwargs):
        return TranscribeResult(
            text="ok", usage=TokenUsage(total_tokens=10), model="gemini-1.5-flash",
        )
    monkeypatch.setattr(pt, "transcribe_audio", fake_transcribe)
    client = TestClient(_make_app())
    for mime in ("audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/webm", "video/mp4"):
        resp = client.post(
            "/api/gateway/transcribeAudio",
            files={"file": (f"test.bin", _fake_audio(), mime)},
        )
        assert resp.status_code == 200, f"mime={mime} resp={resp.text}"


def test_transcribe_recovers_generic_mime_by_extension(monkeypatch):
    """빈/generic MIME 이라도 .m4a 등 확장자면 보정해 통과 (정상 녹음 false 415 방지)."""
    captured = {}

    async def fake_transcribe(audio_bytes, mime_type, **kwargs):
        captured["mime"] = mime_type
        return TranscribeResult(
            text="ok", usage=TokenUsage(total_tokens=10), model="gemini-1.5-flash",
        )
    monkeypatch.setattr(pt, "transcribe_audio", fake_transcribe)

    client = TestClient(_make_app())
    for filename, sent_mime in [("rec.m4a", ""), ("rec.m4a", "application/octet-stream"), ("rec.mp3", "")]:
        resp = client.post(
            "/api/gateway/transcribeAudio",
            files={"file": (filename, _fake_audio(), sent_mime)},
        )
        assert resp.status_code == 200, f"{filename}/{sent_mime}: {resp.text}"
    # 보정된 mime 이 Gemini 클라이언트로 전달됐는지
    assert captured["mime"] == "audio/mpeg"  # 마지막 rec.mp3


def test_transcribe_generic_mime_unknown_ext_still_415(monkeypatch):
    """generic MIME + 오디오 아닌 확장자 → 여전히 415."""
    client = TestClient(_make_app())
    resp = client.post(
        "/api/gateway/transcribeAudio",
        files={"file": ("notes.txt", _fake_audio(), "application/octet-stream")},
    )
    assert resp.status_code == 415


def test_transcribe_passes_truncated_flag(monkeypatch):
    """전사가 잘리면 응답에 truncated=True 가 실린다 (FE 재시도 안내용)."""
    async def fake_transcribe(audio_bytes, mime_type, **kwargs):
        return TranscribeResult(
            text="부분 전사", usage=TokenUsage(total_tokens=10),
            model="gemini-1.5-flash", truncated=True,
        )
    monkeypatch.setattr(pt, "transcribe_audio", fake_transcribe)
    client = TestClient(_make_app())
    resp = client.post(
        "/api/gateway/transcribeAudio",
        files={"file": ("m.mp3", _fake_audio(), "audio/mpeg")},
    )
    assert resp.status_code == 200
    assert resp.json()["truncated"] is True


def test_transcribe_with_project_name_runs_ownership_check(monkeypatch):
    """projectName 전달 시 assert_owns 호출됨."""
    async def fake_transcribe(*a, **kw):
        return TranscribeResult(
            text="ok", usage=TokenUsage(total_tokens=10), model="gemini-1.5-flash",
        )
    monkeypatch.setattr(pt, "transcribe_audio", fake_transcribe)

    client = TestClient(_make_app())
    resp = client.post(
        "/api/gateway/transcribeAudio",
        files={"file": ("m.mp3", _fake_audio(), "audio/mpeg")},
        data={"projectName": "myproj"},
    )
    assert resp.status_code == 200
    pt.ownership_repository.assert_owns.assert_awaited_with("t@b.com", "myproj")


def test_transcribe_without_project_skips_ownership(monkeypatch):
    """projectName 없으면 assert_owns 안 부름 — 단순 전사만 사용 가능."""
    async def fake_transcribe(*a, **kw):
        return TranscribeResult(
            text="ok", usage=TokenUsage(total_tokens=10), model="gemini-1.5-flash",
        )
    monkeypatch.setattr(pt, "transcribe_audio", fake_transcribe)

    client = TestClient(_make_app())
    resp = client.post(
        "/api/gateway/transcribeAudio",
        files={"file": ("m.mp3", _fake_audio(), "audio/mpeg")},
    )
    assert resp.status_code == 200
    pt.ownership_repository.assert_owns.assert_not_awaited()
