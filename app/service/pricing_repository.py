"""
PricingConfig — 등급별 동적 가격/할인율 관리.

[배경 — 2026-05]
기존엔 FE `subscription.js` 의 priceKRW 하드코딩 → 가격 변경 시 코드 수정 + 재배포 필요.
admin 운영 편의를 위해 BE 의 단일 정보 출처 (single source of truth) 로 이동.

[통화 — 2026-06 USD 전환]
Paddle(MoR) 도입으로 결제 통화를 USD 로 통일. 내부 저장 단위는 **최소 단위(센트)** 정수.
(USD: 센트 → $9 = 900 / KRW: 원 → ₩9,900 = 9900. Paddle 도 lowest-denomination 사용.)
`currency` 필드로 FE 가 통화 보고 포맷 → BE 미배포 구간에도 안전(₩ fallback).

[스키마]
(:PricingConfig {
    tier: str,               # 'free' | 'pro' | 'pro_plus' | 'pro_max'
    base_price: int,         # 정가 (최소 단위 — USD 센트 / KRW 원)
    discount_pct: int,       # 할인율 (%, 0-100)
    currency: str,           # 'USD' | 'KRW' (없으면 legacy=KRW)
    updated_at: datetime,
    updated_by: str          # 마지막 수정 admin email (없으면 'SYSTEM:SEED')
})

[final_price 계산]
- USD: round(base * (1 - pct/100))          # 센트 단위 반올림
- KRW: round(base * (1 - pct/100), -2)       # 100원 단위 반올림(legacy)

[seed 정책]
부팅 시 ensure_pricing_seeded() — 노드 없으면 default 생성 (idempotent).
기존 KRW 노드는 ensure_pricing_usd_migration() 가 USD 캐노니컬로 1회 전환.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from app.clients import neo4j_client
from app.core.subscription import (
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    SUBSCRIPTION_PRO_MAX,
    SUBSCRIPTION_PRO_PLUS,
    SUBSCRIPTION_TYPES,
)

logger = logging.getLogger(__name__)

# 결제 통화 — 2026-06 USD 전환. (legacy 데이터는 currency 없음 → KRW 로 해석.)
CURRENCY_USD = "USD"
CURRENCY_KRW = "KRW"
_LEGACY_CURRENCY = CURRENCY_KRW


# ===== 도메인 모델 =====


@dataclass(frozen=True)
class PricingConfig:
    """등급 1개의 가격 설정."""

    tier: str
    base_price: int          # 정가 (최소 단위: USD 센트 / KRW 원)
    discount_pct: int        # 0-100
    final_price: int         # 자동 계산 (통화별 반올림)
    currency: str = CURRENCY_USD
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "base_price": self.base_price,
            "discount_pct": self.discount_pct,
            "final_price": self.final_price,
            "currency": self.currency,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


# ===== 헬퍼 =====


def calculate_final_price(
    base_price: int, discount_pct: int, currency: str = CURRENCY_USD
) -> int:
    """정가 + 할인율 → 통화별 반올림한 최종 가격(최소 단위 정수).

    - USD: 센트 단위 반올림 (round to 1)
    - KRW: 100원 단위 반올림 (legacy)
    """
    if base_price <= 0:
        return 0
    pct = max(0, min(100, int(discount_pct)))
    raw = base_price * (100 - pct) / 100.0
    if currency == CURRENCY_KRW:
        return int(round(raw / 100) * 100)  # 100원 단위
    return int(round(raw))                  # USD 센트 단위


# ===== 기본값 (USD — 2026-06, pricing-final.md) =====
# 최소 단위(센트). Pro $9 / Pro+ $19 / Pro Max $29, 플랫(할인 0%).
# (KRW-era 볼륨할인은 USD 최종가에 이미 반영 — 할인 중복 방지.)

_DEFAULT_CURRENCY = CURRENCY_USD

_DEFAULT_PRICING: dict[str, dict[str, int]] = {
    SUBSCRIPTION_FREE:     {"base_price": 0,    "discount_pct": 0},
    # [2026-06-11 .99 전환] 좌측 자릿수 효과 — 체감가는 "$9대" 그대로, 매출 +3~11%.
    # (개인 등급만 .99 — 향후 팀/엔터프라이즈 플랜은 라운드 가격 권장.)
    SUBSCRIPTION_PRO:      {"base_price": 999,  "discount_pct": 0},   # $9.99
    SUBSCRIPTION_PRO_PLUS: {"base_price": 1999, "discount_pct": 0},   # $19.99
    SUBSCRIPTION_PRO_MAX:  {"base_price": 2999, "discount_pct": 0},   # $29.99
}


# ===== Cypher =====


_ENSURE_PRICING_CONSTRAINT_CYPHER = """\
// PricingConfig.tier UNIQUE — 등급당 1개 노드.
CREATE CONSTRAINT pricing_config_tier_unique IF NOT EXISTS
FOR (p:PricingConfig) REQUIRE p.tier IS UNIQUE
"""


# 부팅 seed — 노드 없을 때만 생성 (idempotent). 기존 값은 보존.
_SEED_PRICING_CYPHER = """\
MERGE (p:PricingConfig {tier: $tier})
ON CREATE SET p.base_price = $base_price,
              p.discount_pct = $discount_pct,
              p.currency = $currency,
              p.updated_at = datetime(),
              p.updated_by = 'SYSTEM:SEED'
RETURN p.tier AS tier,
       p.base_price AS base_price,
       p.discount_pct AS discount_pct,
       p.currency AS currency,
       toString(p.updated_at) AS updated_at,
       p.updated_by AS updated_by
"""


# USD 전환 마이그레이션 — currency 가 USD 가 아닌(=legacy KRW 또는 없음) 시스템 행을
# USD 캐노니컬 값으로 1회 덮어씀. 멱등 (이미 USD 면 WHERE 로 skip).
_MIGRATE_USD_CYPHER = """\
MATCH (p:PricingConfig {tier: $tier})
WHERE coalesce(p.currency, 'KRW') <> 'USD'
SET p.base_price = $base_price,
    p.discount_pct = $discount_pct,
    p.currency = 'USD',
    p.updated_at = datetime(),
    p.updated_by = 'SYSTEM:USD_MIGRATION'
RETURN p.tier AS tier
"""


_LIST_PRICING_CYPHER = """\
MATCH (p:PricingConfig)
RETURN p.tier AS tier,
       p.base_price AS base_price,
       p.discount_pct AS discount_pct,
       p.currency AS currency,
       toString(p.updated_at) AS updated_at,
       p.updated_by AS updated_by
ORDER BY
  CASE p.tier
    WHEN 'free' THEN 0
    WHEN 'pro' THEN 1
    WHEN 'pro_plus' THEN 2
    WHEN 'pro_max' THEN 3
    ELSE 99
  END
"""


_GET_PRICING_CYPHER = """\
MATCH (p:PricingConfig {tier: $tier})
RETURN p.tier AS tier,
       p.base_price AS base_price,
       p.discount_pct AS discount_pct,
       p.currency AS currency,
       toString(p.updated_at) AS updated_at,
       p.updated_by AS updated_by
"""


_UPDATE_PRICING_CYPHER = """\
MATCH (p:PricingConfig {tier: $tier})
SET p.base_price = $base_price,
    p.discount_pct = $discount_pct,
    p.updated_at = datetime(),
    p.updated_by = $updated_by
RETURN p.tier AS tier,
       p.base_price AS base_price,
       p.discount_pct AS discount_pct,
       p.currency AS currency,
       toString(p.updated_at) AS updated_at,
       p.updated_by AS updated_by
"""


# ===== 부팅 헬퍼 =====


async def ensure_pricing_constraint() -> None:
    """PricingConfig.tier UNIQUE 제약 ensure. Neo4j 미연결 시 warning."""
    try:
        await neo4j_client.run_cypher(_ENSURE_PRICING_CONSTRAINT_CYPHER)
        logger.info("pricing: PricingConfig.tier UNIQUE 제약 ensure 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("pricing: UNIQUE 제약 실패 (%s)", e)


async def ensure_pricing_seeded() -> None:
    """4개 등급의 default 가격 노드 생성 (idempotent — 기존 값 보존).

    부팅 시 호출. ON CREATE 만 발동하므로 기존 admin 이 수정한 값은 안 덮어씀.
    """
    try:
        for tier, cfg in _DEFAULT_PRICING.items():
            await neo4j_client.run_cypher(
                _SEED_PRICING_CYPHER,
                {
                    "tier": tier,
                    "base_price": cfg["base_price"],
                    "discount_pct": cfg["discount_pct"],
                    "currency": _DEFAULT_CURRENCY,
                },
            )
        logger.info("pricing: 4개 등급 default 가격 seed 완료 (기존 값 보존)")
    except Exception as e:  # noqa: BLE001
        logger.warning("pricing: seed 실패 (%s)", e)


async def ensure_pricing_usd_migration() -> None:
    """[2026-06] KRW → USD 전환 마이그레이션 (멱등, 1회성).

    currency 가 'USD' 가 아닌(legacy KRW 또는 미설정) 시스템 행을 USD 캐노니컬
    값으로 덮어쓴다. 라이브 DB 의 ₩9,900(=9900) 같은 값은 USD 전환 후 의미가 없으므로
    USD 정가($9=900 등)로 교체. 이미 USD 면 WHERE 절로 skip → 재부팅에도 안전.
    """
    try:
        migrated: list[str] = []
        for tier, cfg in _DEFAULT_PRICING.items():
            rows = await neo4j_client.run_cypher(
                _MIGRATE_USD_CYPHER,
                {
                    "tier": tier,
                    "base_price": cfg["base_price"],
                    "discount_pct": cfg["discount_pct"],
                },
            )
            if rows:
                migrated.append(tier)
        if migrated:
            logger.info("pricing: USD 마이그레이션 완료 — %s", ", ".join(migrated))
    except Exception as e:  # noqa: BLE001
        logger.warning("pricing: USD 마이그레이션 실패 (%s)", e)


# ===== 함수 =====


def _row_to_config(row: dict) -> PricingConfig:
    base = int(row.get("base_price") or 0)
    pct = int(row.get("discount_pct") or 0)
    # legacy 행은 currency 속성이 없음 → KRW 로 해석 (마이그레이션 전 안전망).
    currency = row.get("currency") or _LEGACY_CURRENCY
    return PricingConfig(
        tier=row["tier"],
        base_price=base,
        discount_pct=pct,
        final_price=calculate_final_price(base, pct, currency),
        currency=currency,
        updated_at=row.get("updated_at"),
        updated_by=row.get("updated_by"),
    )


async def list_pricing() -> List[PricingConfig]:
    """모든 등급의 가격 설정 (Free → Pro → Pro+ → Pro Max 순)."""
    rows = await neo4j_client.run_cypher(_LIST_PRICING_CYPHER)
    return [_row_to_config(r) for r in rows if r.get("tier")]


async def get_pricing(tier: str) -> Optional[PricingConfig]:
    """등급 1개의 가격 설정. 미존재면 None."""
    if tier not in SUBSCRIPTION_TYPES:
        return None
    rows = await neo4j_client.run_cypher(_GET_PRICING_CYPHER, {"tier": tier})
    if not rows:
        return None
    return _row_to_config(rows[0])


async def update_pricing(
    tier: str,
    base_price: int,
    discount_pct: int,
    updated_by: str,
) -> Optional[PricingConfig]:
    """가격 수정. 존재하지 않는 tier 또는 노드 없으면 None.

    currency 는 변경하지 않음(시스템 통화 고정) — 노드 기존 값 유지.
    호출자(admin route) 가 audit_repository 에 별도 기록.
    """
    if tier not in SUBSCRIPTION_TYPES:
        return None
    # 입력 검증
    base = max(0, int(base_price))
    pct = max(0, min(100, int(discount_pct)))

    rows = await neo4j_client.run_cypher(
        _UPDATE_PRICING_CYPHER,
        {
            "tier": tier,
            "base_price": base,
            "discount_pct": pct,
            "updated_by": updated_by or "UNKNOWN",
        },
    )
    if not rows:
        return None
    return _row_to_config(rows[0])
