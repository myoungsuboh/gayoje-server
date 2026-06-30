"""
GeminiError 분류 + gemini_error_to_http 헬퍼 단위 테스트.

[목적]
운영에서 LLM 호출이 quota / auth / transient 로 실패할 때 사용자에게
친절한 토스트를 띄울 수 있도록 BE 가 응답을 정확히 분류해야 함.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.clients.gemini_client import (
    GeminiError,
    _classify_status,
    gemini_error_to_http,
)


# ─── _classify_status ──────────────────────────────────────────


class TestClassifyStatus:
    def test_429_always_quota(self):
        assert _classify_status(429, "") == "quota"
        assert _classify_status(429, "anything") == "quota"

    def test_401_always_auth(self):
        assert _classify_status(401, "") == "auth"
        assert _classify_status(401, "{'error': 'invalid_api_key'}") == "auth"

    def test_403_with_quota_keyword_is_quota(self):
        # Google API 는 quota 도 403 으로 줄 때가 있음 — 본문으로 구분
        for body in [
            '{"error": {"status": "RESOURCE_EXHAUSTED"}}',
            "quota exceeded",
            "rate limit exceeded",
            '{"message": "Resource has been exhausted"}',
        ]:
            assert _classify_status(403, body) == "quota", f"failed for: {body}"

    def test_403_without_quota_keyword_is_auth(self):
        for body in [
            "permission denied",
            "forbidden",
            '{"error": "API key not valid"}',
            "",
        ]:
            assert _classify_status(403, body) == "auth", f"failed for: {body}"

    def test_5xx_transient(self):
        for code in [500, 502, 503, 504]:
            assert _classify_status(code, "") == "transient"

    def test_other_4xx_unknown(self):
        # 400 / 404 등은 unknown (코드 버그 가능성)
        assert _classify_status(400, "") == "unknown"
        assert _classify_status(404, "") == "unknown"

    def test_empty_body_safe(self):
        # 본문이 None / 빈 문자열 / 비-string 이어도 crash 안 함
        assert _classify_status(403, "") == "auth"
        assert _classify_status(403, None) == "auth"


# ─── GeminiError.kind 보존 ─────────────────────────────────────


class TestGeminiError:
    def test_default_kind_unknown(self):
        e = GeminiError("oops")
        assert e.kind == "unknown"

    def test_explicit_kind(self):
        e = GeminiError("rate limit", kind="quota")
        assert e.kind == "quota"
        assert str(e) == "rate limit"

    def test_isinstance_runtime_error(self):
        # raise 시 RuntimeError 로 catch 가능해야 backward compat
        with pytest.raises(RuntimeError):
            raise GeminiError("x", kind="auth")


# ─── gemini_error_to_http ──────────────────────────────────────


class TestGeminiErrorToHttp:
    """detail 은 dict shape: {code, message, legacy_message}.

    - code: FE 가 구조화 매칭에 사용 (axios interceptor)
    - message: 사용자 친화 한국어 (prefix 없음)
    - legacy_message: `[gemini_*]` prefix 유지 — 옛 FE 빌드 호환용
    """

    def test_quota_maps_to_429(self):
        exc = gemini_error_to_http(GeminiError("Gemini 429: ...", kind="quota"))
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 429
        assert exc.detail["code"] == "gemini_quota"
        assert "AI 사용량 한도" in exc.detail["message"]
        # 하위 호환 — 옛 FE 의 startsWith("[gemini_quota]") 매칭 보장
        assert exc.detail["legacy_message"].startswith("[gemini_quota]")

    def test_auth_maps_to_503(self):
        exc = gemini_error_to_http(GeminiError("invalid key", kind="auth"))
        assert exc.status_code == 503
        assert exc.detail["code"] == "gemini_auth"
        assert "관리자" in exc.detail["message"]
        assert exc.detail["legacy_message"].startswith("[gemini_auth]")

    def test_transient_maps_to_502(self):
        exc = gemini_error_to_http(GeminiError("Gemini 502: ...", kind="transient"))
        assert exc.status_code == 502
        assert exc.detail["code"] == "gemini_transient"
        assert exc.detail["legacy_message"].startswith("[gemini_transient]")

    def test_unknown_maps_to_502_with_message(self):
        exc = gemini_error_to_http(GeminiError("weird", kind="unknown"))
        assert exc.status_code == 502
        assert exc.detail["code"] == "gemini_unknown"
        # 원본 메시지 snippet 포함
        assert "weird" in exc.detail["message"]
        assert exc.detail["legacy_message"].startswith("[gemini_unknown]")

    def test_detail_snippet_truncation(self):
        # 너무 긴 메시지는 200 자로 잘림 — message + legacy_message 둘 다 cap.
        long_msg = "x" * 500
        exc = gemini_error_to_http(GeminiError(long_msg, kind="unknown"))
        assert len(exc.detail["message"]) < 250
        assert len(exc.detail["legacy_message"]) < 260   # prefix 길이 여유
        assert exc.detail["code"] == "gemini_unknown"

    def test_invalid_response_kind_falls_to_unknown_branch(self):
        # 'invalid_response' 는 quota/auth/transient 어디에도 안 맞음 → unknown 분기
        exc = gemini_error_to_http(
            GeminiError("empty candidates", kind="invalid_response")
        )
        assert exc.status_code == 502
        assert exc.detail["code"] == "gemini_unknown"
        assert exc.detail["legacy_message"].startswith("[gemini_unknown]")

    def test_legacy_message_preserves_original_prefix_byte_for_byte(self):
        """옛 FE 빌드가 'detail.startsWith(\"[gemini_quota]\")' 같이 패턴 매칭하던 코드 보호."""
        exc = gemini_error_to_http(GeminiError("x", kind="quota"))
        assert exc.detail["legacy_message"].startswith("[gemini_quota] ")
