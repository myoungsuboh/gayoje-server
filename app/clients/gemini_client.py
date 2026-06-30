"""
Gemini async wrapper.

LangChain Gemini agent 조합을 파이썬 단일 호출로 대체한다.
재시도/타임아웃/idempotency_key 책임 포함.

[호출 경로]
  - LITELLM_PROXY_URL 가 설정되어 있고 LITELLM_MASTER_KEY 도 있으면
    LiteLLM proxy 의 OpenAI 호환 endpoint (`/v1/chat/completions`) 를 호출.
    → multi-key 자동 로테이션 + 429 시 다른 키 자동 retry + flash 모델 fallback.
  - 둘 중 하나라도 없으면 Google Generative Language API (REST) 를 직접 호출.
    → 기존 단일 GEMINI_API_KEY 경로 (legacy / 로컬 dev).

운영에서는 docker-compose 의 litellm 서비스가 동작 중이라 proxy 경로가 기본.

[에러 분류 — GeminiError.kind]
사용자에게 친절한 메시지로 변환하기 위해 응답 status + 본문 키워드로 분류:
  - 'quota'    : Gemini API 사용량 한도 (429 또는 403 + quota/RESOURCE_EXHAUSTED)
  - 'auth'     : API 키 인증 오류 (401 또는 403 권한 부족)
  - 'transient': 일시 오류 (5xx, 재시도 후에도 실패)
  - 'unknown'  : 분류 못 한 4xx 또는 그 외

라우트 / FastAPI exception_handler 가 kind 에 따라 적절한 HTTP 상태 + detail 매핑.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

# 기본 모델 — GeminiClient(model=...) 명시 안 했을 때 사용하는 fallback.
# [정책]
# 운영에선 호출자가 `quota.get_model_for_subscription(...)` 결과를 model 인자로
# 명시 — 즉 이 DEFAULT_MODEL 은 거의 발화 안 됨. 단일 모델 운영 (GEMINI_MODEL 만
# 설정한 legacy 환경) 호환 + 일부 테스트가 model 인자 없이 GeminiClient() 호출
# 하는 경우 (e.g. mock 어려운 stage) 안전망.
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# 기본 per-call timeout (sec) — env 로 운영 중 조절 가능.
# [2026-06-10] 90 → 240: design(SPACK/DDD/Arch)·autofix 등 대형 프롬프트는
# gemini-2.5-flash 도 90s 를 넘김 (실사고: design 잡이 90s 타임아웃 ×3 재시도
# 모두 실패 → 파이프라인 전체 사망). litellm proxy 쪽 timeout 보다 길게 잡아야
# proxy 의 키 로테이션 재시도가 끝나기 전에 클라이언트가 끊고 중복 요청을
# 쏘는 증폭(worker 3 × proxy 3 = 최대 9 회 Google 호출)을 피한다.
# 빈 문자열/오타 env 방어 — float("") 는 import 시점 crash (2026-05-16 사고 패턴).
try:
    DEFAULT_TIMEOUT_SEC = float(os.getenv("GEMINI_TIMEOUT_SEC") or "240")
except ValueError:
    DEFAULT_TIMEOUT_SEC = 240.0


def _thinking_budget() -> Optional[int]:
    """GEMINI_THINKING_BUDGET env — Gemini thinking 토큰 상한 (성능 노브).

    [2026-06-10 — 기본값은 동작 변화 0]
    미설정/빈 문자열이면 None → 요청 body 에 아무것도 추가하지 않음 (현재와 동일:
    gemini-2.5-flash 는 dynamic thinking ON). 정수 설정 시:
      - 0  : thinking 비활성 — flash/flash-lite 만 허용 (2.5-pro 는 비활성 불가)
      - N>0: thinking 토큰 상한 N (예: 2048)
    design 3-stage 같은 schema 강제 구조화 추출에선 thinking 이 호출 시간의
    30~50% 로 추정 — evals 로 품질 확인 후 운영 env 로 켠다. 매 호출 평가이므로
    Portainer env 변경 + 컨테이너 재시작만으로 on/off (코드 재배포 불필요).
    generate()/generate_via_litellm 비스트리밍 경로에만 적용 — 스트리밍(인터뷰 등)
    은 체감 TTFT 가 따로 중요해 별도 판단 전까지 기존 동작 유지.
    """
    raw = (os.getenv("GEMINI_THINKING_BUDGET") or "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("GEMINI_THINKING_BUDGET 가 정수가 아님 — 무시: %r", raw)
        return None


def _schema_skip_substrings() -> List[str]:
    """response_schema(structured output) 를 보내면 안 되는 모델 substring 목록.

    [2026-06-12 운영 장애 — Lite 오버플로우 공백 폭주]
    gemini-2.5-flash-lite 는 responseSchema 강제 모드에서 스키마의 자유형
    properties(빈 object) 자리에 들어서는 순간 허용 토큰이 공백뿐이라 출력
    한도까지 공백만 생성한다. 실측: 응답 52만 자가 전부 공백(finish=length),
    CPS/PRD extract 에서 결정적 재현, 호출당 ~2분 + 수만 토큰이 사용자 Lite
    버킷에 과금. 같은 모델이라도 schema 없는 자유 텍스트 호출은 항상 정상
    JSON 을 반환했으므로 lite 계열은 schema 를 빼고 호출한다 — 파싱은 기존
    extract_json_object 경로(generate_json_with_retry)가 처리.

    env `GEMINI_SCHEMA_SKIP_MODELS` (콤마 구분 substring) 로 운영에서 조정.
    빈 문자열 설정 시 비활성 (모든 모델에 schema 전송 — 이전 동작).
    """
    raw = os.getenv("GEMINI_SCHEMA_SKIP_MODELS")
    if raw is None:
        return ["flash-lite"]
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def _schema_unsupported(model: str) -> bool:
    """이 모델에 response_schema 를 보내면 퇴행 출력이 나는가 (substring 매치)."""
    m = (model or "").lower()
    return any(sub in m for sub in _schema_skip_substrings())


@dataclass(frozen=True)
class TokenUsage:
    """LLM 호출의 토큰 사용량. 비용/지연 가시성용.

    LiteLLM proxy 와 Google API 직접 호출 양쪽 응답 모두에서 추출 시도하고,
    필드가 없으면 0 으로 둔다 (None 보다 합산이 자연).

    [2026-05-27 cached_tokens] Gemini 2.5+ implicit context caching 적중
    여부 가시화 — prompt_tokens 중 캐시에서 재사용된 토큰 수.
      - 0 → cache miss (또는 1024-token 임계 미달)
      - >0 → cache hit (75% 비용 할인 + TTFT 단축)
    프롬프트 prefix 재구조 효과 검증에 사용.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )


class TokenAccumulator:
    """한 작업(job) 동안의 LLM 토큰 사용량 누적기.

    [용도]
    quota 기능: arq job 또는 라우트의 sync LLM 호출이 시작될 때 인스턴스 1개 생성,
    `TrackedGemini` 로 GeminiClient 를 wrap → 자동 누적. job 끝에서
    `accumulator.total.total_tokens` 만큼 `usage_repository.add_tokens` 호출.

    [왜 mutable 인가]
    PipelineContext 는 frozen dataclass 라 안에 mutable 객체 두면 race 위험. 그러나
    arq job 1건 = 단일 async task = 동시 호출 없음. pipeline 내부 LLM 호출들이 sequential
    또는 asyncio.gather 인데 양쪽 모두 같은 event loop 라 mutate race 없음 (GIL + single-threaded).
    """

    def __init__(self) -> None:
        self.total = TokenUsage()
        # [2026-06 mid-job 강등] 잡 진행 중 메인 한도를 넘는 순간 lite 풀로 강등된
        # 경우, 강등 시점까지의 누적 토큰(main 버킷). None=강등 없음(전량 단일 버킷).
        # _persist_token_usage 가 이 값으로 main/lite 분할 적재.
        self.main_bucket_tokens: Optional[int] = None

    def add(self, usage: TokenUsage) -> None:
        self.total = self.total + usage


class TrackedGemini:
    """GeminiClient 를 wrap 해서 generate() 호출마다 accumulator 에 자동 누적.

    [동작]
    inner.generate() 호출 → result.usage 를 accumulator.add() 로 누적 후 result 그대로 반환.
    pipeline 코드는 ctx.gemini.generate() 만 호출하면 됨 — 누적은 투명하게 일어남.

    [수명]
    하나의 job / 한 번의 sync 라우트 호출 = TrackedGemini 인스턴스 1개. accumulator 도 1개.
    inner GeminiClient 는 worker 의 lifeline-shared 인스턴스 (HTTP pool 공유) — wrap 만 함.

    [PipelineContext.gemini Protocol 호환]
    `async def generate(prompt, *, temperature=...)` 시그니처만 만족하면 됨.
    """

    def __init__(
        self,
        inner: "GeminiClient",
        accumulator: TokenAccumulator,
        *,
        downgrade_lite_inner: Optional["GeminiClient"] = None,
        base_usage: int = 0,
        main_limit: int = 0,
        force_model: Optional[str] = None,
        downgrade_force_model: Optional[str] = None,
    ) -> None:
        self._inner = inner
        self._accumulator = accumulator
        # [2026-06 overflow 모델 강제]
        # overflow 결정(메인 소진 유료 등급)이면 lite 모델로 작업해야 한다. 그런데
        # 일부 파이프라인(예: 인터뷰)이 generate(model="gemini-2.5-flash") 처럼 비싼
        # 모델을 명시 강제해 overflow→lite 강등을 무력화하던 버그가 있었다. force_model
        # 이 설정되면 호출자의 model 인자를 무시하고 항상 이 모델을 쓴다(중앙 차단).
        self._force_model = force_model
        # mid-job 강등(#1)이 트리거되면 이후 호출에 강제할 lite 모델명.
        self._downgrade_force_model = downgrade_force_model
        # [2026-06 mid-job 강등 안전망]
        # _tracked_ctx 가 main 모드 + 오버플로우 가능 등급일 때만 채워서 전달.
        # 잡 진행 중 (base_usage + 이번 잡 누적) 이 main_limit 을 넘는 순간, 이후
        # LLM 호출을 lite 풀(downgrade_lite_inner)로 전환하고 강등 시점을 기록.
        # 결정이 잡 시작 1회뿐이라 한 잡이 폭주하면(또는 동시 잡이 누적 반영 전이면)
        # 메인이 한도를 크게 넘던 race 를 잡 내부에서 차단.
        self._downgrade_lite_inner = downgrade_lite_inner
        self._base_usage = base_usage
        self._main_limit = main_limit
        self._downgraded = False

    def _maybe_downgrade(self) -> None:
        """누적이 메인 한도를 넘으면 inner 를 lite 로 교체하고 분할 시점 기록.

        한 번만 전환 (이후 호출은 no-op). lite 클라이언트가 없거나 한도 정보가
        없으면(armed 아님) 아무것도 안 함.
        """
        if self._downgraded or self._downgrade_lite_inner is None or self._main_limit <= 0:
            return
        projected = self._base_usage + self._accumulator.total.total_tokens
        if projected >= self._main_limit:
            # 강등 시점까지의 이번 잡 누적 → main 버킷. 이후 → lite 버킷.
            self._accumulator.main_bucket_tokens = self._accumulator.total.total_tokens
            self._inner = self._downgrade_lite_inner
            self._downgraded = True
            # 이후 호출은 lite 모델 강제 — 호출자가 비싼 모델을 명시해도 무시.
            if self._downgrade_force_model is not None:
                self._force_model = self._downgrade_force_model

    async def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        response_schema: Optional[dict] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
    ) -> "GeminiResult":
        # [2026-05] response_schema passthrough — 호출자가 structured output 강제 시
        # GeminiClient 가 backend(LiteLLM/Google) 에 맞게 전달. None 이면 기존 동작.
        # [2026-05-26 perf A] model passthrough — stage 단위로 더 가벼운 모델로
        # 다운그레이드 가능 (e.g. impact analyzer → gemini-2.5-flash-lite).
        # [2026-06-01 fast-fail] timeout/max_retries passthrough — autofill 등 폴백
        # 전략이 중요한 호출이 primary 모델에 짧게 매달리도록.
        # FakeGemini 같은 테스트 fake 가 인자를 모를 수 있어 try/except 로
        # 방어적 호출 — 인자 지원 안 하면 점차 줄여서 호출 (backward compat).
        # [2026-06 overflow 모델 강제] force_model 이 설정됐으면 호출자의 model 인자를
        # 무시하고 강제 모델 사용 — overflow 가 비싼 모델로 우회되던 버그 차단.
        effective_model = self._force_model if self._force_model is not None else model
        try:
            result = await self._inner.generate(
                prompt, temperature=temperature,
                response_schema=response_schema, model=effective_model,
                timeout=timeout, max_retries=max_retries,
                max_output_tokens=max_output_tokens,
            )
        except TypeError:
            try:
                result = await self._inner.generate(
                    prompt, temperature=temperature,
                    response_schema=response_schema, model=effective_model,
                )
            except TypeError:
                try:
                    result = await self._inner.generate(
                        prompt, temperature=temperature, response_schema=response_schema,
                    )
                except TypeError:
                    # 옛 fake (response_schema/model 인자 모름) — bare 호출
                    result = await self._inner.generate(prompt, temperature=temperature)
        # 방어적 — fake 클라이언트 (e.g. tests/conftest.py 의 FakeGemini) 가 usage 미보유 케이스
        # 대응. 실제 GeminiClient 응답은 항상 TokenUsage 객체.
        usage = getattr(result, "usage", None)
        if usage is not None:
            self._accumulator.add(usage)
            # 누적 후 메인 한도 초과면 이후 호출을 lite 로 강등 (mid-job 안전망).
            self._maybe_downgrade()
        return result

    async def generate_stream(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        model: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """inner.generate_stream 을 위임하고, 스트림 소진 후 토큰 누적."""
        # [2026-06 overflow 모델 강제] force_model 설정 시 호출자 model 무시.
        effective_model = self._force_model if self._force_model is not None else model
        async for chunk in self._inner.generate_stream(
            prompt, temperature=temperature, model=effective_model,
        ):
            yield chunk
        usage = getattr(self._inner, "_last_stream_usage", None)
        if usage is not None:
            self._accumulator.add(usage)
            self._maybe_downgrade()


@dataclass(frozen=True)
class GeminiResult:
    text: str
    model: str
    finish_reason: Optional[str]
    usage: TokenUsage = TokenUsage()


# detail prefix — FE 가 메시지 패턴 매칭으로 사용자 친화 토스트 표시
_PREFIX_QUOTA = "[gemini_quota]"
_PREFIX_AUTH = "[gemini_auth]"
_PREFIX_TRANSIENT = "[gemini_transient]"
_PREFIX_UNKNOWN = "[gemini_unknown]"


# [2026-06-04] Gemini 안전필터 오탐 방지 — B2B 기획 도구의 회의록/명세 본문은 결제·인증·
# 보안 취약성·계정관리·서약서 등 정상 업무 용어를 포함하는데, Gemini 기본 임계값이 이를
# 위험 콘텐츠로 오분류해 content_filter 로 **빈 응답**(LiteLLM empty content)을 반환하던
# 운영 사고가 있었다. 정상 입력이므로 4개 표준 카테고리를 BLOCK_NONE 으로 완화한다.
# 동일 list 를 두 경로에 사용: LiteLLM body 키 `safety_settings`, Google 직접 body 키
# `safetySettings`. (CIVIC_INTEGRITY 는 일부 엔드포인트 미지원이라 제외 — 호환성 우선.)
_GEMINI_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


def _empty_content_message(finish_reason: Optional[str]) -> str:
    """빈 LLM 응답을 사용자 친화 메시지로 변환 (raw JSON dump 대신).

    content_filter/safety 가 원인이면 회의록 표현 조정을 안내하고, 그 외(빈 응답)는
    일시 오류 안내. (finish_reason 은 디버깅용으로 메시지에 짧게 포함.)"""
    fr = (finish_reason or "").lower()
    if "filter" in fr or "safety" in fr or "block" in fr:
        return (
            "AI 모델이 안전 필터로 빈 응답을 반환했습니다. 회의록에 민감하게 분류될 수 "
            "있는 표현이 있는지 확인 후 일부 문구를 바꿔 다시 시도해주세요. "
            f"(finish_reason={finish_reason})"
        )
    if "recitation" in fr:
        return (
            "AI 모델이 표절 방지 가드로 빈 응답을 반환했습니다. 잠시 후 다시 시도해주세요. "
            f"(finish_reason={finish_reason})"
        )
    return (
        "AI 모델이 빈 응답을 반환했습니다 (일시적 오류일 수 있음). 잠시 후 다시 "
        f"시도해주세요. (finish_reason={finish_reason})"
    )


def _classify_status(status_code: int, body: str) -> str:
    """
    Gemini HTTP 응답을 사람 친화 카테고리로 분류.

    [예시]
      429 ANY                        → 'quota'
      403 "...RESOURCE_EXHAUSTED..." → 'quota' (Google 가끔 403 으로 줌)
      403 "...permission denied..."  → 'auth'
      401 ANY                        → 'auth'
      500/502/503/504                → 'transient'
      그 외                          → 'unknown'
    """
    body_lower = (body or "").lower()
    if status_code == 429:
        return "quota"
    if status_code == 401:
        return "auth"
    if status_code == 403:
        # Google API 는 quota 도 403 으로 줄 때가 있음 — 본문으로 구분
        if any(
            kw in body_lower
            for kw in ("quota", "resource_exhausted", "rate limit", "exhausted")
        ):
            return "quota"
        return "auth"
    if 500 <= status_code < 600:
        return "transient"
    return "unknown"


class GeminiError(RuntimeError):
    """Gemini 호출이 비복구적으로 실패했을 때.

    Attributes:
        kind: 'quota' | 'auth' | 'transient' | 'invalid_response' | 'unknown'.
              라우트 / handler 가 사용자 메시지 결정에 사용.
    """

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


# 에러 코드 — FE 가 detail.code 로 패턴 매칭. 단순 문자열 prefix 보다 구조화.
# 하위 호환: detail 안에 prefix 도 유지 (기존 FE 버전이 prefix 매칭하던 시기).
ERROR_CODE_QUOTA = "gemini_quota"
ERROR_CODE_AUTH = "gemini_auth"
ERROR_CODE_TRANSIENT = "gemini_transient"
ERROR_CODE_UNKNOWN = "gemini_unknown"


def gemini_error_to_http(e: GeminiError) -> HTTPException:
    """
    GeminiError → FastAPI HTTPException 변환 (공통 헬퍼).

    상태 매핑:
        quota     → 429 Too Many Requests
        auth      → 503 Service Unavailable (서비스 키 문제 — 사용자 액션 불가)
        transient → 502 Bad Gateway
        그 외     → 502 Bad Gateway

    detail 구조 (FE 매칭용):
        {
            "code": "gemini_quota" | "gemini_auth" | "gemini_transient" | "gemini_unknown",
            "message": "사람이 읽을 한국어 안내"
        }

    이전엔 detail 이 `[gemini_quota] ...` 문자열 prefix — FE 가 .startsWith 매칭.
    이제 detail.code 로 구조화 매칭 + message 는 그대로 토스트.
    하위 호환: 동일한 prefix 문자열을 `legacy_message` 로 함께 노출 → 옛 FE 빌드도 동작.
    """
    msg = str(e)
    snippet = msg[:200]
    if e.kind == "quota":
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": ERROR_CODE_QUOTA,
                "message": (
                    "AI 사용량 한도에 도달했습니다 (Gemini quota). "
                    "1~2분 후 다시 시도해 주세요."
                ),
                "legacy_message": (
                    f"{_PREFIX_QUOTA} AI 사용량 한도에 도달했습니다 (Gemini quota). "
                    "1~2분 후 다시 시도해 주세요."
                ),
            },
        )
    if e.kind == "auth":
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": ERROR_CODE_AUTH,
                "message": "AI 서비스 인증 오류입니다. 관리자에게 문의해 주세요.",
                "legacy_message": (
                    f"{_PREFIX_AUTH} AI 서비스 인증 오류입니다. 관리자에게 문의해 주세요."
                ),
            },
        )
    if e.kind == "transient":
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": ERROR_CODE_TRANSIENT,
                "message": "AI 일시 오류입니다. 잠시 후 다시 시도해 주세요.",
                "legacy_message": (
                    f"{_PREFIX_TRANSIENT} AI 일시 오류입니다. 잠시 후 다시 시도해 주세요."
                ),
            },
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "code": ERROR_CODE_UNKNOWN,
            "message": f"AI 응답 실패: {snippet}",
            "legacy_message": f"{_PREFIX_UNKNOWN} AI 응답 실패: {snippet}",
        },
    )


class GeminiClient:
    """
    얇은 async 래퍼. SDK 의존을 피하고 REST를 직접 친다 — 의존성 표면 최소화.

    호출 경로:
      - LITELLM_PROXY_URL + LITELLM_MASTER_KEY 가 모두 있으면 LiteLLM proxy 사용
        (multi-key 라우팅 + flash fallback — proxy 가 알아서 처리).
      - 둘 중 하나라도 비어있으면 Google Generative Language API 직접 호출
        (단일 GEMINI_API_KEY).

    재시도 정책:
      - 5xx / timeout / network: 지수 백오프 3회 (2s, 4s, 8s)
      - 4xx (auth/quota): 재시도 안 함 (즉시 GeminiError)

    참고: LiteLLM proxy 경로에서는 proxy 자체가 num_retries=3 으로
          키 로테이션을 시도하므로, 여기서 추가 retry 는 proxy 가 모든 키를 다
          소진했거나 5xx 인 경우에만 의미가 있음.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        max_retries: int = 3,
    ):
        # LiteLLM proxy 모드 vs 직접 호출 모드 결정
        proxy_url = os.getenv("LITELLM_PROXY_URL")
        master_key = os.getenv("LITELLM_MASTER_KEY")
        self._use_litellm = bool(proxy_url and master_key)

        if self._use_litellm:
            # proxy 모드 — Google API 키는 LiteLLM 컨테이너 환경변수에만 필요.
            # backend 는 LITELLM_MASTER_KEY 로만 인증.
            self._api_key = master_key
            self._api_base = proxy_url.rstrip("/")
            logger.info("gemini_client: LiteLLM proxy 모드 (url=%s)", self._api_base)
        else:
            # 직접 호출 모드 (legacy / dev)
            key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not key:
                raise GeminiError(
                    "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set",
                    kind="auth",
                )
            self._api_key = key
            self._api_base = api_base.rstrip("/")
            logger.info("gemini_client: 직접 호출 모드 (Google API)")

        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._last_stream_usage: Optional[TokenUsage] = None

        # ─── HTTP connection pool (lazy) ─────────────────────────────
        # 같은 GeminiClient 인스턴스의 generate() 호출이 여러 번이면
        # TCP / TLS handshake 를 재사용 (httpx 내부 keep-alive pool).
        # 특히 worker(arq) 는 instance 가 프로세스 lifetime 살아있어서
        # 모든 job 의 LLM 호출이 같은 pool 공유 — 큰 효과.
        # backend 는 요청 단위 인스턴스라 한 요청 내 stage 사이만 재사용.
        #
        # lazy init: __init__ 은 sync 컨텍스트, AsyncClient 생성은 OK 지만
        # close 는 async. 첫 generate() 호출 시 만들어서 일관 async 컨텍스트
        # 안에서 라이프사이클 관리.
        self._client: Optional[httpx.AsyncClient] = None

    def _get_or_create_client(self) -> httpx.AsyncClient:
        """싱글톤 httpx.AsyncClient 반환 (lazy). aclose() 후 재호출 시 재생성."""
        if self._client is None or self._client.is_closed:
            # default limits: max_keepalive_connections=20, max_connections=100.
            # Gemini / LiteLLM 한 호스트라 더 작은 한도로 줄여도 OK 이지만 default 가
            # 메모리/소켓 부담 작아서 그대로 사용.
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """connection pool 정리. worker on_shutdown / backend lifespan 에서 호출."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ─── 스트리밍 ────────────────────────────────────────────────────

    async def generate_stream(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        model: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """텍스트 청크를 도착 순서대로 yield — response_schema 없이 plain text."""
        self._last_stream_usage = None
        if self._use_litellm:
            async for chunk in self._stream_via_litellm(
                prompt, temperature=temperature, model=model,
            ):
                yield chunk
        else:
            async for chunk in self._stream_direct(
                prompt, temperature=temperature, model=model,
            ):
                yield chunk
        # [토큰 가시성] 스트림 종료 후 사용량 로그 — 특히 cached_tokens 로 implicit
        # context caching 적중 확인 (보완 인터뷰의 기존 초안 재전송 비용이 캐시로
        # 할인되는지 검증). usage 가 None 이면(일부 fake/경로) 생략.
        u = self._last_stream_usage
        if u is not None:
            logger.info(
                "gemini_stream usage: prompt=%d cached=%d completion=%d total=%d (cache_hit=%.0f%%)",
                u.prompt_tokens, u.cached_tokens, u.completion_tokens, u.total_tokens,
                (100.0 * u.cached_tokens / u.prompt_tokens) if u.prompt_tokens else 0.0,
            )

    async def _stream_via_litellm(
        self,
        prompt: str,
        *,
        temperature: float,
        model: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """LiteLLM proxy 스트리밍 — OpenAI SSE 형식."""
        effective_model = model or self._model
        url = f"{self._api_base}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body: Dict[str, Any] = {
            "model": effective_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        client = self._get_or_create_client()
        try:
            async with client.stream(
                "POST", url, headers=headers, json=body, timeout=self._timeout,
            ) as resp:
                if resp.status_code >= 400:
                    body_bytes = await resp.aread()
                    kind = _classify_status(resp.status_code, body_bytes.decode())
                    raise GeminiError(
                        f"LiteLLM stream {resp.status_code}: {body_bytes[:200].decode()}",
                        kind=kind,
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw == "[DONE]":
                        break
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if choices:
                        content = (choices[0].get("delta") or {}).get("content") or ""
                        if content:
                            yield content
                    usage_raw = data.get("usage")
                    if usage_raw:
                        prompt_details = (usage_raw.get("prompt_tokens_details") or {})
                        self._last_stream_usage = TokenUsage(
                            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
                            total_tokens=int(usage_raw.get("total_tokens") or 0),
                            cached_tokens=int(prompt_details.get("cached_tokens") or 0),
                        )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise GeminiError(str(e), kind="transient") from e

    async def _stream_direct(
        self,
        prompt: str,
        *,
        temperature: float,
        model: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Google API 직접 스트리밍 (dev/legacy) — streamGenerateContent SSE."""
        effective_model = model or self._model
        url = (
            f"{self._api_base}/models/{effective_model}"
            f":streamGenerateContent?key={self._api_key}&alt=sse"
        )
        generation_config: Dict[str, Any] = {
            "temperature": temperature,
            "responseMimeType": "text/plain",
        }
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
            # [2026-06-04] 안전필터 오탐 방지 — LiteLLM 경로와 동일 정책 (직접 호출 fallback).
            "safetySettings": _GEMINI_SAFETY_SETTINGS,
        }
        client = self._get_or_create_client()
        try:
            async with client.stream(
                "POST", url, json=body, timeout=self._timeout,
            ) as resp:
                if resp.status_code >= 400:
                    body_bytes = await resp.aread()
                    kind = _classify_status(resp.status_code, body_bytes.decode())
                    raise GeminiError(
                        f"Gemini stream {resp.status_code}: {body_bytes[:200].decode()}",
                        kind=kind,
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    cands = data.get("candidates") or []
                    if cands:
                        parts = cands[0].get("content", {}).get("parts") or []
                        text = "".join(p.get("text", "") for p in parts)
                        if text:
                            yield text
                    um = data.get("usageMetadata")
                    if um:
                        self._last_stream_usage = TokenUsage(
                            prompt_tokens=int(um.get("promptTokenCount") or 0),
                            completion_tokens=int(um.get("candidatesTokenCount") or 0),
                            total_tokens=int(um.get("totalTokenCount") or 0),
                            cached_tokens=int(um.get("cachedContentTokenCount") or 0),
                        )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise GeminiError(str(e), kind="transient") from e

    async def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        response_schema: Optional[dict] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
    ) -> GeminiResult:
        """
        프롬프트 1회 호출 → 응답 텍스트.

        proxy / 직접 호출 모드를 통합해서 동일 인터페이스 제공.

        [response_schema — 2026-05]
        호출자가 JSON schema 를 전달하면 LLM 출력이 schema 안에 머무르도록 강제.
          - LiteLLM 경로 (OpenAI 호환): body 의 `response_format` 으로 전달.
          - Google 직접 호출: `generationConfig.responseSchema` + `responseMimeType=application/json` 으로.
        None 이면 기존 동작 (자유 텍스트 / fence 섞임 가능).

        [model — 2026-05-26 perf A]
        호출별 모델 override. 영향도/메타 분석 같은 단순 stage 는 flash-lite 로
        다운그레이드 → 지연/비용 절감. None 이면 self._model (인스턴스 default).

        [timeout / max_retries — 2026-06-01 fast-fail]
        호출별 타임아웃·재시도 override. None 이면 인스턴스 기본값(90s, 3회) — 즉 다른
        호출부 동작은 불변. autofill 처럼 "빨리 실패하고 폴백" 이 중요한 단순 작업이
        primary 모델이 느릴 때 90s×3=270s 매달리지 않고 즉시 폴백하도록 짧게 줄여 전달.
        """
        if self._use_litellm:
            return await self._generate_via_litellm(
                prompt, temperature=temperature,
                response_schema=response_schema, model=model,
                timeout=timeout, max_retries=max_retries,
                max_output_tokens=max_output_tokens,
            )
        return await self._generate_direct(
            prompt, temperature=temperature,
            response_schema=response_schema, model=model,
            timeout=timeout, max_retries=max_retries,
            max_output_tokens=max_output_tokens,
        )

    # ─── LiteLLM proxy 경로 (OpenAI chat completion 호환) ──────────────

    async def _generate_via_litellm(
        self,
        prompt: str,
        *,
        temperature: float,
        response_schema: Optional[dict] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
    ) -> GeminiResult:
        """LiteLLM proxy 의 /v1/chat/completions endpoint 호출.

        proxy 가 multi-key 라우팅 + 429 자동 재시도 + flash fallback 까지 처리.
        여기선 transient(5xx) 정도만 추가 재시도.

        [timeout/max_retries — 2026-06-01] None 이면 인스턴스 기본값. 짧은 값이
        주어지면 per-request httpx 타임아웃 + 재시도 횟수를 그만큼만 사용 (fast-fail).
        """
        effective_timeout = timeout if timeout is not None else self._timeout
        effective_max_retries = max_retries if max_retries is not None else self._max_retries
        effective_model = model or self._model
        url = f"{self._api_base}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body: Dict[str, Any] = {
            "model": effective_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            # [2026-06-04] 안전필터 오탐 방지 — 정상 회의록이 content_filter 로 빈 응답
            # 되던 사고 차단. drop_params:true 라 미인식 시 안전히 무시됨.
            "safety_settings": _GEMINI_SAFETY_SETTINGS,
        }
        # [2026-06] 출력 상한 — 호출자가 지정 시만. None 이면 미포함(기존 동작).
        if max_output_tokens is not None:
            body["max_tokens"] = max_output_tokens
        # [2026-06-10] thinking budget 노브 — env 미설정이면 미포함(기존 동작).
        # LiteLLM 이 Gemini thinkingConfig 로 변환. 0(비활성)은 reasoning_effort
        # ="disable" 가 버전 호환이 가장 넓고, proxy 가 drop_params:true 라
        # 미인식 구버전에서도 안전하게 무시된다.
        _tb = _thinking_budget()
        if _tb == 0:
            body["reasoning_effort"] = "disable"
        elif _tb is not None:
            body["thinking"] = {"type": "enabled", "budget_tokens": _tb}
        # [2026-05] structured output — OpenAI 호환 response_format.
        # LiteLLM 이 Gemini 백엔드로 변환하면서 responseSchema 매핑을 처리.
        #
        # [운영 안전 — strict 필드 미명시]
        # OpenAI strict=True 는 schema 에 `additionalProperties: false` + 모든
        # property required 강제. 우리 schema 는 동적 properties dict (nested object)
        # 가 있어 strict 와 호환 안 됨. strict 는 OpenAI spec 상 optional 이라
        # 미명시 = default false → 호환성 최대 (구버전 LiteLLM 도 안전).
        #
        # [2026-06-12 lite 계열 보호] flash-lite 는 schema 강제 시 공백 폭주
        # (_schema_skip_substrings 참고) — schema 를 빼고 자유 텍스트로 호출.
        if response_schema is not None and _schema_unsupported(effective_model):
            logger.info(
                "response_schema 생략 (model=%s — schema 강제 시 퇴행 출력 모델)",
                effective_model,
            )
            response_schema = None
        if response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "harness_response",
                    "schema": response_schema,
                },
            }

        last_exc: Optional[Exception] = None
        client = self._get_or_create_client()
        for attempt in range(1, effective_max_retries + 1):
            try:
                resp = await client.post(
                    url, headers=headers, json=body, timeout=effective_timeout
                )

                if 500 <= resp.status_code < 600:
                    last_exc = GeminiError(
                        f"LiteLLM {resp.status_code}: {resp.text[:200]}",
                        kind="transient",
                    )
                    if attempt < effective_max_retries:
                        await asyncio.sleep(2 ** attempt)
                    continue

                if resp.status_code >= 400:
                    kind = _classify_status(resp.status_code, resp.text)
                    raise GeminiError(
                        f"LiteLLM {resp.status_code}: {resp.text[:300]}",
                        kind=kind,
                    )

                data = resp.json()
                # OpenAI chat completion shape — choices[0].message.content
                choices = data.get("choices") or []
                if not choices:
                    raise GeminiError(
                        f"LiteLLM empty choices: {data}",
                        kind="invalid_response",
                    )
                message = choices[0].get("message") or {}
                text = message.get("content") or ""
                if not text:
                    fr = choices[0].get("finish_reason")
                    # raw 응답은 디버깅용으로 로그에만, 사용자에겐 친화 메시지.
                    logger.warning("litellm empty content (finish_reason=%s): %s", fr, data)
                    raise GeminiError(
                        _empty_content_message(fr),
                        kind="invalid_response",
                    )
                # LiteLLM (OpenAI shape) usage — 키 누락 시 0.
                # [2026-05-27] OpenAI shape 의 cached_tokens 는
                # usage.prompt_tokens_details.cached_tokens 위치. LiteLLM 이
                # Gemini cachedContentTokenCount 를 이 경로로 매핑.
                usage_raw = data.get("usage") or {}
                prompt_details = usage_raw.get("prompt_tokens_details") or {}
                usage = TokenUsage(
                    prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                    completion_tokens=int(usage_raw.get("completion_tokens") or 0),
                    total_tokens=int(usage_raw.get("total_tokens") or 0),
                    cached_tokens=int(prompt_details.get("cached_tokens") or 0),
                )
                return GeminiResult(
                    text=text,
                    model=data.get("model") or effective_model,
                    finish_reason=choices[0].get("finish_reason"),
                    usage=usage,
                )

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e
                logger.warning(
                    "litellm network error (attempt %d/%d): %s",
                    attempt,
                    effective_max_retries,
                    e,
                )
                # 마지막 attempt 뒤에는 자지 않음 — fast-fail (특히 timeout override 시).
                if attempt < effective_max_retries:
                    await asyncio.sleep(2 ** attempt)

        if isinstance(last_exc, GeminiError):
            raise GeminiError(
                f"LiteLLM exhausted retries: {last_exc}", kind=last_exc.kind
            )
        raise GeminiError(
            f"LiteLLM exhausted retries: {last_exc}", kind="transient"
        )

    # ─── Google API 직접 호출 경로 (legacy / fallback) ──────────────────

    async def _generate_direct(
        self,
        prompt: str,
        *,
        temperature: float,
        response_schema: Optional[dict] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
    ) -> GeminiResult:
        """Google Generative Language API REST endpoint 직접 호출 (단일 키)."""
        effective_timeout = timeout if timeout is not None else self._timeout
        effective_max_retries = max_retries if max_retries is not None else self._max_retries
        effective_model = model or self._model
        url = f"{self._api_base}/models/{effective_model}:generateContent?key={self._api_key}"
        # [2026-05] schema 가 있으면 JSON 모드 + responseSchema, 없으면 기존 text/plain.
        # [2026-06-12 lite 계열 보호] LiteLLM 경로와 동일 정책 — flash-lite 는
        # responseSchema 강제 시 공백 폭주 (_schema_skip_substrings 참고).
        if response_schema is not None and _schema_unsupported(effective_model):
            logger.info(
                "responseSchema 생략 (model=%s — schema 강제 시 퇴행 출력 모델)",
                effective_model,
            )
            response_schema = None
        if response_schema is not None:
            generation_config: Dict[str, Any] = {
                "temperature": temperature,
                "responseMimeType": "application/json",
                "responseSchema": response_schema,
            }
        else:
            generation_config = {
                "temperature": temperature,
                "responseMimeType": "text/plain",
            }
        # [2026-06] 출력 상한 — 호출자가 지정 시만. None 이면 미포함(기존 동작).
        if max_output_tokens is not None:
            generation_config["maxOutputTokens"] = max_output_tokens
        # [2026-06-10] thinking budget 노브 — env 미설정이면 미포함(기존 동작).
        _tb = _thinking_budget()
        if _tb is not None:
            generation_config["thinkingConfig"] = {"thinkingBudget": _tb}
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
            # [2026-06-04] 안전필터 오탐 방지 — LiteLLM 경로와 동일 정책 (직접 호출 fallback).
            "safetySettings": _GEMINI_SAFETY_SETTINGS,
        }

        last_exc: Optional[Exception] = None
        client = self._get_or_create_client()
        for attempt in range(1, effective_max_retries + 1):
            try:
                resp = await client.post(url, json=body, timeout=effective_timeout)

                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    kind = _classify_status(resp.status_code, resp.text)
                    last_exc = GeminiError(
                        f"Gemini {resp.status_code}: {resp.text[:200]}",
                        kind=kind,
                    )
                    if attempt < effective_max_retries:
                        await asyncio.sleep(2 ** attempt)
                    continue

                if resp.status_code >= 400:
                    kind = _classify_status(resp.status_code, resp.text)
                    raise GeminiError(
                        f"Gemini {resp.status_code}: {resp.text[:300]}",
                        kind=kind,
                    )

                data = resp.json()
                cands = data.get("candidates") or []
                if not cands:
                    raise GeminiError(
                        f"Gemini empty candidates: {data}",
                        kind="invalid_response",
                    )
                parts = cands[0].get("content", {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts)
                if not text:
                    fr = cands[0].get("finishReason")
                    logger.warning("gemini empty text (finishReason=%s): %s", fr, data)
                    raise GeminiError(
                        _empty_content_message(fr),
                        kind="invalid_response",
                    )
                # Google API usage shape — `usageMetadata.promptTokenCount` 등.
                # 필드명이 camelCase 라 LiteLLM 과 다름.
                # [2026-05-27] cachedContentTokenCount — Gemini 2.5+ implicit
                # caching 적중 시 양수. 캐시 효과 가시화용.
                um = data.get("usageMetadata") or {}
                usage = TokenUsage(
                    prompt_tokens=int(um.get("promptTokenCount") or 0),
                    completion_tokens=int(um.get("candidatesTokenCount") or 0),
                    total_tokens=int(um.get("totalTokenCount") or 0),
                    cached_tokens=int(um.get("cachedContentTokenCount") or 0),
                )
                return GeminiResult(
                    text=text,
                    model=effective_model,
                    finish_reason=cands[0].get("finishReason"),
                    usage=usage,
                )

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e
                logger.warning(
                    "gemini network error (attempt %d/%d): %s",
                    attempt,
                    effective_max_retries,
                    e,
                )
                if attempt < effective_max_retries:
                    await asyncio.sleep(2 ** attempt)

        if isinstance(last_exc, GeminiError):
            raise GeminiError(
                f"Gemini exhausted retries: {last_exc}", kind=last_exc.kind
            )
        raise GeminiError(
            f"Gemini exhausted retries: {last_exc}", kind="transient"
        )
