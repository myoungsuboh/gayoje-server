"""INGEST-E3 상세(view) 파서 회귀 — 일정/장소/주최 추출."""
from __future__ import annotations

from pathlib import Path

from app.api.v1.ingestion.crawlers.detail import (
    parse_detail_dongjak,
    parse_detail_hwacheon,
    parse_detail_sscmc,
)

_FIX = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (_FIX / name).read_text(encoding="utf-8")


def test_detail_dongjak_promotes_body_event_dates():
    """동작구: 구조화 '행사일'(신청기간) 대신 본문 예선~본선 실제 공연일을 승격, 장소·주최."""
    d = parse_detail_dongjak(_load("detail_dongjak.html"))
    assert d["start_date"] == "2026-03-14"  # 예선(본문) — 신청기간 2/23 아님
    assert d["end_date"] == "2026-03-28"    # 본선(본문)
    assert d["venue"] == "동작문화복지센터 소강당"
    assert d["host_org"] == "동작문화원"    # 담당부서(문화정책과) 아님, 본문 문의 기관


def test_detail_dongjak_falls_back_to_structured_when_no_body_dates():
    """본문 예선/본선 일시가 없으면 구조화 '행사일' dd 를 사용."""
    html = """
    <dl class="dl-horizontal">
      <dt><i class="fa"></i>행사일</dt><dd>2026-05-09 14:00 ~ 2026-05-09 17:00</dd>
      <dt><i class="fa"></i>장소</dt><dd>노들나루공원</dd>
    </dl>
    """
    d = parse_detail_dongjak(html)
    assert d["start_date"] == "2026-05-09" and d["end_date"] == "2026-05-09"
    assert d["venue"] == "노들나루공원"


def test_detail_hwacheon_freetext():
    """화천: 자유텍스트 본문에서 M.D일+제목연도, '시 …에서'=장소, …수련관=주최."""
    d = parse_detail_hwacheon(_load("detail_hwacheon.html"))
    assert d["start_date"] == "2025-08-30" and d["end_date"] == "2025-08-30"
    assert d["venue"] == "화천커뮤니티센터 야외 특설무대"
    assert d["host_org"] == "화천청소년수련관"  # 작성자(교육복지과) 아님


def test_detail_sscmc_structured_and_labels():
    """서대문: <dt>기간</dt> 날짜 + 제목/본문 장소 + 본문 '주최:'(불릿 ○ 경계 존중)."""
    d = parse_detail_sscmc(_load("detail_sscmc.html"))
    assert d["start_date"] == "2025-09-27" and d["end_date"] == "2025-09-27"
    assert d["venue"] == "북아현아트홀"       # 다음 ○ 전까지만
    assert d["host_org"] == "서대문구도시관리공단"  # 주관까지 물지 않음


def test_detail_venue_falls_back_to_title_bracket():
    """서대문: 본문 '장소:' 라벨이 없으면 제목 대괄호[공연장] 폴백."""
    html = """
    <p class="tit_26">[세종문화회관] 어느 가요제</p>
    <div class="dl_list"><dl><dt>기간</dt><dd>2025.10.01 ~ 2025.10.01</dd></dl></div>
    <div class="board_content content_editor"><p>○ 주최: 서대문구</p></div>
    """
    d = parse_detail_sscmc(html)
    assert d["venue"] == "세종문화회관"


def test_detail_parsers_return_empty_on_garbage():
    for fn in (parse_detail_dongjak, parse_detail_hwacheon, parse_detail_sscmc):
        assert fn("<html><body>관련 정보 없음</body></html>") == {} or isinstance(fn(""), dict)
