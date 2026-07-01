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


def get_board(name: str) -> BoardConfig | None:
    return BOARD_CONFIGS.get(name)


def list_boards() -> list[str]:
    return list(BOARD_CONFIGS.keys())
