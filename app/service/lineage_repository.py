"""
LineageResult CRUD — analyzeLineage 결과 저장 + getLastLineage 조회.

[스테이지 매핑]
- Prepare Lineage Save + Save Lineage Neo4j → `save_lineage_result`
- getLastLineage / Get Last Lineage Neo4j / Format Lineage Response → `get_last_lineage`

[Cypher 호환성]
큰 result 는 base64 (`dataB64`) 로 인코딩해 저장. 기존 데이터 호환.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from app.core.project_scope import scoped_project

from pydantic import BaseModel

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


class LineageImpl(BaseModel):
    repoUrl: str
    role: Optional[str] = None
    filePath: str
    confidence: str  # high | medium | low
    reason: Optional[str] = None
    verified: bool = True


class LineageArtifact(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    endpoint: Optional[str] = None
    method: Optional[str] = None
    type: Optional[str] = None
    tech_stack: Optional[str] = None
    implementations: List[LineageImpl] = []


class LineageMissing(BaseModel):
    type: str
    id: str
    name: str
    reason: Optional[str] = None


class LineageDrift(BaseModel):
    """
    Drift item — 코드에 존재하는 spec 후보(controller/service/aggregate 등)인데
    PRD/DDD/Spack 에 매칭되는 명세가 없는 항목.

    "명세화되지 않은 코드" → PM/아키텍트가 review 해야 할 항목.
    """
    kind: str          # 'controller' | 'service' | 'repository' | 'aggregate' | 'event' | 'route'
    repoUrl: str
    role: Optional[str] = None
    filePath: str
    symbol: str        # 파일에서 추출한 이름 (예: 'OrderController')
    hint: Optional[str] = None  # 후보 이유 (예: 'filename matches *Controller')


class LineageStats(BaseModel):
    storiesCount: int = 0
    aggregatesCount: int = 0
    apisCount: int = 0
    servicesCount: int = 0
    totalImpls: int = 0
    verifiedImpls: int = 0
    unverifiedImpls: int = 0
    missingCount: int = 0
    driftCount: int = 0


class LineageResultData(BaseModel):
    summary: str = ""
    stories: List[LineageArtifact] = []
    aggregates: List[LineageArtifact] = []
    apis: List[LineageArtifact] = []
    services: List[LineageArtifact] = []
    missingImpl: List[LineageMissing] = []
    drifts: List[LineageDrift] = []
    stats: LineageStats = LineageStats()


class LineageResult(BaseModel):
    id: Optional[str] = None
    project: Optional[str] = None
    summary: str = ""
    storiesCount: int = 0
    aggregatesCount: int = 0
    apisCount: int = 0
    servicesCount: int = 0
    totalImpls: int = 0
    missingCount: int = 0
    driftCount: int = 0
    data: Optional[LineageResultData] = None
    saved_at: Optional[int] = None


_SAVE_LINEAGE_CYPHER = """\
MERGE (p:Project { name: $project })
CREATE (l:LineageResult {
    id: $id,
    project: $project,
    summary: $summary,
    storiesCount: $stories_count,
    aggregatesCount: $aggregates_count,
    apisCount: $apis_count,
    servicesCount: $services_count,
    totalImpls: $total_impls,
    missingCount: $missing_count,
    driftCount: $drift_count,
    dataB64: $data_b64,
    savedAt: $saved_at
})
MERGE (p)-[:HAS_LINEAGE]->(l)
RETURN l.id AS saved_id
"""


_GET_LAST_LINEAGE_CYPHER = """\
MATCH (l:LineageResult { project: $project })
RETURN l {
    .id, .summary, .storiesCount, .aggregatesCount, .apisCount, .servicesCount,
    .totalImpls, .missingCount, .driftCount, .dataB64, .savedAt
} AS lineage
ORDER BY l.savedAt DESC
LIMIT 1
"""


def _encode_data(data: LineageResultData) -> str:
    return base64.b64encode(
        json.dumps(data.model_dump(), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def _decode_data(b64: str) -> Optional[LineageResultData]:
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64).decode("utf-8")
        obj = json.loads(raw)
        return LineageResultData(**obj)
    except Exception as e:  # noqa: BLE001
        logger.warning("LineageResult dataB64 decode 실패: %s", e)
        return None


async def save_lineage_result(
    project: str, data: LineageResultData
) -> str:
    """Neo4j 저장 + 새 id 반환."""
    lineage_id = (
        f"lineage-{project}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    )
    await neo4j_client.run_cypher(
        _SAVE_LINEAGE_CYPHER,
        {
            "id": lineage_id,
            "project": project,
            "summary": data.summary,
            "stories_count": data.stats.storiesCount,
            "aggregates_count": data.stats.aggregatesCount,
            "apis_count": data.stats.apisCount,
            "services_count": data.stats.servicesCount,
            "total_impls": data.stats.totalImpls,
            "missing_count": data.stats.missingCount,
            "drift_count": data.stats.driftCount,
            "data_b64": _encode_data(data),
            "saved_at": int(time.time() * 1000),
        },
    )
    return lineage_id


async def get_last_lineage(project: str, team_id: str = "") -> Optional[LineageResult]:
    """가장 최근 LineageResult 조회. dataB64 decode."""
    project = scoped_project(project, team_id)
    records = await neo4j_client.run_cypher(
        _GET_LAST_LINEAGE_CYPHER, {"project": project}
    )
    if not records:
        return None
    row = records[0].get("lineage") or {}
    if not row.get("id"):
        return None
    data = _decode_data(row.get("dataB64") or "")
    return LineageResult(
        id=row.get("id"),
        project=project,
        summary=row.get("summary") or "",
        storiesCount=int(row.get("storiesCount") or 0),
        aggregatesCount=int(row.get("aggregatesCount") or 0),
        apisCount=int(row.get("apisCount") or 0),
        servicesCount=int(row.get("servicesCount") or 0),
        totalImpls=int(row.get("totalImpls") or 0),
        missingCount=int(row.get("missingCount") or 0),
        driftCount=int(row.get("driftCount") or 0),
        data=data,
        saved_at=int(row["savedAt"]) if row.get("savedAt") is not None else None,
    )


# ─── Lineage History ────────────────────────────────────────────
# 분석 이력 — 같은 LineageResult 노드들을 최신순 N 개. data 본문은
# `summary`/카운트만 노출하고 본문 (dataB64) 은 노출 안 함 (목록은 가볍게).


class LineageHistoryItem(BaseModel):
    id: str
    summary: str = ""
    storiesCount: int = 0
    aggregatesCount: int = 0
    apisCount: int = 0
    servicesCount: int = 0
    totalImpls: int = 0
    missingCount: int = 0
    driftCount: int = 0
    saved_at: int


_GET_LINEAGE_HISTORY_CYPHER = """\
MATCH (l:LineageResult { project: $project })
RETURN l {
    .id, .summary, .storiesCount, .aggregatesCount, .apisCount, .servicesCount,
    .totalImpls, .missingCount, .driftCount, .savedAt
} AS lineage
ORDER BY l.savedAt DESC
LIMIT $limit
"""


async def get_lineage_history(
    project: str, limit: int = 10, team_id: str = ""
) -> List[LineageHistoryItem]:
    """프로젝트의 LineageResult 이력 (최신순). dataB64 미포함 — 목록용 경량 응답."""
    project = scoped_project(project, team_id)
    records = await neo4j_client.run_cypher(
        _GET_LINEAGE_HISTORY_CYPHER, {"project": project, "limit": max(1, min(limit, 50))}
    )
    out: List[LineageHistoryItem] = []
    for rec in records:
        row = rec.get("lineage") or {}
        if not row.get("id"):
            continue
        out.append(
            LineageHistoryItem(
                id=row["id"],
                summary=row.get("summary") or "",
                storiesCount=int(row.get("storiesCount") or 0),
                aggregatesCount=int(row.get("aggregatesCount") or 0),
                apisCount=int(row.get("apisCount") or 0),
                servicesCount=int(row.get("servicesCount") or 0),
                totalImpls=int(row.get("totalImpls") or 0),
                missingCount=int(row.get("missingCount") or 0),
                driftCount=int(row.get("driftCount") or 0),
                saved_at=int(row.get("savedAt") or 0),
            )
        )
    return out


async def get_lineage_by_id(
    project: str, lineage_id: str, team_id: str = ""
) -> Optional[LineageResult]:
    """특정 id 의 LineageResult 조회 (history 항목 클릭 → 본문 fetch).

    project 도 함께 필터링 — 다른 프로젝트의 id 로 우회 조회 불가.
    """
    project = scoped_project(project, team_id)
    records = await neo4j_client.run_cypher(
        """
        MATCH (l:LineageResult { project: $project, id: $id })
        RETURN l {
            .id, .summary, .storiesCount, .aggregatesCount, .apisCount, .servicesCount,
            .totalImpls, .missingCount, .driftCount, .dataB64, .savedAt
        } AS lineage
        """,
        {"project": project, "id": lineage_id},
    )
    if not records:
        return None
    row = records[0].get("lineage") or {}
    if not row.get("id"):
        return None
    data = _decode_data(row.get("dataB64") or "")
    return LineageResult(
        id=row.get("id"),
        project=project,
        summary=row.get("summary") or "",
        storiesCount=int(row.get("storiesCount") or 0),
        aggregatesCount=int(row.get("aggregatesCount") or 0),
        apisCount=int(row.get("apisCount") or 0),
        servicesCount=int(row.get("servicesCount") or 0),
        totalImpls=int(row.get("totalImpls") or 0),
        missingCount=int(row.get("missingCount") or 0),
        driftCount=int(row.get("driftCount") or 0),
        data=data,
        saved_at=int(row["savedAt"]) if row.get("savedAt") is not None else None,
    )


# ─── Lineage Truth (정답 라벨) ──────────────────────────────────
# 사용자가 "이 PRD 항목의 실제 구현 파일은 이것들이다" 라고 라벨링한 정답.
# precision/recall 계산용 ground truth. 이전에는 클라이언트 localStorage 만 →
# 디바이스 의존, 팀 공유 불가, 시크릿 모드 손실. BE 로 일원화.


class LineageTruth(BaseModel):
    project: str
    itemType: str         # 'aggregate' | 'api' | 'service' | 'story' 등
    itemId: str
    expectedFiles: List[str] = []
    updatedAt: int


_UPSERT_TRUTH_CYPHER = """\
MERGE (t:LineageTruth {
    project: $project,
    itemType: $item_type,
    itemId: $item_id
})
SET
    t.expectedFiles = $expected_files,
    t.updatedAt = $updated_at
RETURN t.project AS project, t.itemType AS itemType, t.itemId AS itemId,
       t.expectedFiles AS expectedFiles, t.updatedAt AS updatedAt
"""


_LIST_TRUTH_CYPHER = """\
MATCH (t:LineageTruth { project: $project })
RETURN t.project AS project, t.itemType AS itemType, t.itemId AS itemId,
       t.expectedFiles AS expectedFiles, t.updatedAt AS updatedAt
ORDER BY t.itemType, t.itemId
"""


_LIST_TRUTH_BY_TYPE_CYPHER = """\
MATCH (t:LineageTruth { project: $project, itemType: $item_type })
RETURN t.project AS project, t.itemType AS itemType, t.itemId AS itemId,
       t.expectedFiles AS expectedFiles, t.updatedAt AS updatedAt
ORDER BY t.itemId
"""


_DELETE_TRUTH_CYPHER = """\
MATCH (t:LineageTruth {
    project: $project,
    itemType: $item_type,
    itemId: $item_id
})
DELETE t
RETURN count(*) AS deleted
"""


async def save_lineage_truth(
    project: str,
    item_type: str,
    item_id: str,
    expected_files: List[str],
    team_id: str = "",
) -> LineageTruth:
    """Upsert — 같은 (project, itemType, itemId) 면 expectedFiles 만 교체."""
    project = scoped_project(project, team_id)
    if not project or not item_type or not item_id:
        raise ValueError("project, item_type, item_id 모두 필수")
    files = [str(f) for f in (expected_files or [])]
    now_ms = int(time.time() * 1000)
    records = await neo4j_client.run_cypher(
        _UPSERT_TRUTH_CYPHER,
        {
            "project": project,
            "item_type": item_type,
            "item_id": item_id,
            "expected_files": files,
            "updated_at": now_ms,
        },
    )
    row = records[0] if records else {}
    return LineageTruth(
        project=row.get("project") or project,
        itemType=row.get("itemType") or item_type,
        itemId=row.get("itemId") or item_id,
        expectedFiles=list(row.get("expectedFiles") or files),
        updatedAt=int(row.get("updatedAt") or now_ms),
    )


async def list_lineage_truth(
    project: str, item_type: Optional[str] = None, team_id: str = ""
) -> List[LineageTruth]:
    """프로젝트의 모든 truth — 선택적 itemType 필터."""
    project = scoped_project(project, team_id)
    if item_type:
        records = await neo4j_client.run_cypher(
            _LIST_TRUTH_BY_TYPE_CYPHER,
            {"project": project, "item_type": item_type},
        )
    else:
        records = await neo4j_client.run_cypher(
            _LIST_TRUTH_CYPHER, {"project": project}
        )
    return [
        LineageTruth(
            project=r.get("project") or project,
            itemType=r.get("itemType") or "",
            itemId=r.get("itemId") or "",
            expectedFiles=list(r.get("expectedFiles") or []),
            updatedAt=int(r.get("updatedAt") or 0),
        )
        for r in records
        if r.get("itemType") and r.get("itemId")
    ]


async def delete_lineage_truth(
    project: str, item_type: str, item_id: str, team_id: str = ""
) -> bool:
    """삭제 — 존재했으면 True, 없었으면 False."""
    project = scoped_project(project, team_id)
    records = await neo4j_client.run_cypher(
        _DELETE_TRUTH_CYPHER,
        {"project": project, "item_type": item_type, "item_id": item_id},
    )
    return bool(records and int(records[0].get("deleted") or 0) > 0)


async def import_lineage_truth(
    project: str,
    items: List[Dict[str, Any]],
    override: bool = False,
    team_id: str = "",
) -> Dict[str, int]:
    """벌크 import (CSV/JSON 임포트 화면용).

    Args:
        items: [{itemType, itemId, expectedFiles[]}, ...]
        override: False 면 이미 존재하는 (itemType, itemId) skip.

    Returns:
        {"written": int, "skipped": int}
    """
    if not project:
        raise ValueError("project required")
    project = scoped_project(project, team_id)
    if not isinstance(items, list):
        return {"written": 0, "skipped": 0}

    written = 0
    skipped = 0

    # 미리 존재하는 키 목록 조회 — override=False 시 skip 판단.
    existing_keys = set()
    if not override:
        existing = await list_lineage_truth(project)
        existing_keys = {(t.itemType, t.itemId) for t in existing}

    for raw in items:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        item_type = raw.get("itemType")
        item_id = raw.get("itemId")
        if not item_type or not item_id:
            skipped += 1
            continue
        item_id = str(item_id)
        if not override and (item_type, item_id) in existing_keys:
            skipped += 1
            continue
        files = raw.get("expectedFiles")
        if not isinstance(files, list):
            files = []
        await save_lineage_truth(project, item_type, item_id, files)
        written += 1

    return {"written": written, "skipped": skipped}
