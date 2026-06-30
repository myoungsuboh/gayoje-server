"""notion_export_service — 멱등 허브/하위페이지 업서트 테스트 (client + repo 모킹)."""
import pytest

from app.clients.notion_client import NotionError
from app.service import notion_export_service as svc
from app.service.query_repository import CpsMaster

pytestmark = pytest.mark.asyncio


class FakeClient:
    def __init__(self, get_page_404=(), archive_404=()):
        self.created = []
        self.appended = {}
        self.archived = []
        self._n = 0
        self._get_page_404 = set(get_page_404)
        self._archive_404 = set(archive_404)

    async def get_page(self, page_id):
        if page_id in self._get_page_404:
            raise NotionError("notion_not_found", status=404)
        return {"id": page_id}

    async def create_page(self, *, parent_page_id, title, icon_emoji=None, children=None):
        self._n += 1
        pid = f"pg{self._n}"
        self.created.append({
            "parent": parent_page_id, "title": title, "id": pid,
            "children": list(children or []),
        })
        return {"id": pid, "url": f"u/{pid}"}

    async def append_block_children(self, *, block_id, children):
        self.appended.setdefault(block_id, []).extend(children)

    async def archive_block_children(self, *, block_id):
        if block_id in self._archive_404:
            raise NotionError("notion_not_found", status=404)
        self.archived.append(block_id)


def _patch_map(monkeypatch, existing):
    async def fake_get(email, key):
        return dict(existing) if existing else None

    async def fake_save(email, key, **kw):
        return None

    monkeypatch.setattr(svc.user_repository, "get_notion_export_map", fake_get)
    monkeypatch.setattr(svc.user_repository, "save_notion_export_map", fake_save)


def _patch_cps(monkeypatch, content):
    async def fake(project_name, team_id=""):
        return CpsMaster(content=content) if content is not None else None

    monkeypatch.setattr(svc.query_repository, "get_master_cps", fake)


async def test_first_export_creates_hub_and_subpage(monkeypatch):
    _patch_map(monkeypatch, None)
    _patch_cps(monkeypatch, "# CPS\nbody")
    c = FakeClient()
    out = await svc.export_project_to_notion(
        email="u@e.com", project_name="p", team_id="", docs=["cps"],
        parent_page_id="PAR", client=c,
    )
    assert "notion.so" in out["hub_url"]
    cps_res = [r for r in out["results"] if r["doc"] == "cps"][0]
    assert cps_res["status"] == "created"
    assert c.archived == []           # 최초엔 archive 없음
    assert len(c.created) == 2        # 허브 + cps 하위페이지
    # [2026-06-05] 내용은 페이지 생성과 함께 들어간다(빈 페이지 방지). cps 하위페이지의
    # children 에 변환된 블록이 실려야 함 (작은 문서라 append 없이 create 로 충분).
    cps_page = c.created[1]
    assert len(cps_page["children"]) > 0   # 빈 페이지가 아니어야 함


async def test_reexport_archives_then_appends(monkeypatch):
    _patch_map(monkeypatch, {"hub_page_id": "H", "cps_page_id": "C"})
    _patch_cps(monkeypatch, "# CPS\nv2")
    c = FakeClient()
    out = await svc.export_project_to_notion(
        email="u@e.com", project_name="p", team_id="", docs=["cps"],
        parent_page_id=None, client=c,
    )
    cps_res = [r for r in out["results"] if r["doc"] == "cps"][0]
    assert cps_res["status"] == "updated"
    assert "C" in c.archived
    assert "C" in c.appended
    assert c.created == []            # 재공유엔 새 페이지 생성 없음


async def test_need_parent_when_no_hub_and_no_parent(monkeypatch):
    _patch_map(monkeypatch, None)
    _patch_cps(monkeypatch, "# CPS\nx")
    c = FakeClient()
    out = await svc.export_project_to_notion(
        email="u@e.com", project_name="p", team_id="", docs=["cps"],
        parent_page_id=None, client=c,
    )
    assert out["hub_url"] is None
    assert out["results"][0]["status"] == "need_parent"
    assert c.created == []


async def test_skips_empty_doc(monkeypatch):
    _patch_map(monkeypatch, {"hub_page_id": "H"})
    _patch_cps(monkeypatch, None)  # 내용 없음
    c = FakeClient()
    out = await svc.export_project_to_notion(
        email="u@e.com", project_name="p", team_id="", docs=["cps"],
        parent_page_id=None, client=c,
    )
    assert out["results"][0]["status"] == "skipped"
    assert c.appended == {}
    assert c.created == []


async def test_hub_deleted_recreates_when_parent_given(monkeypatch):
    _patch_map(monkeypatch, {"hub_page_id": "H", "cps_page_id": "C"})
    _patch_cps(monkeypatch, "# CPS\nx")
    c = FakeClient(get_page_404={"H"})  # 허브가 Notion 에서 삭제됨
    out = await svc.export_project_to_notion(
        email="u@e.com", project_name="p", team_id="", docs=["cps"],
        parent_page_id="PAR", client=c,
    )
    cps_res = [r for r in out["results"] if r["doc"] == "cps"][0]
    assert cps_res["status"] == "created"   # 스테일 매핑 폐기 → 재생성
    assert len(c.created) == 2              # 허브 + cps 재생성
    assert c.archived == []                 # 폐기됐으니 archive 없음


async def test_hub_deleted_without_parent_needs_parent(monkeypatch):
    _patch_map(monkeypatch, {"hub_page_id": "H", "cps_page_id": "C"})
    _patch_cps(monkeypatch, "# CPS\nx")
    c = FakeClient(get_page_404={"H"})
    out = await svc.export_project_to_notion(
        email="u@e.com", project_name="p", team_id="", docs=["cps"],
        parent_page_id=None, client=c,
    )
    assert out["results"][0]["status"] == "need_parent"


async def test_subpage_deleted_recreates(monkeypatch):
    _patch_map(monkeypatch, {"hub_page_id": "H", "cps_page_id": "C"})
    _patch_cps(monkeypatch, "# CPS\nx")
    c = FakeClient(archive_404={"C"})  # cps 하위페이지만 삭제됨 (허브는 살아있음)
    out = await svc.export_project_to_notion(
        email="u@e.com", project_name="p", team_id="", docs=["cps"],
        parent_page_id=None, client=c,
    )
    cps_res = [r for r in out["results"] if r["doc"] == "cps"][0]
    assert cps_res["status"] == "created"   # 재생성
    assert len(c.created) == 1              # cps 하위페이지만 (허브 유지)
