"""
InfraCost — admin 이 입력하는 월별 인프라 비용 (수익 대시보드 원가 계산용).

[배경]
LLM 토큰 원가는 자동 계산 가능 (total_tokens × unit_price). 그러나 Neo4j /
Vercel / Redis 등 인프라 고정 비용은 BE 에서 추적 불가 — admin 이 매월 직접 입력.

[스키마]
(:InfraCost {
    year: int,            # 2026
    month: int,           # 5 (1-12)
    amount_krw: int,      # 76000 — 한 달 인프라 총 비용 (items 있으면 그 합계)
    items_json: str,      # JSON 배열 "[{category, amount_krw, note}, ...]" — 항목별 분리
    note: str,            # 월 전체 메모 (선택)
    updated_at: datetime,
    updated_by: str       # admin email
})

[항목별 분리 — 2026-06]
서버 운영비 / LLM API / 지적재산 등록비 등 항목을 items 로 분리 입력. amount_krw 는
항목 합계로 유지되어 순이익 계산(= 매출 - LLM원가 - amount_krw)은 그대로 동작한다.
Neo4j 노드 property 는 map 배열을 못 담으므로 JSON 문자열(items_json)로 저장.
하위호환: 옛 데이터(items_json 없음)는 items=[] 로 읽히고 amount_krw(lump)만 사용.

[Composite key]
(year, month) UNIQUE — 같은 월 1개만. MERGE 로 upsert.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# ===== 도메인 모델 =====


@dataclass(frozen=True)
class InfraCost:
    year: int
    month: int
    amount_krw: int
    note: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "year": self.year,
            "month": self.month,
            "amount_krw": self.amount_krw,
            "note": self.note or "",
            "items": [dict(i) for i in (self.items or [])],
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


# ===== Cypher =====


_ENSURE_INFRA_COST_CONSTRAINT_CYPHER = """\
// (year, month) composite UNIQUE — Neo4j 5.x node key 제약.
CREATE CONSTRAINT infra_cost_year_month_key IF NOT EXISTS
FOR (c:InfraCost) REQUIRE (c.year, c.month) IS UNIQUE
"""


_UPSERT_INFRA_COST_CYPHER = """\
MERGE (c:InfraCost {year: $year, month: $month})
SET c.amount_krw = $amount_krw,
    c.items_json = $items_json,
    c.note = $note,
    c.updated_at = datetime(),
    c.updated_by = $updated_by
RETURN c.year AS year,
       c.month AS month,
       c.amount_krw AS amount_krw,
       c.items_json AS items_json,
       c.note AS note,
       toString(c.updated_at) AS updated_at,
       c.updated_by AS updated_by
"""


_GET_INFRA_COST_CYPHER = """\
MATCH (c:InfraCost {year: $year, month: $month})
RETURN c.year AS year,
       c.month AS month,
       c.amount_krw AS amount_krw,
       c.items_json AS items_json,
       c.note AS note,
       toString(c.updated_at) AS updated_at,
       c.updated_by AS updated_by
"""


_LIST_INFRA_COST_BY_YEAR_CYPHER = """\
MATCH (c:InfraCost {year: $year})
RETURN c.year AS year,
       c.month AS month,
       c.amount_krw AS amount_krw,
       c.items_json AS items_json,
       c.note AS note,
       toString(c.updated_at) AS updated_at,
       c.updated_by AS updated_by
ORDER BY c.month ASC
"""


# ===== 부팅 헬퍼 =====


async def ensure_infra_cost_constraint() -> None:
    """(year, month) UNIQUE 제약 ensure. Neo4j 미연결 시 warning."""
    try:
        await neo4j_client.run_cypher(_ENSURE_INFRA_COST_CONSTRAINT_CYPHER)
        logger.info("infra_cost: (year, month) UNIQUE 제약 ensure 완료")
    except Exception as e:  # noqa: BLE001
        logger.warning("infra_cost: UNIQUE 제약 실패 (%s)", e)


# ===== 항목(item) 정규화 =====

# 입력 항목 화이트리스트 정규화. 한 항목 = {category, amount_krw, note, fixed}.
# fixed=매월 반복 고정비 표식(서버 운영비·AI 구독 등). 빈 항목(카테고리·금액·메모 모두
# 없음)은 버림. 금액은 음수 방지. 합계도 함께 반환.
def _normalize_items(items: Optional[List[Dict[str, Any]]]) -> Tuple[List[Dict[str, Any]], int]:
    norm: List[Dict[str, Any]] = []
    total = 0
    for it in items or []:
        if not isinstance(it, dict):
            continue
        category = str(it.get("category") or "").strip()[:60]
        amount = max(0, int(it.get("amount_krw") or 0))
        note = str(it.get("note") or "").strip()[:200]
        fixed = bool(it.get("fixed"))
        if not category and amount == 0 and not note:
            continue
        norm.append({"category": category or "기타", "amount_krw": amount, "note": note, "fixed": fixed})
        total += amount
    return norm, total


# ===== 함수 =====


def _row_to_cost(row: dict) -> InfraCost:
    items: List[Dict[str, Any]] = []
    raw = row.get("items_json")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                items = [i for i in parsed if isinstance(i, dict)]
        except (ValueError, TypeError):
            items = []
    return InfraCost(
        year=int(row.get("year") or 0),
        month=int(row.get("month") or 0),
        amount_krw=int(row.get("amount_krw") or 0),
        note=row.get("note") or "",
        items=items,
        updated_at=row.get("updated_at"),
        updated_by=row.get("updated_by"),
    )


async def get_infra_cost(year: int, month: int) -> Optional[InfraCost]:
    """해당 월의 인프라 비용. 미설정이면 None."""
    if not (1 <= month <= 12):
        return None
    rows = await neo4j_client.run_cypher(
        _GET_INFRA_COST_CYPHER, {"year": int(year), "month": int(month)}
    )
    if not rows:
        return None
    return _row_to_cost(rows[0])


async def upsert_infra_cost(
    year: int,
    month: int,
    amount_krw: int,
    note: str,
    updated_by: str,
    items: Optional[List[Dict[str, Any]]] = None,
) -> Optional[InfraCost]:
    """월별 인프라 비용 upsert. (year, month) UNIQUE 라 MERGE 패턴.

    items 가 있으면 amount_krw 는 항목 합계로 강제(순이익 계산 일관성). items 가
    없으면 전달된 amount_krw(lump)를 그대로 사용 — 옛 호출/단일금액 입력 호환.

    호출자(admin route) 가 audit_repository 에 별도 기록.
    """
    if not (1 <= month <= 12):
        return None
    if year < 2020 or year > 2100:
        return None
    norm_items, items_total = _normalize_items(items)
    final_amount = items_total if norm_items else max(0, int(amount_krw))
    rows = await neo4j_client.run_cypher(
        _UPSERT_INFRA_COST_CYPHER,
        {
            "year": int(year),
            "month": int(month),
            "amount_krw": final_amount,
            "items_json": json.dumps(norm_items, ensure_ascii=False),
            "note": (note or "").strip()[:500],
            "updated_by": updated_by or "UNKNOWN",
        },
    )
    if not rows:
        return None
    return _row_to_cost(rows[0])


async def list_infra_cost_by_year(year: int) -> List[InfraCost]:
    """연간 12개월의 인프라 비용 (설정된 월만)."""
    rows = await neo4j_client.run_cypher(
        _LIST_INFRA_COST_BY_YEAR_CYPHER, {"year": int(year)}
    )
    return [_row_to_cost(r) for r in rows]


# ===== 인프라 비용 default =====


_DEFAULT_INFRA_COST_KRW = 80_000  # admin 미입력 시 fallback (운영 추정치)


def default_infra_cost_for_month() -> int:
    """admin 이 미입력한 월의 fallback 추정치 (월 단위)."""
    return _DEFAULT_INFRA_COST_KRW


def current_year_month() -> tuple[int, int]:
    """현재 시점 (year, month) — admin route 의 default param."""
    now = datetime.utcnow()
    return now.year, now.month
