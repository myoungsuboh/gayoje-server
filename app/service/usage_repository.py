"""
사용량(쿼터) Neo4j 입출력.

[책임]
- User 노드의 누적 카운터(usage_meeting_count, usage_total_tokens, usage_total_chars)
  read / atomic increment.
- 정책(한도 숫자)은 `app/core/quota.py` 가 보유. 이 모듈은 Cypher 만.
- 호출자는 등급별 한도(limit)를 미리 결정해서 try_increment_* 에 전달.
  cypher 안에 정책 baked-in 하지 않음 — 정책 변경 시 cypher 수정 불필요.

[저장 모델 — User 노드 추가 필드]
- u.usage_meeting_count   : int  — postMeeting 호출 수 (현재 주기)
- u.usage_total_tokens    : int  — LLM 호출별 total_tokens 누적 (현재 주기)
- u.usage_total_chars     : int  — 회의록 입력 글자수 누적 (모니터링용)
- u.usage_updated_at      : datetime — 마지막 갱신 시각
- u.usage_reset_at        : datetime — 다음 자동 reset 시점 (2026-05 도입)
모두 lazy — 첫 increment 시 COALESCE 로 0 default 후 +N. 마이그레이션 0.

[동시성 — race 안전]
meeting_count 증가는 한도 체크 + 증가를 단일 cypher 트랜잭션에서 처리.
같은 사용자가 동시 두 요청을 보내도 둘 다 한도 체크 통과 후 둘 다 +1 되는
TOCTOU 버그 없음. (Neo4j 의 단일 트랜잭션 = MVCC 격리.)

[월간 reset 정책 — 2026-05 변경]
이전: "한 번 소진 시 영구 차단" (lifetime). 사용자 LTV 안정성/MRR 예측성 약함.
이후: get_usage / try_increment / add_tokens / add_chars 모든 경로의 cypher
      안에서 atomic 으로 reset_at 체크 → null 이거나 지났으면 카운터 0 +
      다음 reset_at 설정.

      [Anchor 정책 — 2026-05 가입일 기준]
      user_repository 의 _CREATE_USER_CYPHER 가 가입 시점에 usage_reset_at
      = now + 1mo 로 박음 → "가입일 기준 매월 reset" 캘린더 일관성 확보.
      예: 1/15 가입 → reset 매월 15일 (28~31일 변동 있음, 윤년 등).

      legacy 사용자 (usage_reset_at NULL) 는 self-healing cypher 가 첫 호출
      시점에 박아 anchor → last-access-anchored — 가입일 미상 케이스 호환.
      별도 cron 불필요 (자가-치유).

reset 주기는 datetime() + duration({months: 1}) — 약 28~31일.

[동시성 — 정확한 보증]
같은 사용자 두 요청이 reset_at 경계 ±수 ms 에 동시 도착하면 두 트랜잭션 모두
need_reset=true 로 stale read 가능 → Neo4j 의 노드 쓰기락 직렬화로 한 쪽이
앞서 reset+increment 후 commit, 다른 쪽이 자기 컨텍스트에 보관된 stale
need_reset 으로 다시 reset → 첫 쪽의 +1 이 0 으로 덮이는 under-count 가
이론상 가능. 사용자에 유리한 방향(보너스 1회). 빈도 매우 낮음. 정밀
serialization 이 필요하면 apoc.locks.nodes 도입 검토.

[마이그레이션 친화 — 2026-05]
기존 lifetime 정책 사용자의 누적 카운터는 reset_at NULL 상태로 잔존.
첫 호출 시 reset 분기가 발동 (NULL OR datetime() >= reset_at) → 카운터 0 +
reset_at 신규 설정. 즉 정책 변경 시점에 모든 기존 사용자가 새 출발.
이유: 한도 도달한 사용자가 갑자기 "한 달 대기" 충격을 막기 위함. 결제 시스템
미도입 상태라 마이그레이션 이득의 사업 손실 거의 0.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.clients import neo4j_client
from app.service.user_repository import SUBSCRIPTION_FREE

logger = logging.getLogger(__name__)


# ===== 도메인 모델 =====


@dataclass(frozen=True)
class Usage:
    """현재 사용량 + 등급. /auth/me/usage 응답 / FE 대시보드 카드용.

    [2026-05] reset_at 신규 — 다음 자동 reset (월간) 시점. FE 가 "다음 reset N일 후"
    표시. None 이면 첫 호출 (곧 reset_at 이 설정됨).
    """

    email: str
    subscription_type: str
    meeting_count: int
    total_tokens: int
    total_chars: int
    reset_at: Optional[str] = None  # ISO datetime string
    # [2026-06] 관리자 기간제 부여 만료 시점. None = 영구(또는 Paddle 구독). 지나면 free 로 self-heal.
    subscription_ends_at: Optional[str] = None  # ISO datetime string
    # [2026-06] Lite 오버플로우 카운터 — 메인(total_tokens) 소진 후 flash-lite 사용량.
    #   lite_tokens         : 월간 누적 (메인과 함께 월간 reset, 리포팅용)
    #   lite_daily_tokens   : 주기 누적 — [2026-06-11 주간 전환] 롤링 7일 self-healing
    #                         reset (이전 24h). 필드명의 daily 는 DB/API 호환 위해 유지.
    #   lite_daily_reset_at : 다음 주간 reset 시점
    lite_tokens: int = 0
    lite_daily_tokens: int = 0
    lite_daily_reset_at: Optional[str] = None  # ISO datetime string


@dataclass(frozen=True)
class IncrementResult:
    """`try_increment_meeting_count` 결과.

    exceeded=True 면 카운터는 증가하지 않음. current 는 한도 도달 시점의 값.
    exceeded=False 면 +1 적용된 새 값.

    [2026-05] reset_at — 다음 자동 reset 시점 (월간 정책). 한도 초과 응답에 포함되어
    FE 의 UpgradePromptDialog 가 "N일 후 reset" 안내.
    """

    exceeded: bool
    current: int
    limit: int
    subscription_type: str
    reset_at: Optional[str] = None  # ISO datetime string


# ===== Cypher =====


# [2026-05 월간 reset] 모든 read/write cypher 에 self-healing reset 로직 임베드.
# 한 트랜잭션 안에서 reset_at 체크 → 지났으면 카운터 0 + 다음 reset_at 설정 →
# 그 이후 일반 read/increment 수행. 별도 cron job 불필요.
#
# 정책:
#   - usage_reset_at IS NULL  → 첫 호출. reset_at 만 +1month 설정 (카운터 0 유지)
#   - datetime() >= usage_reset_at → 주기 종료. 카운터 reset + reset_at +1month
#   - 그 외                    → 그대로 유지
#
# Neo4j 의 FOREACH + CASE 가 conditional SET 관용구.

_GET_USAGE_CYPHER = """\
// 사용량 + 등급 한 번에 조회. 노드 없으면 row 없음.
// [2026-05] reset_at NULL (legacy/신규) 또는 지났으면 atomic 자동 reset.
// [2026-06-11] Lite 주기 카운터도 별도 타이머로 self-healing reset (롤링 7일 — 주간 전환).
// 마이그레이션 친화: 기존 카운터를 가진 사용자도 첫 호출 시 새 출발.
MATCH (u:User {email: $email})
// [2026-06] 관리자 기간제 부여 만료 — subscription_ends_at 지났으면 free 강등(self-healing).
//   admin change_subscription 이 기간제로 부여 시 ends_at 설정. null = 영구.
//   Paddle 구독은 ends_at 을 안 쓴다(해지 웹훅이 강등 담당) → 영향 없음.
//   need_expire 는 ends_at 이 박힌 사용자만 true → 그 외 전원 no-op (블래스트 반경 극소).
WITH u,
     (u.subscription_ends_at IS NOT NULL AND datetime() >= u.subscription_ends_at) AS need_expire
FOREACH (_ IN CASE WHEN need_expire THEN [1] ELSE [] END |
    SET u.subscription_type = 'free',
        u.subscription_ends_at = null,
        u.subscription_updated_at = datetime()
)
WITH u,
     (u.usage_reset_at IS NULL OR datetime() >= u.usage_reset_at) AS need_reset
FOREACH (_ IN CASE WHEN need_reset THEN [1] ELSE [] END |
    SET u.usage_meeting_count = 0,
        u.usage_total_tokens = 0,
        u.usage_total_chars = 0,
        u.usage_lite_tokens = 0,
        u.usage_reset_at = datetime() + duration({months: 1}),
        u.usage_updated_at = datetime()
)
WITH u,
     (u.usage_lite_daily_reset_at IS NULL OR datetime() >= u.usage_lite_daily_reset_at) AS need_daily_reset
FOREACH (_ IN CASE WHEN need_daily_reset THEN [1] ELSE [] END |
    SET u.usage_lite_daily_tokens = 0,
        u.usage_lite_daily_reset_at = datetime() + duration({days: 7})
)
RETURN {
    email: u.email,
    subscription_type: COALESCE(u.subscription_type, 'free'),
    meeting_count: COALESCE(u.usage_meeting_count, 0),
    total_tokens: COALESCE(u.usage_total_tokens, 0),
    total_chars: COALESCE(u.usage_total_chars, 0),
    reset_at: toString(u.usage_reset_at),
    subscription_ends_at: toString(u.subscription_ends_at),
    lite_tokens: COALESCE(u.usage_lite_tokens, 0),
    lite_daily_tokens: COALESCE(u.usage_lite_daily_tokens, 0),
    lite_daily_reset_at: toString(u.usage_lite_daily_reset_at)
} AS usage
"""

# meeting_count: 한도 체크 + 증가 atomic. exceeded=True 면 증가 안 함.
# 한도 값($limit)은 호출자가 등급별로 미리 결정해서 주입 — 정책 변경 시 cypher 수정 불필요.
# [2026-05] 한도 체크 전 reset_at 체크 — 주기 지났으면 먼저 reset 후 +1 시도.
#
# [Fix #3 — 2026-05 보안 점검: Quota race]
# 첫 줄의 `SET u.usage_updated_at = datetime()` 는 단순 timestamp 갱신이 아니라
# Neo4j 의 노드 exclusive lock 을 조기 획득하기 위한 "touch first to lock" 패턴.
# 이전 버전: WITH 로 current 캡쳐 후 (current >= limit) 체크 → SET current+1.
#   동시 두 transaction 모두 current=N-1 읽으면 둘 다 통과 후 둘 다 SET=N →
#   한 카운트 누락 = quota 우회 가능.
# 변경: 시작 직후 SET 으로 lock 획득 → 다음 transaction 은 lock 해제까지 block →
#   해제 후 갱신된 값을 읽어 정확히 판단. 두 transaction 이 직렬화됨.
_TRY_INCREMENT_MEETING_COUNT_CYPHER = """\
MATCH (u:User {email: $email})
// [Fix #3] Acquire exclusive write lock on u BEFORE reading current count.
// Without this SET, two concurrent transactions both read current=N-1,
// both pass (current >= limit) check, both increment to N — 1 attempt lost
// = quota bypass. Touching usage_updated_at forces lock, serializing them.
SET u.usage_updated_at = datetime()
WITH u,
     (u.usage_reset_at IS NULL OR datetime() >= u.usage_reset_at) AS need_reset
FOREACH (_ IN CASE WHEN need_reset THEN [1] ELSE [] END |
    SET u.usage_meeting_count = 0,
        u.usage_total_tokens = 0,
        u.usage_total_chars = 0,
        u.usage_lite_tokens = 0,
        u.usage_reset_at = datetime() + duration({months: 1})
)
WITH u,
     COALESCE(u.usage_meeting_count, 0) AS current,
     COALESCE(u.subscription_type, 'free') AS subscription_type,
     $limit AS limit
WITH u, current, subscription_type, limit,
     (current >= limit) AS exceeded
FOREACH (_ IN CASE WHEN exceeded THEN [] ELSE [1] END |
    SET u.usage_meeting_count = current + 1
)
RETURN {
    exceeded: exceeded,
    current: CASE WHEN exceeded THEN current ELSE current + 1 END,
    limit: limit,
    subscription_type: subscription_type,
    reset_at: toString(u.usage_reset_at)
} AS result
"""

# 토큰 누적 — 한도 체크 없이 항상 +N. (LLM 응답 후 사후 적재 — 한도 초과는 다음 pre-check 가 막음.)
# [2026-05] reset_at 지났으면 먼저 reset (포함 +$delta).
# [Fix #3 — 보안 점검] 첫 SET 이 lock 조기 획득 — reset+increment 조합 직렬화.
_ADD_TOKENS_CYPHER = """\
MATCH (u:User {email: $email})
SET u.usage_updated_at = datetime()
WITH u,
     (u.usage_reset_at IS NULL OR datetime() >= u.usage_reset_at) AS need_reset
FOREACH (_ IN CASE WHEN need_reset THEN [1] ELSE [] END |
    SET u.usage_meeting_count = 0,
        u.usage_total_tokens = 0,
        u.usage_total_chars = 0,
        u.usage_lite_tokens = 0,
        u.usage_reset_at = datetime() + duration({months: 1})
)
SET u.usage_total_tokens = COALESCE(u.usage_total_tokens, 0) + $delta
RETURN COALESCE(u.usage_total_tokens, 0) AS total
"""

# [2026-06] Lite 오버플로우 토큰 누적 — 메인 소진 후 flash-lite 사용량.
# 월간 카운터(usage_lite_tokens) + 주기 카운터(usage_lite_daily_tokens) 동시 +N.
# 월간 reset(메인과 동일 타이머) + 주간 reset(롤링 7일 별도 타이머) 모두 임베드.
# [Fix #3 패턴] 첫 SET 으로 lock 조기 획득 — reset+increment 직렬화 (주간캡 race 최소화).
_ADD_LITE_TOKENS_CYPHER = """\
MATCH (u:User {email: $email})
SET u.usage_updated_at = datetime()
WITH u,
     (u.usage_reset_at IS NULL OR datetime() >= u.usage_reset_at) AS need_reset
FOREACH (_ IN CASE WHEN need_reset THEN [1] ELSE [] END |
    SET u.usage_meeting_count = 0,
        u.usage_total_tokens = 0,
        u.usage_total_chars = 0,
        u.usage_lite_tokens = 0,
        u.usage_reset_at = datetime() + duration({months: 1})
)
WITH u,
     (u.usage_lite_daily_reset_at IS NULL OR datetime() >= u.usage_lite_daily_reset_at) AS need_daily_reset
FOREACH (_ IN CASE WHEN need_daily_reset THEN [1] ELSE [] END |
    SET u.usage_lite_daily_tokens = 0,
        u.usage_lite_daily_reset_at = datetime() + duration({days: 7})
)
SET u.usage_lite_tokens = COALESCE(u.usage_lite_tokens, 0) + $delta,
    u.usage_lite_daily_tokens = COALESCE(u.usage_lite_daily_tokens, 0) + $delta
RETURN COALESCE(u.usage_lite_daily_tokens, 0) AS daily_total
"""

_ADD_CHARS_CYPHER = """\
MATCH (u:User {email: $email})
SET u.usage_updated_at = datetime()
WITH u,
     (u.usage_reset_at IS NULL OR datetime() >= u.usage_reset_at) AS need_reset
FOREACH (_ IN CASE WHEN need_reset THEN [1] ELSE [] END |
    SET u.usage_meeting_count = 0,
        u.usage_total_tokens = 0,
        u.usage_total_chars = 0,
        u.usage_lite_tokens = 0,
        u.usage_reset_at = datetime() + duration({months: 1})
)
SET u.usage_total_chars = COALESCE(u.usage_total_chars, 0) + $delta
RETURN COALESCE(u.usage_total_chars, 0) AS total
"""

# admin override — 사용량 초기화. 감사 로그는 호출자(admin_routes) 책임.
#
# [정책 — 2026-05]
# admin reset 은 "이번 cycle 카운터만 0 으로 리셋" 의미. reset_at 은 건드리지 않음
# (= 사용자가 cycle 25일째 받았다면 남은 5일 그대로). 이유:
#   - admin 이 무한히 reset 호출해서 사용자가 cycle 무한 확장하는 abuse 방지.
#   - "이번 cycle 살려주자" 의도 그대로 반영. 새 cycle 부여하고 싶으면
#     admin 이 의도적으로 등급 변경 (change_subscription) 호출 — 그 cypher 가
#     reset_at 도 갱신.
_RESET_USAGE_CYPHER = """\
MATCH (u:User {email: $email})
SET u.usage_meeting_count = 0,
    u.usage_total_tokens = 0,
    u.usage_total_chars = 0,
    u.usage_lite_tokens = 0,
    u.usage_lite_daily_tokens = 0,
    u.usage_updated_at = datetime()
RETURN u.email AS email
"""


# ===== 함수 =====


async def get_usage(email: str) -> Optional[Usage]:
    """사용량 + 등급 조회. 사용자 없으면 None."""
    rows = await neo4j_client.run_cypher(_GET_USAGE_CYPHER, {"email": email})
    if not rows:
        return None
    row = (rows[0] or {}).get("usage") or {}
    if not row.get("email"):
        return None
    return Usage(
        email=row["email"],
        subscription_type=row.get("subscription_type") or SUBSCRIPTION_FREE,
        meeting_count=int(row.get("meeting_count") or 0),
        total_tokens=int(row.get("total_tokens") or 0),
        total_chars=int(row.get("total_chars") or 0),
        reset_at=row.get("reset_at"),
        subscription_ends_at=row.get("subscription_ends_at"),
        lite_tokens=int(row.get("lite_tokens") or 0),
        lite_daily_tokens=int(row.get("lite_daily_tokens") or 0),
        lite_daily_reset_at=row.get("lite_daily_reset_at"),
    )


async def try_increment_meeting_count(
    email: str, limit: int
) -> Optional[IncrementResult]:
    """
    `postMeeting` 진입 시 호출 — 한도 체크 + atomic +1.

    Args:
        email: 대상 사용자
        limit: 등급별 meeting_logs 한도 (`quota.get_limit(grade, "meeting_logs")`)

    Returns:
        IncrementResult: exceeded / current / limit / subscription_type.
        사용자가 없으면 None.

    [동시성]
    한도 체크 + SET 이 단일 cypher 트랜잭션 안에서 처리되므로 TOCTOU 안전.
    """
    rows = await neo4j_client.run_cypher(
        _TRY_INCREMENT_MEETING_COUNT_CYPHER,
        {"email": email, "limit": int(limit)},
    )
    if not rows:
        return None
    r = (rows[0] or {}).get("result") or {}
    return IncrementResult(
        exceeded=bool(r.get("exceeded")),
        current=int(r.get("current") or 0),
        limit=int(r.get("limit") or 0),
        subscription_type=r.get("subscription_type") or SUBSCRIPTION_FREE,
        reset_at=r.get("reset_at"),
    )


async def add_tokens(email: str, delta: int, *, bucket: str = "main") -> Optional[int]:
    """LLM 호출 후 토큰 누적. 사용자 없으면 None.

    Args:
        delta: 누적할 토큰 수. <= 0 은 no-op (잘못된 LLM 응답 방어).
        bucket: "main" (메인/Flash, total_tokens) 또는 "lite" (오버플로우/flash-lite,
                lite_tokens 월간 + lite_daily_tokens 일일). 호출자(_quota_helpers /
                jobs)가 QuotaDecision.bucket 을 그대로 전달.

    Returns:
        main 버킷이면 누적된 total_tokens, lite 버킷이면 누적된 lite_daily_tokens.
    """
    if delta <= 0:
        if delta < 0:
            logger.warning("add_tokens: negative delta ignored (email=%s, delta=%d)", email, delta)
        return None
    cypher = _ADD_LITE_TOKENS_CYPHER if bucket == "lite" else _ADD_TOKENS_CYPHER
    result_key = "daily_total" if bucket == "lite" else "total"
    rows = await neo4j_client.run_cypher(cypher, {"email": email, "delta": int(delta)})
    if not rows:
        return None
    return int((rows[0] or {}).get(result_key) or 0)


async def add_chars(email: str, delta: int) -> Optional[int]:
    """회의록 입력 글자수 누적 (모니터링용)."""
    if delta <= 0:
        return None
    rows = await neo4j_client.run_cypher(
        _ADD_CHARS_CYPHER, {"email": email, "delta": int(delta)}
    )
    if not rows:
        return None
    return int((rows[0] or {}).get("total") or 0)


async def reset_usage(email: str) -> bool:
    """admin 전용 — 사용량 초기화. audit 로그는 호출자(admin_routes) 책임."""
    rows = await neo4j_client.run_cypher(_RESET_USAGE_CYPHER, {"email": email})
    if not rows:
        return False
    return bool((rows[0] or {}).get("email"))
