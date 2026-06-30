"""
NotionClient 단위 테스트 — httpx mock + ID 정규화 + 페이지 메타 추출.

httpx 호출은 monkeypatch 로 가짜 AsyncClient 주입. 실제 네트워크 호출 없음.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
import pytest

from app.clients import notion_client as notion
from app.clients.notion_client import (
    NotionClient,
    NotionError,
    NotionRateLimited,
    NotionUnauthorized,
    extract_page_icon,
    extract_page_title,
)

pytestmark = pytest.mark.asyncio


# ─── _normalize_id (sync) ──────────────────────────────────────


class TestNormalizeId:
    def test_uuid_pass_through(self):
        out = notion._normalize_id("abcd1234-abcd-1234-abcd-1234abcd1234")
        assert out == "abcd1234-abcd-1234-abcd-1234abcd1234"

    def test_hex32_to_uuid(self):
        out = notion._normalize_id("abcd1234abcd1234abcd1234abcd1234")
        assert out == "abcd1234-abcd-1234-abcd-1234abcd1234"

    def test_extracts_id_from_notion_url(self):
        url = "https://www.notion.so/myws/My-Page-abcd1234abcd1234abcd1234abcd1234"
        out = notion._normalize_id(url)
        assert out == "abcd1234-abcd-1234-abcd-1234abcd1234"

    def test_empty_raises(self):
        with pytest.raises(NotionError, match="empty"):
            notion._normalize_id("")

    def test_invalid_raises(self):
        with pytest.raises(NotionError, match="invalid"):
            notion._normalize_id("not-a-valid-id")

    def test_lowercases_hex(self):
        out = notion._normalize_id("ABCD1234ABCD1234ABCD1234ABCD1234")
        assert out == "abcd1234-abcd-1234-abcd-1234abcd1234"


# ─── extract_page_title ────────────────────────────────────────


class TestExtractTitle:
    def test_normal_page(self):
        page = {
            "properties": {
                "title": {
                    "type": "title",
                    "title": [
                        {"plain_text": "Hello "},
                        {"plain_text": "World"},
                    ],
                }
            }
        }
        assert extract_page_title(page) == "Hello World"

    def test_db_page_with_named_title_column(self):
        # database row 의 title 컬럼 이름이 "Name" 일 수 있음
        page = {
            "properties": {
                "Name": {
                    "type": "title",
                    "title": [{"plain_text": "DB Row"}],
                },
                "Status": {"type": "select", "select": {"name": "Done"}},
            }
        }
        assert extract_page_title(page) == "DB Row"

    def test_empty_falls_back(self):
        assert extract_page_title({}) == "(제목 없음)"
        assert extract_page_title({"properties": {}}) == "(제목 없음)"


# ─── extract_page_icon ─────────────────────────────────────────


class TestExtractIcon:
    def test_emoji(self):
        assert extract_page_icon({"icon": {"type": "emoji", "emoji": "📄"}}) == "📄"

    def test_external_url(self):
        assert extract_page_icon({
            "icon": {"type": "external", "external": {"url": "https://x.png"}}
        }) == "https://x.png"

    def test_file_url(self):
        assert extract_page_icon({
            "icon": {"type": "file", "file": {"url": "https://notion.so/f.png"}}
        }) == "https://notion.so/f.png"

    def test_none_returns_none(self):
        assert extract_page_icon({}) is None
        assert extract_page_icon({"icon": None}) is None


# ─── httpx mock helpers ────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, json_data: Optional[Dict[str, Any]] = None,
                 text: str = "", headers: Optional[Dict[str, str]] = None) -> None:
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text or ""
        self.headers = headers or {}

    def json(self) -> Dict[str, Any]:
        return self._json


class _FakeAsyncClient:
    """
    httpx.AsyncClient 대체. queue 된 응답을 순서대로 반환.

    monkeypatch 로 `httpx.AsyncClient = lambda *a, **k: _FakeAsyncClient(responses)` 패턴.
    """

    def __init__(self, responses: List[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        return self.responses.pop(0)


def _patch_httpx(monkeypatch, responses: List[_FakeResponse]) -> _FakeAsyncClient:
    fake = _FakeAsyncClient(responses)
    # NotionClient 안에서 `httpx.AsyncClient(timeout=...)` 호출하므로 그 위치를 가로채야 함.
    monkeypatch.setattr(notion.httpx, "AsyncClient", lambda *a, **k: fake)
    return fake


# ─── search_pages ──────────────────────────────────────────────


class TestSearchPages:
    async def test_calls_search_endpoint(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, [
            _FakeResponse(200, {"results": [], "has_more": False, "next_cursor": None}),
        ])
        client = NotionClient(user_token="secret")
        await client.search_pages(query="회의", page_size=20)
        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["method"] == "POST"
        assert call["url"].endswith("/v1/search")
        body = call["json"]
        assert body["query"] == "회의"
        assert body["page_size"] == 20
        assert body["filter"]["value"] == "page"

    async def test_empty_query_omits_query_field(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, [
            _FakeResponse(200, {"results": []}),
        ])
        await NotionClient(user_token="t").search_pages(query="")
        body = fake.calls[0]["json"]
        assert "query" not in body  # Notion 은 빈 query 면 최근 페이지

    async def test_cursor_passed_through(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, [_FakeResponse(200, {"results": []})])
        await NotionClient(user_token="t").search_pages(start_cursor="cur123")
        assert fake.calls[0]["json"]["start_cursor"] == "cur123"

    async def test_authorization_header(self, monkeypatch):
        fake = _patch_httpx(monkeypatch, [_FakeResponse(200, {"results": []})])
        await NotionClient(user_token="secret-xyz").search_pages()
        headers = fake.calls[0]["headers"]
        assert headers["Authorization"] == "Bearer secret-xyz"
        assert headers["Notion-Version"] == "2022-06-28"


# ─── error 분류 ────────────────────────────────────────────────


class TestErrorClassification:
    async def test_401_raises_unauthorized(self, monkeypatch):
        _patch_httpx(monkeypatch, [_FakeResponse(401, text="unauthorized")])
        with pytest.raises(NotionUnauthorized):
            await NotionClient(user_token="t").search_pages()

    async def test_429_with_retry_after(self, monkeypatch):
        _patch_httpx(monkeypatch, [
            _FakeResponse(429, text="rate limited", headers={"Retry-After": "2.5"}),
        ])
        with pytest.raises(NotionRateLimited) as exc:
            await NotionClient(user_token="t").search_pages()
        assert exc.value.retry_after == 2.5

    async def test_429_without_retry_after(self, monkeypatch):
        _patch_httpx(monkeypatch, [_FakeResponse(429, text="rate limited")])
        with pytest.raises(NotionRateLimited) as exc:
            await NotionClient(user_token="t").search_pages()
        assert exc.value.retry_after is None

    async def test_404_raises_with_status(self, monkeypatch):
        _patch_httpx(monkeypatch, [_FakeResponse(404, text="not found")])
        with pytest.raises(NotionError) as exc:
            await NotionClient(user_token="t").get_page(
                "abcd1234abcd1234abcd1234abcd1234"
            )
        assert exc.value.status == 404

    async def test_5xx_raises_generic(self, monkeypatch):
        _patch_httpx(monkeypatch, [_FakeResponse(503, text="service down")])
        with pytest.raises(NotionError) as exc:
            await NotionClient(user_token="t").search_pages()
        assert exc.value.status == 503

    async def test_network_error_wrapped(self, monkeypatch):
        class _Boom:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def request(self, *a, **kw):
                raise httpx.TimeoutException("timeout")

        monkeypatch.setattr(notion.httpx, "AsyncClient", lambda *a, **k: _Boom())
        with pytest.raises(NotionError, match="network_error"):
            await NotionClient(user_token="t").search_pages()


# ─── get_page_blocks 재귀 + 페이지네이션 ──────────────────────


class TestGetPageBlocks:
    async def test_simple_flat_list(self, monkeypatch):
        _patch_httpx(monkeypatch, [
            _FakeResponse(200, {
                "results": [
                    {"id": "b1", "type": "paragraph", "has_children": False},
                    {"id": "b2", "type": "paragraph", "has_children": False},
                ],
                "has_more": False,
            }),
        ])
        out = await NotionClient(user_token="t").get_page_blocks(
            "abcd1234abcd1234abcd1234abcd1234"
        )
        assert len(out) == 2
        assert all("_children" not in b or not b.get("_children") for b in out)

    async def test_paginates_with_next_cursor(self, monkeypatch):
        _patch_httpx(monkeypatch, [
            _FakeResponse(200, {
                "results": [{"id": "b1", "type": "paragraph", "has_children": False}],
                "has_more": True, "next_cursor": "cur1",
            }),
            _FakeResponse(200, {
                "results": [{"id": "b2", "type": "paragraph", "has_children": False}],
                "has_more": False,
            }),
        ])
        out = await NotionClient(user_token="t").get_page_blocks(
            "abcd1234abcd1234abcd1234abcd1234"
        )
        assert [b["id"] for b in out] == ["b1", "b2"]

    async def test_fetches_children_when_has_children(self, monkeypatch):
        _patch_httpx(monkeypatch, [
            # 첫 번째: 부모 블록 목록
            _FakeResponse(200, {
                "results": [{
                    "id": "parent1", "type": "toggle", "has_children": True,
                }],
                "has_more": False,
            }),
            # 두 번째: parent1 의 자식
            _FakeResponse(200, {
                "results": [{"id": "child1", "type": "paragraph", "has_children": False}],
                "has_more": False,
            }),
        ])
        out = await NotionClient(user_token="t").get_page_blocks(
            "abcd1234abcd1234abcd1234abcd1234"
        )
        assert len(out) == 1
        assert out[0]["id"] == "parent1"
        assert out[0]["_children"][0]["id"] == "child1"

    async def test_search_validates_user_token(self):
        with pytest.raises(NotionError, match="user_token"):
            NotionClient(user_token="")
