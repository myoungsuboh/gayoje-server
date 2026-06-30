"""
사용량(쿼터) 정책 — Free / Pro / Pro+ / Pro Max 등급별 한도 + 한도 초과 응답 형식.

[설계 요약 — 2026-05 월간 reset 도입]
- 한도는 **월간 자동 reset** (가입일 기준). usage_repository 의 cypher 안에서
  self-healing 으로 reset_at 체크 → 지났으면 카운터 0 + 다음 reset_at 설정.
  - 표준 SaaS 모델 (MRR 안정). 별도 cron 불필요 (사용자 호출 시점에 atomic 처리).
  - 이전 정책 (lifetime) 은 폐기 — 사업적 위험 (재구매 동기 없음, 헤비 유저 즉시 차단).
- 등급은 User 노드의 `subscription_type` 필드 ('free' | 'pro' | 'pro_plus' | 'pro_max').
- 한도 종류 5가지:
    1) meeting_logs   : `postMeeting` 등 미팅 등록 횟수 (월간 누적)
    2) summary_chars  : 한 번에 보낼 수 있는 회의록 글자수 (per-request, 누적 아님)
    3) total_tokens   : 모든 LLM 호출 누적 토큰 합산 (월간 누적)
    4) library_skills : 라이브러리 저장 가능 스킬 수 (현재 시점 count)
    5) max_projects   : 동시 보유 가능 프로젝트 수 (현재 시점 count, reset 무관)

[저장]
- 카운터는 Neo4j User 노드에 직접. reset_at 도 동일 노드의 usage_reset_at 필드.
- atomic 증가 + reset 체크는 Cypher 단일 트랜잭션에서 처리.
  → `app/service/usage_repository.py`.

[모듈 책임 분리]
- 이 모듈(`core/quota.py`) = 정책 (한도 숫자 + 등급별 매핑 + 한도 초과 응답 형식).
- `service/usage_repository.py` = Neo4j 입출력 (카운터 read/increment + auto-reset).
- 라우트는 둘 다 import 해서 pre-check / post-update 결정.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

# subscription 상수는 별도 lightweight module 에서 import — user_repository 경로는
# token_encryption → config → settings 평가를 트리거하므로 worker (JWT_SECRET 미보유)
# 부팅 시 fail. 같은 사유로 _quota_helpers / jobs / usage_repository 도 직접 import.
from app.core.subscription import (
    PAID_SUBSCRIPTIONS,
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    SUBSCRIPTION_PRO_MAX,
    SUBSCRIPTION_PRO_PLUS,
)

# ───────────────────────────────────────────────────────────────
# 한도 정의 — 등급별
# ───────────────────────────────────────────────────────────────
#
# 값 조정 시 README / FE Pricing 페이지도 함께 갱신.
# 토큰 한도 산정 근거 (2026-05 LiteLLM 1주일 실측 기준 예시):
#   - post_meeting 1회 (CPS+PRD) ≈ 15~25K tokens
#   - createDesign 1회 ≈ 20~40K tokens
#   - runLint 1회 (중간 repo) ≈ 30~80K tokens
#   - createMD 1회 ≈ 10~20K tokens
# Free 100K → 실제 회의록(약 10K자) 기준 2~3 사이클 완주 가능.
#   (입력 글자수를 5K→10K 로 올린 뒤로는 한 사이클당 토큰이 더 들지만,
#    '진짜 회의록을 끝까지 체험' 이 잘린 5건보다 첫인상에 유리하다는 판단.)
# Pro 4M → 활발한 PoC 운영 1개월 충분 (2026 마진 정상화로 5M→4M).

LIMIT_TYPE = Literal[
    "meeting_logs",
    "summary_chars",
    "total_tokens",
    "library_skills",
    "max_projects",  # 동시 보유 프로젝트 수 — 다른 항목과 달리 lifetime 카운터 아님 (현재 시점 count)
]

# Free / Pro / Pro+ / Pro Max 한도 — 월간 카운터 (가입일 기준 자동 reset).
#
# [토큰 정책 — 2026-06 (메인 + Lite 오버플로우 모델)]
# `total_tokens` = 메인(Flash) 월간 쿼터. 소진하면 차단(402)이 아니라:
#   - Free            → 하드월 (업그레이드 유도)
#   - Pro             → Lite 모델 + 주간캡 (소프트랜딩, 사실상 안 막힘)
#   - Pro+ / Pro Max  → Lite 모델 "무제한" (공정사용 주간캡)
# Lite 주간캡은 `_LITE_DAILY_CAP` (이름은 호환 유지). "무제한"과 "캡"은 별도 코드가 아니라 캡 숫자
# 차이 + 마케팅 카피일 뿐 (동일 메커니즘).
#
# [2026-06-11 마진 재조정] 이전 1M/3M/6M/11M + 캡 0.5M/1.4M/1.8M 은 워스트
# (메인 100% + 캡 매일 100%) 토큰 원가가 매출의 64~73% — PG 수수료·인프라를
# 더하면 헤비 유저 구간 적자. "대부분 80%+ 사용" 가정으로 하향:
#   메인 1M / 2M / 4M / 8M, Lite 주간캡 0 / 1.5M / 3M / 5M (주간 전환은 아래 참조)
#   → 워스트 원가 35% / 33% / 41%, 80% 사용 시 ~30% (단가: Flash 1,250원/M,
#     Lite 250원/M, $1=1,300원 기준. 매출 $9/$19/$29).
# 모델: 메인은 Free=flash-lite / 유료=flash, 오버플로우는 전 등급 flash-lite.
# 차별화: 메인 용량(이 dict) + Lite 주간캡 + 동시 프로젝트 수.
# ⚠️ 운영 DB(QuotaConfig)는 seed ON CREATE 라 이 값으로 자동 갱신되지 않음 —
#    admin 한도 관리 화면에서 저장해야 라이브 반영.
_FREE_LIMITS: dict[str, int] = {
    "meeting_logs": 5,
    # [2026-05] 5_000 → 10_000. 한국어 회의 녹취록이 5K 한도에선 잘려 업로드조차
    # 안 되던 첫-체험 이탈 문제 해소.
    "summary_chars": 10_000,
    "total_tokens": 1_000_000,      # [2026-06] 메인 쿼터. 소진 시 하드월 (오버플로우 없음).
    "library_skills": 100,   # 사용자 라이브러리에 저장 가능한 LibrarySkill 노드 수
}
_PRO_LIMITS: dict[str, int] = {
    "meeting_logs": 50,             # 2026-05 운영 조정 (실측 헤비유저 ≤ 월 30건)
    "summary_chars": 50_000,
    "total_tokens": 2_000_000,      # [2026-06-11 마진 재조정 3M→2M] 소진 시 Lite 소프트랜딩(주간캡).
    "library_skills": 1_000,
}
_PRO_PLUS_LIMITS: dict[str, int] = {
    "meeting_logs": 100,            # Pro × 2
    "summary_chars": 100_000,       # Pro × 2
    "total_tokens": 4_000_000,      # [2026-06-11 마진 재조정 6M→4M] 소진 시 Lite 무제한(공정사용 주간캡).
    "library_skills": 2_000,        # Pro × 2
}
_PRO_MAX_LIMITS: dict[str, int] = {
    "meeting_logs": 200,            # Pro × 4
    "summary_chars": 200_000,       # Pro × 4
    "total_tokens": 8_000_000,      # [2026-06-11 마진 재조정 11M→8M] 소진 시 Lite 무제한(공정사용 주간캡).
    "library_skills": 4_000,        # Pro × 4
}

# ─── Lite 오버플로우 주간 캡 (2026-06-11 주간 전환) ──────────────
# 메인 쿼터 소진 후 한 주기에 쓸 수 있는 Lite 토큰 한도. 0 = 오버플로우 불가(하드월).
# 롤링 7일 self-healing reset (usage_repository — 이전 24h).
#   왜 주간: 일일캡은 "조금 쓰면 바로 막히는" 답답함(burst 불가)이 컸다. 주간이면
#   같은 워스트 원가로 한 번에 6배(예: Pro 250K/일 → 1.5M/주) 몰아 쓸 수 있다.
#   Pro 1.5M/주 (소프트랜딩) · Pro+ 3M/주 · Pro Max 5M/주 ("무제한" 체감)
# worst-case 월 손실 = 캡×(30/7)×250원/M ≈ Pro 1,607 / Pro+ 3,214 / Max 5,357원
# → 워스트 토큰 원가(메인+Lite) 매출 대비 35% / 33% / 41%.
# 이름의 DAILY 는 DB(QuotaConfig.lite_daily_cap)/API 필드 호환 위해 유지 — 의미는 주간.
_LITE_DAILY_CAP: dict[str, int] = {
    SUBSCRIPTION_FREE: 0,
    SUBSCRIPTION_PRO: 1_500_000,
    SUBSCRIPTION_PRO_PLUS: 3_000_000,
    SUBSCRIPTION_PRO_MAX: 5_000_000,
}
# 주간캡 대비 이 비율 도달 시 "사용량 많음 → 엔터프라이즈?" 넛지.
_LITE_NUDGE_RATIO = 0.7

# 동시 프로젝트 수 한도 — 다른 항목과 의미가 다름(현재 시점 count, reset 무관).
# 별도 dict 로 분리해서 LIMIT_TYPE / get_limits 흐름을 오염시키지 않음.
MAX_PROJECTS_BY_SUBSCRIPTION: dict[str, int] = {
    SUBSCRIPTION_FREE: 1,
    SUBSCRIPTION_PRO: 3,
    SUBSCRIPTION_PRO_PLUS: 6,
    SUBSCRIPTION_PRO_MAX: 12,
}


# ─── DB-backed override (2026-05-17) ─────────────────────────
#
# QuotaConfig 노드 (`app/service/quota_config_repository.py`) 의 값을 부팅 시점에
# 이 dict 에 load. admin 라우트가 update 하면 즉시 갱신해 다음 가드 호출부터 반영.
#
# 비어있으면 (Neo4j 미연결 / 부팅 hook 미수행 / 테스트 환경) 위의 코드 상수
# fallback 으로 동작 — 운영 환경 의존성 없음.
_LIMITS_OVERRIDE: dict[str, dict[str, int]] = {}


# [2026-06 워커 신선도] admin 이 한도(예: Pro+ total_tokens 500K→5M)를 바꾸면
# API 프로세스의 _LIMITS_OVERRIDE 는 즉시 갱신되지만, 별도 워커 프로세스의
# 메모리는 부팅 시 값으로 stale. 결과: 워커가 옛 한도로 결정을 내려 메인이
# 폭주(예: 1.4M / 500K). 워커가 결정 직전에 짧은 TTL 로 DB 에서 다시 로드
# 하면 stale 창이 최대 _OVERRIDE_TTL_SEC 로 제한됨.
_OVERRIDE_TTL_SEC: float = 15.0
_override_last_loaded_at: float = 0.0
_override_reload_lock: Any = None  # asyncio.Lock, 첫 호출 시 lazy 생성


async def ensure_overrides_fresh(force: bool = False) -> None:
    """잡/요청 진입 시 호출 — 마지막 로드가 TTL 초과면 DB 에서 _LIMITS_OVERRIDE 재로드.

    동시 다발 호출에도 동시 로드는 1회만 (asyncio.Lock). 실패해도 swallow —
    이전 캐시로 계속 진행한다(가용성 우선).
    """
    import asyncio
    import time as _time
    import logging

    global _override_last_loaded_at, _override_reload_lock
    now = _time.monotonic()
    if not force and (now - _override_last_loaded_at) < _OVERRIDE_TTL_SEC:
        return
    if _override_reload_lock is None:
        _override_reload_lock = asyncio.Lock()
    async with _override_reload_lock:
        # 락 안에서 재확인 — 다른 코루틴이 직전에 갱신했으면 skip.
        now = _time.monotonic()
        if not force and (now - _override_last_loaded_at) < _OVERRIDE_TTL_SEC:
            return
        try:
            # 지연 import — quota.py 가 quota_config_repository 를 직접 끌어오면
            # 부팅 시점에 Neo4j 환경 변수 evaluation 트리거. 운영/테스트 분리.
            from app.service import quota_config_repository
            rows = await quota_config_repository.list_quota_config()
            for row in rows:
                apply_limits_override(
                    row.tier,
                    {
                        "meeting_logs": row.meeting_logs,
                        "summary_chars": row.summary_chars,
                        "total_tokens": row.total_tokens,
                        "library_skills": row.library_skills,
                        "max_projects": row.max_projects,
                        "lite_daily_cap": row.lite_daily_cap,
                    },
                )
            _override_last_loaded_at = _time.monotonic()
        except Exception as e:  # noqa: BLE001 — 가용성 우선, 이전 캐시로 진행
            logging.getLogger(__name__).warning(
                "ensure_overrides_fresh: DB 재로드 실패 — 이전 캐시 유지: %s", e
            )


def _reset_override_cache_for_test() -> None:
    """테스트 격리용 — TTL 타임스탬프 초기화."""
    global _override_last_loaded_at
    _override_last_loaded_at = 0.0


def apply_limits_override(tier: str, limits: dict[str, int]) -> None:
    """admin update 또는 부팅 hook 에서 호출. dict copy 로 저장."""
    if not tier:
        return
    _LIMITS_OVERRIDE[tier] = {k: int(v) for k, v in (limits or {}).items()}


def clear_limits_override() -> None:
    """테스트 격리용 — 부팅 시점 이전 상태로 리셋."""
    _LIMITS_OVERRIDE.clear()


def get_limits(subscription_type: str) -> dict[str, int]:
    """등급 → 한도 dict.

    1) DB-backed override (`_LIMITS_OVERRIDE`) 가 있으면 그 값.
    2) 없으면 코드 상수 fallback.
    알 수 없는 값은 안전한 default 로 Free 한도. (DB 에 비정상 값이 박혀도 차단 동작 유지.)
    """
    override = _LIMITS_OVERRIDE.get(subscription_type)
    if override:
        return dict(override)
    if subscription_type == SUBSCRIPTION_PRO_MAX:
        return dict(_PRO_MAX_LIMITS)
    if subscription_type == SUBSCRIPTION_PRO_PLUS:
        return dict(_PRO_PLUS_LIMITS)
    if subscription_type == SUBSCRIPTION_PRO:
        return dict(_PRO_LIMITS)  # 호출자가 mutate 못 하게 copy
    # free, '', None, 기타 모두 free 한도 — 항상 보수적으로.
    free_override = _LIMITS_OVERRIDE.get(SUBSCRIPTION_FREE)
    if free_override:
        return dict(free_override)
    return dict(_FREE_LIMITS)


def get_max_projects_override_or_default(subscription_type: str) -> int:
    """get_max_projects 동일 — override 우선. (호환용 별칭은 만들지 않음, 본체에서 처리.)"""
    override = _LIMITS_OVERRIDE.get(subscription_type)
    if override and "max_projects" in override:
        return int(override["max_projects"])
    return MAX_PROJECTS_BY_SUBSCRIPTION.get(
        subscription_type, MAX_PROJECTS_BY_SUBSCRIPTION[SUBSCRIPTION_FREE]
    )


def get_limit(subscription_type: str, limit_type: LIMIT_TYPE) -> int:
    """등급 + 한도 종류 → 한도 값."""
    return get_limits(subscription_type)[limit_type]


def get_lite_daily_cap(subscription_type: str) -> int:
    """등급별 Lite 오버플로우 주간 캡 (롤링 7일). 0 = 오버플로우 불가(하드월).

    1) DB override (`_LIMITS_OVERRIDE[tier]["lite_daily_cap"]`) 가 있으면 그 값.
    2) 없으면 `_LITE_DAILY_CAP` 상수. 알 수 없는 등급은 0 (보수적 하드월).
    """
    override = _LIMITS_OVERRIDE.get(subscription_type)
    if override and "lite_daily_cap" in override:
        return max(0, int(override["lite_daily_cap"]))
    return _LITE_DAILY_CAP.get(subscription_type, 0)


def token_usage_summary(used: int, subscription_type: str) -> dict[str, Any]:
    """[2026-05-27 관리자 대시보드] 토큰 사용량 → {used, limit, pct}.

    관리자 유저 목록에서 "이번 cycle 토큰을 등급 한도 대비 몇 % 썼는지" 표시용.
    한도 초과는 100% 초과로 그대로 노출(캡 안 함) — 관리자가 초과 인지 가능.
    한도 0(방어적; 실제 등급 한도는 >0) 이면 ZeroDivision 없이 pct=None.
    """
    used_i = int(used or 0)
    limit = get_limit(subscription_type, "total_tokens")
    pct = round(used_i / limit * 100, 1) if limit and limit > 0 else None
    return {"token_used": used_i, "token_limit": int(limit), "token_pct": pct}


def get_max_projects(subscription_type: str) -> int:
    """등급별 동시 보유 가능한 프로젝트 수 한도.

    1) DB-backed override 가 있으면 그 값 (max_projects key).
    2) 없으면 MAX_PROJECTS_BY_SUBSCRIPTION 상수 fallback.
    알 수 없는 값은 보수적으로 Free 한도.
    """
    return get_max_projects_override_or_default(subscription_type)


# ───────────────────────────────────────────────────────────────
# 등급별 Gemini 모델 선택
# ───────────────────────────────────────────────────────────────
#
# Free / Pro 가 서로 다른 모델을 쓰는 정책. 단가 차이로 인한 비용 통제.
# 실제 모델 이름은 .env 의 GEMINI_MODEL_FREE / GEMINI_MODEL_PRO 가 결정.
# 미설정 시 GEMINI_MODEL (legacy) 로 fallback — 단일 모델 운영 호환.


def get_model_for_subscription(subscription_type: str) -> str:
    """등급별 사용할 Gemini 모델 이름.

    [정책 — 2026-05]
    - Pro / Pro+ / Pro Max: settings.gemini_model_for_pro (.env: GEMINI_MODEL_PRO)
      → 세 유료 등급은 동일 모델. 차별화는 용량 + 프로젝트 수에서 처리.
    - Free / 기타: settings.gemini_model_for_free (.env: GEMINI_MODEL_FREE)
    - 두 env 미설정 시 settings.GEMINI_MODEL 단일 모델로 fallback.

    [Why string indirection]
    호출자(라우트/jobs)가 quota.get_model_for_subscription(...) 만 알면 됨.
    settings 의 fallback 로직을 일관 적용 + 추후 등급 추가(예: enterprise) 시
    이 함수만 확장.
    """
    # 지연 import — quota.py 가 settings 를 import 하면 순환 가능성 회피.
    from app.core.config import settings

    if subscription_type in PAID_SUBSCRIPTIONS:
        return settings.gemini_model_for_pro
    # free / '' / None / 알 수 없는 값 — 보수적으로 free 모델.
    return settings.gemini_model_for_free


# ───────────────────────────────────────────────────────────────
# 토큰 쿼터 결정 (2026-06 — 메인 + Lite 오버플로우)
# ───────────────────────────────────────────────────────────────
#
# LLM 호출 진입 시 "이 요청을 어떻게 처리할지" 단일 결정으로 응축.
#   main     : 메인(Flash) 쿼터 잔여 → 등급별 Flash/free 모델, 토큰은 main 버킷.
#   overflow : 메인 소진 + Lite 주간캡 잔여 → flash-lite 모델, 토큰은 lite 버킷.
#   blocked  : Free 메인 소진(오버플로우 불가) OR Lite 주간캡 소진 → 402.
#
# 가드(assert_tokens_within_limit)와 모델 선택(_quota_helpers / jobs)이 같은
# 결정을 공유한다 — 둘 다 이 함수를 호출. 읽기 전용이라 멱등 (race 무해).

# Lite 오버플로우 풀 키 — 비동기 워커 on_startup 이 미리 만든 gemini_lite 인스턴스.
MODEL_POOL_FREE = "gemini_free"
MODEL_POOL_PRO = "gemini_pro"
MODEL_POOL_LITE = "gemini_lite"


@dataclass(frozen=True)
class QuotaDecision:
    """한 LLM 요청의 토큰 처리 결정. resolve_quota_decision 가 생성."""

    mode: str                         # "main" | "overflow" | "blocked"
    subscription_type: str
    bucket: str                       # 토큰 적재 버킷: "main" | "lite"
    reset_at: Optional[str] = None    # 메인(월간) reset 시점
    lite_daily_reset_at: Optional[str] = None  # Lite 주간 reset 시점 (필드명은 호환 유지)
    # blocked 일 때만 채워짐 — 라우트가 402 detail 에 사용.
    blocked_reason: Optional[str] = None      # "free_main" | "lite_daily_cap"
    blocked_current: int = 0
    blocked_limit: int = 0
    # 주간캡 임박(≥70%) 시 엔터프라이즈 넛지 문구. 그 외 None.
    warning: Optional[str] = None
    # [2026-06 mid-job 강등] mode=="main" 일 때 결정 시점의 DB 누적/한도 스냅샷.
    # 워커가 잡 진행 중 (base + 이번 잡 누적) 이 한도를 넘으면 나머지를 lite 로 강등.
    main_current: int = 0             # 결정 시점 DB 월간 누적
    main_limit: int = 0               # 해당 등급 메인 한도
    overflow_available: bool = False  # 메인 소진 시 lite 오버플로우 가능 등급인지

    @property
    def allowed(self) -> bool:
        return self.mode != "blocked"


_ENTERPRISE_NUDGE = (
    "현재 사용량이 매우 많습니다. 상업적 용도라면 엔터프라이즈 플랜을 확인해 주세요."
)


def model_for_decision(decision: "QuotaDecision") -> str:
    """sync 경로용 — 결정 → 실제 Gemini 모델 이름."""
    from app.core.config import settings

    if decision.mode == "overflow":
        return settings.gemini_model_lite
    return get_model_for_subscription(decision.subscription_type)


def pool_for_decision(decision: "QuotaDecision") -> str:
    """async 워커용 — 결정 → 공유 GeminiClient 풀 키 (gemini_free/pro/lite)."""
    if decision.mode == "overflow":
        return MODEL_POOL_LITE
    if decision.subscription_type in PAID_SUBSCRIPTIONS:
        return MODEL_POOL_PRO
    return MODEL_POOL_FREE


async def resolve_quota_decision(email: str) -> QuotaDecision:
    """이메일 → 토큰 처리 결정 (main/overflow/blocked). 읽기 전용.

    사용자 노드가 없으면 보수적으로 main (다운스트림이 404 책임 — 기존 동작).
    """
    import logging
    from app.service import usage_repository

    # [2026-06 신선도] admin 한도 변경이 즉시 반영되도록 결정 직전 DB 재로드 (15s TTL).
    # _tracked_ctx / tracked_pipeline_context 가 이미 호출하지만 직접 호출 경로도 안전화.
    await ensure_overrides_fresh()

    try:
        usage = await usage_repository.get_usage(email)
    except Exception as e:  # noqa: BLE001 — 조회 실패 시 보수적으로 main/free 진행 (가드 우회 아님)
        logging.getLogger(__name__).warning(
            "resolve_quota_decision: get_usage 실패 (email=%s) — main/free fallback: %s", email, e
        )
        return QuotaDecision(mode="main", subscription_type=SUBSCRIPTION_FREE, bucket="main")
    if usage is None:
        return QuotaDecision(mode="main", subscription_type=SUBSCRIPTION_FREE, bucket="main")

    sub = usage.subscription_type
    main_limit = get_limit(sub, "total_tokens")
    # 1) 메인 쿼터 잔여 → Flash
    if usage.total_tokens < main_limit:
        # mid-job 강등 스냅샷 — 워커가 (base + 이번 잡 누적) 으로 한도 초과 감지 시 lite 전환.
        return QuotaDecision(
            mode="main", subscription_type=sub, bucket="main", reset_at=usage.reset_at,
            main_current=usage.total_tokens, main_limit=main_limit,
            overflow_available=get_lite_daily_cap(sub) > 0,
        )

    # 2) 메인 소진 — 오버플로우 가능 여부
    daily_cap = get_lite_daily_cap(sub)
    if daily_cap <= 0:
        # Free 등 — 하드월
        return QuotaDecision(
            mode="blocked", subscription_type=sub, bucket="main",
            reset_at=usage.reset_at, blocked_reason="free_main",
            blocked_current=usage.total_tokens, blocked_limit=main_limit,
        )
    # 3) 주간캡 소진 → 차단 (엔터프라이즈 넛지)
    if usage.lite_daily_tokens >= daily_cap:
        return QuotaDecision(
            mode="blocked", subscription_type=sub, bucket="lite",
            reset_at=usage.reset_at, lite_daily_reset_at=usage.lite_daily_reset_at,
            blocked_reason="lite_daily_cap",
            blocked_current=usage.lite_daily_tokens, blocked_limit=daily_cap,
            warning=_ENTERPRISE_NUDGE,
        )
    # 4) 오버플로우 허용 (Lite). 주간캡 임박 시 넛지.
    warning = _ENTERPRISE_NUDGE if usage.lite_daily_tokens >= daily_cap * _LITE_NUDGE_RATIO else None
    return QuotaDecision(
        mode="overflow", subscription_type=sub, bucket="lite",
        reset_at=usage.reset_at, lite_daily_reset_at=usage.lite_daily_reset_at,
        warning=warning,
    )


# ───────────────────────────────────────────────────────────────
# 한도 초과 응답 형식
# ───────────────────────────────────────────────────────────────
#
# 라우트가 한도 초과로 차단 시 HTTPException 의 detail 로 이 dict 를 그대로 반환.
# FE 는 detail.code === "QUOTA_EXCEEDED" 로 매칭해서 Pro 안내 모달 띄움.
#
# HTTP 상태:
#   402 Payment Required — 결제로 해소 가능한 차단 (RFC 9110 의도와 일치).
#   FE 의 axios interceptor 는 402 만 별도 처리 (다른 4xx 와 분리).

ERROR_CODE_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"


@dataclass(frozen=True)
class QuotaExceeded:
    """한도 초과 응답 본문 빌더.

    라우트 호출 패턴:
        raise HTTPException(
            status_code=402,
            detail=QuotaExceeded(...).to_dict(),
        )
    """

    limit_type: LIMIT_TYPE
    current: int
    limit: int
    subscription_type: str
    # 사용자에게 보여줄 한국어 메시지. 없으면 한도 종류별 default 사용.
    message: Optional[str] = None
    # 결제/안내 페이지. FE 가 결정.
    upgrade_url: str = "/pricing"
    # [2026-05] 다음 자동 reset 시점 (월간 정책). 없으면 None.
    # `max_projects` 같이 reset 무관한 한도는 None 으로 둠.
    reset_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "code": ERROR_CODE_QUOTA_EXCEEDED,
            "limit_type": self.limit_type,
            "current": self.current,
            "limit": self.limit,
            "subscription_type": self.subscription_type,
            "message": self.message or _default_message(self.limit_type, self.subscription_type),
            "upgrade_url": self.upgrade_url,
            # [2026-05] reset_at — FE 가 "N일 후 reset" 안내. None 이면 reset 무관 한도
            # (max_projects 등) 또는 사용자 노드 부재 케이스.
            "reset_at": self.reset_at,
        }


def _grade_label(subscription_type: str) -> str:
    """사용자 메시지에 노출할 등급 라벨."""
    return {
        SUBSCRIPTION_FREE: "무료",
        SUBSCRIPTION_PRO: "Pro",
        SUBSCRIPTION_PRO_PLUS: "Pro+",
        SUBSCRIPTION_PRO_MAX: "Pro Max",
    }.get(subscription_type, "현재")


def _next_tier_suggestion(subscription_type: str) -> str:
    """다음 등급 업그레이드 안내 문구. Pro Max 는 더 이상 업그레이드 불가 → 문의 안내."""
    if subscription_type == SUBSCRIPTION_FREE:
        return "Pro / Pro+ / Pro Max 로 업그레이드하면 더 넉넉히 사용할 수 있습니다."
    if subscription_type == SUBSCRIPTION_PRO:
        return "Pro+ 또는 Pro Max 로 업그레이드하면 용량이 2~4배로 늘어납니다."
    if subscription_type == SUBSCRIPTION_PRO_PLUS:
        return "Pro Max 로 업그레이드하면 용량이 2배로 늘어납니다."
    # Pro Max — 더 이상 업그레이드 단계 없음. Enterprise 문의 유도.
    return "Pro Max 한도에 도달했습니다. 추가 용량이 필요하시면 문의해 주세요."


def _default_message(limit_type: LIMIT_TYPE, subscription_type: str) -> str:
    grade = _grade_label(subscription_type)
    suggestion = _next_tier_suggestion(subscription_type)
    if limit_type == "meeting_logs":
        return f"{grade} 등급의 미팅 로그 등록 한도에 도달했습니다. {suggestion}"
    if limit_type == "summary_chars":
        return (
            f"{grade} 등급은 한 번에 보낼 수 있는 회의록 글자수가 제한됩니다. "
            f"{suggestion}"
        )
    if limit_type == "total_tokens":
        return f"{grade} 등급의 AI 사용량 한도에 도달했습니다. {suggestion}"
    if limit_type == "library_skills":
        return (
            f"{grade} 등급의 스킬 라이브러리 저장 한도에 도달했습니다. {suggestion}"
        )
    if limit_type == "max_projects":
        return (
            f"{grade} 등급의 동시 보유 프로젝트 수 한도에 도달했습니다. {suggestion}"
        )
    return f"{grade} 등급의 사용량 한도에 도달했습니다. {suggestion}"


# ───────────────────────────────────────────────────────────────
# 라우트용 가드 — pre-check
# ───────────────────────────────────────────────────────────────
#
# 라우트 진입 시점에 호출. 한도 초과면 HTTPException(402) 으로 즉시 차단 →
# LLM 호출 시작 전이라 비용 발생 0.
#
# 두 가드를 **이 순서로** 호출하는 게 비용·UX 최적:
#   1) assert_summary_within_limit  — get_usage 만, cheap. 글자수는 사용자가 즉시 인지 가능.
#   2) acquire_meeting_quota        — atomic +1. summary 통과한 요청에만 카운트 차감.
#
# 둘 다 통과해야 LLM 호출 단계로 진행.

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.service.usage_repository import IncrementResult


async def assert_summary_within_limit(
    email: str, meeting_content: str
) -> None:
    """
    회의록 입력 글자수가 등급별 summary_chars 한도 안인지 검사.

    초과 시 HTTPException(402, detail=QuotaExceeded) 으로 즉시 차단.
    한도 안이면 silent return — 카운터 변경 없음 (per-request 검증이라 누적 아님).

    [why per-request]
    summary_chars 는 한 번에 보낼 수 있는 글자수의 상한. 회의록이 너무 길면
    LLM context window 와 비용 모두 위험. 월간 누적 한도와는 분리.

    Raises:
        fastapi.HTTPException(402): 한도 초과.
    """
    # 지연 import — fastapi 의존을 모듈 import 시점에 강제하지 않기 위해.
    from fastapi import HTTPException, status as http_status
    from app.service import usage_repository

    # [2026-06-11] admin 한도 변경의 멀티프로세스 전파 — 토큰 가드(resolve_quota_decision)만
    # 재로드하고 이 가드는 부팅 캐시를 읽어서, 다른 프로세스에선 옛 한도로 검사하던 갭.
    await ensure_overrides_fresh()

    chars = len(meeting_content or "")
    if chars == 0:
        # 빈 입력은 라우트의 다른 가드 (Field min_length) 가 처리 — 여기서는 통과.
        return

    usage = await usage_repository.get_usage(email)
    # 사용자 노드가 없으면 보수적으로 free 한도 적용. 인증 토큰만 있는 비정상 케이스.
    subscription = usage.subscription_type if usage else SUBSCRIPTION_FREE
    limit = get_limit(subscription, "summary_chars")
    if chars > limit:
        raise HTTPException(
            status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
            detail=QuotaExceeded(
                limit_type="summary_chars",
                current=chars,
                limit=limit,
                subscription_type=subscription,
                reset_at=usage.reset_at if usage else None,
            ).to_dict(),
        )


async def assert_tokens_within_limit(email: str) -> "QuotaDecision":
    """
    LLM 호출 진입점 가드 — 토큰 처리 결정 후 차단 여부 판단.

    LLM 호출 진입점 전체에 호출 — postMeeting / Design / Lint / FixSpec /
    CreateMD / RecommendSkills 등.

    [정책 — 2026-06 메인 + Lite 오버플로우]
    이전엔 메인 한도 도달 시 무조건 402. 이제는 `resolve_quota_decision` 로:
      - main / overflow → 통과 (overflow 는 Lite 모델로 작업 이어감, 비용 발생).
      - blocked → 402 (Free 메인 소진, 또는 Lite 일일캡 소진).
    즉 유료 등급은 메인을 소진해도 차단되지 않고 Lite 로 강등돼 진행한다.

    반환: QuotaDecision (호출처는 무시해도 됨 — 모델/버킷 결정은
    _quota_helpers / jobs 가 별도로 resolve 한다). blocked 면 raise.

    [User 없음]
    안전망으로 silent 통과 (decision.mode='main', acquire_meeting_quota 가 404 책임).
    """
    from fastapi import HTTPException, status as http_status

    decision = await resolve_quota_decision(email)
    if decision.mode != "blocked":
        return decision

    if decision.blocked_reason == "lite_daily_cap":
        grade = _grade_label(decision.subscription_type)
        # [2026-06-13] 일→주 전환 후속 — 캡이 롤링 7일인데 메시지가 '오늘/내일'이라
        # 하루 기다려도 안 풀려 혼란. 주간 표현 + 정확한 해제 시점은 reset_at(FE 표시)에 위임.
        message = (
            f"{grade} 등급의 이번 주 추가 사용량(Lite) 한도에 도달했습니다. "
            f"한도는 7일마다 다시 채워집니다. {_ENTERPRISE_NUDGE}"
        )
        reset_at = decision.lite_daily_reset_at
    else:  # free_main — 메인 쿼터 하드월
        message = None  # _default_message(total_tokens) 사용
        reset_at = decision.reset_at

    raise HTTPException(
        status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
        detail=QuotaExceeded(
            limit_type="total_tokens",
            current=decision.blocked_current,
            limit=decision.blocked_limit,
            subscription_type=decision.subscription_type,
            message=message,
            reset_at=reset_at,
        ).to_dict(),
    )


async def assert_projects_within_limit(email: str) -> None:
    """
    현재 보유한 프로젝트 수가 등급별 max_projects 한도 안인지 검사.

    [호출 시점]
    `ownership_repository.claim_project` 가 신규 Project 노드를 생성하기 직전.
    이미 본인 소유인 프로젝트에 대한 멱등 claim 은 가드 우회 (호출자 책임으로
    is_owner 먼저 체크 후 신규일 때만 호출).

    [정책]
    - max_projects = 1, 3, 6, 12 (Free/Pro/Pro+/Pro Max)
    - lifetime 누적 아니라 현재 시점 카운트 — 프로젝트를 삭제하면 다시 만들 수 있음.
    - 한도 도달 시 신규 생성만 차단, 기존 데이터 접근/조작은 가능.

    [TOCTOU 정책]
    cypher 한 트랜잭션 안에서 atomic 처리하지 않음 — 동시 두 요청이 12/12 시점에
    모두 통과 후 둘 다 +1 되는 race 가 이론상 가능. 현실에서 동일 사용자가 한
    프로젝트를 동시 두 번 만들 가능성은 매우 낮음. 발생해도 사용자 본인 데이터만
    영향. MVP 단계에서 단순성 우선.

    Raises:
        fastapi.HTTPException(402): 한도 초과.
    """
    from fastapi import HTTPException, status as http_status

    # 지연 import — quota.py 가 ownership_repository import 하면 부팅 시점에
    # 트리거되는 modules 가 늘어남. claim 호출 시점에만 평가.
    from app.service import ownership_repository, usage_repository

    await ensure_overrides_fresh()   # [2026-06-11] admin 변경 멀티프로세스 전파

    usage = await usage_repository.get_usage(email)
    subscription = usage.subscription_type if usage else SUBSCRIPTION_FREE
    limit = get_max_projects(subscription)
    current = await ownership_repository.count_user_projects(email)
    if current >= limit:
        raise HTTPException(
            status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
            detail=QuotaExceeded(
                limit_type="max_projects",   # FE 의 limitTypeLabel 매핑이 "동시 프로젝트 수"
                                             # 로 정확히 표시 — UpgradePromptDialog 부제 누락 방지.
                current=current,
                limit=limit,
                subscription_type=subscription,
                message=(
                    f"{_grade_label(subscription)} 등급은 동시에 보유 가능한 "
                    f"프로젝트가 {limit}개 입니다. {_next_tier_suggestion(subscription)}"
                ),
            ).to_dict(),
        )


async def acquire_meeting_quota(email: str) -> "IncrementResult":
    """
    `postMeeting` 진입 시 호출 — atomic 한도 체크 + +1.

    초과 시 HTTPException(402). 통과 시 카운트가 +1 된 IncrementResult 반환.

    [의도 기준 카운팅]
    LLM 호출이 사후에 실패해도 카운트는 차감되지 않음 (롤백 없음). 운영 단순성
    우선. 사용자 불만 시 admin 의 reset_usage 로 해소.

    Raises:
        fastapi.HTTPException(402): 한도 초과 또는 사용자 노드 없음.
    """
    from fastapi import HTTPException, status as http_status
    from app.service import usage_repository

    await ensure_overrides_fresh()   # [2026-06-11] admin 변경 멀티프로세스 전파

    usage = await usage_repository.get_usage(email)
    if usage is None:
        # 인증 토큰만 있고 User 노드가 없는 비정상 상태 — 일관성 차원에서 차단.
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="사용자 정보를 찾을 수 없습니다. 다시 로그인 해주세요.",
        )
    limit = get_limit(usage.subscription_type, "meeting_logs")
    result = await usage_repository.try_increment_meeting_count(email, limit)
    if result is None:
        # try_increment 시점에 노드가 사라진 race — 위와 동일 응답.
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="사용자 정보를 찾을 수 없습니다. 다시 로그인 해주세요.",
        )
    if result.exceeded:
        raise HTTPException(
            status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
            detail=QuotaExceeded(
                limit_type="meeting_logs",
                current=result.current,
                limit=result.limit,
                subscription_type=result.subscription_type,
                reset_at=result.reset_at,
            ).to_dict(),
        )
    return result
