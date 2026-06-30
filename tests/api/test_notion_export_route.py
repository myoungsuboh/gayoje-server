"""POST /api/v2/notion/export 라우트 테스트 — 가드/성공/오류매핑."""
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import notion_routes as routes
from app.service.user_repository import UserPublic

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _disable_limiter():
    routes.limiter.enabled = False
    yield
    routes.limiter.enabled = True


def _fake_request() -> Request:
    return Request(
        scope={
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 0),
        }
    )


def _user(email: str = "u@e.com") -> UserPublic:
    return UserPublic(
        id="u-1",
        email=email,
        name="t",
        subscription_type="free",
        is_admin=False,
        auto_progress=True,
    )


async def _ok_notion_info(*a, **k):
    return {
        "access_token": "tok",
        "workspace_id": "w",
        "workspace_name": "W",
        "bot_id": "b",
        "linked_at": None,
    }


async def _no_notion_info(*a, **k):
    return None


async def _noop(*a, **k):
    return None


def _patch_common(monkeypatch):
    monkeypatch.setattr(routes.ownership_repository, "assert_access", _noop)
    monkeypatch.setattr(routes, "NotionClient", lambda **kw: object())


async def test_export_412_when_not_linked(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(routes.user_repository, "get_notion_info", _no_notion_info)
    with pytest.raises(HTTPException) as exc:
        await routes.export_to_notion(
            request=_fake_request(),
            payload=routes.NotionExportRequest(project_name="p"),
            current_user=_user(),
        )
    assert exc.value.status_code == 412
    assert exc.value.detail["code"] == "NOTION_NOT_LINKED"


async def test_export_success(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(routes.user_repository, "get_notion_info", _ok_notion_info)

    async def fake_export(**kw):
        return {
            "hub_url": "https://www.notion.so/H",
            "results": [{"doc": "cps", "status": "updated", "url": "https://www.notion.so/C"}],
        }

    monkeypatch.setattr(routes.notion_export_service, "export_project_to_notion", fake_export)
    out = await routes.export_to_notion(
        request=_fake_request(),
        payload=routes.NotionExportRequest(project_name="p", docs=["cps"]),
        current_user=_user(),
    )
    assert out.hub_url == "https://www.notion.so/H"
    assert out.results[0].doc == "cps"
    assert out.results[0].status == "updated"


async def test_export_unauthorized_maps_to_412_and_unlinks(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(routes.user_repository, "get_notion_info", _ok_notion_info)
    monkeypatch.setattr(routes.user_repository, "unlink_notion", _noop)

    async def boom(**kw):
        raise routes.NotionUnauthorized()

    monkeypatch.setattr(routes.notion_export_service, "export_project_to_notion", boom)
    with pytest.raises(HTTPException) as exc:
        await routes.export_to_notion(
            request=_fake_request(),
            payload=routes.NotionExportRequest(project_name="p"),
            current_user=_user(),
        )
    assert exc.value.status_code == 412
    assert exc.value.detail["code"] == "NOTION_TOKEN_REVOKED"


async def test_export_partial_failure_passthrough(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(routes.user_repository, "get_notion_info", _ok_notion_info)

    async def fake_export(**kw):
        return {
            "hub_url": "https://www.notion.so/H",
            "results": [
                {"doc": "cps", "status": "updated", "url": "u/c"},
                {"doc": "prd", "status": "failed", "error": "boom"},
                {"doc": "design", "status": "skipped"},
            ],
        }

    monkeypatch.setattr(routes.notion_export_service, "export_project_to_notion", fake_export)
    out = await routes.export_to_notion(
        request=_fake_request(),
        payload=routes.NotionExportRequest(project_name="p"),
        current_user=_user(),
    )
    statuses = {r.doc: r.status for r in out.results}
    assert statuses == {"cps": "updated", "prd": "failed", "design": "skipped"}
