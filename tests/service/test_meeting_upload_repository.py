"""
meeting_upload_repository 단위 테스트.

neo4j_client.run_cypher 를 monkeypatch 로 fake 응답 주입.
- 크기 가드 (MAX_CONTENT_BYTES) 회귀 테스트
- 소유자 격리 (다른 사용자 id 로 조회/삭제 시 None / False)
- Cypher 파라미터 바인딩 검증 (Cypher injection 방어)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import meeting_upload_repository as repo
from app.service.meeting_upload_repository import (
    MAX_CONTENT_BYTES,
    MeetingUploadInput,
)


pytestmark = pytest.mark.asyncio


# ─── Fake neo4j_client.run_cypher ───────────────────────────────


class _FakeRunCypher:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(
        self,
        cypher: str,
        params: Optional[Dict[str, Any]] = None,
        database: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self.calls.append(
            {"cypher": cypher, "params": params or {}, "database": database}
        )
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses: Optional[List[List[Dict[str, Any]]]] = None) -> _FakeRunCypher:
        fake = _FakeRunCypher(responses=responses)
        monkeypatch.setattr(
            "app.service.meeting_upload_repository.neo4j_client.run_cypher", fake
        )
        return fake

    return _setup


def _upload_row(
    upload_id: str = "uid-1",
    filename: str = "log.txt",
    size: int = 12,
    uploaded_at: int = 1_700_000_000_000,
    content: Optional[str] = None,
):
    upload: Dict[str, Any] = {
        "id": upload_id,
        "filename": filename,
        "size": size,
        "uploaded_at": uploaded_at,
    }
    if content is not None:
        upload["content"] = content
    return {"upload": upload}


# ─── add_upload ──────────────────────────────────────────────────


async def test_add_upload_returns_meta_without_content(fake_run):
    fake = fake_run([[_upload_row(filename="meeting.txt", size=11)]])
    result = await repo.add_upload(
        "a@b.com", MeetingUploadInput(filename="meeting.txt", content="hello world")
    )
    assert result.id == "uid-1"
    assert result.filename == "meeting.txt"
    assert result.size == 11
    assert result.uploaded_at == 1_700_000_000_000

    # Cypher 파라미터 바인딩 확인
    call = fake.calls[0]
    assert call["params"]["email"] == "a@b.com"
    assert call["params"]["filename"] == "meeting.txt"
    assert call["params"]["content"] == "hello world"
    assert call["params"]["size"] == len("hello world".encode("utf-8"))


async def test_add_upload_rejects_empty_email():
    with pytest.raises(ValueError, match="email"):
        await repo.add_upload("", MeetingUploadInput(filename="x.txt", content="x"))


async def test_add_upload_rejects_oversize_content():
    # MAX_CONTENT_BYTES + 1 — utf-8 ASCII 이므로 byte 수 == char 수
    huge = "a" * (MAX_CONTENT_BYTES + 1)
    with pytest.raises(ValueError, match="크기"):
        await repo.add_upload(
            "a@b.com", MeetingUploadInput(filename="big.txt", content=huge)
        )


async def test_add_upload_accepts_exact_max_size(fake_run):
    fake_run(
        [
            [
                _upload_row(
                    filename="max.txt",
                    size=MAX_CONTENT_BYTES,
                )
            ]
        ]
    )
    exact = "a" * MAX_CONTENT_BYTES
    result = await repo.add_upload(
        "a@b.com", MeetingUploadInput(filename="max.txt", content=exact)
    )
    assert result.size == MAX_CONTENT_BYTES


async def test_add_upload_counts_bytes_not_chars(fake_run):
    """크기 가드는 UTF-8 byte 기준 — 한글 1자 = 3 bytes."""
    # 'ㄱ' 한 자 = 3 bytes. MAX/3 + 1 글자면 byte 로는 초과.
    over_chars = "ㄱ" * (MAX_CONTENT_BYTES // 3 + 1)
    assert len(over_chars.encode("utf-8")) > MAX_CONTENT_BYTES
    with pytest.raises(ValueError):
        await repo.add_upload(
            "a@b.com", MeetingUploadInput(filename="ko.txt", content=over_chars)
        )


async def test_add_upload_raises_if_user_missing(fake_run):
    fake_run([[]])  # User 노드 없음 → 빈 결과
    with pytest.raises(RuntimeError, match="User"):
        await repo.add_upload(
            "ghost@b.com", MeetingUploadInput(filename="x.txt", content="x")
        )


# ─── list_uploads ────────────────────────────────────────────────


async def test_list_uploads_returns_metas_in_order(fake_run):
    fake_run(
        [
            [
                _upload_row(upload_id="u3", filename="c.txt", uploaded_at=300),
                _upload_row(upload_id="u2", filename="b.txt", uploaded_at=200),
                _upload_row(upload_id="u1", filename="a.txt", uploaded_at=100),
            ]
        ]
    )
    result = await repo.list_uploads("a@b.com")
    assert [u.id for u in result] == ["u3", "u2", "u1"]
    # 본문 필드는 메타 모델에 없음 — 직렬화 시 빠짐
    assert all(not hasattr(u, "content") or getattr(u, "content", None) is None for u in result)


async def test_list_uploads_empty_email_returns_empty():
    assert await repo.list_uploads("") == []


async def test_list_uploads_skips_invalid_rows(fake_run):
    """id 없는 row 는 skip — Neo4j 가 부분 결과를 줄 수도 있어 graceful."""
    fake_run(
        [
            [
                {"upload": {"id": None}},  # invalid
                _upload_row(upload_id="ok"),
            ]
        ]
    )
    result = await repo.list_uploads("a@b.com")
    assert [u.id for u in result] == ["ok"]


async def test_list_uploads_passes_limit(fake_run):
    fake = fake_run([[]])
    await repo.list_uploads("a@b.com", limit=10)
    assert fake.calls[0]["params"]["limit"] == 10


# ─── get_upload ──────────────────────────────────────────────────


async def test_get_upload_returns_detail_with_content(fake_run):
    fake_run(
        [
            [
                _upload_row(
                    upload_id="u1",
                    filename="x.txt",
                    size=5,
                    content="hello",
                )
            ]
        ]
    )
    detail = await repo.get_upload("a@b.com", "u1")
    assert detail is not None
    assert detail.content == "hello"
    assert detail.filename == "x.txt"


async def test_get_upload_not_found_returns_none(fake_run):
    fake_run([[]])
    detail = await repo.get_upload("a@b.com", "missing")
    assert detail is None


async def test_get_upload_isolates_per_user(fake_run):
    """다른 사용자의 동일 id 로는 조회되지 않음 — Cypher 가 email 매칭 필수."""
    fake = fake_run([[]])
    await repo.get_upload("attacker@b.com", "u1-owned-by-someone-else")
    # Cypher 가 email 을 매칭하는지 검증
    cypher = fake.calls[0]["cypher"]
    assert "$email" in cypher
    assert "m.user_email = $email" in cypher


async def test_get_upload_empty_input_returns_none():
    assert await repo.get_upload("", "x") is None
    assert await repo.get_upload("a@b.com", "") is None


# ─── delete_upload ───────────────────────────────────────────────


async def test_delete_upload_returns_true_on_success(fake_run):
    fake_run([[{"deleted_id": "u1"}]])
    ok = await repo.delete_upload("a@b.com", "u1")
    assert ok is True


async def test_delete_upload_not_found_returns_false(fake_run):
    fake_run([[]])
    ok = await repo.delete_upload("a@b.com", "missing")
    assert ok is False


async def test_delete_upload_empty_input_returns_false():
    assert await repo.delete_upload("", "x") is False
    assert await repo.delete_upload("a@b.com", "") is False


# ─── Cypher injection 방어 회귀 ────────────────────────────────


async def test_cypher_uses_param_binding_not_string_interp(fake_run):
    """악성 페이로드가 들어와도 Cypher 가 변하지 않고 $param 으로만 전달되는지."""
    fake = fake_run([[_upload_row()]])
    malicious_filename = "x.txt'; MATCH (n) DELETE n; //"
    malicious_content = "a' OR '1'='1"
    await repo.add_upload(
        "a@b.com",
        MeetingUploadInput(filename=malicious_filename, content=malicious_content),
    )
    call = fake.calls[0]
    # 본문/파일명에 든 페이로드가 Cypher 텍스트가 아니라 params 로 들어가야 함
    assert malicious_filename not in call["cypher"]
    assert malicious_content not in call["cypher"]
    assert call["params"]["filename"] == malicious_filename
    assert call["params"]["content"] == malicious_content
