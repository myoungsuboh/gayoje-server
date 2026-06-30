"""
QuotaConfig — 등급별 동적 한도 (meeting_logs / summary_chars / total_tokens
/ library_skills / max_projects) 관리.

[배경 — 2026-05-17]
이전엔 `app/core/quota.py` 의 `_FREE_LIMITS` / `_PRO_LIMITS` / ... 가 코드 상수라
한도 조정 시 코드 수정 + 재배포 필요. admin 운영 편의를 위해 PricingConfig 와
동일한 패턴으로 DB 노드화.

[스키마]
(:QuotaConfig {
    tier: str,               # 'free' | 'pro' | 'pro_plus' | 'pro_max'
    meeting_logs: int,       # 월간 미팅 로그 등록 한도
    summary_chars: int,      # 회의록 1회 입력 글자수 상한 (per-request)
    total_tokens: int,       # 월간 LLM 누적 토큰
    library_skills: int,     # 라이브러리 저장 스킬 수 (현재 시점 count)
    max_projects: int,       # 동시 보유 프로젝트 수 (현재 시점 count)
    updated_at: datetime,
    updated_by: str          # 마지막 수정 admin email (없으면 'SYSTEM:SEED')
})

[seed 정책]
부팅 시 ensure_quota_config_seeded() — 노드 없으면 default 생성 (idempotent).
default 값은 `app/core/quota.py` 의 _FREE_LIMITS / _PRO_LIMITS / ... 와 동일
(코드 상수가 single source of truth 였던 시점의 마지막 값).
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


# ===== 도메인 모델 =====


@dataclass(frozen=True)
class QuotaConfig:
    """등급 1개의 한도 설정."""

    tier: str
    meeting_logs: int
    summary_chars: int
    total_tokens: int
    library_skills: int
    max_projects: int
    lite_daily_cap: int = 0   # 메인 소진 후 Lite 오버플로우 주간 캡 (롤링 7일, 0=하드월)
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "meeting_logs": self.meeting_logs,
            "summary_chars": self.summary_chars,
            "total_tokens": self.total_tokens,
            "library_skills": self.library_skills,
            "max_projects": self.max_projects,
            "lite_daily_cap": self.lite_daily_cap,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


# ===== 기본값 (quota.py 와 동기) =====
#
# 코드 상수 정책의 마지막 값과 동일. seed 후 admin 이 변경하면 그 값이 우선.
# quota.py 의 상수는 DB 미사용 환경 (테스트 / Neo4j 미연결) fallback 으로 유지.

# [2026-06-11 마진 재조정] quota.py 의 _FREE_LIMITS/.. + _LITE_DAILY_CAP 와 동기.
# 메인 1M/2M/4M/8M, Lite 주간캡 0/1.5M/3M/5M (2026-06-11 일→주 전환) — 워스트
# 토큰 원가를 매출의 ~33-41% 로 바운드 (이전 64~73%, 산식은 quota.py 주석).
# (seed 는 ON CREATE 라 기존 운영 노드는 보존 — 운영 반영은 admin 한도 관리에서 저장.)
_DEFAULT_QUOTA: dict[str, dict[str, int]] = {
    SUBSCRIPTION_FREE: {
        "meeting_logs": 5,
        "summary_chars": 10_000,
        "total_tokens": 1_000_000,
        "library_skills": 100,
        "max_projects": 1,
        "lite_daily_cap": 0,            # 하드월 (오버플로우 없음). 캡은 주간(롤링 7일) 단위.
    },
    SUBSCRIPTION_PRO: {
        "meeting_logs": 50,
        "summary_chars": 50_000,
        "total_tokens": 2_000_000,
        "library_skills": 1_000,
        "max_projects": 3,
        "lite_daily_cap": 1_500_000,    # 주간(롤링 7일) 소프트랜딩
    },
    SUBSCRIPTION_PRO_PLUS: {
        "meeting_logs": 100,
        "summary_chars": 100_000,
        "total_tokens": 4_000_000,
        "library_skills": 2_000,
        "max_projects": 6,
        "lite_daily_cap": 3_000_000,    # 주간 "무제한" (공정사용)
    },
    SUBSCRIPTION_PRO_MAX: {
        "meeting_logs": 200,
        "summary_chars": 200_000,
        "total_tokens": 8_000_000,
        "library_skills": 4_000,
        "max_projects": 12,
        "lite_daily_cap": 5_000_000,    # 주간 "무제한" (공정사용)
    },
}


# 변경 가능한 필드 — admin 이 한 번에 부분 갱신 가능.
LIMIT_FIELDS = (
    "meeting_logs",
    "summary_chars",
    "total_tokens",
    "library_skills",
    "max_projects",
    "lite_daily_cap",
)


# ===== Cypher =====


_ENSURE_QUOTA_CONSTRAINT_CYPHER = """\
CREATE CONSTRAINT quota_config_tier_unique IF NOT EXISTS
FOR (q:QuotaConfig) REQUIRE q.tier IS UNIQUE
"""


_SEED_QUOTA_CYPHER = """\
MERGE (q:QuotaConfig {tier: $tier})
ON CREATE SET q.meeting_logs = $meeting_logs,
              q.summary_chars = $summary_chars,
              q.total_tokens = $total_tokens,
              q.library_skills = $library_skills,
              q.max_projects = $max_projects,
              q.lite_daily_cap = $lite_daily_cap,
              q.updated_at = datetime(),
              q.updated_by = 'SYSTEM:SEED'
RETURN q.tier AS tier,
       q.meeting_logs AS meeting_logs,
       q.summary_chars AS summary_chars,
       q.total_tokens AS total_tokens,
       q.library_skills AS library_skills,
       q.max_projects AS max_projects,
       q.lite_daily_cap AS lite_daily_cap,
       toString(q.updated_at) AS updated_at,
       q.updated_by AS updated_by
"""


_LIST_QUOTA_CYPHER = """\
MATCH (q:QuotaConfig)
RETURN q.tier AS tier,
       q.meeting_logs AS meeting_logs,
       q.summary_chars AS summary_chars,
       q.total_tokens AS total_tokens,
       q.library_skills AS library_skills,
       q.max_projects AS max_projects,
       q.lite_daily_cap AS lite_daily_cap,
       toString(q.updated_at) AS updated_at,
       q.updated_by AS updated_by
ORDER BY
  CASE q.tier
    WHEN 'free' THEN 0
    WHEN 'pro' THEN 1
    WHEN 'pro_plus' THEN 2
    WHEN 'pro_max' THEN 3
    ELSE 99
  END
"""


_GET_QUOTA_CYPHER = """\
MATCH (q:QuotaConfig {tier: $tier})
RETURN q.tier AS tier,
       q.meeting_logs AS meeting_logs,
       q.summary_chars AS summary_chars,
       q.total_tokens AS total_tokens,
       q.library_skills AS library_skills,
       q.max_projects AS max_projects,
       q.lite_daily_cap AS lite_daily_cap,
       toString(q.updated_at) AS updated_at,
       q.updated_by AS updated_by
"""


_UPDATE_QUOTA_CYPHER = """\
MATCH (q:QuotaConfig {tier: $tier})
SET q.meeting_logs = $meeting_logs,
    q.summary_chars = $summary_chars,
    q.total_tokens = $total_tokens,
    q.library_skills = $library_skills,
    q.max_projects = $max_projects,
    q.lite_daily_cap = $lite_daily_cap,
    q.updated_at = datetime(),
    q.updated_by = $updated_by
RETURN q.tier AS tier,
       q.meeting_logs AS meeting_logs,
       q.summary_chars AS summary_chars,
       q.total_tokens AS total_tokens,
       q.library_skills AS library_skills,
       q.max_projects AS max_projects,
       q.lite_daily_cap AS lite_daily_cap,
       toString(q.updated_at) AS updated_at,
       q.updated_by AS updated_by
"""


# ===== 부팅 헬퍼 =====


async def ensure_quota_config_constraint() -> None:
    """QuotaConfig.tier UNIQUE 제약 ensure."""
    try:
        await neo4j_client.run_cypher(_ENSURE_QUOTA_CONSTRAINT_CYPHER)
        logger.info("quota_config: QuotaConfig.tier UNIQUE 제약 ensure 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("quota_config: UNIQUE 제약 실패 (%s)", e)


async def ensure_quota_config_seeded() -> None:
    """4개 등급의 default 한도 노드 생성 (idempotent — 기존 값 보존)."""
    try:
        for tier, cfg in _DEFAULT_QUOTA.items():
            await neo4j_client.run_cypher(
                _SEED_QUOTA_CYPHER,
                {"tier": tier, **cfg},
            )
        logger.info("quota_config: 4개 등급 default 한도 seed 완료 (기존 값 보존)")
    except Exception as e:  # noqa: BLE001
        logger.warning("quota_config: seed 실패 (%s)", e)


# ===== 함수 =====


def _row_to_config(row: dict) -> QuotaConfig:
    # [2026-06] lite_daily_cap 은 신규 필드 — 기존 운영 노드엔 속성이 없어 NULL.
    # 그 경우 코드 기본값(_DEFAULT_QUOTA)으로 fallback 해서, override 로딩이 0 을
    # 박아 라이브 오버플로우를 하드월로 깨뜨리는 사고를 방지한다 (마이그레이션 불필요).
    tier = row["tier"]
    raw_cap = row.get("lite_daily_cap")
    if raw_cap is None:
        raw_cap = _DEFAULT_QUOTA.get(tier, {}).get("lite_daily_cap", 0)
    return QuotaConfig(
        tier=tier,
        meeting_logs=int(row.get("meeting_logs") or 0),
        summary_chars=int(row.get("summary_chars") or 0),
        total_tokens=int(row.get("total_tokens") or 0),
        library_skills=int(row.get("library_skills") or 0),
        max_projects=int(row.get("max_projects") or 0),
        lite_daily_cap=int(raw_cap or 0),
        updated_at=row.get("updated_at"),
        updated_by=row.get("updated_by"),
    )


async def list_quota_config() -> List[QuotaConfig]:
    """모든 등급의 한도 설정 (Free → Pro → Pro+ → Pro Max 순)."""
    rows = await neo4j_client.run_cypher(_LIST_QUOTA_CYPHER)
    return [_row_to_config(r) for r in rows if r.get("tier")]


async def get_quota_config(tier: str) -> Optional[QuotaConfig]:
    """등급 1개의 한도 설정. 미존재면 None."""
    if tier not in SUBSCRIPTION_TYPES:
        return None
    rows = await neo4j_client.run_cypher(_GET_QUOTA_CYPHER, {"tier": tier})
    if not rows:
        return None
    return _row_to_config(rows[0])


async def update_quota_config(
    tier: str,
    meeting_logs: int,
    summary_chars: int,
    total_tokens: int,
    library_skills: int,
    max_projects: int,
    updated_by: str,
    lite_daily_cap: int = 0,
) -> Optional[QuotaConfig]:
    """한도 수정. 라우트가 audit_repository 에 별도 기록."""
    if tier not in SUBSCRIPTION_TYPES:
        return None

    # 입력 검증 — 음수 차단, 상한은 라우트 (pydantic) 가 검사.
    payload = {
        "tier": tier,
        "meeting_logs": max(0, int(meeting_logs)),
        "summary_chars": max(0, int(summary_chars)),
        "total_tokens": max(0, int(total_tokens)),
        "library_skills": max(0, int(library_skills)),
        "max_projects": max(0, int(max_projects)),
        "lite_daily_cap": max(0, int(lite_daily_cap)),
        "updated_by": updated_by or "UNKNOWN",
    }
    rows = await neo4j_client.run_cypher(_UPDATE_QUOTA_CYPHER, payload)
    if not rows:
        return None
    return _row_to_config(rows[0])
