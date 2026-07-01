"""상세(view) 페이지 필드 추출 — 보드별 parse_detail (INGEST-E3 상세 보강).

각 parse_detail(html) → {start_date,end_date,venue,host_org}(원시 문자열, 없으면 키 생략).
날짜는 ISO(YYYY-MM-DD) 문자열로 반환(저장 시 parse_date 로 date 변환).
사이트마다 구조가 달라(구조화 dl/dt · 자유텍스트 본문) 보드별 파서로 분리한다.

법적/윤리: 제목·일정·장소·주최·상세링크만 취득. 포스터/본문 전문·첨부 재호스팅 안 함.
"""
from __future__ import annotations

import re
from typing import Optional

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&apos;": "'",
}


def _text(fragment: str) -> str:
    """HTML 조각 → 순수 텍스트(태그 제거·엔티티 복원·공백 정리)."""
    t = _TAG.sub(" ", fragment)
    for ent, ch in _ENTITIES.items():
        t = t.replace(ent, ch)
    return _WS.sub(" ", t).strip()


def _iso(y, m, d) -> str:
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def _clean_val(s: str) -> str:
    return _WS.sub(" ", s).strip("  \t\r\n:·-")


# =========================== 동작구 (dongjak) ===========================
# 구조화 <dl class="dl-horizontal"> <dt>라벨</dt><dd>값</dd> + 본문 자유텍스트.
# '행사일' dd 는 '참가신청' 글에선 신청기간이라, 본문의 예선/본선/공연 실제일시를 우선 승격.

def _dj_dl(label: str) -> re.Pattern:
    return re.compile(
        r"<dt[^>]*>(?:<i[^>]*></i>)?\s*" + label + r"\s*</dt>\s*<dd[^>]*>\s*([^<]*?)\s*</dd>",
        re.I,
    )


_DJ_HAENGSA = _dj_dl("행사일")
_DJ_JANGSO = _dj_dl("장소")
_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
# 본문 '예선/본선/공연/행사 일시 : 2026.3.14.(토)' (마침표 구분, 요일 괄호)
_DJ_BODY_DATE = re.compile(
    r"(?:예선|본선|공연|행사)\s*일시[^:：\d]*[:：]?\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})"
)
# 주최 필드가 없어 본문 '문의' 기관명을 주최 프록시로(담당과 아닌 주최/주관 기관).
_DJ_HOST = re.compile(r"문\s*의\s*[:：]\s*([^<☎,\d\n(]+?)\s*(?:☎|\(|<|,|\d{2,3}\s*-|$)")


def parse_detail_dongjak(html: str) -> dict:
    out: dict[str, str] = {}
    # 1) 구조화 행사일(기본값).
    m = _DJ_HAENGSA.search(html)
    if m:
        ds = _ISO_DATE.findall(m.group(1))
        if ds:
            out["start_date"] = _iso(*ds[0])
            out["end_date"] = _iso(*ds[-1])
    # 2) 본문 실제 공연/예선/본선 일시가 있으면 승격(신청기간 오인 방지).
    body_dates = [_iso(y, mo, d) for y, mo, d in _DJ_BODY_DATE.findall(html)]
    if body_dates:
        body_dates.sort()
        out["start_date"] = body_dates[0]
        out["end_date"] = body_dates[-1]
    # 3) 장소.
    v = _DJ_JANGSO.search(html)
    if v and v.group(1).strip():
        out["venue"] = _clean_val(v.group(1))
    # 4) 주최 — 본문 문의/접수처 기관명.
    h = _DJ_HOST.search(_text(html))
    if h:
        out["host_org"] = _clean_val(h.group(1))
    return out


# =========================== 화천 (hwacheon) ===========================
# eGovFrame BBS, 본문 자유텍스트 한 문장에 일시·장소·주최가 녹아있음.
_HW_BODY = re.compile(r'<td[^>]*class="bbs_content"[^>]*>(.*?)</td>', re.S | re.I)
_HW_SUBJECT = re.compile(r'<tr class="subject"[^>]*>.*?<td[^>]*>(.*?)</td>', re.S | re.I)
_HW_WRITE = re.compile(r"작성일[^\d]{0,6}(20\d{2})[.\-](\d{1,2})[.\-](\d{1,2})")
_HW_ABS = re.compile(r"(20\d{2})\s*[.\-]\s*(\d{1,2})\s*[.\-]\s*(\d{1,2})")
_HW_MD = re.compile(r"(?<!\d)(\d{1,2})\s*[.\-]\s*(\d{1,2})\s*일")
_HW_MWD = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_HW_VENUE = re.compile(r"시\s+([가-힣A-Za-z0-9·\s]+?)에서")
_HW_HOST = re.compile(r"([가-힣]+(?:청소년수련관|문화재단|문화원|재단|시설관리공단))")


def _hw_year(html: str, body: str) -> Optional[str]:
    ym = re.search(r"(20\d{2})", body)
    if ym:
        return ym.group(1)
    ms = _HW_SUBJECT.search(html)
    if ms:
        y2 = re.search(r"(20\d{2})", _text(ms.group(1)))
        if y2:
            return y2.group(1)
    wd = _HW_WRITE.search(_text(html))
    return wd.group(1) if wd else None


def parse_detail_hwacheon(html: str) -> dict:
    out: dict[str, str] = {}
    mb = _HW_BODY.search(html)
    if not mb:
        return out
    body_html = re.sub(r'<div class="photo_area".*?</div>', " ", mb.group(1), flags=re.S | re.I)
    body_html = re.sub(r"<br\s*/?>", "\n", body_html, flags=re.I)
    body = _text(body_html)

    # 날짜: 절대연 포함형 > '월/일'형 > 'M.D일'형(+제목/작성일 연도).
    ma = _HW_ABS.search(body)
    if ma:
        out["start_date"] = out["end_date"] = _iso(*ma.groups())
    else:
        year = _hw_year(html, body)
        mmd = _HW_MWD.search(body) or _HW_MD.search(body)
        if mmd and year:
            out["start_date"] = out["end_date"] = _iso(year, mmd.group(1), mmd.group(2))

    v = _HW_VENUE.search(body)
    if v:
        out["venue"] = _WS.sub(" ", v.group(1)).strip()
    h = _HW_HOST.search(body)
    if h:
        out["host_org"] = h.group(1)
    return out


# =========================== 서대문 (sscmc) ===========================
# 구조화 <dt>기간</dt><dd> + 제목 대괄호[공연장] + 본문 '주최:' 라벨.
_SS_GIGAN = re.compile(r"<dt>\s*기간\s*</dt>\s*<dd>\s*([^<]+?)\s*</dd>", re.I)
_SS_DATE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")
_SS_TITLE = re.compile(r'<p class="tit_26">(.*?)</p>', re.S | re.I)
_SS_BRACKET = re.compile(r"\[([^\]]+)\]")
# 본문은 '○ 라벨: 값 ○ 라벨: 값' 불릿 나열 → 값은 다음 ○/◇ 전까지.
_SS_LABEL = lambda lbl: re.compile(lbl + r"\s*[:：]\s*([^○◇<\n]+?)\s*(?:○|◇|<|\n|$)")
_SS_VENUE_L = _SS_LABEL("장\\s*소")
_SS_HOST_L = _SS_LABEL("주\\s*최")


def parse_detail_sscmc(html: str) -> dict:
    out: dict[str, str] = {}
    m = _SS_GIGAN.search(html)
    if m:
        ds = _SS_DATE.findall(m.group(1))
        if ds:
            out["start_date"] = _iso(*ds[0])
            out["end_date"] = _iso(*ds[-1])
    # 본문 영역(에디터) 격리 후 라벨 파싱.
    idx = html.find('board_content content_editor')
    body = _text(html[idx:idx + 12000]) if idx >= 0 else ""
    v = _SS_VENUE_L.search(body)
    if v:
        out["venue"] = _clean_val(v.group(1))
    else:  # 제목 대괄호 폴백
        mt = _SS_TITLE.search(html)
        if mt:
            b = _SS_BRACKET.search(_text(mt.group(1)))
            if b:
                out["venue"] = _clean_val(b.group(1))
    h = _SS_HOST_L.search(body)
    if h:
        out["host_org"] = _clean_val(h.group(1))
    return out
