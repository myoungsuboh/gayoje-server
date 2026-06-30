"""
domain_indexes.ensure_domain_indexes() 단위 테스트.

[검증]
- 각 핵심 도메인 라벨에 인덱스 CREATE 가 호출됨 (라벨/속성 화이트리스트)
- 실패한 statement 가 다른 statement 진행을 막지 않음 (개별 try/except)
- Neo4j 미연결 (모든 호출 raise) 환경에서도 부팅 안 막힘
- 모든 statement 가 `IF NOT EXISTS` 포함 (멱등성 회귀 방지)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import domain_indexes


pytestmark = pytest.mark.asyncio


class _Recorder:
    """run_cypher 호출 기록 + 미리 정의된 실패 인덱스 시뮬레이션."""

    def __init__(self, fail_indices: Optional[List[int]] = None):
        self.calls: List[str] = []
        self._fail = set(fail_indices or [])

    async def __call__(
        self, cypher: str, params: Optional[Dict[str, Any]] = None, database: Optional[str] = None
    ):
        idx = len(self.calls)
        self.calls.append(cypher)
        if idx in self._fail:
            raise RuntimeError("simulated Neo4j error")
        return []


async def test_all_expected_labels_get_indexed(monkeypatch):
    """모든 핵심 라벨에 CREATE INDEX 가 시도되어야 함."""
    rec = _Recorder()
    monkeypatch.setattr(
        "app.service.domain_indexes.neo4j_client.run_cypher", rec
    )
    await domain_indexes.ensure_domain_indexes()

    joined = " ".join(rec.calls)
    # 라벨 화이트리스트 — 빠짐 없이.
    for label in [
        "CPS_Document",
        "PRD_Document",
        "Skill",
        "Meeting_Log",
        "MeetingUpload",
        "Story",
        "LintResult",
        "LineageResult",
        "LineageTruth",
    ]:
        assert f":{label})" in joined or f":{label} " in joined, (
            f"index for {label} missing"
        )


async def test_all_statements_use_if_not_exists(monkeypatch):
    """멱등성 — 모든 CREATE INDEX 가 IF NOT EXISTS 포함."""
    rec = _Recorder()
    monkeypatch.setattr(
        "app.service.domain_indexes.neo4j_client.run_cypher", rec
    )
    await domain_indexes.ensure_domain_indexes()

    for stmt in rec.calls:
        assert "IF NOT EXISTS" in stmt, f"non-idempotent statement: {stmt[:80]}"


async def test_individual_failure_does_not_block_others(monkeypatch):
    """한 statement 가 실패해도 다음 statement 는 시도되어야 함."""
    rec = _Recorder(fail_indices=[0, 2])   # 첫 번째, 세 번째 인덱스 실패
    monkeypatch.setattr(
        "app.service.domain_indexes.neo4j_client.run_cypher", rec
    )
    # 예외 던지면 안 됨
    await domain_indexes.ensure_domain_indexes()
    # 모든 statement 가 시도되었어야 함
    assert len(rec.calls) == len(domain_indexes._INDEX_STATEMENTS)


async def test_complete_failure_does_not_raise(monkeypatch):
    """Neo4j 미연결 — 모든 statement 가 실패해도 함수는 정상 리턴."""
    all_fail = list(range(len(domain_indexes._INDEX_STATEMENTS)))
    rec = _Recorder(fail_indices=all_fail)
    monkeypatch.setattr(
        "app.service.domain_indexes.neo4j_client.run_cypher", rec
    )
    # 예외 던지면 안 됨 (부팅 보호)
    await domain_indexes.ensure_domain_indexes()


async def test_composite_project_id_indexes_present_for_node_edit_hot_path():
    """
    [회귀 — 2026-05] PATCH /api/v2/{cps,prd}/nodes/{id} 의 lookup hot-path
    (MATCH (n {id, project})) 를 위한 composite 인덱스 보장.
    Problem / Solution / Epic / Story 네 라벨.
    """
    cypher_blob = "\n".join(domain_indexes._INDEX_STATEMENTS)
    for label in ("Problem", "Solution", "Epic", "Story"):
        marker = f"FOR (n:{label}) ON (n.project, n.id)"
        assert marker in cypher_blob, (
            f"{label} composite (project, id) index missing — "
            f"PATCH /nodes/{{id}} 가 project 인덱스로 narrow 후 id in-memory 스캔"
        )


async def test_solution_label_used_not_resolution():
    """
    [회귀 — 2026-05] 코드베이스 label 통일: 실제 노드 라벨은 :Solution
    (cps_extract.md LLM 프롬프트 + cps_pipeline MERGE).
    domain_indexes 의 사용 안 되는 :Resolution 잔재 방지.
    """
    cypher_blob = "\n".join(domain_indexes._INDEX_STATEMENTS)
    assert "FOR (n:Resolution)" not in cypher_blob, (
        "Resolution 라벨 인덱스 잔재 — 실제 노드는 :Solution"
    )
    assert "FOR (n:Solution)" in cypher_blob, "Solution 라벨 인덱스 누락"


async def test_only_safe_property_keys_used():
    """
    CREATE INDEX ON (n.project) / (n.project, n.id) 같이 화이트리스트 속성만.
    composite 인덱스도 같은 검증 — 동적 보간 0.

    [2026-05] FULLTEXT INDEX (`ON EACH [n.name, n.description]`) 는 별도 화이트리스트.
    fulltext 는 의도적으로 text 검색용 — name/description 인덱싱이 정상.

    [2026-05-18] CREATE CONSTRAINT ... REQUIRE (...) IS UNIQUE 패턴도 인식.
    Meeting_Log 의 (project, version) UNIQUE 같은 중복 차단 제약.
    """
    import re

    # 일반(range/composite) 인덱스용 화이트리스트
    # [2026-05-18] Meeting_Log (project, version) composite 추가 → version 도 허용.
    safe_props = {"project", "user_email", "id", "version"}
    # fulltext 전용 화이트리스트 (text 검색 대상 속성)
    safe_fulltext_props = {"name", "description"}
    # CONSTRAINT 화이트리스트 — version 도 동시접속 차단용 합법 컬럼
    safe_constraint_props = safe_props | {"version"}

    # 일반 인덱스 패턴: `ON (alias.prop)` 또는 `ON (alias.prop1, alias.prop2)`
    range_pattern = re.compile(r"ON \(([a-z]\.\w+(?:, [a-z]\.\w+)*)\)")
    # fulltext 인덱스 패턴: `ON EACH [alias.prop1, alias.prop2]`
    fulltext_pattern = re.compile(r"ON EACH \[([a-z]\.\w+(?:, [a-z]\.\w+)*)\]")
    # constraint 패턴: `REQUIRE (alias.prop1, alias.prop2) IS UNIQUE`
    constraint_pattern = re.compile(
        r"REQUIRE \(([a-z]\.\w+(?:, [a-z]\.\w+)*)\)\s+IS\s+UNIQUE",
    )

    for stmt in domain_indexes._INDEX_STATEMENTS:
        is_fulltext = "FULLTEXT INDEX" in stmt
        is_constraint = "CREATE CONSTRAINT" in stmt
        if is_fulltext:
            m = fulltext_pattern.search(stmt)
            assert m, f"unexpected fulltext statement shape: {stmt}"
            allowed = safe_fulltext_props
        elif is_constraint:
            m = constraint_pattern.search(stmt)
            assert m, f"unexpected constraint statement shape: {stmt}"
            allowed = safe_constraint_props
        else:
            m = range_pattern.search(stmt)
            assert m, f"unexpected statement shape: {stmt}"
            allowed = safe_props

        for part in m.group(1).split(","):
            prop = part.strip().split(".")[1]
            assert prop in allowed, (
                f"unexpected property: {prop} — must be in {allowed} "
                f"(fulltext={is_fulltext})"
            )
