"""
inquiry_repository 단위 테스트.

[검증 범위]
- create_inquiry: category 검증 + cypher 호출
- get/list_my/list_admin: row 매핑
- update: status/admin_reply 부분 갱신 + 검증
- count_by_status: 상태별 카운트 dict 정규화
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import inquiry_repository
from app.service.inquiry_repository import (
    INQUIRY_CATEGORIES,
    INQUIRY_STATUSES,
    Inquiry,
    bulk_update_replies,
    count_by_status,
    create_inquiry,
    get_inquiries_by_ids,
    get_inquiry,
    list_admin_inquiries,
    list_my_inquiries,
    update_inquiry,
)


pytestmark = pytest.mark.asyncio


class _FakeRunCypher:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher, params=None, database=None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses=None):
        fake = _FakeRunCypher(responses)
        monkeypatch.setattr(
            "app.service.inquiry_repository.neo4j_client.run_cypher", fake
        )
        return fake
    return _setup


_INQUIRY_ROW_PRO = {
    "id": "abc-123",
    "user_email": "a@b.com",
    "user_name": "Alice",
    "category": "bug",
    "subject": "버그 발견",
    "body": "Plan 페이지에서...",
    "status": "open",
    "admin_reply": "",
    "admin_replied_by": "",
    "admin_replied_at": None,
    "created_at": "2026-05-17T10:00:00Z",
    "updated_at": "2026-05-17T10:00:00Z",
}


# ─── create_inquiry ─────────────────────────


async def test_create_inquiry_returns_normalized(fake_run):
    fake_run([[{"inquiry": _INQUIRY_ROW_PRO}]])
    r = await create_inquiry(
        user_email="a@b.com", user_name="Alice",
        category="bug", subject="버그 발견", body="Plan 페이지에서...",
    )
    assert isinstance(r, Inquiry)
    assert r.category == "bug"
    assert r.status == "open"


async def test_create_inquiry_invalid_category(fake_run):
    fake = fake_run()
    assert await create_inquiry(
        user_email="a@b.com", user_name="A",
        category="invalid", subject="x", body="y",
    ) is None
    # cypher 호출도 안 함
    assert len(fake.calls) == 0


async def test_create_inquiry_truncates_long_body(fake_run):
    fake = fake_run([[{"inquiry": _INQUIRY_ROW_PRO}]])
    long_body = "a" * 10_000
    await create_inquiry(
        user_email="a@b.com", user_name="A",
        category="general", subject="제목", body=long_body,
    )
    # 5000자로 truncate
    assert len(fake.calls[0]["params"]["body"]) <= 5000


# ─── get / list ──────────────────────────────


async def test_get_inquiry_returns_none_when_missing(fake_run):
    fake_run([[]])
    assert await get_inquiry("not-exist") is None


async def test_get_inquiry_empty_id(fake_run):
    fake = fake_run()
    assert await get_inquiry("") is None
    assert len(fake.calls) == 0


async def test_list_my_inquiries_returns_array(fake_run):
    fake_run([[
        {"inquiry": _INQUIRY_ROW_PRO},
        {"inquiry": {**_INQUIRY_ROW_PRO, "id": "def-456", "subject": "두번째"}},
    ]])
    rows = await list_my_inquiries("a@b.com")
    assert len(rows) == 2
    assert rows[0].subject == "버그 발견"
    assert rows[1].subject == "두번째"


async def test_list_my_inquiries_empty_email(fake_run):
    fake = fake_run()
    assert await list_my_inquiries("") == []
    assert len(fake.calls) == 0


# ─── list_admin_inquiries ────────────────────


async def test_list_admin_inquiries_with_filter(fake_run):
    # 2개 cypher 호출 — list + count
    fake_run([
        [{"inquiry": _INQUIRY_ROW_PRO}],
        [{"total": 42}],
    ])
    result = await list_admin_inquiries(status_filter="open", q="버그", limit=50, offset=0)
    assert len(result["inquiries"]) == 1
    assert result["total"] == 42


async def test_list_admin_clamps_limit(fake_run):
    fake = fake_run([[], [{"total": 0}]])
    await list_admin_inquiries(limit=500, offset=-5)
    # limit 200 으로 clamp, offset 0 으로 clamp
    assert fake.calls[0]["params"]["limit"] == 200
    assert fake.calls[0]["params"]["offset"] == 0


# ─── update_inquiry ──────────────────────────


async def test_update_inquiry_invalid_status(fake_run):
    fake = fake_run()
    r = await update_inquiry(inquiry_id="x", status="invalid_status")
    assert r is None
    assert len(fake.calls) == 0


async def test_update_inquiry_status_only(fake_run):
    fake = fake_run([[{"inquiry": {**_INQUIRY_ROW_PRO, "status": "in_progress"}}]])
    r = await update_inquiry(inquiry_id="abc-123", status="in_progress")
    assert r is not None
    assert r.status == "in_progress"
    # admin_reply 는 None (기존 유지)
    assert fake.calls[0]["params"]["admin_reply"] is None


async def test_update_inquiry_truncates_long_reply(fake_run):
    fake = fake_run([[{"inquiry": _INQUIRY_ROW_PRO}]])
    long_reply = "x" * 10_000
    await update_inquiry(
        inquiry_id="abc-123",
        admin_reply=long_reply,
        admin_email="admin@b.com",
    )
    # 5000자로 truncate
    assert len(fake.calls[0]["params"]["admin_reply"]) <= 5000


async def test_update_inquiry_passes_admin_email(fake_run):
    fake = fake_run([[{"inquiry": _INQUIRY_ROW_PRO}]])
    await update_inquiry(
        inquiry_id="abc-123",
        admin_reply="답변입니다.",
        admin_email="admin@harness.com",
    )
    assert fake.calls[0]["params"]["admin_email"] == "admin@harness.com"


# ─── count_by_status ─────────────────────────


async def test_count_by_status_returns_all_keys(fake_run):
    fake_run([[
        {"status": "open", "cnt": 3},
        {"status": "resolved", "cnt": 5},
    ]])
    counts = await count_by_status()
    # 모든 상태 + total 키 존재
    assert counts["open"] == 3
    assert counts["resolved"] == 5
    assert counts["in_progress"] == 0
    assert counts["closed"] == 0
    assert counts["total"] == 8


async def test_count_by_status_ignores_unknown_status(fake_run):
    fake_run([[{"status": "WEIRD_STATUS", "cnt": 99}]])
    counts = await count_by_status()
    assert counts["total"] == 0  # WEIRD_STATUS 는 무시


# ─── get_inquiries_by_ids (일괄 조회) ─────────


async def test_get_inquiries_by_ids_empty(fake_run):
    fake = fake_run()
    assert await get_inquiries_by_ids([]) == []
    assert len(fake.calls) == 0  # 빈 입력 → cypher 호출 X


async def test_get_inquiries_by_ids_filters_falsy(fake_run):
    fake = fake_run([[]])
    await get_inquiries_by_ids(["", "abc-123"])  # 빈 문자열 제거
    assert fake.calls[0]["params"]["ids"] == ["abc-123"]


async def test_get_inquiries_by_ids_maps_rows(fake_run):
    fake_run([[
        {"inquiry": _INQUIRY_ROW_PRO},
        {"inquiry": {**_INQUIRY_ROW_PRO, "id": "def-456"}},
    ]])
    rows = await get_inquiries_by_ids(["abc-123", "def-456"])
    assert len(rows) == 2
    assert {r.id for r in rows} == {"abc-123", "def-456"}


# ─── bulk_update_replies (일괄 갱신) ──────────


async def test_bulk_update_replies_invalid_status(fake_run):
    fake = fake_run()
    r = await bulk_update_replies(
        items=[{"id": "a", "reply": "x"}], status="bogus", admin_email="ad@b.com",
    )
    assert r == []
    assert len(fake.calls) == 0  # 잘못된 status → cypher 호출 X


async def test_bulk_update_replies_empty_items(fake_run):
    fake = fake_run()
    assert await bulk_update_replies(items=[], status="resolved") == []
    assert len(fake.calls) == 0


async def test_bulk_update_replies_passes_items_and_meta(fake_run):
    fake = fake_run([[{"inquiry": {**_INQUIRY_ROW_PRO, "status": "resolved", "admin_reply": "고쳤어요"}}]])
    r = await bulk_update_replies(
        items=[{"id": "abc-123", "reply": "고쳤어요"}],
        status="resolved",
        admin_email="admin@harness.com",
    )
    assert len(r) == 1
    p = fake.calls[0]["params"]
    assert p["status"] == "resolved"
    assert p["admin_email"] == "admin@harness.com"
    assert p["items"] == [{"id": "abc-123", "reply": "고쳤어요"}]


async def test_bulk_update_replies_truncates_reply(fake_run):
    fake = fake_run([[{"inquiry": _INQUIRY_ROW_PRO}]])
    await bulk_update_replies(
        items=[{"id": "abc-123", "reply": "y" * 10_000}], status="resolved",
    )
    assert len(fake.calls[0]["params"]["items"][0]["reply"]) <= 5000  # MAX_REPLY_LENGTH


async def test_bulk_update_replies_skips_idless(fake_run):
    fake = fake_run([[]])
    await bulk_update_replies(
        items=[{"reply": "no id"}, {"id": "abc", "reply": "ok"}], status="resolved",
    )
    assert fake.calls[0]["params"]["items"] == [{"id": "abc", "reply": "ok"}]


# ─── _apply_template (routes 변수 치환 헬퍼) ──


async def test_apply_template_substitutes():
    from app.api.inquiry_routes import _apply_template
    out = _apply_template("{이름}님, '{제목}' 고쳤어요", name="Alice", subject="버그X")
    assert out == "Alice님, '버그X' 고쳤어요"


async def test_apply_template_no_double_substitution():
    from app.api.inquiry_routes import _apply_template
    # name 값에 {제목} 문자열이 섞여도 2차 치환 안 됨 (단일 패스)
    out = _apply_template("{이름}", name="{제목}", subject="LEAK")
    assert out == "{제목}"


async def test_apply_template_empty_vars():
    from app.api.inquiry_routes import _apply_template
    assert _apply_template("{이름}/{제목}", name="", subject="") == "/"
