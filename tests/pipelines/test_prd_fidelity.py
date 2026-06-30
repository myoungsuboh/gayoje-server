"""prd_fidelity — 고신뢰 신호 추출/양방향 대조 단위 테스트."""
from app.pipelines.prd_fidelity import extract_signals, compare_fidelity


def test_extracts_high_confidence_signals():
    sig = extract_signals("응답 2초 이내, 2026-04-30 미팅, SQL 최적화, 만족도 80%")
    vals = {v for _, v in sig}
    assert "2초" in vals
    assert "80%" in vals
    assert "2026-04-30" in vals
    assert "sql" in vals


def test_korean_common_nouns_excluded():
    # 한글 일반 명사는 형태소 분석 부재로 추출 안 함(노이즈 방지) — 신호 0.
    assert extract_signals("법인카드 정책 검토 회의 진행") == set()


def test_compare_missing_and_hallucination():
    meeting = "응답 2초, 80% 만족, PDF 내보내기"
    prd = "응답 2초, 99.9% 가용성, PDF 다운로드, RBAC 적용"
    r = compare_fidelity(meeting, prd)
    miss = {m["value"] for m in r["missing"]}
    hall = {h["value"] for h in r["hallucination"]}
    assert "80%" in miss                      # 회의록엔 있으나 PRD 에 없음(누락)
    assert "99.9%" in hall and "rbac" in hall  # PRD 엔 있으나 회의록에 없음(환각)
    assert r["fidelity_pct"] < 100


def test_empty_meeting_yields_full_pct():
    r = compare_fidelity("", "응답 2초 이내")
    assert r["fidelity_pct"] == 100
    assert r["meeting_signal_count"] == 0
    assert r["matched_count"] == 0


def test_perfect_reflection():
    r = compare_fidelity("응답 2초, PDF", "응답 2초, PDF 다운로드")
    assert r["fidelity_pct"] == 100
    assert not r["missing"]
