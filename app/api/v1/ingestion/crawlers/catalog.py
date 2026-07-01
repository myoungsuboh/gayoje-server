"""온보딩된 지자체 게시판 카탈로그 (INGEST-E3-T4).

각 보드는 robots 허용 + httpx 도달 확인 후 등록한다. 사이트마다 URL 스킴/HTML 이 달라
parse_rows 를 스킴에 맞춰 지정(사이트별 오버라이드). 1차 공공 출처(지자체 공식 홈) only.

온보딩 절차: robots.txt 허용 확인 → 정직 UA+Accept 헤더로 목록 도달(200) 확인 →
목록 행 스킴 파악(view.do?nttId= / ?idx=&amode=view 등) → parse_rows 선택 → 등록.
"""
from __future__ import annotations

from app.api.v1.ingestion.crawlers.board import (
    BoardConfig,
    egovframe_rows,
    query_idx_rows,
    sscmc_rows,
)

BOARD_CONFIGS: dict[str, BoardConfig] = {}


def _register(cfg: BoardConfig) -> None:
    BOARD_CONFIGS[cfg.name] = cfg


# 동작구 문화행사 게시판(eGovFrame 갤러리형) — 노들가요제 등 게시.
# robots 허용, 정직 봇 UA + Accept/Referer 로 HTTP 200 확인.
_register(
    BoardConfig(
        name="dongjak_culture",
        source_system="egov:dongjak",
        base_url="https://www.dongjak.go.kr",
        list_url=(
            "https://www.dongjak.go.kr/portal/bbs/B0000173/list.do"
            "?menuNo=201030&pageIndex={page}"
        ),
        parse_rows=egovframe_rows,
        max_pages=3,
    )
)

# 화천문화재단 청소년수련관 커뮤니티 게시판(eGovFrame selectBbsNttView?nttNo) —
# T&U 전국청소년가요제(제13~15회) 게재. robots 무규칙(허용), 정직 봇 UA 200.
# 가요제 글이 2~7페이지에 연도별 분포 → max_pages 넉넉히.
_register(
    BoardConfig(
        name="hwacheon_youth",
        source_system="egov:hwacheon",
        base_url="https://www.ihc.go.kr",
        list_url=(
            "https://www.ihc.go.kr/hcyc/selectBbsNttList.do"
            "?bbsNo=6&key=598&pageIndex={page}"
        ),
        parse_rows=egovframe_rows,
        max_pages=8,
    )
)

# 서대문문화체육회관 공연안내(전체일정) 보드(자체 CMS ?action=read&action-value) —
# 서대문구민가요제 게재. robots 전면 허용, 정직 봇 UA 200.
_register(
    BoardConfig(
        name="sscmc_events",
        source_system="egov:sscmc",
        base_url="https://cs.sscmc.or.kr",
        list_url="https://cs.sscmc.or.kr/sdmcs/11?action=list&page={page}",
        parse_rows=sscmc_rows,
        max_pages=5,
    )
)


def get_board(name: str) -> BoardConfig | None:
    return BOARD_CONFIGS.get(name)


def list_boards() -> list[str]:
    return list(BOARD_CONFIGS.keys())
