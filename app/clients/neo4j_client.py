"""
Neo4j async client (lazy singleton).

ExecuteQuery 패턴(쿼리 한 덩어리 실행)을 1:1 제공하는 얇은 래퍼.
복잡한 트랜잭션 패턴은 일부러 노출하지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import ServiceUnavailable

logger = logging.getLogger(__name__)

_driver: Optional[AsyncDriver] = None

# [2026-05-27] run_cypher transient 재시도 — 운영 ServiceUnavailable(연결 defunct,
# Neo4j 재시작·유휴 연결 끊김·네트워크 blip)이 일시적일 때 사용자 Network Error 로
# 노출되지 않도록 새 세션(=새 연결)으로 재시도. ServiceUnavailable 은 서버 도달
# 실패=쿼리 미실행이라 write 쿼리여도 재시도가 멱등적으로 안전.
_RUN_CYPHER_MAX_ATTEMPTS = 3
_RUN_CYPHER_RETRY_BASE_DELAY = 0.5


def _get_uri() -> str:
    uri = os.getenv("NEO4J_URI")
    if not uri:
        raise RuntimeError("NEO4J_URI is not set")
    return uri


def _get_auth() -> tuple[str, str]:
    user = os.getenv("NEO4J_USERNAME", "neo4j")
    pw = os.getenv("NEO4J_PASSWORD")
    if not pw:
        raise RuntimeError("NEO4J_PASSWORD is not set")
    return user, pw


async def get_driver() -> AsyncDriver:
    """프로세스 수명 동안 단일 driver를 재사용. lifespan에서 close 호출."""
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(_get_uri(), auth=_get_auth())
    return _driver


async def close_driver() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def run_cypher(
    cypher: str,
    params: Optional[Dict[str, Any]] = None,
    database: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    하나의 Cypher 쿼리(여러 절을 `;` 없이 이어 붙인 것 포함)를 실행하고
    모든 레코드를 dict 리스트로 반환.

    주의:
      - Save Meeting Log 단계가 생성하는 쿼리는 multi-statement 가 아니라
        WITH 로 이어진 단일 statement 이므로 그대로 동작.
      - 정말 multi-statement(;로 분리) 가 필요하면 호출자가 분리해서 여러 번 호출해야 함.
      - 인접한 write 들을 atomic 으로 묶고 싶으면 `run_in_transaction` 사용.
    """
    driver = await get_driver()
    db = database or os.getenv("NEO4J_DATABASE", "neo4j")
    last_err: Optional[ServiceUnavailable] = None
    for attempt in range(_RUN_CYPHER_MAX_ATTEMPTS):
        try:
            async with driver.session(database=db) as session:
                result = await session.run(cypher, params or {})
                records: List[Dict[str, Any]] = []
                async for r in result:
                    records.append(dict(r))
                return records
        except ServiceUnavailable as e:
            # 연결 실패(서버 도달 못 함/유휴 끊김) — 쿼리 미실행이므로 새 세션으로
            # 재시도해도 멱등. 마지막 시도까지 실패하면 호출자에게 raise.
            last_err = e
            if attempt < _RUN_CYPHER_MAX_ATTEMPTS - 1:
                logger.warning(
                    "run_cypher transient error (attempt %d/%d), retrying: %s",
                    attempt + 1, _RUN_CYPHER_MAX_ATTEMPTS, e,
                )
                await asyncio.sleep(_RUN_CYPHER_RETRY_BASE_DELAY * (attempt + 1))
                continue
            logger.error(
                "run_cypher failed after %d attempts: %s",
                _RUN_CYPHER_MAX_ATTEMPTS, e,
            )
            raise
    # 도달 불가 — 루프는 return 하거나 raise 함. 방어적으로.
    raise last_err if last_err else RuntimeError("run_cypher: unreachable")


async def run_in_transaction(
    operations: List[tuple[str, Dict[str, Any]]],
    database: Optional[str] = None,
) -> List[List[Dict[str, Any]]]:
    """
    여러 Cypher 작업을 **단일 트랜잭션** 안에서 atomic 실행 — 실패 시 자동 롤백.

    [용도 — 2026-05 도입]
    파이프라인의 인접 write 들을 묶어 부분 commit 회피.
    예: cps_pipeline 의 Save CPS + Save Meeting Log 가 한 트랜잭션 →
        Save CPS 성공 후 Save Meeting Log 실패해도 둘 다 롤백 → "orphan
        Document 노드" 없음.

    [주의 — long-running 회피]
    LLM 호출 사이에 트랜잭션을 들고 있으면 트랜잭션 lock 30~120초 hold →
    동시 사용자에 영향. LLM 호출 전에 트랜잭션 commit / close 권장.
    이 함수는 짧은 인접 write 묶음에만 사용.

    Args:
        operations: [(cypher_string, params_dict), ...] 리스트. 순서대로 실행됨.
        database: 미지정 시 NEO4J_DATABASE env (default "neo4j").

    Returns:
        각 operation 의 records 리스트 — operations 와 동일 순서.

    Raises:
        neo4j.exceptions.* : 어느 한 operation 이 실패하면 전체 롤백 후 raise.
    """
    if not operations:
        return []

    driver = await get_driver()
    db = database or os.getenv("NEO4J_DATABASE", "neo4j")

    async def _unit_of_work(tx, ops):
        out: List[List[Dict[str, Any]]] = []
        for cypher, params in ops:
            result = await tx.run(cypher, params or {})
            rows: List[Dict[str, Any]] = []
            async for r in result:
                rows.append(dict(r))
            out.append(rows)
        return out

    async with driver.session(database=db) as session:
        # execute_write — 실패 시 자동 retry (transient 에러 시) + 예외는 그대로 raise.
        return await session.execute_write(_unit_of_work, operations)
