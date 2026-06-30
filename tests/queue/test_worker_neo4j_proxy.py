"""
ARQ 워커 ctx 에 주입되는 neo4j proxy 의 프로토콜 호환성.

[배경]
파이프라인(특히 design, delete) 들은 `ctx.neo4j.run_in_transaction(operations)` 로
인접 write 를 atomic 하게 묶는다. API 라우트는 `app.pipelines.base.Neo4jClientProxy`
를 주입하며 그 클래스는 `run_cypher` + `run_in_transaction` 둘 다 노출.

[과거 버그 — 2026-05-28]
`app/queue/jobs.py` 의 로컬 `_Neo4jProxy` 가 `run_cypher` 만 구현한 채 통합에서
누락 → arq 워커 경로로 design 업데이트 호출 시
`'_Neo4jProxy' object has no attribute 'run_in_transaction'` AttributeError 로
운영 폭발. 이 회귀가 재발하지 않도록 워커 proxy 가 protocol 을 완전히 만족하는지
명시 검증.
"""
from __future__ import annotations

from app.pipelines.base import Neo4jClientProxy
from app.queue.jobs import _Neo4jProxy


def test_worker_neo4j_proxy_exposes_run_in_transaction():
    """워커 ctx 에 주입되는 proxy 가 run_in_transaction 메서드를 노출."""
    proxy = _Neo4jProxy()
    assert hasattr(proxy, "run_in_transaction"), (
        "워커 _Neo4jProxy 가 run_in_transaction 없음 — design/delete 파이프라인의 "
        "atomic write 가 깨진다."
    )
    assert callable(proxy.run_in_transaction)


def test_worker_neo4j_proxy_exposes_run_cypher():
    """기본 run_cypher 도 보장 (기존 동작 회귀 차단)."""
    proxy = _Neo4jProxy()
    assert hasattr(proxy, "run_cypher")
    assert callable(proxy.run_cypher)


def test_worker_proxy_is_unified_base_class():
    """워커 proxy 는 base 의 Neo4jClientProxy 와 동일 — 중복 정의 재발 방지.

    base.py 주석: "9개 라우트 파일에 산재했던 _Neo4jProxy 중복을 흡수". 워커도 같은
    클래스를 써야 protocol drift 가 생기지 않는다.
    """
    assert _Neo4jProxy is Neo4jClientProxy
