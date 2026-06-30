"""
notion_routes 단위 테스트 — 가드 + import 흐름.

- Bearer 인증은 Depends(get_current_user) 라 user 직접 주입.
- NotionClient / user_repository / quota / ownership / enqueue 는 monkeypatch.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import notion_routes as routes
from app.clients.notion_client import (
    NotionError,
    NotionRateLimited,
    NotionUnauthorized,
)
from app.service.user_repository import UserPublic

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _disable_limiter():
    """slowapi rate-limit 비활성 — 단위 테스트 노이즈 제거. 운영은 limit 살아있음."""
    routes.limiter.enabled = False
    yield
    routes.limiter.enabled = True


def _fake_request() -> Request:
    """slowapi 가 request 객체를 요구해서 더미 ASGI scope 로 생성."""
    return Request(scope={
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 0),
    })


# ─── helpers ───────────────────────────────────────────────────


def _user(email: str = "u@e.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="t",
        subscription_type="free", is_admin=False, auto_progress=True,
    )


class _FakeNotionClient:
    """NotionClient stub. 명시적으로 응답/예외를 주입."""

    def __init__(
        self,
        *,
        search_result: Optional[Dict[str, Any]] = None,
        page: Optional[Dict[str, Any]] = None,
        blocks: Optional[List[Dict[str, Any]]] = None,
        raise_on: Optional[str] = None,  # 'search' | 'get_page' | 'get_blocks'
        exc: Optional[Exception] = None,
    ) -> None:
        self._search = search_result or {"results": [], "has_more": False}
        self._page = page or {}
        self._blocks = blocks or []
        self._raise_on = raise_on
        self._exc = exc

    def _maybe_raise(self, on: str) -> None:
        if self._raise_on == on and self._exc:
            raise self._exc

    async def search_pages(self, **kw):
        self._maybe_raise("search")
        return self._search

    async def get_page(self, page_id: str):
        self._maybe_raise("get_page")
        return self._page

    async def get_page_blocks(self, page_id: str):
        self._maybe_raise("get_blocks")
        return self._blocks


def _patch_client(monkeypatch, client: _FakeNotionClient) -> None:
    monkeypatch.setattr(routes, "NotionClient", lambda **kw: client)


async def _ok_notion_info(*a, **kw):
    return {
        "access_token": "decrypted-token",
        "workspace_id": "ws1",
        "workspace_name": "MyWs",
        "bot_id": "bot",
        "linked_at": None,
    }


async def _no_notion_info(*a, **kw):
    return None


def _patch_users(monkeypatch, info_fn=_ok_notion_info, unlink_fn=None):
    monkeypatch.setattr(routes.user_repository, "get_notion_info", info_fn)
    if unlink_fn is not None:
        monkeypatch.setattr(routes.user_repository, "unlink_notion", unlink_fn)


# ─── /pages (search) ───────────────────────────────────────────


class TestListPagesRoute:
    async def test_412_when_not_linked(self, monkeypatch):
        _patch_users(monkeypatch, info_fn=_no_notion_info)
        with pytest.raises(HTTPException) as exc:
            await routes.list_notion_pages(
                request=_fake_request(), q="", cursor=None, page_size=25,
                current_user=_user(),
            )
        assert exc.value.status_code == 412
        assert exc.value.detail["code"] == "NOTION_NOT_LINKED"

    async def test_returns_slim_summaries(self, monkeypatch):
        _patch_users(monkeypatch)
        client = _FakeNotionClient(search_result={
            "results": [
                {
                    "id": "page-1",
                    "object": "page",
                    "url": "https://notion.so/page-1",
                    "last_edited_time": "2026-05-18T10:00:00.000Z",
                    "parent": {"type": "workspace", "workspace": True},
                    "icon": {"type": "emoji", "emoji": "📝"},
                    "properties": {
                        "title": {
                            "type": "title",
                            "title": [{"plain_text": "My Page"}],
                        }
                    },
                },
                # database 가 섞여 들어와도 걸러져야 함
                {"id": "db-1", "object": "database"},
            ],
            "has_more": True,
            "next_cursor": "cur-next",
        })
        _patch_client(monkeypatch, client)
        resp = await routes.list_notion_pages(
            request=_fake_request(), q="", cursor=None, page_size=25,
            current_user=_user(),
        )
        assert len(resp.results) == 1
        assert resp.results[0].id == "page-1"
        assert resp.results[0].title == "My Page"
        assert resp.results[0].icon == "📝"
        assert resp.has_more is True
        assert resp.next_cursor == "cur-next"

    async def test_unauthorized_triggers_auto_unlink(self, monkeypatch):
        unlink_called = {"yes": False}

        async def _unlink(email):
            unlink_called["yes"] = True
            return True

        _patch_users(monkeypatch, unlink_fn=_unlink)
        _patch_client(monkeypatch, _FakeNotionClient(
            raise_on="search", exc=NotionUnauthorized(),
        ))
        with pytest.raises(HTTPException) as exc:
            await routes.list_notion_pages(
                request=_fake_request(), q="", cursor=None, page_size=25,
                current_user=_user(),
            )
        assert exc.value.status_code == 412
        assert exc.value.detail["code"] == "NOTION_TOKEN_REVOKED"
        assert unlink_called["yes"] is True

    async def test_rate_limit_passes_retry_after(self, monkeypatch):
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            raise_on="search",
            exc=NotionRateLimited("rl", retry_after=3.0),
        ))
        with pytest.raises(HTTPException) as exc:
            await routes.list_notion_pages(
                request=_fake_request(), q="x", cursor=None, page_size=25,
                current_user=_user(),
            )
        assert exc.value.status_code == 429
        assert exc.value.detail["code"] == "NOTION_RATE_LIMITED"
        assert exc.value.detail["retry_after"] == 3.0


# ─── /pages/{id}/preview ───────────────────────────────────────


class TestPreviewRoute:
    async def test_preview_renders_markdown(self, monkeypatch):
        _patch_users(monkeypatch)
        client = _FakeNotionClient(
            page={
                "id": "page-2",
                "last_edited_time": "2026-05-18T11:00:00.000Z",
                "properties": {
                    "title": {"type": "title",
                              "title": [{"plain_text": "Doc"}]}
                },
            },
            blocks=[{
                "type": "paragraph",
                "paragraph": {"rich_text": [{
                    "plain_text": "hello world",
                    "annotations": {}, "href": None,
                }]},
            }],
        )
        _patch_client(monkeypatch, client)
        # 캐시 회피 — preview 캐시 비우기
        routes._preview_cache.clear()

        resp = await routes.preview_notion_page(
            request=_fake_request(),
            page_id="page-2",
            current_user=_user(email="cache-isolation@e.com"),
        )
        assert resp.title == "Doc"
        assert "hello world" in resp.markdown
        assert resp.block_count == 1

    async def test_404_when_page_missing(self, monkeypatch):
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            raise_on="get_page",
            exc=NotionError("not_found", status=404),
        ))
        routes._preview_cache.clear()
        with pytest.raises(HTTPException) as exc:
            await routes.preview_notion_page(
                request=_fake_request(), page_id="missing-page",
                current_user=_user(email="missing@e.com"),
            )
        assert exc.value.status_code == 404
        assert exc.value.detail["code"] == "NOTION_NOT_FOUND"

    async def test_cache_hit_returns_without_recall(self, monkeypatch):
        """동일 (user,page_id) 두 번째 호출은 FakeClient.get_page 호출 안 함."""
        _patch_users(monkeypatch)
        call_counter = {"page": 0, "blocks": 0}

        class _Counting(_FakeNotionClient):
            async def get_page(self, page_id):
                call_counter["page"] += 1
                return {"id": page_id, "properties": {
                    "title": {"type": "title",
                              "title": [{"plain_text": "T"}]}}}

            async def get_page_blocks(self, page_id):
                call_counter["blocks"] += 1
                return []

        _patch_client(monkeypatch, _Counting())
        routes._preview_cache.clear()

        user = _user(email="cache@e.com")
        await routes.preview_notion_page(
            request=_fake_request(), page_id="page-A", current_user=user,
        )
        await routes.preview_notion_page(
            request=_fake_request(), page_id="page-A", current_user=user,
        )
        assert call_counter["page"] == 1
        assert call_counter["blocks"] == 1


# ─── /import ───────────────────────────────────────────────────


class _QuotaShim:
    """quota 모듈 stub — 3개 가드 모두 통과시키되 호출 추적."""

    def __init__(self) -> None:
        self.tokens_called = False
        self.summary_called = False
        self.acquire_called = False

    async def assert_tokens_within_limit(self, email):
        self.tokens_called = True

    async def assert_summary_within_limit(self, email, content):
        self.summary_called = True

    async def acquire_meeting_quota(self, email):
        self.acquire_called = True


class TestImportRoute:
    async def test_412_when_not_linked(self, monkeypatch):
        _patch_users(monkeypatch, info_fn=_no_notion_info)
        from app.api.notion_routes import NotionImportRequest
        with pytest.raises(HTTPException) as exc:
            await routes.import_notion_page(
                request=_fake_request(),
                payload=NotionImportRequest(
                    page_id="any", project_name="p", version="v1",
                ),
                current_user=_user(),
            )
        assert exc.value.status_code == 412
        assert exc.value.detail["code"] == "NOTION_NOT_LINKED"

    async def test_422_when_page_empty(self, monkeypatch):
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            page={"properties": {"title": {"type": "title",
                                            "title": [{"plain_text": "Empty"}]}}},
            blocks=[],  # 빈 블록 → 변환 결과도 빈 markdown
        ))
        from app.api.notion_routes import NotionImportRequest
        with pytest.raises(HTTPException) as exc:
            await routes.import_notion_page(
                request=_fake_request(),
                payload=NotionImportRequest(
                    page_id="abcd1234abcd1234abcd1234abcd1234",
                    project_name="p", version="v1",
                ),
                current_user=_user(),
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "NOTION_PAGE_EMPTY"

    async def test_400_when_page_too_short(self, monkeypatch):
        """비어있진 않지만 200자 미만인 페이지는 88a356a 검증에 차단.

        LLM 환각 차단 + 미팅 카운트 차감 방지 (사용자 보호).
        """
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            page={"properties": {"title": {
                "type": "title", "title": [{"plain_text": "Tiny"}],
            }}},
            blocks=[{
                "type": "paragraph",
                "paragraph": {"rich_text": [{
                    "plain_text": "hi",   # 너무 짧음 — 200자 한참 미만
                    "annotations": {}, "href": None,
                }]},
            }],
        ))

        # quota / enqueue 가드는 가드 전에 차단돼서 호출되면 안 됨.
        quota_calls = {"count": 0}

        async def _q(*a, **kw):
            quota_calls["count"] += 1
        monkeypatch.setattr(routes.quota, "assert_tokens_within_limit", _q)
        monkeypatch.setattr(routes.quota, "assert_summary_within_limit", _q)
        monkeypatch.setattr(routes.quota, "acquire_meeting_quota", _q)

        enqueue_called = {"yes": False}

        async def _enqueue(**kw):
            enqueue_called["yes"] = True
        monkeypatch.setattr(routes, "enqueue_post_meeting", _enqueue)

        from app.api.notion_routes import NotionImportRequest
        with pytest.raises(HTTPException) as exc:
            await routes.import_notion_page(
                request=_fake_request(),
                payload=NotionImportRequest(
                    page_id="abcd1234abcd1234abcd1234abcd1234",
                    project_name="p", version="v1",
                ),
                current_user=_user(),
            )
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "NOTION_PAGE_TOO_SHORT"
        # 검증 실패 시 quota / enqueue 호출되면 안 됨 — 사용자 보호의 핵심.
        assert quota_calls["count"] == 0
        assert enqueue_called["yes"] is False

    async def test_happy_path_enqueues_post_meeting(self, monkeypatch):
        _patch_users(monkeypatch)
        # 본문이 200자 / 공백제외 100자 가드 (88a356a) 를 통과하도록 충분한 분량.
        long_paragraph = (
            "오늘 미팅에서는 신규 회원 가입 플로우에 대한 깊이 있는 논의를 진행했습니다. "
            "PM 은 사용자가 최소 정보로 빠르게 가입할 수 있도록 이메일과 비밀번호 두 가지만 "
            "필수 입력으로 두자고 제안했습니다. 보안팀은 비밀번호 정책에 대해 최소 12자 "
            "이상이며 영문 대소문자, 숫자, 특수문자를 모두 포함해야 한다고 요구했습니다. "
            "기획팀은 이메일 인증을 가입 직후가 아닌 첫 결제 시점으로 미루는 안을 제시했습니다."
        )
        _patch_client(monkeypatch, _FakeNotionClient(
            page={
                "id": "page-X",
                "url": "https://notion.so/page-X",
                "properties": {"title": {
                    "type": "title", "title": [{"plain_text": "Meeting Notes"}],
                }},
            },
            blocks=[{
                "type": "heading_1",
                "heading_1": {"rich_text": [{
                    "plain_text": "Agenda", "annotations": {}, "href": None,
                }]},
            }, {
                "type": "paragraph",
                "paragraph": {"rich_text": [{
                    "plain_text": long_paragraph,
                    "annotations": {}, "href": None,
                }]},
            }],
        ))

        # ownership / quota / enqueue 모두 stub.
        claim_called = {"project": None}

        async def _claim(email, project):
            claim_called["project"] = project
            return None
        monkeypatch.setattr(routes.ownership_repository, "claim_project", _claim)

        quota_shim = _QuotaShim()
        monkeypatch.setattr(routes.quota, "assert_tokens_within_limit",
                            quota_shim.assert_tokens_within_limit)
        monkeypatch.setattr(routes.quota, "assert_summary_within_limit",
                            quota_shim.assert_summary_within_limit)
        monkeypatch.setattr(routes.quota, "acquire_meeting_quota",
                            quota_shim.acquire_meeting_quota)

        enqueue_args = {}

        async def _enqueue(**kw):
            enqueue_args.update(kw)
        monkeypatch.setattr(routes, "enqueue_post_meeting", _enqueue)

        from app.api.notion_routes import NotionImportRequest
        resp = await routes.import_notion_page(
            request=_fake_request(),
            payload=NotionImportRequest(
                page_id="abcd1234abcd1234abcd1234abcd1234",
                project_name="my-proj",
                version="v1.2",
            ),
            current_user=_user(email="user@e.com"),
        )

        # 응답
        assert resp.status == "accepted"
        assert resp.task_id
        assert resp.title == "Meeting Notes"
        # 가드 호출 순서
        assert quota_shim.tokens_called
        assert quota_shim.summary_called
        assert quota_shim.acquire_called
        # claim
        assert claim_called["project"] == "my-proj"
        # enqueue 페이로드 검증
        assert enqueue_args["project_name"] == "my-proj"
        assert enqueue_args["version"] == "v1.2"
        assert enqueue_args["user_email"] == "user@e.com"
        # meeting_content 머리에 title + 출처가 prepend 되어야 함
        mc = enqueue_args["meeting_content"]
        assert "# Meeting Notes" in mc
        assert "imported from Notion" in mc
        assert "Agenda" in mc
        assert "신규 회원 가입 플로우" in mc

    async def test_ownership_conflict_to_409(self, monkeypatch):
        _patch_users(monkeypatch)
        # 본문은 충분히 길게 — too-short 가드를 통과한 다음에야 ownership 가드까지 도달.
        long_body = (
            "이번 회의에서 결제 모듈의 기본 정책을 다시 점검했습니다. "
            "PM 은 결제 실패 시 자동 재시도를 도입하자고 했고, 보안팀은 카드 토큰화를 "
            "더 강력하게 적용하자고 했습니다. 운영팀은 환불 처리 SLA 를 24시간 이내로 "
            "맞추기 위한 운영 가이드를 별도로 작성하기로 했습니다."
        )
        _patch_client(monkeypatch, _FakeNotionClient(
            page={"properties": {"title": {
                "type": "title", "title": [{"plain_text": "결제 정책 미팅"}],
            }}},
            blocks=[{"type": "paragraph", "paragraph": {
                "rich_text": [{
                    "plain_text": long_body, "annotations": {}, "href": None,
                }],
            }}],
        ))

        from app.service.ownership_repository import ProjectOwnershipConflict

        async def _conflict(email, project):
            raise ProjectOwnershipConflict(project=project)
        monkeypatch.setattr(routes.ownership_repository, "claim_project", _conflict)

        from app.api.notion_routes import NotionImportRequest
        with pytest.raises(HTTPException) as exc:
            await routes.import_notion_page(
                request=_fake_request(),
                payload=NotionImportRequest(
                    page_id="abcd1234abcd1234abcd1234abcd1234",
                    project_name="taken", version="v1",
                ),
                current_user=_user(),
            )
        assert exc.value.status_code == 409


# ─── /normalize ───────────────────────────────────────────────


class TestNormalizeRoute:
    @staticmethod
    def _patch_quota_ok(monkeypatch):
        async def _ok(*a, **kw):
            return None
        monkeypatch.setattr(routes.quota, "assert_tokens_within_limit", _ok)
        monkeypatch.setattr(routes.quota, "assert_summary_within_limit", _ok)

    @staticmethod
    def _patch_pipeline(
        monkeypatch,
        *,
        normalized="",
        raise_exc=None,
        # 분류 stub — default ACCEPT (meeting_log)
        classify_type="meeting_log",
        classify_confidence=0.9,
        classify_reason="발화자 명확",
    ):
        """run_notion_normalize + run_notion_classify 를 stub. tracked_pipeline_context 도 동시 stub."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_ctx(**kw):
            class _Ctx:
                pass
            yield _Ctx()

        monkeypatch.setattr(routes, "tracked_pipeline_context", _fake_ctx)

        # 분류 stub
        from app.pipelines.notion_classify_pipeline import (
            NotionClassifyResult, _TIER_MAP,
        )

        async def _fake_classify(ctx, payload):
            return NotionClassifyResult(
                type=classify_type,
                confidence=classify_confidence,
                reason=classify_reason,
                tier=_TIER_MAP.get(classify_type, "BLOCK"),
            )

        monkeypatch.setattr(routes, "run_notion_classify", _fake_classify)

        async def _fake_run(ctx, payload):
            if raise_exc:
                raise raise_exc
            from app.pipelines.notion_normalize_pipeline import NotionNormalizeResult
            return NotionNormalizeResult(
                normalized_markdown=normalized,
                truncated=False,
                char_count=len(normalized),
            )

        monkeypatch.setattr(routes, "run_notion_normalize", _fake_run)

    async def test_412_when_not_linked(self, monkeypatch):
        _patch_users(monkeypatch, info_fn=_no_notion_info)
        from app.api.notion_routes import NotionNormalizeRequest
        with pytest.raises(HTTPException) as exc:
            await routes.normalize_notion_page(
                request=_fake_request(),
                payload=NotionNormalizeRequest(
                    page_id="abcd1234abcd1234abcd1234abcd1234",
                    project_name="p", version="v1",
                ),
                current_user=_user(),
            )
        assert exc.value.status_code == 412
        assert exc.value.detail["code"] == "NOTION_NOT_LINKED"

    async def test_422_when_page_empty(self, monkeypatch):
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            page={"properties": {"title": {
                "type": "title", "title": [{"plain_text": "T"}],
            }}},
            blocks=[],
        ))
        from app.api.notion_routes import NotionNormalizeRequest
        with pytest.raises(HTTPException) as exc:
            await routes.normalize_notion_page(
                request=_fake_request(),
                payload=NotionNormalizeRequest(
                    page_id="abcd1234abcd1234abcd1234abcd1234",
                    project_name="p", version="v1",
                ),
                current_user=_user(),
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "NOTION_PAGE_EMPTY"

    async def test_happy_path_returns_both_markdowns(self, monkeypatch):
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            page={
                "id": "page-N",
                "url": "https://notion.so/page-N",
                "properties": {"title": {
                    "type": "title", "title": [{"plain_text": "테스트 페이지"}],
                }},
            },
            blocks=[{
                "type": "paragraph",
                "paragraph": {"rich_text": [{
                    "plain_text": "어쩌고 저쩌고 충분한 길이의 페이지 본문입니다 " * 5,
                    "annotations": {}, "href": None,
                }]},
            }],
        ))
        self._patch_quota_ok(monkeypatch)
        self._patch_pipeline(monkeypatch, normalized=(
            "### [미팅 로그 v1.1] - 테스트\n"
            "* **일시:** 2026-05-18\n"
            "* **참석자:** —\n"
            "* **작성자:** \"본문 요약\"\n"
            "* **[진척도: — - 테스트]**\n---\n"
        ))
        from app.api.notion_routes import NotionNormalizeRequest
        resp = await routes.normalize_notion_page(
            request=_fake_request(),
            payload=NotionNormalizeRequest(
                page_id="abcd1234abcd1234abcd1234abcd1234",
                project_name="proj", version="v1.1",
            ),
            current_user=_user(),
        )
        assert resp.title == "테스트 페이지"
        assert "테스트" in resp.normalized_markdown
        assert "### [미팅 로그 v1.1]" in resp.normalized_markdown
        assert resp.original_markdown   # 비어있지 않음
        assert resp.normalized_char_count > 0
        # 분류 결과 포함 (default ACCEPT)
        assert resp.classification.tier == "ACCEPT"
        assert resp.classification.type == "meeting_log"

    async def test_block_returns_400_with_classification(self, monkeypatch):
        """task_request / general_doc → BLOCK → 400 + classification 메타.
        정형화 LLM 은 호출되면 안 됨 (비용 보호)."""
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            page={"properties": {"title": {
                "type": "title", "title": [{"plain_text": "T"}],
            }}},
            blocks=[{"type": "paragraph", "paragraph": {
                "rich_text": [{
                    "plain_text": "엑셀 보고서 만들어줘 " * 30,
                    "annotations": {}, "href": None,
                }],
            }}],
        ))
        self._patch_quota_ok(monkeypatch)
        # 분류 결과: task_request → BLOCK
        normalize_called = {"yes": False}

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_ctx(**kw):
            class _Ctx: pass
            yield _Ctx()

        monkeypatch.setattr(routes, "tracked_pipeline_context", _fake_ctx)

        from app.pipelines.notion_classify_pipeline import (
            NotionClassifyResult, _TIER_MAP,
        )

        async def _classify(ctx, payload):
            return NotionClassifyResult(
                type="task_request", confidence=0.95,
                reason="엑셀 보고서 작성 요청",
                tier=_TIER_MAP["task_request"],
            )
        monkeypatch.setattr(routes, "run_notion_classify", _classify)

        async def _normalize(ctx, payload):
            normalize_called["yes"] = True
            from app.pipelines.notion_normalize_pipeline import NotionNormalizeResult
            return NotionNormalizeResult("x", False, 1)
        monkeypatch.setattr(routes, "run_notion_normalize", _normalize)

        from app.api.notion_routes import NotionNormalizeRequest
        with pytest.raises(HTTPException) as exc:
            await routes.normalize_notion_page(
                request=_fake_request(),
                payload=NotionNormalizeRequest(
                    page_id="abcd1234abcd1234abcd1234abcd1234",
                    project_name="p", version="v1",
                ),
                current_user=_user(),
            )
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "NOTION_CONTENT_NOT_SUPPORTED"
        cls_meta = exc.value.detail["classification"]
        assert cls_meta["type"] == "task_request"
        assert cls_meta["tier"] == "BLOCK"
        assert cls_meta["confidence"] == 0.95
        # 정형화 LLM 호출되면 안 됨 — 비용 보호의 핵심
        assert normalize_called["yes"] is False

    async def test_warn_accepts_but_marks_tier(self, monkeypatch):
        """retrospective / spec_doc → WARN → 정형화 진행하되 응답 tier 표시."""
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            page={
                "id": "page-W",
                "url": "https://notion.so/p",
                "properties": {"title": {
                    "type": "title", "title": [{"plain_text": "Sprint Retro"}],
                }},
            },
            blocks=[{"type": "paragraph", "paragraph": {
                "rich_text": [{
                    "plain_text": "이번 sprint 회고: 좋았던 점 등등 " * 10,
                    "annotations": {}, "href": None,
                }],
            }}],
        ))
        self._patch_quota_ok(monkeypatch)
        self._patch_pipeline(
            monkeypatch,
            normalized=(
                "### [미팅 로그 v1.1] - Sprint 회고\n"
                "* **일시:** 2026-05-18\n"
                "* **참석자:** —\n"
                "* **작성자:** \"이번 sprint 회고 — 좋았던 점, 아쉬웠던 점 정리.\"\n"
                "* **[진척도: — - 회고 압축]**\n---\n"
            ),
            classify_type="retrospective",
            classify_confidence=0.83,
            classify_reason="KPT 구조 + 1인칭 회고체",
        )
        from app.api.notion_routes import NotionNormalizeRequest
        resp = await routes.normalize_notion_page(
            request=_fake_request(),
            payload=NotionNormalizeRequest(
                page_id="abcd1234abcd1234abcd1234abcd1234",
                project_name="p", version="v1.1",
            ),
            current_user=_user(),
        )
        # 정형화는 진행됨
        assert "### [미팅 로그 v1.1]" in resp.normalized_markdown
        # 분류는 WARN — FE 가 경고 배너 노출
        assert resp.classification.tier == "WARN"
        assert resp.classification.type == "retrospective"

    async def test_summary_limit_exceeded_blocks_llm_calls(self, monkeypatch):
        """원본 markdown 이 등급별 summary_chars 한도 초과 → 분류 + 정형화 둘 다
        호출되면 안 됨. /normalize 단계에서 차단해야 /import 우회 (압축 후 등록) 막힘."""
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            page={"properties": {"title": {
                "type": "title", "title": [{"plain_text": "Huge Page"}],
            }}},
            blocks=[{"type": "paragraph", "paragraph": {
                "rich_text": [{
                    "plain_text": "x" * 1000,
                    "annotations": {}, "href": None,
                }],
            }}],
        ))

        # assert_tokens 통과, assert_summary 만 한도 초과로 raise.
        from fastapi import HTTPException as HE
        async def _tokens_ok(email): return None

        async def _summary_exceeded(email, content):
            raise HE(
                status_code=402,
                detail={"code": "QUOTA_EXCEEDED", "limit_type": "summary_chars"},
            )
        monkeypatch.setattr(routes.quota, "assert_tokens_within_limit", _tokens_ok)
        monkeypatch.setattr(routes.quota, "assert_summary_within_limit", _summary_exceeded)

        # LLM 호출 stub — 호출되면 안 됨 (가드가 먼저 차단)
        classify_called = {"yes": False}
        normalize_called = {"yes": False}

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_ctx(**kw):
            class _Ctx: pass
            yield _Ctx()
        monkeypatch.setattr(routes, "tracked_pipeline_context", _fake_ctx)

        async def _classify(ctx, payload):
            classify_called["yes"] = True
            from app.pipelines.notion_classify_pipeline import (
                NotionClassifyResult, _TIER_MAP,
            )
            return NotionClassifyResult(
                type="meeting_log", confidence=0.9, reason="x",
                tier=_TIER_MAP["meeting_log"],
            )
        monkeypatch.setattr(routes, "run_notion_classify", _classify)

        async def _normalize(ctx, payload):
            normalize_called["yes"] = True
            from app.pipelines.notion_normalize_pipeline import NotionNormalizeResult
            return NotionNormalizeResult("x", False, 1)
        monkeypatch.setattr(routes, "run_notion_normalize", _normalize)

        from app.api.notion_routes import NotionNormalizeRequest
        with pytest.raises(HE) as exc:
            await routes.normalize_notion_page(
                request=_fake_request(),
                payload=NotionNormalizeRequest(
                    page_id="abcd1234abcd1234abcd1234abcd1234",
                    project_name="p", version="v1",
                ),
                current_user=_user(),
            )
        assert exc.value.status_code == 402
        # 핵심: LLM 호출 둘 다 0회 — 우회 차단의 본질
        assert classify_called["yes"] is False
        assert normalize_called["yes"] is False

    async def test_pipeline_value_error_to_502(self, monkeypatch):
        """LLM 출력이 표준 포맷 위반 시 502 NORMALIZE_FAILED."""
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            page={"properties": {"title": {
                "type": "title", "title": [{"plain_text": "T"}],
            }}},
            blocks=[{"type": "paragraph", "paragraph": {
                "rich_text": [{
                    "plain_text": "body " * 50,
                    "annotations": {}, "href": None,
                }],
            }}],
        ))
        self._patch_quota_ok(monkeypatch)
        self._patch_pipeline(monkeypatch, raise_exc=ValueError("표준 포맷 위반"))
        from app.api.notion_routes import NotionNormalizeRequest
        with pytest.raises(HTTPException) as exc:
            await routes.normalize_notion_page(
                request=_fake_request(),
                payload=NotionNormalizeRequest(
                    page_id="abcd1234abcd1234abcd1234abcd1234",
                    project_name="p", version="v1",
                ),
                current_user=_user(),
            )
        assert exc.value.status_code == 502
        assert exc.value.detail["code"] == "NOTION_NORMALIZE_FAILED"


# ─── /import — meeting_content 옵션 동작 ──────────────────────


class TestImportWithMeetingContent:
    """body.meeting_content 제공 시 BE 가 그 텍스트를 그대로 사용 (정형화 결과 + 편집)."""

    @staticmethod
    def _setup(monkeypatch):
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            page={
                "id": "page-X",
                "url": "https://notion.so/page-X",
                "properties": {"title": {
                    "type": "title", "title": [{"plain_text": "T"}],
                }},
            },
            # blocks 가 호출되면 안 됨 — meeting_content 제공 시 폴백 fetch 우회.
            blocks=None,
        ))

        async def _claim(email, project): return None
        monkeypatch.setattr(routes.ownership_repository, "claim_project", _claim)

        async def _ok(*a, **kw): return None
        monkeypatch.setattr(routes.quota, "assert_tokens_within_limit", _ok)
        monkeypatch.setattr(routes.quota, "assert_summary_within_limit", _ok)
        monkeypatch.setattr(routes.quota, "acquire_meeting_quota", _ok)

        captured = {}

        async def _enqueue(**kw):
            captured.update(kw)
        monkeypatch.setattr(routes, "enqueue_post_meeting", _enqueue)
        return captured

    async def test_uses_provided_meeting_content(self, monkeypatch):
        captured = self._setup(monkeypatch)
        # 충분히 긴 사용자 편집 결과.
        user_content = (
            "### [미팅 로그 v1.1] - 결제 정책 검토 회의\n"
            "* **일시:** 2026-05-18\n"
            "* **참석자:** PM, 보안팀\n"
            "* **PM:** \"결제 실패 자동 재시도 도입을 제안합니다. "
            "최대 3회까지 재시도하고, 그 이후엔 별도 환불 절차 안내가 필요합니다.\"\n"
            "* **보안팀:** \"카드 토큰화를 더 강력하게 적용하겠습니다.\"\n"
            "* **[진척도: — - 결제 안건 정리]**\n---\n"
        )
        from app.api.notion_routes import NotionImportRequest
        resp = await routes.import_notion_page(
            request=_fake_request(),
            payload=NotionImportRequest(
                page_id="abcd1234abcd1234abcd1234abcd1234",
                project_name="p", version="v1.1",
                meeting_content=user_content,
            ),
            current_user=_user(),
        )
        assert resp.status == "accepted"
        # enqueue 페이로드 검증 — 사용자 content 그대로 + Notion 출처 메타 prepend
        mc = captured["meeting_content"]
        assert "imported from Notion" in mc   # 메타 prepend
        assert "### [미팅 로그 v1.1]" in mc   # 사용자 정형화 내용 보존
        assert "결제 실패 자동 재시도" in mc

    async def test_provided_content_too_short_blocked(self, monkeypatch):
        captured = self._setup(monkeypatch)
        from app.api.notion_routes import NotionImportRequest
        with pytest.raises(HTTPException) as exc:
            await routes.import_notion_page(
                request=_fake_request(),
                payload=NotionImportRequest(
                    page_id="abcd1234abcd1234abcd1234abcd1234",
                    project_name="p", version="v1.1",
                    meeting_content="hi",   # 너무 짧음
                ),
                current_user=_user(),
            )
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "NOTION_PAGE_TOO_SHORT"
        # enqueue 호출되면 안 됨
        assert "meeting_content" not in captured


# ─── error mapping 헬퍼 ────────────────────────────────────────


class TestErrorMapping:
    async def test_500_for_unknown(self, monkeypatch):
        """5xx / 알 수 없는 에러는 502 (upstream)."""
        _patch_users(monkeypatch)
        _patch_client(monkeypatch, _FakeNotionClient(
            raise_on="search", exc=NotionError("boom", status=503),
        ))
        with pytest.raises(HTTPException) as exc:
            await routes.list_notion_pages(
                request=_fake_request(), q="", cursor=None, page_size=25,
                current_user=_user(),
            )
        assert exc.value.status_code == 502
        assert exc.value.detail["code"] == "NOTION_UPSTREAM_ERROR"
