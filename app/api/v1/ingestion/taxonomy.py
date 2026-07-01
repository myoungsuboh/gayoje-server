"""가요제 공급 상한 검증용 택소노미 — 고재현율 분류 + 정규화 (INGEST 분석 전용).

is_gayoje(정밀 우선, 저장 필터)와 목적이 다르다: '공급의 진짜 상한'을 재려면 재현율을
최대화한 넓은 키워드 스윕이 필요하다. 여기 classify 는 합창·오디션·송페스티벌 등 경계까지
포함하고, 매칭된 키워드 버킷을 함께 반환해 '키워드별 기여'를 추적한다.

부수 유틸: 제N회/연도 파싱, 이름 정규화, 시도 추출 — 회차·연례성 분석과 17시도 갭맵에 사용.
분석·검증 목적일 뿐 저장 파이프라인은 여전히 is_gayoje(정밀)를 쓴다.
"""
from __future__ import annotations

import re
from typing import Optional

# ===== 17 시도 =====
SIDO_17 = [
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
]
# 주소 접두 → 표준 시도(긴 별칭이 짧은 것에 포함되지 않게 startswith 로 충분).
_SIDO_PREFIX = [
    ("서울", "서울"), ("부산", "부산"), ("대구", "대구"), ("인천", "인천"),
    ("광주", "광주"), ("대전", "대전"), ("울산", "울산"), ("세종", "세종"),
    ("경기", "경기"), ("강원", "강원"),
    ("충청북도", "충북"), ("충북", "충북"),
    ("충청남도", "충남"), ("충남", "충남"),
    ("전라북도", "전북"), ("전북", "전북"),
    ("전라남도", "전남"), ("전남", "전남"),
    ("경상북도", "경북"), ("경북", "경북"),
    ("경상남도", "경남"), ("경남", "경남"),
    ("제주", "제주"),
]


def extract_sido(address: Optional[str]) -> Optional[str]:
    """주소 문자열 → 표준 시도명. 미상이면 None."""
    if not address:
        return None
    a = address.strip()
    for prefix, sido in _SIDO_PREFIX:
        if a.startswith(prefix):
            return sido
    return None


# ===== 고재현율 키워드 택소노미 =====
# 단독 확정 버킷(우선순위 순) — (버킷 라벨, 트리거 문자열들).
_STRONG_BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("가요제", ("가요제", "가요대회", "가요큰잔치", "가요한마당")),
    ("노래자랑", ("노래자랑",)),
    ("노래대회", ("노래대회", "노래경연", "노래콘테스트")),
    ("동요제", ("동요제", "동요대회", "창작동요")),
    ("가요콘테스트", ("가요콘테스트", "가요콘써트", "가요선발")),
    ("송페스티벌", ("송페스티벌", "송페스티발", "가요페스티벌", "가요페스티발", "송페스타")),
    ("보컬경연", ("보컬경연", "보컬대회", "보컬콘테스트", "보컬오디션")),
    ("트로트경연", ("트로트가요제", "트롯가요제", "트로트대회", "트롯대회",
                 "트로트경연", "트롯경연", "트로트오디션", "트롯오디션",
                 "트로트콘테스트", "트롯콘테스트")),
    ("합창·중창대회", ("합창대회", "합창경연", "합창제", "합창축제", "중창대회", "중창경연")),
    ("가창오디션", ("가창오디션", "가창경연", "가창대회")),
]
# 조합 매칭용(가창어 AND 경연/축제어) — 위 버킷에 안 걸린 넓은 그물.
_MUSIC = (
    "가요", "노래", "보컬", "트로트", "트롯", "가창", "성악", "합창", "중창",
    "동요", "k팝", "케이팝", "판소리", "가곡", "싱어", "칠곡가곡",
)
_CONTEST = (
    "경연", "오디션", "콘테스트", "콩쿠르", "콩쿨", "선발대회", "챔피언",
    "그랑프리", "페스티벌", "열창", "대회", "자랑", "제전", "경창",
)


def classify(title: Optional[str]) -> Optional[str]:
    """제목이 가요제/노래대회류인지 고재현율 판정. 매칭 시 키워드 버킷 라벨, 아니면 None."""
    if not title:
        return None
    t = title.replace(" ", "")
    for label, triggers in _STRONG_BUCKETS:
        if any(k in t for k in triggers):
            return label
    music = next((m for m in _MUSIC if m in t), None)
    contest = next((c for c in _CONTEST if c in t), None)
    if music and contest:
        return f"가창+경연(광의:{music})"
    return None


# ===== 회차·이름 정규화 =====
_ROUND = re.compile(r"제?\s*(\d{1,3})\s*회")
_YEAR = re.compile(r"(19|20)\d{2}")
_BRACKET = re.compile(r"[<(\[「『【][^>)\]」』】]*[>)\]」』】]")
_NONWORD = re.compile(r"[^가-힣a-z0-9]")


def extract_round(title: Optional[str]) -> Optional[int]:
    """'제N회' → N. 없으면 None."""
    if not title:
        return None
    m = _ROUND.search(title)
    return int(m.group(1)) if m else None


def extract_years(title: Optional[str]) -> list[int]:
    if not title:
        return []
    return sorted({int(m.group(0)) for m in _YEAR.finditer(title)})


def normalize_name(title: Optional[str]) -> str:
    """회차·연도·괄호수식·공백·기호 제거해 프랜차이즈 이름으로 정규화(dedup 키용)."""
    if not title:
        return ""
    s = _ROUND.sub(" ", title)
    s = _YEAR.sub(" ", s)
    s = _BRACKET.sub(" ", s)          # <경기도 안산시편>, (2차예선) 등 제거
    s = s.lower()
    s = _NONWORD.sub("", s)           # 공백·기호 제거
    return s


def dedup_key(title: Optional[str], address: Optional[str], host: Optional[str]) -> tuple:
    """(정규화 이름, 시도, 정규화 주최) — 회차/연도 다른 같은 프랜차이즈를 1건으로."""
    name = normalize_name(title)
    sido = extract_sido(address) or "?"
    h = _NONWORD.sub("", (host or "").lower())[:10]  # 주최 느슨 정규화(변형 흡수)
    return (name, sido, h)
