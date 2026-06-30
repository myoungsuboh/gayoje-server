"""
Meeting Upload Repository — 사용자 단위의 "직접 업로드한 미팅 로그 원본 파일" 히스토리.

[배경]
plan 페이지의 `샘플 미팅 로그 순차 처리` 패널에서 사용자가 직접 .txt 파일을
업로드해 배치 처리할 수 있다. 지금까지는 업로드 즉시 메모리에서 파싱 후 폐기 →
새로고침/재로그인하면 사라짐. 사용자가 "내가 업로드했던 원본 로그를 다시 꺼내
쓰고 싶다" 는 요구가 있어 사용자별 히스토리로 영속화.

[모델]
(:User {email})-[:UPLOADED_MEETING_LOG {uploaded_at}]->(:MeetingUpload {
    id,             # randomUUID (path param 으로 사용)
    user_email,     # 소유자 식별 (per-user 노드)
    filename,       # 업로드 시 원본 파일명 (표시용)
    content,        # 본문 텍스트 (text/plain — 최대 MAX_CONTENT_BYTES)
    size,           # len(content.encode('utf-8'))
    uploaded_at,    # int ms unix timestamp
})

per-user 노드 — 동일 파일을 다른 사람이 올려도 분리. 본문 자체가 user-private.

[보안]
- 본문이 노드에 직접 저장되므로 크기 상한 강제 (DoS / Neo4j 메모리 보호).
- 모든 mutation/read 는 호출자가 current_user.email 통과 필수.
- Cypher 는 전부 $param 바인딩.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.clients import neo4j_client

logger = logging.getLogger(__name__)


# 본문 크기 상한 — 일반 미팅 로그가 수십 KB 이므로 1MB 면 충분.
# Neo4j 노드 property 자체에 강제 제한은 없지만, 그래프 메모리 보호용 가드.
MAX_CONTENT_BYTES = 1_048_576  # 1 MiB


# ===== 도메인 모델 =====


class MeetingUploadInput(BaseModel):
    """업로드 등록 요청 body."""

    filename: str = Field(..., min_length=1, max_length=512)
    content: str = Field(..., min_length=1)


class MeetingUploadMeta(BaseModel):
    """목록 응답용 메타 (본문 제외 — payload 크기 절약)."""

    id: str
    filename: str
    size: int
    uploaded_at: Optional[int] = None


class MeetingUploadDetail(MeetingUploadMeta):
    """단건 조회용 — 본문 포함."""

    content: str


# ===== Cypher =====


_ADD_UPLOAD_CYPHER = """\
MATCH (u:User {email: $email})
CREATE (m:MeetingUpload {
    id: randomUUID(),
    user_email: $email,
    filename: $filename,
    content: $content,
    size: $size,
    uploaded_at: $now
})
CREATE (u)-[:UPLOADED_MEETING_LOG {uploaded_at: $now}]->(m)
RETURN m {
    .id, .filename, .size, .uploaded_at
} AS upload
"""


_LIST_UPLOADS_CYPHER = """\
MATCH (u:User {email: $email})-[:UPLOADED_MEETING_LOG]->(m:MeetingUpload)
WHERE m.user_email = $email
RETURN m {
    .id, .filename, .size, .uploaded_at
} AS upload
ORDER BY m.uploaded_at DESC
LIMIT $limit
"""


_GET_UPLOAD_CYPHER = """\
MATCH (u:User {email: $email})-[:UPLOADED_MEETING_LOG]->(m:MeetingUpload {id: $id})
WHERE m.user_email = $email
RETURN m {
    .id, .filename, .content, .size, .uploaded_at
} AS upload
LIMIT 1
"""


_DELETE_UPLOAD_CYPHER = """\
// 소유 확인 후 노드 + 관계 삭제.
MATCH (u:User {email: $email})-[:UPLOADED_MEETING_LOG]->(m:MeetingUpload {id: $id})
WHERE m.user_email = $email
WITH m, m.id AS deleted_id
DETACH DELETE m
RETURN deleted_id AS deleted_id
"""


# ===== Helpers =====


def _first(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return records[0] if records else None


def _to_meta(row: Dict[str, Any]) -> Optional[MeetingUploadMeta]:
    r = row.get("upload") or {}
    if not r.get("id"):
        return None
    return MeetingUploadMeta(
        id=r["id"],
        filename=r.get("filename") or "",
        size=int(r.get("size") or 0),
        uploaded_at=int(r["uploaded_at"]) if r.get("uploaded_at") is not None else None,
    )


def _to_detail(row: Dict[str, Any]) -> Optional[MeetingUploadDetail]:
    r = row.get("upload") or {}
    if not r.get("id"):
        return None
    return MeetingUploadDetail(
        id=r["id"],
        filename=r.get("filename") or "",
        content=r.get("content") or "",
        size=int(r.get("size") or 0),
        uploaded_at=int(r["uploaded_at"]) if r.get("uploaded_at") is not None else None,
    )


# ===== CRUD =====


async def add_upload(
    email: str, payload: MeetingUploadInput
) -> MeetingUploadMeta:
    """
    업로드 등록. 본문 자체를 노드에 저장.

    Raises:
      ValueError: 본문 크기 초과 (MAX_CONTENT_BYTES) — 호출자가 413/422 매핑
      RuntimeError: User 노드를 찾지 못한 경우
    """
    if not email:
        raise ValueError("email 이 비어 있습니다.")

    size = len(payload.content.encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise ValueError(
            f"본문 크기가 한도를 초과했습니다 ({size} bytes > {MAX_CONTENT_BYTES})."
        )

    now = int(time.time() * 1000)

    records = await neo4j_client.run_cypher(
        _ADD_UPLOAD_CYPHER,
        {
            "email": email,
            "filename": payload.filename,
            "content": payload.content,
            "size": size,
            "now": now,
        },
    )
    row = _first(records)
    if row is None:
        raise RuntimeError("User 노드를 찾을 수 없습니다.")
    meta = _to_meta(row)
    if meta is None:
        raise RuntimeError("업로드 저장 결과가 비어 있습니다.")
    return meta


async def list_uploads(email: str, limit: int = 50) -> List[MeetingUploadMeta]:
    """
    내 업로드 메타 목록 (최근순). 본문은 제외 → 페이지 진입 시 가볍게.
    """
    if not email:
        return []
    records = await neo4j_client.run_cypher(
        _LIST_UPLOADS_CYPHER, {"email": email, "limit": int(limit)}
    )
    result: List[MeetingUploadMeta] = []
    for row in records:
        meta = _to_meta(row)
        if meta is not None:
            result.append(meta)
    return result


async def get_upload(email: str, upload_id: str) -> Optional[MeetingUploadDetail]:
    """
    단건 본문 조회. 소유자가 아니면 None (호출자가 404 매핑).
    """
    if not email or not upload_id:
        return None
    records = await neo4j_client.run_cypher(
        _GET_UPLOAD_CYPHER, {"email": email, "id": upload_id}
    )
    row = _first(records)
    if row is None:
        return None
    return _to_detail(row)


async def delete_upload(email: str, upload_id: str) -> bool:
    """
    소유자의 업로드 삭제. 다른 사용자의 동일 id 노드는 영향 없음 (per-user).
    """
    if not email or not upload_id:
        return False
    records = await neo4j_client.run_cypher(
        _DELETE_UPLOAD_CYPHER, {"email": email, "id": upload_id}
    )
    row = _first(records)
    return bool(row and row.get("deleted_id"))
