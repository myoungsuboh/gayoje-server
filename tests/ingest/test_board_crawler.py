"""INGEST-E3 회귀 — 지자체 게시판 크롤러(파서·필터·robots 게이트·크롤 루프)."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.api.v1.ingestion.adapters.base import is_gayoje
from app.api.v1.ingestion.crawlers.board import (
    BoardConfig,
    crawl_board,
    egovframe_rows,
    query_idx_rows,
    sscmc_rows,
)
from app.api.v1.ingestion.robots import RobotsGate
from app.api.v1.ingestion.service import _board_record_id

_FIX = Path(__file__).parent / "fixtures"
FIXTURE = (_FIX / "egov_board_dongjak.html").read_text(encoding="utf-8")
HWACHEON = (_FIX / "egov_bbs_hwacheon.html").read_text(encoding="utf-8")
SSCMC = (_FIX / "board_sscmc.html").read_text(encoding="utf-8")

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


def test_egovframe_rows_bbs_variant_hwacheon():
    """selectBbsNttView?nttNo 변형(화천) 도 egovframe_rows 로 파싱, '핫이슈' 배지 제거."""
    page = "https://www.ihc.go.kr/hcyc/selectBbsNttList.do?bbsNo=6&key=598&pageIndex=3"
    posts = egovframe_rows(HWACHEON, page)
    assert len(posts) == 3  # nttNo 3개
    gayoje = [p for p in posts if is_gayoje(p.title)]
    assert len(gayoje) == 2  # T&U 청소년가요제 2건(모집/발표), 특강은 제외
    top = next(p for p in posts if "참가팀 모집" in p.title)
    assert "핫이슈" not in top.title  # 배지 제거됨
    assert top.detail_url.startswith("https://www.ihc.go.kr/hcyc/selectBbsNttView.do")
    assert "nttNo=12345" in top.detail_url


def test_egovframe_bbs_record_id():
    assert _board_record_id(
        "https://www.ihc.go.kr/hcyc/selectBbsNttView.do?key=598&bbsNo=6&nttNo=12345"
    ) == "12345"


def test_sscmc_rows_title_from_tit():
    """서대문: 앵커 내부 <p class='tit'> 가 제목(날짜/상태 라벨 배제), query-only href 절대화."""
    page = "https://cs.sscmc.or.kr/sdmcs/11?action=list&page=2"
    posts = sscmc_rows(SSCMC, page)
    assert len(posts) == 3
    gayoje = [p for p in posts if is_gayoje(p.title)]
    assert [p.title for p in gayoje] == ["[북아현아트홀] 희망시대! 2025 서대문구민 가요제"]
    g = gayoje[0]
    # query-only 상대 href 는 목록 경로(/sdmcs/11) 기준으로 절대화
    assert g.detail_url.startswith("https://cs.sscmc.or.kr/sdmcs/11?action=read")
    assert "action-value=10610.0" in g.detail_url
    assert _board_record_id(g.detail_url) == "10610"


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


@pytest.mark.asyncio
async def test_ingest_board_posts_persists_detail_fields(db_ready):
    """상세 보강 필드(일정/장소/주최)가 FestivalEvent 로 저장되고, 나중에 채워지면 update."""
    from datetime import date

    from sqlalchemy import select

    from app.api.v1.festivals.models import FestivalEvent
    from app.api.v1.ingestion.service import ingest_board_posts

    url = "https://www.dongjak.go.kr/portal/bbs/B0000173/view.do?nttId=10736698"
    # 1차: 목록만(상세 전) → 필드 NULL
    async with db_ready.session_scope() as s:
        await ingest_board_posts(s, "egov:dongjak", [{"title": "제29회 노들가요제", "detail_url": url}])
    # 2차: 상세 보강값 포함 → update 로 필드 채움
    enriched = [{
        "title": "제29회 노들가요제", "detail_url": url,
        "start_date": "2026-03-14", "end_date": "2026-03-28",
        "venue": "동작문화복지센터 소강당", "host_org": "동작문화원",
    }]
    async with db_ready.session_scope() as s:
        counts = await ingest_board_posts(s, "egov:dongjak", enriched)
    assert counts["updated"] == 1  # 해시 변화 → update

    async with db_ready.session_scope() as s:
        row = await s.scalar(select(FestivalEvent).where(FestivalEvent.source_record_id == "10736698"))
    assert row.start_date == date(2026, 3, 14)
    assert row.end_date == date(2026, 3, 28)
    assert row.venue == "동작문화복지센터 소강당"
    assert row.host_org == "동작문화원"
