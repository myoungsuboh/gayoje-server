"""NotionClient write ops (export 용) 단위 테스트 — _request 를 monkeypatch.

create_page / append_block_children(100 분할) / archive_block_children(1-레벨).
"""
import pytest

from app.clients.notion_client import NotionClient

pytestmark = pytest.mark.asyncio


def _client() -> NotionClient:
    return NotionClient(user_token="t")


async def test_create_page_posts_parent_title_icon(monkeypatch):
    calls = []

    async def fake_request(self, method, url, *, json=None, params=None, context):
        calls.append((method, url, json))
        return {"id": "pg1", "url": "https://notion.so/pg1"}

    monkeypatch.setattr(NotionClient, "_request", fake_request)
    out = await _client().create_page(
        parent_page_id="par", title="Hello", icon_emoji="📦", children=[{"x": 1}]
    )
    assert out["id"] == "pg1"
    method, url, body = calls[0]
    assert method == "POST" and url.endswith("/pages")
    assert body["parent"]["page_id"] == "par"
    assert body["icon"]["emoji"] == "📦"
    assert body["properties"]["title"]["title"][0]["text"]["content"] == "Hello"
    assert body["children"] == [{"x": 1}]


async def test_create_page_without_icon_or_children(monkeypatch):
    bodies = []

    async def fake_request(self, method, url, *, json=None, params=None, context):
        bodies.append(json)
        return {"id": "pg", "url": "u"}

    monkeypatch.setattr(NotionClient, "_request", fake_request)
    await _client().create_page(parent_page_id="par", title="T")
    assert "icon" not in bodies[0]
    assert bodies[0]["children"] == []


async def test_create_page_caps_children_at_100(monkeypatch):
    bodies = []

    async def fake_request(self, method, url, *, json=None, params=None, context):
        bodies.append(json)
        return {"id": "pg", "url": "u"}

    monkeypatch.setattr(NotionClient, "_request", fake_request)
    await _client().create_page(
        parent_page_id="par", title="T", children=[{"i": i} for i in range(150)]
    )
    assert len(bodies[0]["children"]) == 100


async def test_append_chunks_of_100(monkeypatch):
    chunks = []
    methods = []
    urls = []

    async def fake_request(self, method, url, *, json=None, params=None, context):
        chunks.append(len(json["children"]))
        methods.append(method)
        urls.append(url)
        return {}

    monkeypatch.setattr(NotionClient, "_request", fake_request)
    await _client().append_block_children(
        block_id="b", children=[{"i": i} for i in range(230)]
    )
    assert chunks == [100, 100, 30]
    # [2026-06-05 회귀] append 는 PATCH 여야 한다 (POST 면 405 → 빈 페이지 버그).
    assert methods == ["PATCH", "PATCH", "PATCH"]
    assert all(u.endswith("/blocks/b/children") for u in urls)


async def test_append_empty_is_noop(monkeypatch):
    calls = []

    async def fake_request(self, method, url, *, json=None, params=None, context):
        calls.append(1)
        return {}

    monkeypatch.setattr(NotionClient, "_request", fake_request)
    await _client().append_block_children(block_id="b", children=[])
    assert calls == []


async def test_archive_children_patches_each_with_archived_true(monkeypatch):
    patched = []

    async def fake_request(self, method, url, *, json=None, params=None, context):
        if method == "GET":
            return {"results": [{"id": "c1"}, {"id": "c2"}], "has_more": False}
        if method == "PATCH":
            patched.append((url, json))
            return {}
        return {}

    monkeypatch.setattr(NotionClient, "_request", fake_request)
    await _client().archive_block_children(block_id="b")
    assert len(patched) == 2
    assert all(j["archived"] is True for _, j in patched)
    # [2026-06-12 hang fix] 자식 archive 는 동시 처리(Semaphore=3) 라 순서 비결정 —
    # 집합으로 단언. 모든 자식이 정확히 한 번씩 PATCH 됐는지만 확인.
    assert {u.rsplit("/", 1)[-1] for u, _ in patched} == {"c1", "c2"}


async def test_archive_children_paginates(monkeypatch):
    pages = [
        {"results": [{"id": "c1"}], "has_more": True, "next_cursor": "cur2"},
        {"results": [{"id": "c2"}], "has_more": False},
    ]
    gets = {"n": 0}
    patched = []

    async def fake_request(self, method, url, *, json=None, params=None, context):
        if method == "GET":
            data = pages[gets["n"]]
            gets["n"] += 1
            return data
        patched.append(url)
        return {}

    monkeypatch.setattr(NotionClient, "_request", fake_request)
    await _client().archive_block_children(block_id="b")
    assert gets["n"] == 2
    assert len(patched) == 2
