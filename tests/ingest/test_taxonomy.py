"""공급 상한 검증용 택소노미 회귀 — 고재현율 분류 + 정규화/회차/시도."""
from __future__ import annotations

import pytest

from app.api.v1.ingestion.taxonomy import (
    SIDO_17,
    classify,
    dedup_key,
    extract_round,
    extract_sido,
    normalize_name,
)


@pytest.mark.parametrize(
    "title,expected_bucket",
    [
        ("제29회 노들가요제 예선", "가요제"),
        ("KBS 전국노래자랑 안산편", "노래자랑"),
        ("제15회 창작동요제", "동요제"),
        ("서대문구민 합창대회", "합창·중창대회"),
        ("청소년 보컬 오디션 대회", "보컬경연"),
        ("남도 트로트 경연대회", "트로트경연"),
        ("OO 송페스티벌 2025", "송페스티벌"),
    ],
)
def test_classify_expanded_buckets(title, expected_bucket):
    assert classify(title) == expected_bucket


def test_classify_broad_music_plus_contest():
    # 강버킷 미해당이나 가창어+경연어 → 광의 버킷(재현율 확장분).
    assert classify("청소년 가요 경연 한마당").startswith("가창+경연")


@pytest.mark.parametrize(
    "title",
    ["신년 음악회", "군립합창단 정기연주회", "미술 전시회", "관현악 연주회", ""],
)
def test_classify_rejects_non_contest(title):
    # 연주회/전시회 등 '경연 아님'은 재현율을 넓혀도 배제(합창 '연주회'도 대회 아님).
    assert classify(title) is None


@pytest.mark.parametrize(
    "title,n",
    [("제29회 노들가요제", 29), ("제 3 회 동요제", 3), ("2025 나루 동요제", None),
     ("전국노래자랑", None)],
)
def test_extract_round(title, n):
    assert extract_round(title) == n


@pytest.mark.parametrize(
    "addr,sido",
    [
        ("서울특별시 동작구 장승배기로10길 42", "서울"),
        ("경상북도 봉화군 봉화읍", "경북"),
        ("강원특별자치도 화천군 화천읍", "강원"),
        ("전북특별자치도 군산시 백토로", "전북"),
        ("제주특별자치도 제주시", "제주"),
        ("세종특별자치시 한누리대로", "세종"),
        ("충청북도 청주시 서원구", "충북"),
        (None, None),
        ("", None),
    ],
)
def test_extract_sido(addr, sido):
    assert extract_sido(addr) == sido
    assert sido is None or sido in SIDO_17


def test_normalize_name_strips_round_year_bracket():
    assert normalize_name("제1회봉화글로벌가요제(2차예선)") == "봉화글로벌가요제"
    assert normalize_name("전국노래자랑<경기도 안산시편>") == "전국노래자랑"
    assert normalize_name("2025 나루 동요제") == "나루동요제"


def test_dedup_key_merges_franchise_rounds():
    a = dedup_key("제29회 노들가요제", "서울특별시 동작구 x", "동작문화원")
    b = dedup_key("제28회 노들가요제", "서울특별시 동작구 y", "동작문화원")
    assert a == b  # 회차만 다른 같은 프랜차이즈 → 1건
    c = dedup_key("제1회 통영가요제", "경상남도 통영시", "통영시")
    assert c != a  # 다른 가요제
