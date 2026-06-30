"""
Coupon — 베타 신청자 / 지인 / 마케팅 캠페인용 N개월 무료 코드.

[배경 — 2026-05]
지인 / 베타 테스터에게 "한 달 Pro 무료 코드" 같은 형태로 발급해 진짜 결제
flow 까지 사용해보게 하는 게 목적. 친구분 조언 — "진짜 판매 단계까지 만든
다음 공짜 쿠폰 뿌려서 피드백 받으라". 결제 인프라 (Paddle MoR) 위에서
쿠폰 추가만으로 시뮬레이션 가능.

[스키마]
(:Coupon {
    code: str,               # 사용자가 입력하는 코드 (e.g. "BETA-A1B2C3", UNIQUE)
    applies_to_tier: str,    # 적용 가능한 등급. 'pro'면 pro 결제에만 적용 가능.
                             # 'any' 이면 모든 유료 등급 (Pro/Pro+/Pro Max) 에 적용.
    free_months: int,        # 무료 제공 개월수 (1-12). 끝나면 자동 결제 재개.
    max_uses: int,           # 사용 가능 총 횟수. 0 = 무제한.
    used_count: int,         # 현재까지 사용된 횟수.
    expires_at: datetime?,   # 발급 만료. null = 무기한.
    active: bool,            # 회수 (revoke) 시 false.
    note: str,               # admin 메모 (e.g. "지인 베타 — kakao 채팅방")
    created_at: datetime,
    created_by: str          # admin email
})

(:User)-[:REDEEMED {at: datetime, free_months: int}]->(:Coupon)
  — 사용 이력 + 같은 코드 중복 사용 방지.

[흐름]
- 발급: admin/coupons.vue → POST /api/admin/coupons → create_coupon()
- 검증: pricing.vue 쿠폰 입력 → POST /api/coupons/validate → validate_coupon()
- 적용: subscribe_route 내부 → redeem_coupon() (atomic increment + REDEEMED edge)
- 회수: admin → DELETE /api/admin/coupons/{code} → revoke_coupon() (active=false)

[정책]
- 같은 사용자가 같은 코드 두 번 사용 불가 (REDEEMED edge 검사)
- max_uses 초과 / expires_at 지남 / active=false 시 validate 실패
- free_months > 0 + discount 100% 가 묵시적 (= 첫 N개월 결제 skip)
- 결제 skip 시점은 subscribe_route 에서 결정 (이 모듈은 검증/카운트만)
"""
from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from app.clients import neo4j_client
from app.core.subscription import PAID_SUBSCRIPTIONS, SUBSCRIPTION_TYPES

logger = logging.getLogger(__name__)


# ===== 도메인 모델 =====


# applies_to_tier 의 특수값 — 모든 유료 등급에 적용 가능.
COUPON_TIER_ANY = "any"


@dataclass(frozen=True)
class Coupon:
    """쿠폰 1장."""

    code: str
    applies_to_tier: str
    free_months: int
    max_uses: int
    used_count: int
    active: bool
    note: str
    created_at: Optional[str]
    created_by: Optional[str]
    expires_at: Optional[str]

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "applies_to_tier": self.applies_to_tier,
            "free_months": self.free_months,
            "max_uses": self.max_uses,
            "used_count": self.used_count,
            "active": self.active,
            "note": self.note,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "expires_at": self.expires_at,
            "remaining": (self.max_uses - self.used_count) if self.max_uses > 0 else None,
        }

    def is_valid_for_tier(self, tier: str) -> bool:
        """이 쿠폰을 해당 등급에 적용 가능한가."""
        if self.applies_to_tier == COUPON_TIER_ANY:
            return tier in PAID_SUBSCRIPTIONS
        return self.applies_to_tier == tier

    def is_exhausted(self) -> bool:
        """max_uses 도달 여부 (0 = 무제한이므로 항상 False)."""
        if self.max_uses <= 0:
            return False
        return self.used_count >= self.max_uses

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """expires_at 지남 여부. expires_at 없으면 False."""
        if not self.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        cur = now or datetime.now(timezone.utc)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return cur >= exp


# ===== 코드 생성 헬퍼 =====


# 사람이 읽기 쉬운 알파벳 + 숫자 (0/O, 1/I/l 같은 헷갈리는 글자 제외).
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_code(prefix: str = "BETA", length: int = 6) -> str:
    """랜덤 쿠폰 코드 생성 — `BETA-XXXXXX` 형식.

    secrets.choice 로 cryptographically random. 6자 + 31 alphabet 이면 9억 조합.
    충돌 시 호출자가 재시도 (UNIQUE 제약).
    """
    tail = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))
    if prefix:
        return f"{prefix.upper()}-{tail}"
    return tail


# ===== Cypher =====


_ENSURE_COUPON_CONSTRAINT_CYPHER = """\
// Coupon.code UNIQUE — 같은 코드 중복 발급 차단.
CREATE CONSTRAINT coupon_code_unique IF NOT EXISTS
FOR (c:Coupon) REQUIRE c.code IS UNIQUE
"""


_CREATE_COUPON_CYPHER = """\
CREATE (c:Coupon {
    code: $code,
    applies_to_tier: $applies_to_tier,
    free_months: $free_months,
    max_uses: $max_uses,
    used_count: 0,
    active: true,
    note: $note,
    created_at: datetime(),
    created_by: $created_by,
    expires_at: $expires_at
})
RETURN c.code AS code,
       c.applies_to_tier AS applies_to_tier,
       c.free_months AS free_months,
       c.max_uses AS max_uses,
       c.used_count AS used_count,
       c.active AS active,
       c.note AS note,
       toString(c.created_at) AS created_at,
       c.created_by AS created_by,
       toString(c.expires_at) AS expires_at
"""


_GET_COUPON_CYPHER = """\
MATCH (c:Coupon {code: $code})
RETURN c.code AS code,
       c.applies_to_tier AS applies_to_tier,
       c.free_months AS free_months,
       c.max_uses AS max_uses,
       c.used_count AS used_count,
       c.active AS active,
       c.note AS note,
       toString(c.created_at) AS created_at,
       c.created_by AS created_by,
       toString(c.expires_at) AS expires_at
"""


_LIST_COUPONS_CYPHER = """\
MATCH (c:Coupon)
RETURN c.code AS code,
       c.applies_to_tier AS applies_to_tier,
       c.free_months AS free_months,
       c.max_uses AS max_uses,
       c.used_count AS used_count,
       c.active AS active,
       c.note AS note,
       toString(c.created_at) AS created_at,
       c.created_by AS created_by,
       toString(c.expires_at) AS expires_at
ORDER BY c.created_at DESC
LIMIT $limit
"""


# 사용 이력 — REDEEMED edge 가 있으면 같은 사용자 재사용 불가.
_CHECK_USER_REDEEMED_CYPHER = """\
MATCH (u:User {email: $user_email})-[r:REDEEMED]->(c:Coupon {code: $code})
RETURN count(r) AS n
"""


# atomic redeem — used_count++ 와 REDEEMED edge 생성을 한 트랜잭션에.
# max_uses 도달했으면 SET 안 됨 (조건부).
_REDEEM_COUPON_CYPHER = """\
MATCH (u:User {email: $user_email})
MATCH (c:Coupon {code: $code})
WHERE c.active = true
  AND (c.max_uses = 0 OR c.used_count < c.max_uses)
SET c.used_count = c.used_count + 1
MERGE (u)-[r:REDEEMED]->(c)
ON CREATE SET r.at = datetime(), r.free_months = c.free_months
RETURN c.code AS code,
       c.applies_to_tier AS applies_to_tier,
       c.free_months AS free_months,
       c.max_uses AS max_uses,
       c.used_count AS used_count,
       c.active AS active,
       c.note AS note,
       toString(c.created_at) AS created_at,
       c.created_by AS created_by,
       toString(c.expires_at) AS expires_at
"""


_REVOKE_COUPON_CYPHER = """\
MATCH (c:Coupon {code: $code})
SET c.active = false
RETURN c.code AS code
"""


# ===== 부팅 헬퍼 =====


async def ensure_coupon_constraint() -> None:
    """Coupon.code UNIQUE 제약 ensure. Neo4j 미연결 시 warning."""
    try:
        await neo4j_client.run_cypher(_ENSURE_COUPON_CONSTRAINT_CYPHER)
        logger.info("coupon: Coupon.code UNIQUE 제약 ensure 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("coupon: UNIQUE 제약 실패 (%s)", e)


# ===== 함수 =====


def _row_to_coupon(row: dict) -> Coupon:
    return Coupon(
        code=row["code"],
        applies_to_tier=row.get("applies_to_tier") or COUPON_TIER_ANY,
        free_months=int(row.get("free_months") or 0),
        max_uses=int(row.get("max_uses") or 0),
        used_count=int(row.get("used_count") or 0),
        active=bool(row.get("active")),
        note=row.get("note") or "",
        created_at=row.get("created_at"),
        created_by=row.get("created_by"),
        expires_at=row.get("expires_at"),
    )


async def create_coupon(
    *,
    code: Optional[str],
    applies_to_tier: str,
    free_months: int,
    max_uses: int,
    expires_at: Optional[datetime],
    note: str,
    created_by: str,
) -> Optional[Coupon]:
    """쿠폰 1장 발급. code 가 None 이면 자동 생성. 충돌 시 최대 5회 재시도."""
    if applies_to_tier != COUPON_TIER_ANY and applies_to_tier not in PAID_SUBSCRIPTIONS:
        raise ValueError(
            f"applies_to_tier 는 'any' 또는 유료 등급 {PAID_SUBSCRIPTIONS} 중 하나여야 합니다."
        )
    if free_months < 1 or free_months > 12:
        raise ValueError("free_months 는 1~12 개월 사이여야 합니다.")
    if max_uses < 0:
        raise ValueError("max_uses 는 0 (무제한) 또는 양수여야 합니다.")

    exp_iso = expires_at.isoformat() if expires_at else None

    for attempt in range(5):
        the_code = (code or generate_code()).upper().strip()
        try:
            rows = await neo4j_client.run_cypher(
                _CREATE_COUPON_CYPHER,
                {
                    "code": the_code,
                    "applies_to_tier": applies_to_tier,
                    "free_months": int(free_months),
                    "max_uses": int(max_uses),
                    "note": note or "",
                    "created_by": created_by or "UNKNOWN",
                    "expires_at": exp_iso,
                },
            )
            if rows:
                return _row_to_coupon(rows[0])
        except Exception as e:  # noqa: BLE001
            # UNIQUE 위반 시 다시 시도 (자동 코드 생성 케이스).
            if code:
                # 사용자 지정 코드 — 충돌은 곧 에러.
                logger.warning("coupon: 사용자 지정 code 충돌 (%s)", the_code)
                return None
            logger.warning("coupon: 코드 충돌 — 재시도 %d (%s)", attempt + 1, e)
            continue
    return None


async def get_coupon(code: str) -> Optional[Coupon]:
    """쿠폰 1장 조회. 미존재면 None."""
    if not code:
        return None
    rows = await neo4j_client.run_cypher(
        _GET_COUPON_CYPHER, {"code": code.upper().strip()},
    )
    if not rows:
        return None
    return _row_to_coupon(rows[0])


async def list_coupons(limit: int = 200) -> List[Coupon]:
    """최근 발급된 쿠폰 목록 (created_at DESC). admin 용."""
    rows = await neo4j_client.run_cypher(
        _LIST_COUPONS_CYPHER, {"limit": max(1, int(limit))},
    )
    return [_row_to_coupon(r) for r in rows if r.get("code")]


async def user_already_redeemed(user_email: str, code: str) -> bool:
    """이 사용자가 이미 이 코드를 사용했는가."""
    if not user_email or not code:
        return False
    rows = await neo4j_client.run_cypher(
        _CHECK_USER_REDEEMED_CYPHER,
        {"user_email": user_email, "code": code.upper().strip()},
    )
    if not rows:
        return False
    return int(rows[0].get("n") or 0) > 0


@dataclass(frozen=True)
class CouponValidation:
    """validate_coupon 결과 — 이유 코드 + 메타."""
    ok: bool
    code: str
    reason: str = ""       # 'not_found' | 'inactive' | 'expired' | 'exhausted'
                           # | 'tier_mismatch' | 'already_redeemed'
    coupon: Optional[Coupon] = None


async def validate_coupon(
    code: str, *, user_email: str, tier: str,
) -> CouponValidation:
    """결제 전 검증 — 적용 가능 여부 + 이유.

    redeem 은 별도 호출 (atomic). validate 는 멱등.
    """
    code_norm = (code or "").upper().strip()
    if not code_norm:
        return CouponValidation(ok=False, code="", reason="not_found")

    coupon = await get_coupon(code_norm)
    if not coupon:
        return CouponValidation(ok=False, code=code_norm, reason="not_found")
    if not coupon.active:
        return CouponValidation(ok=False, code=code_norm, reason="inactive", coupon=coupon)
    if coupon.is_expired():
        return CouponValidation(ok=False, code=code_norm, reason="expired", coupon=coupon)
    if coupon.is_exhausted():
        return CouponValidation(ok=False, code=code_norm, reason="exhausted", coupon=coupon)
    if tier not in SUBSCRIPTION_TYPES or not coupon.is_valid_for_tier(tier):
        return CouponValidation(ok=False, code=code_norm, reason="tier_mismatch", coupon=coupon)
    if await user_already_redeemed(user_email, code_norm):
        return CouponValidation(ok=False, code=code_norm, reason="already_redeemed", coupon=coupon)

    return CouponValidation(ok=True, code=code_norm, coupon=coupon)


async def redeem_coupon(
    code: str, *, user_email: str,
) -> Optional[Coupon]:
    """원자적 사용 — used_count++ + REDEEMED edge.

    호출자는 사전에 validate_coupon 으로 검증해야 함. 이 함수는 race 안전한
    final commit. 동시에 같은 코드를 두 사용자가 사용해서 max_uses 초과되는
    상황은 Neo4j MATCH .. WHERE c.used_count < c.max_uses 조건절로 차단.
    """
    code_norm = (code or "").upper().strip()
    if not code_norm or not user_email:
        return None
    rows = await neo4j_client.run_cypher(
        _REDEEM_COUPON_CYPHER,
        {"user_email": user_email, "code": code_norm},
    )
    if not rows:
        return None
    return _row_to_coupon(rows[0])


async def revoke_coupon(code: str) -> bool:
    """active=false 로 마킹 (기존 사용 이력은 보존)."""
    code_norm = (code or "").upper().strip()
    if not code_norm:
        return False
    rows = await neo4j_client.run_cypher(
        _REVOKE_COUPON_CYPHER, {"code": code_norm},
    )
    return bool(rows)
