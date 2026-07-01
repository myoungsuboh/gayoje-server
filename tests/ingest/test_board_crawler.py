"""INGEST-E3 회귀 — 지자체 게시판 크롤러(파서·필터·robots 게이트·크롤 루프)."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.api.v1.ingestion.crawlers.board import (
    BoardConfig,
    crawl_board,
    egovframe_rows,
    query_idx_rows,
)
from app.api.v1.ingestion.robots import RobotsGate
from app.api.v1.ingestion.service import _board_record_id

FIXTURE = (Path(__file__).parent / "fixtures" / "egov_board_dongjak.html").read_text(
    encoding="utf-8"
)

TONGYEONG_SNIPPET = """
<table><tbody>
<tr><td><a href="?gcode=9001&amp;idx=661551&amp;amode=view&amp;">제12회 통영국제음악제 안내</a></td></tr>
<tr><td><a href="?gcode=9001&amp;idx=661503&amp;amode=view&amp;">2026 통영가요제 참가자 모집 공고</a></td></tr>
</tbody></table>
"""


# ---- 파서 ----

def test_egovframe_rows_picks_title_anchor_not_thumbnail():
    """갤러리형: 썸네일(img) 앵커가 아니라 제목 앵커의 텍스트를 채택, nttId 중복 제거."""
    posts = egovframe_rows(FIXTURE, "https://www.dongjak.go.kr")
    titles = [p.title for p in posts]
    assert len(posts) == 3  # nttId 3개(각 2앵커였지만 제목만)
    assert "2026년 제29회 노들가요제 예선 참가 신청 안내" in titles
    assert all(t and "<img" not in t for t in titles)  # 빈/이미지 제목 없음


def test_egovframe_rows_absolute_url():
    posts = egovframe_rows(FIXTURE, "https://www.dongjak.go.kr")
    p = next(p for p in posts if "노들가요제" in p.title)
    assert p.detail_url.startswith("https://www.dongjak.go.kr/portal/bbs/B0000173/view.do")
    assert "nttId=10736698" in p.detail_url


def test_query_idx_rows():
    posts = query_idx_rows(TONGYEONG_SNIPPET, "https://www.tongyeong.go.kr/03180.web")
    ids = {p.detail_url for p in posts}
    assert len(posts) == 2
    assert any("통영가요제" in p.title for p in posts)
    # HTML 엔티티(&amp;) 복원된 절대 URL
    assert any("idx=661503" in u and "&amp;" not in u for u in ids)


# ---- 가요제 필터(크롤 관점) ----

def test_only_gayoje_survive_filter():
    from app.api.v1.ingestion.adapters.base import is_gayoje

    posts = egovframe_rows(FIXTURE, "https://www.dongjak.go.kr")
    gayoje = [p for p in posts if is_gayoje(p.title)]
    assert [p.title for p in gayoje] == ["2026년 제29회 노들가요제 예선 참가 신청 안내"]
    # '가요가 좋다'(가요+경연어 없음), '어버이날 기념행사'는 제외


# ---- record id 추출 ----

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://x.go.kr/bbs/B1/view.do?nttId=10736698&menuNo=1", "10736698"),
        ("https://y.go.kr/03180.web?gcode=9001&idx=661503&amode=view", "661503"),
        ("https://z.go.kr/board?articleNo=42", "42"),
    ],
)
def test_board_record_id(url, expected):
    assert _board_record_id(url) == expected


def test_board_record_id_hash_fallback():
    rid = _board_record_id("https://x.go.kr/no-id-here")
    assert len(rid) == 24 and rid.isalnum()


# ---- crawl_board 루프(네트워크리스) ----

class _Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text
        self.request = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)


class _FakeClient:
    """robots.txt → 허용, 그 외 → 픽스처 HTML."""

    def __init__(self, html):
        self.html = html

    async def get(self, url, params=None):
        if url.endswith("/robots.txt"):
            return _Resp(200, "User-agent: *\nDisallow: /admin/")
        return _Resp(200, self.html)


@pytest.mark.asyncio
async def test_crawl_board_counts_gayoje():
    gate = RobotsGate("gayoje-bot")
    cfg = BoardConfig(
        name="test_board",
        source_system="egov:test",
        base_url="https://www.dongjak.go.kr",
        list_url="https://www.dongjak.go.kr/portal/bbs/B0000173/list.do?menuNo=201030&pageIndex={page}",
        parse_rows=egovframe_rows,
        max_pages=1,
        page_pause_sec=0,
    )
    result = await crawl_board(cfg, gate=gate, client=_FakeClient(FIXTURE))
    assert result["blocked"] is False
    assert result["crawled"] == 3
    assert result["gayoje"] == 1
    assert result["posts"][0]["title"] == "2026년 제29회 노들가요제 예선 참가 신청 안내"


@pytest.mark.asyncio
async def test_crawl_board_respects_robots_disallow():
    gate = RobotsGate("gayoje-bot")

    class _BlockClient:
        async def get(self, url, params=None):
            if url.endswith("/robots.txt"):
                return _Resp(200, "User-agent: *\nDisallow: /")
            return _Resp(200, FIXTURE)

    cfg = BoardConfig(
        name="blocked",
        source_system="egov:test",
        base_url="https://www.blocked.go.kr",
        list_url="https://www.blocked.go.kr/bbs/list.do?pageIndex={page}",
        parse_rows=egovframe_rows,
        max_pages=1,
        page_pause_sec=0,
    )
    result = await crawl_board(cfg, gate=gate, client=_BlockClient())
    assert result["blocked"] is True
    assert result["crawled"] == 0


# ---- 크롤 결과 저장(DB 통합) ----

@pytest.fixture
async def db_ready(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.infra import base, db
    import app.api.v1.festivals.models  # noqa: F401 — 테이블 등록

    await db.dispose_engine()
    url = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/crawl.db"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    await db.create_all(base.Base.metadata)
    yield db
    await db.dispose_engine()


@pytest.mark.asyncio
async def test_ingest_board_posts_stores_and_idempotent(db_ready):
    from sqlalchemy import select

    from app.api.v1.festivals.models import FestivalEvent
    from app.api.v1.ingestion.service import ingest_board_posts

    gate = RobotsGate("gayoje-bot")
    cfg = BoardConfig(
        name="test_board",
        source_system="egov:dongjak",
        base_url="https://www.dongjak.go.kr",
        list_url="https://www.dongjak.go.kr/portal/bbs/B0000173/list.do?menuNo=201030&pageIndex={page}",
        parse_rows=egovframe_rows,
        max_pages=1,
        page_pause_sec=0,
    )
    result = await crawl_board(cfg, gate=gate, client=_FakeClient(FIXTURE))

    async with db_ready.session_scope() as s:
        counts = await ingest_board_posts(s, result["source_system"], result["posts"])
    assert counts["inserted"] == 1  # 노들가요제 1건 저장

    async with db_ready.session_scope() as s:
        rows = (await s.scalars(select(FestivalEvent))).all()
    assert len(rows) == 1
    row = rows[0]
    assert "노들가요제" in row.title
    assert row.source_system == "egov:dongjak"
    assert row.source_record_id == "10736698"  # URL 의 nttId
    assert row.source_url.startswith("https://www.dongjak.go.kr")
    assert row.start_date is None  # 상세 본문 파싱 전 → NULL

    # 재크롤·재저장 → 멱등(unchanged)
    async with db_ready.session_scope() as s:
        counts2 = await ingest_board_posts(s, result["source_system"], result["posts"])
    assert counts2["unchanged"] == 1 and counts2["inserted"] == 0
