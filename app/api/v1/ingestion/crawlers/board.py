"""지자체 게시판 크롤러 — 사이트별 설정 기반 (INGEST-E3-T3).

eGov/지자체 보드는 URL 스킴·HTML 이 사이트마다 달라 BoardConfig(사이트별 오버라이드)로
온보딩한다. robots 게이트(RobotsGate) 통과분만 fetch(재시도 http_get_text)하고, 목록에서
게시글(제목+상세URL)을 추출해 가요제 필터·카운트한다. 상세 본문(날짜/장소) 파싱은 후속.

법적/윤리: 1차 공공 출처(지자체) only, robots 준수 + crawl-delay 존중, 포스터/본문 재호스팅
안 함(제목·링크만 취득), 정직한 User-Agent. 캡차·차단 감지 시 우회하지 않고 중단.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from app.api.v1.ingestion.adapters.base import http_get_text, is_gayoje
from app.api.v1.ingestion.robots import RobotsGate

logger = logging.getLogger("gayoje.ingest")

# 정직한 봇 UA — Mozilla 호환 프리픽스 + 봇명 + 연락 URL(Googlebot 관용).
DEFAULT_UA = "Mozilla/5.0 (compatible; gayoje-bot/0.1; +https://gayoje.example/bot)"
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&nbsp;": " ",
}


def clean_text(raw: str) -> str:
    """앵커 내부 HTML → 순수 제목 텍스트(태그 제거·엔티티 복원·공백 정리)."""
    text = _TAG.sub(" ", raw)
    for ent, ch in _ENTITIES.items():
        text = text.replace(ent, ch)
    return _WS.sub(" ", text).strip()


@dataclass
class BoardPost:
    title: str
    detail_url: str
    posted_date: Optional[str] = None


# ---- 목록 행 파서(사이트 스킴별) ----
# 각 파서: (html, base_url) -> list[BoardPost]. 같은 글이 여러 앵커(썸네일+제목)로 나오는
# 갤러리형을 고려해, 상세ID별로 '텍스트가 있는' 앵커(=제목)를 채택한다.


def _rows_by_id(
    html: str,
    page_url: str,
    pattern: re.Pattern,
    title_of: Callable[[str], str] = clean_text,
) -> list[BoardPost]:
    """(href, id, inner) 3그룹 정규식으로 목록 행 추출. id별 최초의 '제목 있는' 앵커 채택.

    page_url = 목록 페이지 URL(상대 href 절대화 기준). 같은 글이 썸네일+제목 앵커로 중복돼도
    id 기준 1건, 제목 텍스트가 있는 앵커만 채택(갤러리형 썸네일 앵커 배제).
    """
    by_id: dict[str, BoardPost] = {}
    for m in pattern.finditer(html):
        href, rec_id, inner = m.group(1), m.group(2), m.group(3)
        title = title_of(inner)
        if not title or len(title) < 2 or rec_id in by_id:
            continue
        url = urljoin(page_url, href.replace("&amp;", "&"))
        by_id[rec_id] = BoardPost(title=title, detail_url=url)
    return list(by_id.values())


# eGovFrame 표준 게시판: view.do?nttId (동작구형) + selectBbsNttView.do?nttNo (화천형) 모두 매칭.
_EGOV_PAT = re.compile(
    r'href="([^"]*(?:view\.do[^"]*?nttId|selectBbsNttView\.do[^"]*?nttNo)=(\d+)[^"]*)"'
    r"[^>]*>(.*?)</a>",
    re.I | re.S,
)
_BADGE = re.compile(r"핫이슈|\bNEW\b|\bHOT\b")


def _egov_title(inner: str) -> str:
    return _WS.sub(" ", _BADGE.sub("", clean_text(inner))).strip()


def egovframe_rows(html: str, page_url: str) -> list[BoardPost]:
    """eGovFrame 보드: <a href='...view.do?nttId=N | ...selectBbsNttView.do?nttNo=N'>제목</a>.

    동작구(갤러리 view.do?nttId) + 화천(selectBbsNttView.do?nttNo) 공용. '핫이슈' 배지 제거.
    """
    return _rows_by_id(html, page_url, _EGOV_PAT, _egov_title)


_QUERY_IDX_PAT = re.compile(
    r'href="(\?[^"]*idx=(\d+)[^"]*amode=view[^"]*)"[^>]*>(.*?)</a>', re.I | re.S
)


def query_idx_rows(html: str, page_url: str) -> list[BoardPost]:
    """통영식 .web 보드: <a href='?gcode=..&idx=N&amode=view'>제목</a>."""
    return _rows_by_id(html, page_url, _QUERY_IDX_PAT)


# 서대문문화체육회관(sscmc) 자체 CMS: <a href='?action=read&action-value=N.0'>…<p class="tit">제목</p></a>
_SSCMC_PAT = re.compile(
    r'href="([^"]*action=read[^"]*?action-value=(\d+)\.0[^"]*)"[^>]*>(.*?)</a>',
    re.I | re.S,
)
_SSCMC_TIT = re.compile(r'<p class="tit">(.*?)</p>', re.S)


def _sscmc_title(inner: str) -> str:
    m = _SSCMC_TIT.search(inner)
    return clean_text(m.group(1)) if m else ""


def sscmc_rows(html: str, page_url: str) -> list[BoardPost]:
    """서대문문화체육회관 보드: 앵커 내부 <p class='tit'> 가 제목(날짜/상태 라벨과 분리)."""
    return _rows_by_id(html, page_url, _SSCMC_PAT, _sscmc_title)


@dataclass
class BoardConfig:
    name: str                  # 온보딩 식별자 (예: dongjak_culture)
    source_system: str         # provenance (예: egov:dongjak)
    base_url: str              # 절대 base (예: https://www.dongjak.go.kr)
    list_url: str              # {page} 치환 목록 URL 템플릿(절대)
    parse_rows: Callable[[str, str], list[BoardPost]]
    max_pages: int = 2
    page_pause_sec: float = 1.0


async def crawl_board(
    config: BoardConfig,
    *,
    gate: RobotsGate,
    client: Any = None,
) -> dict:
    """설정된 보드를 robots 게이트 하에 크롤 → 가요제 필터·카운트.

    반환: {board, source_system, crawled, gayoje, pages, blocked, posts:[{title,detail_url}]}.
    """
    import httpx

    owns = client is None
    # 일부 지자체 WAF 는 브라우저형 Accept/Referer 헤더가 없으면 응답을 보류(timeout)한다.
    # 정직한 봇 UA 는 유지하되(위장 아님) 표준 Accept/Referer 를 함께 보낸다.
    client = client or httpx.AsyncClient(
        timeout=45, follow_redirects=True,
        headers={"User-Agent": gate.user_agent, "Referer": config.base_url, **DEFAULT_HEADERS},
    )
    posts: list[BoardPost] = []
    pages = 0
    blocked = False
    try:
        for page in range(1, config.max_pages + 1):
            url = config.list_url.format(page=page)
            if not await gate.allowed(client, url):
                logger.warning("robots 불허 — 크롤 중단: %s", url)
                blocked = True
                break
            try:
                html = await http_get_text(client, url, timeout_sec=45)
            except Exception as e:  # noqa: BLE001 — 사이트별 장애는 해당 보드만 스킵
                logger.warning("보드 fetch 실패(%s) %s", type(e).__name__, url)
                break
            rows = config.parse_rows(html, url)  # url=목록 페이지(상대 href 절대화 기준)
            if not rows:
                break
            posts.extend(rows)
            pages += 1
            delay = await gate.crawl_delay(client, url)
            await asyncio.sleep(delay if delay is not None else config.page_pause_sec)
    finally:
        if owns:
            await client.aclose()

    # detail_url 기준 중복 제거 후 가요제 필터.
    uniq: dict[str, BoardPost] = {}
    for p in posts:
        uniq.setdefault(p.detail_url, p)
    gayoje = [p for p in uniq.values() if is_gayoje(p.title)]
    return {
        "board": config.name,
        "source_system": config.source_system,
        "crawled": len(uniq),
        "gayoje": len(gayoje),
        "pages": pages,
        "blocked": blocked,
        "posts": [{"title": p.title, "detail_url": p.detail_url} for p in gayoje],
    }
