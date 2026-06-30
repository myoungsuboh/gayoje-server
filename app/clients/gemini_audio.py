"""
Gemini audio transcription (STT) — 회의 녹음 파일 → 한국어 전사.

[배경 — 2026-05-18]
"Engineering Log 에 어떤 정보를 어떻게 써야 하는지 막막하다" 사용자 피드백 대응.
회의 녹음 파일(.mp3 / .m4a / .mp4 / .wav 등)을 업로드 → Gemini 가 한국어로 정확히
전사 → 사용자는 textarea 에서 검토/수정 후 기존 Confirm & Archive 흐름으로 저장.

[설계]
GeminiClient 의 generate() 는 텍스트 전용. 음성은 multimodal — 별도 헬퍼.
LiteLLM proxy 의 multimodal 라우팅 호환성을 신뢰하기 어려워 **Google AI 직접 호출**.
GEMINI_API_KEY (or GOOGLE_API_KEY) 가 필요 (production deploy 시 LITELLM 변수와
별개로 설정해야 함 — 미설정 시 503 + 명확한 에러 메시지).

[흐름]
1. Files API 로 업로드 → file_uri 획득. (auto-expire 48h)
2. generateContent({parts:[file_data(file_uri), text(prompt)]}).
3. text + usageMetadata.totalTokenCount 추출.
4. 호출자가 토큰을 usage_repository.add_tokens 로 누적.

[토큰 단가]
Gemini Flash 기준 audio input ~32 tokens/sec = 1920 tokens/min.
1시간 회의 = ~115K tokens 입력. 출력은 전사 텍스트 분량 (보통 입력보다 작음).
free tier 100K tokens/월 안에서 ~50분 가능.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import HTTPException, status

from app.clients.gemini_client import (
    DEFAULT_API_BASE,
    GeminiError,
    TokenUsage,
)

logger = logging.getLogger(__name__)

# STT 전용 모델 — Flash 가 audio 도 지원하면서 cost 효율적.
# [2026-06] gemini-2.5-flash 로 상향: (1) 1.5 계열 노후화 대비, (2) 출력 토큰 상한이
# 훨씬 커(최대 65536) 긴 회의 전사 truncation 이 크게 줄어든다. GEMINI_STT_MODEL 로 override 가능.
STT_MODEL = os.getenv("GEMINI_STT_MODEL", "gemini-2.5-flash")

# [2026-06] 언어 하드코딩 제거 — 제품이 다국어(ko/en/ja/zh)인데 한국어 전사를 강제하면
# 영어/일본어 회의가 한국어로 '번역'돼 원어가 유실됐다. 발화 언어 그대로 전사하게 한다.
DEFAULT_TRANSCRIBE_PROMPT = (
    "이 음성은 회의 녹음입니다. **실제로 말한 언어 그대로** 정확하게 전사해 주세요 "
    "(한국어면 한국어, 영어면 영어, 일본어면 일본어 — 다른 언어로 번역하지 마세요). "
    "다음 규칙을 따르세요:\n"
    "- 화자가 여러 명이면 'A:', 'B:' 등으로 화자를 구분해서 표기 (이름을 알 수 없으면 알파벳).\n"
    "- 같은 화자의 연속 발화는 줄바꿈으로 구분.\n"
    "- 음성 노이즈, '음...', '어...', 'uh...' 같은 filler 는 생략.\n"
    "- 전사 외 부연 설명 / 요약 / 결정사항 추출은 하지 마세요 — raw 전사만.\n"
    "- 약어 / 기술 용어는 원어 그대로 (예: API, PR, DB).\n"
    "출력은 전사된 발화 텍스트만 포함하세요."
)


@dataclass(frozen=True)
class TranscribeResult:
    """음성 전사 결과 — 라우트 핸들러가 응답 build 에 사용."""

    text: str
    usage: TokenUsage
    model: str
    # Gemini Files API 가 응답에 audio duration 을 명시하지 않으므로 별도 추정 없이 None.
    duration_sec: Optional[float] = None
    # [2026-06] maxOutputTokens 도달로 전사가 중간에 잘렸는지 (finishReason=MAX_TOKENS).
    # True 면 호출자/FE 가 "일부 잘림 — 더 짧게 나눠 재시도" 를 안내한다.
    truncated: bool = False


# Files API 업로드 직후 audio/video 는 PROCESSING 상태일 수 있고, ACTIVE 가 되기 전에
# generateContent 를 호출하면 400 ("File is not in an ACTIVE state") 이 난다. ACTIVE 까지 폴링.
_FILE_ACTIVE_TIMEOUT = 60.0   # 초 — ACTIVE 대기 상한
_FILE_POLL_INTERVAL = 1.5     # 초 — 폴링 간격

# [2026-06] transient(5xx/연결오류) 재시도 — 일시적 Gemini 장애가 즉시 502 로 전파돼
# 사용자 실패로 직결되던 문제(재시도 전무). gemini_client 의 지수 백오프 패턴과 동일.
# 의도적 제외: (1) 429 — Gemini 쿼터 한도라 보통 수십 초 대기를 요구하는데 동기 라우트에서
#   그만큼 블록할 수 없다. '사용량 한도, 잠시 후 재시도' 친화 메시지로 즉시 fast-fail.
# (2) 타임아웃 — 이미 timeout(240s) 만큼 소비했으므로 재시도하면 누적 지연만 키워 FE axios
#   상한(300s)을 넘긴다. fast-fail. → 재시도는 '빠르게 실패하는' 5xx/연결오류에만, 백오프는
#   _RETRY_BACKOFF_CAP 으로 상한. 최악 wall-clock ≈ 1회 timeout(240s) + 소량 백오프 < FE 300s.
#   GEMINI_STT_MAX_RETRIES=0 으로 즉시 비활성(롤백).
GEMINI_STT_MAX_RETRIES = max(0, int(os.getenv("GEMINI_STT_MAX_RETRIES", "2")))
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})  # 429·타임아웃은 의도적 제외(위 주석)
_RETRY_BACKOFF_CAP = 8.0   # 초 — 1회 백오프 대기 상한
_POLL_MAX_ERRORS = 3       # 폴링 중 연속 transient 오류 허용(초과 시 낙관적 통과)


def _retry_delay(attempt: int) -> float:
    """지수 백오프 2**attempt, 상한 _RETRY_BACKOFF_CAP."""
    return min(2.0 ** attempt, _RETRY_BACKOFF_CAP)


async def _request_with_retry(
    client: "httpx.AsyncClient",
    method: str,
    url: str,
    *,
    max_retries: Optional[int] = None,
    **kwargs,
) -> "httpx.Response":
    """transient(5xx/연결오류) 재시도 + 지수 백오프. 4xx·429·타임아웃은 즉시 반환/전파(fast-fail).
    재시도 소진 시 마지막 응답/예외를 그대로 돌려 호출부의 기존 에러 매핑·메시지를 보존한다.

    max_retries=None 이면 모듈 GEMINI_STT_MAX_RETRIES 를 *호출 시점*에 읽는다(테스트/롤백 용이).
    """
    if max_retries is None:
        max_retries = GEMINI_STT_MAX_RETRIES
    call = client.post if method.upper() == "POST" else client.get
    attempt = 0
    while True:
        try:
            resp = await call(url, **kwargs)
        except httpx.HTTPError as e:
            # 타임아웃은 이미 timeout 만큼 소비 — 재시도하면 누적 지연만 키워 FE 상한(300s) 초과. fast-fail.
            if isinstance(e, httpx.TimeoutException) or attempt >= max_retries:
                raise
            await asyncio.sleep(_retry_delay(attempt))
            attempt += 1
            continue
        if resp.status_code in _RETRYABLE_STATUS and attempt < max_retries:
            await asyncio.sleep(_retry_delay(attempt))
            attempt += 1
            continue
        return resp


async def _wait_until_file_active(
    client: "httpx.AsyncClient",
    api_base: str,
    file_name: str,
    api_key: str,
    *,
    timeout: float = _FILE_ACTIVE_TIMEOUT,
) -> None:
    """Files API 파일이 ACTIVE 가 될 때까지 폴링. FAILED 면 502, 시간 초과면 504.

    상태 조회 자체가 실패하면 낙관적으로 통과시킨다 (이어지는 generateContent 가 최종 검증).
    """
    deadline = time.monotonic() + timeout
    errors = 0
    while True:
        try:
            resp = await client.get(f"{api_base}/{file_name}", params={"key": api_key})
        except httpx.HTTPError:
            # [2026-06 A5] 일시적 폴링 오류를 즉시 '낙관적 통과' 하면 PROCESSING 중
            # generateContent 가 400 난다. 몇 번 재폴링한 뒤에만 통과한다.
            errors += 1
            if errors > _POLL_MAX_ERRORS or time.monotonic() >= deadline:
                logger.debug("Files API status poll network error x%d — proceed optimistically", errors)
                return
            await asyncio.sleep(_FILE_POLL_INTERVAL)
            continue
        if resp.status_code >= 400:
            errors += 1
            if errors > _POLL_MAX_ERRORS or time.monotonic() >= deadline:
                logger.debug("Files API status poll http %s x%d — proceed optimistically", resp.status_code, errors)
                return
            await asyncio.sleep(_FILE_POLL_INTERVAL)
            continue
        errors = 0
        try:
            state = (resp.json() or {}).get("state")
        except Exception:  # noqa: BLE001
            state = None
        # 상태 미상(None) 이면 더 기다릴 근거가 없으니 통과 — 무한루프 방지.
        if not state or state == "ACTIVE":
            return
        if state == "FAILED":
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="업로드한 음성 파일 처리에 실패했습니다. 다른 파일로 다시 시도해 주세요.",
            )
        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="음성 파일 준비가 시간 내에 끝나지 않았습니다. 잠시 후 다시 시도해 주세요.",
            )
        await asyncio.sleep(_FILE_POLL_INTERVAL)


async def transcribe_audio(
    audio_bytes: bytes,
    mime_type: str,
    *,
    prompt: str = DEFAULT_TRANSCRIBE_PROMPT,
    model: str = STT_MODEL,
    timeout: float = 240.0,
) -> TranscribeResult:
    """
    Google AI Files API + generateContent 로 음성 전사.

    Args:
        audio_bytes: 원본 파일 바이트. 30MB 이내 권장.
        mime_type:   "audio/mpeg" / "audio/mp4" / "audio/wav" 등.
        prompt:      LLM 에 전달할 instruction. 기본은 한국어 raw 전사.
        model:       Gemini 모델 이름.
        timeout:     HTTP 요청 timeout (sec). 30MB 음성은 보통 30-120 초.

    Returns:
        TranscribeResult — text + usage tokens.

    Raises:
        HTTPException(503) — GEMINI_API_KEY 미설정.
        HTTPException(502) — Gemini API 오류.
        HTTPException(400) — 빈 응답.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        logger.error("transcribe_audio: GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "음성 전사 서비스 설정이 누락됐습니다. "
                "관리자에게 GEMINI_API_KEY 설정을 요청해 주세요."
            ),
        )

    api_base = DEFAULT_API_BASE.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as client:
        # ── 1) Files API 로 업로드 → file_uri 획득 ──────────────────
        # simple media upload: uploadType=media + raw bytes body.
        upload_url = f"{api_base.replace('/v1beta', '')}/upload/v1beta/files"
        try:
            upload_resp = await _request_with_retry(
                client, "POST", upload_url,
                params={"key": api_key, "uploadType": "media"},
                content=audio_bytes,
                headers={
                    "Content-Type": mime_type,
                    "X-Goog-Upload-Header-Content-Length": str(len(audio_bytes)),
                    "X-Goog-Upload-Header-Content-Type": mime_type,
                },
            )
        except httpx.HTTPError as e:
            logger.exception("Files API upload network error")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"파일 업로드 실패 (network): {e}",
            ) from e

        if upload_resp.status_code >= 400:
            snippet = upload_resp.text[:300]
            logger.warning(
                "Files API upload failed: %s — %s",
                upload_resp.status_code, snippet,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"파일 업로드 실패 ({upload_resp.status_code}): {snippet}",
            )
        try:
            file_resource = upload_resp.json().get("file") or {}
        except Exception:
            file_resource = {}
        file_uri = file_resource.get("uri")
        file_name = file_resource.get("name")  # "files/abc123"
        if not file_uri:
            logger.warning(
                "Files API response missing 'uri': %s", upload_resp.text[:300],
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="파일 업로드 응답이 비정상입니다.",
            )

        # ── 1.5) ACTIVE 대기 — audio/video 는 업로드 직후 PROCESSING 일 수 있고,
        # ACTIVE 전에 generateContent 를 호출하면 400 이 난다. 이미 ACTIVE 면 즉시 통과.
        if file_resource.get("state") != "ACTIVE" and file_name:
            await _wait_until_file_active(client, api_base, file_name, api_key)

        # ── 2) generateContent — file_data 참조 + transcription prompt ──
        gen_url = f"{api_base}/models/{model}:generateContent"
        generation_config = {
            # 전사는 deterministic 에 가깝게 — temperature 낮춤.
            "temperature": 0.1,
            # [2026-06] 출력 상한 — gemini-2.5-flash 는 최대 65536 까지 지원한다.
            # 1.5(8192 클램프) 시절엔 ~5-10분만 넘어도 잘렸다. 길게 잡아 긴 회의도 커버
            # (그래도 넘으면 finishReason=MAX_TOKENS 로 truncated 표시).
            "maxOutputTokens": 65536,
        }
        # [2026-06] 2.5 계열은 thinking 이 기본 ON — 전사는 추론이 불필요하므로 끈다
        # (thinking 토큰·지연 절감). thinkingConfig 미지원 모델로 override 된 경우엔 안 붙인다.
        if model.startswith("gemini-2.5"):
            generation_config["thinkingConfig"] = {"thinkingBudget": 0}
        gen_body = {
            "contents": [
                {
                    "parts": [
                        {"file_data": {"mime_type": mime_type, "file_uri": file_uri}},
                        {"text": prompt},
                    ]
                }
            ],
            "generationConfig": generation_config,
        }
        try:
            gen_resp = await _request_with_retry(
                client, "POST", gen_url, params={"key": api_key}, json=gen_body,
            )
        except httpx.HTTPError as e:
            logger.exception("generateContent network error")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"전사 실패 (network): {e}",
            ) from e

        if gen_resp.status_code >= 400:
            snippet = gen_resp.text[:400]
            logger.warning(
                "generateContent failed: %s — %s", gen_resp.status_code, snippet,
            )
            # 사용자 친화 메시지
            user_msg = "전사 중 오류가 발생했습니다."
            if gen_resp.status_code == 429:
                user_msg = "Gemini API 사용량 한도에 도달했습니다. 잠시 후 다시 시도해 주세요."
            elif gen_resp.status_code in (401, 403):
                user_msg = "Gemini API 인증 오류입니다. 관리자에게 문의해 주세요."
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"{user_msg} (Gemini {gen_resp.status_code})",
            )

        try:
            data = gen_resp.json()
        except Exception as e:
            logger.exception("generateContent invalid JSON")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"전사 응답 파싱 실패: {e}",
            ) from e

        # 응답에서 텍스트 추출
        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            if block_reason:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"전사가 차단됐습니다 (사유: {block_reason}). 다른 파일로 시도해 주세요.",
                )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="전사 응답이 비어 있습니다. 다시 시도해 주세요.",
            )

        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if not text:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="전사 결과가 비어 있습니다. 음성이 너무 짧거나 인식되지 않았을 수 있습니다.",
            )

        # [2026-06] maxOutputTokens 도달 → 전사가 중간에 잘렸다. 긴 회의(1시간+)는 출력
        # 토큰 상한을 넘어 일부만 반환되는데, 그동안 경고 없이 정상처럼 반환돼 데이터가
        # 조용히 유실됐다. finishReason 으로 감지해 호출자/FE 가 재시도를 안내하게 한다.
        truncated = candidates[0].get("finishReason") == "MAX_TOKENS"
        if truncated:
            logger.warning(
                "transcribe_audio truncated at maxOutputTokens (finishReason=MAX_TOKENS) text_len=%d",
                len(text),
            )

        # 토큰 사용량 — 호출자가 add_tokens 로 누적
        usage_raw = data.get("usageMetadata") or {}
        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("promptTokenCount") or 0),
            completion_tokens=int(usage_raw.get("candidatesTokenCount") or 0),
            total_tokens=int(usage_raw.get("totalTokenCount") or 0),
        )

        logger.info(
            "transcribe_audio ok: model=%s tokens=%d text_len=%d truncated=%s",
            model, usage.total_tokens, len(text), truncated,
        )

        # ── 3) 파일 정리 (best-effort) ───────────────────────────────
        # Files API 는 48h 후 auto-expire. 즉시 삭제하면 quota 소비 즉시 해제됨.
        # 실패해도 핵심 결과는 이미 받았으므로 swallow.
        try:
            if file_name:
                await client.delete(
                    f"{api_base}/{file_name}", params={"key": api_key},
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("Files API delete failed (best-effort): %s", e)

        return TranscribeResult(
            text=text, usage=usage, model=model, duration_sec=None, truncated=truncated,
        )
