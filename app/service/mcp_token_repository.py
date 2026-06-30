"""
MCP Token Repository — Neo4j (:McpToken) 노드 CRUD.

[책임]
- 사용자(:User)-[:OWNS]->(:McpToken) 그래프 유지
- 발급/조회/회수 cypher 캡슐화
- jti 는 unique constraint — 재발급 충돌 방지

[설계 메모]
- 평문 토큰은 절대 저장하지 않음 — jti + 메타데이터만.
- revoked=true 노드도 즉시 삭제하지 않고 보존 → 감사 로그 활용.
- 만료된 토큰 청소는 follow-up cron PR.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel

from app.clients import neo4j_client

logger = logging.getLogger(__name__)

# 사용자당 최대 활성 (revoked=false AND 미만료) 토큰 수
MAX_ACTIVE_TOKENS_PER_USER = 10


class McpTokenRow(BaseModel):
    """McpToken 노드 1행 — 외부 응답에 사용 (평문 토큰 없음)."""
    jti: str
    label: str
    created_at: str
    last_used_at: Optional[str] = None
    expires_at: str
    revoked: bool


_ENSURE_MCP_TOKEN_CONSTRAINT_CYPHER = """\
CREATE CONSTRAINT mcp_token_jti_unique IF NOT EXISTS
FOR (t:McpToken) REQUIRE t.jti IS UNIQUE
"""


async def ensure_constraints() -> None:
    """앱 부팅 시 1회 호출 — McpToken.jti UNIQUE 제약 보장.
    실패해도 부팅 막지 않음 (Neo4j 미연결 환경, e.g. 일부 테스트).
    """
    try:
        await neo4j_client.run_cypher(_ENSURE_MCP_TOKEN_CONSTRAINT_CYPHER)
        logger.info("mcp_token_repository: 제약 ensure 완료")
    except Exception as e:  # noqa: BLE001 — 부팅 가드
        logger.warning("mcp_token_repository: ensure_constraints 실패: %s", e)


class McpTokenLimitExceeded(Exception):
    """사용자가 MAX_ACTIVE_TOKENS_PER_USER 초과로 발급 시도."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_at_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


_COUNT_ACTIVE_CYPHER = """\
MATCH (u:User {email: $email})-[:OWNS]->(t:McpToken)
WHERE t.revoked = false AND datetime(t.expires_at) > datetime()
RETURN count(t) AS active
"""

_CREATE_TOKEN_CYPHER = """\
MATCH (u:User {email: $email})
CREATE (u)-[:OWNS]->(t:McpToken {
    jti: $jti,
    label: $label,
    created_at: $created_at,
    last_used_at: null,
    expires_at: $expires_at,
    revoked: false
})
RETURN {
    jti: t.jti,
    label: t.label,
    created_at: t.created_at,
    last_used_at: t.last_used_at,
    expires_at: t.expires_at,
    revoked: t.revoked
} AS row
"""


async def create_mcp_token_record(
    *, email: str, jti: str, label: str, exp_days: int,
) -> McpTokenRow:
    """McpToken 노드 생성. 한도 초과 시 McpTokenLimitExceeded."""
    count_rows = await neo4j_client.run_cypher(
        _COUNT_ACTIVE_CYPHER, {"email": email}
    )
    active = (count_rows[0] or {}).get("active", 0) if count_rows else 0
    if active >= MAX_ACTIVE_TOKENS_PER_USER:
        raise McpTokenLimitExceeded(
            f"최대 {MAX_ACTIVE_TOKENS_PER_USER}개까지 발급 가능합니다."
        )

    rows = await neo4j_client.run_cypher(
        _CREATE_TOKEN_CYPHER,
        {
            "email": email,
            "jti": jti,
            "label": label.strip()[:80],
            "created_at": _utc_now_iso(),
            "expires_at": _expires_at_iso(exp_days),
        },
    )
    if not rows:
        raise RuntimeError("MCP 토큰 노드 생성 실패")
    return McpTokenRow(**rows[0]["row"])


_LIST_TOKENS_CYPHER = """\
MATCH (u:User {email: $email})-[:OWNS]->(t:McpToken)
RETURN {
    jti: t.jti,
    label: t.label,
    created_at: t.created_at,
    last_used_at: t.last_used_at,
    expires_at: t.expires_at,
    revoked: t.revoked
} AS row
ORDER BY t.created_at DESC
"""


async def list_tokens_for_user(email: str) -> list[McpTokenRow]:
    rows = await neo4j_client.run_cypher(
        _LIST_TOKENS_CYPHER, {"email": email}
    )
    return [McpTokenRow(**r["row"]) for r in rows]


_PEEK_REVOKE_TARGET_CYPHER = """\
MATCH (u:User {email: $email})-[:OWNS]->(t:McpToken {jti: $jti})
WHERE t.revoked = false
RETURN t.expires_at AS expires_at
"""

_MARK_REVOKED_CYPHER = """\
MATCH (u:User {email: $email})-[:OWNS]->(t:McpToken {jti: $jti})
WHERE t.revoked = false
SET t.revoked = true
RETURN t.jti AS jti
"""


async def peek_revoke_target(email: str, jti: str) -> Optional[int]:
    """회수 가능 여부 + exp epoch 만 조회. **state 변경 없음.**

    Returns:
        활성 토큰이고 호출자 소유면 exp epoch, 아니면 None.
    """
    rows = await neo4j_client.run_cypher(
        _PEEK_REVOKE_TARGET_CYPHER, {"email": email, "jti": jti}
    )
    if not rows:
        return None
    exp_iso = rows[0]["expires_at"]
    # Python isoformat 은 +00:00 형태 / Neo4j datetime() 는 다양 — fromisoformat 으로 안전 파싱
    return int(datetime.fromisoformat(exp_iso.replace("Z", "+00:00")).timestamp())


async def mark_token_revoked(email: str, jti: str) -> bool:
    """Neo4j 노드의 revoked=true 마킹. 호출자 소유 + 활성 토큰일 때만."""
    rows = await neo4j_client.run_cypher(
        _MARK_REVOKED_CYPHER, {"email": email, "jti": jti}
    )
    return bool(rows)


_TOUCH_LAST_USED_CYPHER = """\
MATCH (t:McpToken {jti: $jti})
SET t.last_used_at = $now
"""


async def touch_last_used(jti: str) -> None:
    """MCP 호출 직후 best-effort 업데이트. 실패해도 무시.

    노드 미존재 (이미 회수/삭제) 도 silent — 인증은 미들웨어 단계에서 별도 처리.
    """
    try:
        await neo4j_client.run_cypher(
            _TOUCH_LAST_USED_CYPHER, {"jti": jti, "now": _utc_now_iso()}
        )
    except Exception:  # noqa: BLE001
        logger.debug("touch_last_used failed (best-effort)", exc_info=True)


_IS_DURABLY_REVOKED_CYPHER = """\
MATCH (t:McpToken {jti: $jti})
WHERE t.revoked = true
RETURN t.jti AS jti
"""


async def is_durably_revoked(jti: str) -> bool:
    """Neo4j McpToken.revoked=true 면 True — 회수의 durable source-of-truth.

    [왜 필요한가]
    인증 미들웨어의 1차 회수 검사는 Redis jti 블랙리스트인데, 이는 fail-open
    (Redis 미가용/evict 시 통과) 이다. MCP 토큰은 90일 장수명이라 그 창이 크다.
    이 함수가 Neo4j 의 durable `revoked` 플래그를 backstop 으로 제공해, Redis 가
    죽거나 키가 사라져도 명시적으로 회수된 토큰을 차단한다.

    [정책]
    - 노드 미존재(레거시 토큰 등) → False (관용). JWT 서명+exp 가 1차 보안선이므로
      여기서 막진 않는다 — 이 함수의 책임은 '명시 회수' 차단 한 가지.
    - Neo4j 미가용 등 예외 → False (best-effort). 미들웨어는 어차피 직후
      user 조회로 Neo4j 를 다시 치므로, 그 단계가 hard gate 역할을 한다.
    """
    try:
        rows = await neo4j_client.run_cypher(
            _IS_DURABLY_REVOKED_CYPHER, {"jti": jti}
        )
        return bool(rows)
    except Exception:  # noqa: BLE001
        logger.debug("is_durably_revoked check failed (best-effort)", exc_info=True)
        return False
