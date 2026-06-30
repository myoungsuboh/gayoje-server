"""
PRD lint 단위 테스트.

빈약 PRD 가 reject 되는지 + 충실 PRD 가 통과하는지 + 각 lint rule 별
회귀 보호. LLM 호출 없음 — 결정적.
"""
from __future__ import annotations

from pathlib import Path

from app.pipelines.prd_lint import (
    PrdLintReport,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    lint_prd,
)


def _codes(report: PrdLintReport):
    return [i.code for i in report.issues]


# ─── 빈/너무 짧은 PRD ───────────────────────────────────────────────────


def test_empty_prd_yields_too_short_error():
    report = lint_prd("")
    assert "PRD_TOO_SHORT" in _codes(report)
    assert any(i.severity == SEVERITY_ERROR for i in report.issues)
    assert report.score < 0.5


def test_short_prd_below_500_bytes():
    report = lint_prd("# Overview\n짧은 PRD 입니다.")
    assert "PRD_TOO_SHORT" in _codes(report)


def test_none_input_safe():
    """잘못된 입력 (None/숫자) 도 raise 없이 처리."""
    report = lint_prd(None)
    assert isinstance(report, PrdLintReport)
    assert report.score < 0.5


# ─── 섹션 부재 ──────────────────────────────────────────────────────────


def test_no_overview_warns():
    text = "x" * 600 + "\n[Story 1.1] 무엇무엇\n입력: foo, 출력: bar, 필수"
    report = lint_prd(text)
    assert "PRD_NO_OVERVIEW" in _codes(report)


def test_no_story_is_error():
    text = "# Overview\n" + "내용 " * 200 + "\n# NFR\nOAuth 2.0 + 권한\n응답 시간 500ms"
    report = lint_prd(text)
    assert "PRD_NO_STORY" in _codes(report)


def test_bold_and_backtick_story_format_recognized():
    """[2026-06-01 회귀] PRD 합성/autofix 가 내는 `**[Story 1.1] 제목**`(bold) +
    Screen 참조 `` `[Story 1.1]` ``(backtick) 형식을 lint 가 인식해야 한다.

    이전 `(?:^|\\s)` 앵커는 `[Story` 바로 앞의 `*`/백틱을 경계로 안 봐서, 스토리가
    13개 있어도 0개로 오판 → "PRD_NO_STORY" 거짓 경고 + 충실도 저평가했다.
    """
    text = (
        "## 🗺️ Master PRD 조감도\n"
        "### 1. Product Overview\n" + "통합 비전 " * 60 + "\n"
        "### 2. Epic & User Story Map\n"
        "#### 📦 Epic 1: 모니터링\n"
        "- **[Story 1.1] 실시간 에이전트 상태 조회** 입력: 상태, 출력: 표시, 필수 검증\n"
        "- **[Story 1.2] 작업 진행률 시각화** 입력: 진행률, 출력: 바, 필수 검증\n"
        "### 3. Screen Architecture\n"
        "#### 🖥️ [Screen: 대시보드]\n"
        "- 포함된 기능: `[Story 1.1]`, `[Story 1.2]`\n"
        "### 4. NFR\nOAuth 2.0 권한\n응답 시간 500ms\n에러 4xx 처리"
    )
    report = lint_prd(text)
    assert "PRD_NO_STORY" not in _codes(report), (
        f"bold/backtick Story 형식을 인식 못함: {_codes(report)}"
    )


def test_per_story_gap_score_independent_of_story_count():
    """[2026-06-01] per-story spec gap 점수는 스토리 '수'가 아니라 'gap 비율' 기반.

    같은 품질(모든 스토리가 입력/출력/검증 미명시)이면 스토리 2개든 13개든 점수가
    비슷해야 한다 — 이전엔 개수 선형 합산이라 13개 PRD 가 26%까지 폭락(풍부한 PRD 역차별).
    """
    def make(n: int) -> str:
        s = (
            "## PRD 조감도\n"
            "### 1. Product Overview\n- **통합 비전**: 통합 관리 플랫폼.\n"
            "### 2. Epic & User Story Map\n#### 📦 Epic 1: 모니터링\n"
        )
        for i in range(1, n + 1):
            s += (
                f"- **[Story 1.{i}] 기능 {i}** 사용자가 화면에서 동작을 처리하는 "
                "과정을 자세히 풀어 적은 충분히 긴 본문 문장입니다 어쩌고 저쩌고.\n"
            )
        s += "### 4. Global NFR\nOAuth 2.0 권한, 응답 500ms 이하, 에러 401/404 처리.\n"
        return s

    score_small = lint_prd(make(2)).score
    score_large = lint_prd(make(13)).score
    assert abs(score_small - score_large) < 0.05, (
        f"스토리 수에 따라 점수가 크게 달라짐(역차별): 2개={score_small} vs 13개={score_large}"
    )
    # 스토리가 많고 per-story gap 이 있어도 점수가 0.5 미만으로 폭락하면 안 됨.
    assert score_large > 0.7, f"13개 스토리 PRD 점수가 부당하게 낮음: {score_large}"


def test_section3_story_reference_not_linted_as_short_story():
    """[2026-06-01] Section 3 화면의 `[Story X.Y]` 참조 줄을 '본문 너무 짧음'으로 오판하지
    않는다 — 정의는 Section 2 에 충분히 있으므로. #106 백틱 매칭의 부작용(중복 추출) 회귀 가드.

    실사용 증상: autofix 는 "보완할 것 없음"인데 lint 는 "Story-01.1 본문 34자" 경고(모순).
    34자는 화면 참조 "실시간 상태 조회 (from Epic 1)" 였음.
    """
    text = (
        "## PRD\n### 1. Product Overview\n- **통합 비전**: 플랫폼.\n"
        "### 2. Epic & User Story Map\n#### 📦 Epic 1\n"
        "- **[Story 1.1] 실시간 상태 조회** 사용자는 대시보드에서 등록된 에이전트의 실시간 "
        "상태를 확인하여 가용성을 즉시 파악한다. 입력: 없음, 출력: 상태 목록, 필수.\n"
        "### 3. Screen Architecture\n#### 🖥️ [Screen: 대시보드]\n"
        "- 포함된 기능:\n  - `[Story 1.1]` 실시간 상태 조회 (from Epic 1)\n"
        "### 4. NFR\nOAuth 2.0, 응답 500ms, 에러 401/404\n"
    )
    report = lint_prd(text)
    abstract = [i for i in report.issues if i.code == "STORY_TOO_ABSTRACT"]
    assert abstract == [], (
        f"화면 참조를 짧은 스토리로 오판함: {[i.message for i in abstract]}"
    )


def test_no_nfr_warns():
    text = "# Overview\n" + "x" * 600 + "\n[Story 1.1] 등록\n입력: x, 출력: y, 필수"
    report = lint_prd(text)
    # NFR 키워드 없음 → WARNING
    assert "PRD_NO_NFR" in _codes(report)


def test_no_auth_warns():
    text = (
        "# Overview\n" + "x" * 600
        + "\n[Story 1.1] 등록\n입력 출력 필수\n응답 시간 500ms"
    )
    report = lint_prd(text)
    assert "PRD_NO_AUTH" in _codes(report)


def test_no_error_case_warns():
    """5xx HTTP 코드 또는 한국어 에러 키워드 부재 → WARNING.
    "500ms" 같은 false positive 는 \\b word boundary 로 제외."""
    text = (
        "# Overview\n" + "x" * 600
        + "\n[Story 1.1] 등록\n입력 출력 필수\nOAuth 2.0 응답 시간 500ms"
    )
    report = lint_prd(text)
    assert "PRD_NO_ERROR_CASE" in _codes(report)


def test_error_case_503_word_boundary_recognized():
    """\\b 로 둘러싼 5xx 는 정상 매치."""
    text = (
        "# Overview\n" + "x" * 600
        + "\n[Story 1.1] 등록 동작이 길게 적힌 본문 입력 출력 필수\n"
        + "\n# NFR\nOAuth + JWT, 응답 시간 500ms, 503 Service Unavailable 처리"
    )
    report = lint_prd(text)
    assert "PRD_NO_ERROR_CASE" not in _codes(report)


# ─── Story 별 lint ──────────────────────────────────────────────────────


def test_story_extraction_multiple_formats():
    """다양한 Story 표기 형식 인식."""
    text = """# Overview

매우 긴 overview 텍스트 ............................................
............................................................................
............................................................................
............................................................................

[Story 1.1] 첫 번째 스토리
- 입력: foo, 필수
- 출력: bar

## Story 2.3 두 번째 스토리
- 입력: baz, 필수
- 출력: qux

Story-3.5 세 번째 표기

# NFR
OAuth + JWT, 응답 시간 500ms, 401/403 처리
"""
    report = lint_prd(text)
    # 3개 Story 모두 인식
    assert report.summary["stories_found"] == 3


def test_story_no_input_emits_info():
    """Story body 에 입력 키워드 부재 → INFO."""
    text = (
        "# Overview\n" + "x" * 600
        + "\n[Story 1.1] 사용자가 본인의 식물 정보를 자세히 조회한다 "
        + "결과는 화면에 표시된다 출력: 식물 데이터 반환되어야 함 필수 항목"
        + "\n# NFR\nOAuth + JWT, 응답 시간 500ms, 401 처리"
    )
    report = lint_prd(text)
    codes = _codes(report)
    assert "STORY_NO_INPUT" in codes


def test_story_no_output_emits_info():
    """Story body 에 출력 키워드 부재 → INFO."""
    text = (
        "# Overview\n" + "x" * 600
        + "\n[Story 1.1] 사용자는 신규 식물을 시스템에 등록한다 등록 동작은 즉시 반영 "
        + "입력 항목: 식물 이름과 종, 필수\n"
        + "\n# NFR\nOAuth + JWT, 응답 시간 500ms, 401 처리"
    )
    report = lint_prd(text)
    assert "STORY_NO_OUTPUT" in _codes(report)


def test_story_no_validation_emits_info():
    """Story body 에 검증 키워드 부재 → INFO."""
    text = (
        "# Overview\n" + "x" * 600
        + "\n[Story 1.1] 사용자는 신규 항목을 시스템에 등록하고 결과를 받는다 "
        + "입력으로 이름을 받아 처리하고 출력으로 식별자가 반환된다 동작 끝\n"
        + "\n# NFR\nOAuth + JWT, 응답 시간 500ms, 401 처리"
    )
    report = lint_prd(text)
    assert "STORY_NO_VALIDATION" in _codes(report)


def test_story_too_abstract():
    text = (
        "# Overview\n" + "x" * 600
        + "\n[Story 1.1] 짧음\n"
        + "\n# NFR\nOAuth + JWT, 응답 시간 500ms, 401 처리"
    )
    report = lint_prd(text)
    assert "STORY_TOO_ABSTRACT" in _codes(report)


# ─── PRD_S2_S3_STORY_MISMATCH (2026-05-26 P2) ────────────────────────────


def test_s2_s3_story_mismatch_emits_warning():
    """
    AI Agent 케이스 재현: Section 2 (Epic Map) 엔 Story-01.1 만, Section 3 (Screens)
    엔 Story-01.1 / 01.2 / 02.1 / 03.1 / 04.1 참조 → mismatch 4개 warning.
    """
    prd = """## 🗺️ Master PRD

### 1. Product Overview
- **통합 비전**: AI Agent 자동화

### 2. Epic & User Story Map
#### 📦 [Epic-01] 사용자 질의 응답
- [Story 1.1] 사용자는 AI 에게 질문하여 답을 얻는다. 입력: 질문, 출력: 답변, 필수.

### 3. Screen Architecture
#### 🖥️ [Screen: 대시보드]
- 포함된 기능: [Story 1.1], [Story 1.2], [Story 2.1]
#### 🖥️ [Screen: 작업 관리]
- 포함된 기능: [Story 3.1], [Story 4.1]

### 4. Global Non-Functional Requirements
- 성능: 응답 3초 이내, 동시 사용자 1000명
- 보안: OAuth 2.0 + JWT, 401/403 처리
"""
    report = lint_prd(prd)
    codes = _codes(report)
    assert "PRD_S2_S3_STORY_MISMATCH" in codes, (
        f"S2/S3 mismatch lint 빠짐. issues: {codes}"
    )
    # detail 검증
    mismatch_issue = next(
        i for i in report.issues if i.code == "PRD_S2_S3_STORY_MISMATCH"
    )
    assert mismatch_issue.severity == SEVERITY_WARNING
    assert mismatch_issue.detail["missing_count"] == 4
    assert "Story-01.2" in mismatch_issue.detail["missing_in_s2"]
    assert "Story-02.1" in mismatch_issue.detail["missing_in_s2"]
    assert "Story-03.1" in mismatch_issue.detail["missing_in_s2"]
    assert "Story-04.1" in mismatch_issue.detail["missing_in_s2"]


def test_s2_s3_consistent_no_mismatch_warning():
    """Section 2 가 Section 3 의 모든 Story 참조를 cover 하면 mismatch 룰 안 뜸."""
    prd = """## 🗺️ Master PRD

### 1. Product Overview
- **통합 비전**: 자동화 플랫폼

### 2. Epic & User Story Map
#### 📦 [Epic-01] 인증
- [Story 1.1] 로그인 처리 동작. 입력 받고 출력 반환. 필수 검증.
- [Story 1.2] 로그아웃 처리 동작. 입력 받고 출력 반환. 필수 검증.
#### 📦 [Epic-02] 작업
- [Story 2.1] 작업 생성 동작. 입력 받고 출력 반환. 필수 검증.

### 3. Screen Architecture
#### 🖥️ [Screen: 로그인]
- 포함된 기능: [Story 1.1], [Story 1.2]
#### 🖥️ [Screen: 대시보드]
- 포함된 기능: [Story 2.1]

### 4. Global Non-Functional Requirements
- 성능 3초, OAuth 2.0, 401/403 처리
"""
    report = lint_prd(prd)
    assert "PRD_S2_S3_STORY_MISMATCH" not in _codes(report)


def test_s2_s3_mismatch_skipped_when_sections_missing():
    """Section 2 또는 Section 3 헤더가 없으면 mismatch 룰 자체 스킵."""
    prd_no_section_3 = (
        "## Master PRD\n\n"
        "### 1. Product Overview\n비전\n\n"
        "### 2. Epic & User Story Map\n"
        "[Story 1.1] 로그인 동작. 입력 출력 필수.\n\n"
        "### 4. Global Non-Functional Requirements\n"
        + "x" * 600 + "\n응답 3초, OAuth, 401"
    )
    report = lint_prd(prd_no_section_3)
    # Section 3 없으므로 mismatch 룰 안 뜸 (다른 룰은 평가됨)
    assert "PRD_S2_S3_STORY_MISMATCH" not in _codes(report)


# ─── 점수 단조성 ────────────────────────────────────────────────────────


def test_issue_carries_target_section_for_fe_navigation():
    """각 lint issue 가 detail['target_section'] 으로 FE 탭 경로를 안내.

    [2026-05-28] '사용자가 어디를 손볼지 모르겠다' 피드백 → FE PrdLintBadge 가
    '보러가기' 링크 띄울 수 있게 issue 마다 어느 탭(overview/epic/screen/nfr)
    으로 안내할지 명시.
    """
    # 빈 PRD → PRD_TOO_SHORT + PRD_NO_OVERVIEW + PRD_NO_STORY + PRD_NO_NFR + PRD_NO_AUTH + PRD_NO_ERROR_CASE
    report = lint_prd("")
    by_code = {i.code: i for i in report.issues}
    expected_sections = {
        "PRD_TOO_SHORT": "overview",
        "PRD_NO_OVERVIEW": "overview",
        "PRD_NO_STORY": "epic",
        "PRD_NO_NFR": "nfr",
        "PRD_NO_AUTH": "nfr",
        "PRD_NO_ERROR_CASE": "epic",
    }
    for code, expected_section in expected_sections.items():
        assert code in by_code, f"{code} 누락"
        target = by_code[code].detail.get("target_section")
        assert target == expected_section, f"{code}: 기대 {expected_section}, 실제 {target}"


def test_hint_messages_are_concrete_with_examples():
    """hint 메시지가 추상적 jargon ('LLM 입력', 'Policy 도출') 대신 구체 예시 포함.

    [2026-05-28] 사용자 피드백: '솔직히 나도 잘 모르겠어' — 사용자가 어디를 어떻게
    손봐야 할지 한 줄에서 파악 가능하도록 모든 hint 에 '예: ...' 또는 탭 이름 포함.
    """
    report = lint_prd("")
    by_code = {i.code: i for i in report.issues}
    # 추상 jargon 금지 토큰
    forbidden_jargon = ("LLM", "Policy 도출", "Request body schema", "attribute.constraint")
    for code, issue in by_code.items():
        hint_lower = issue.hint
        for jargon in forbidden_jargon:
            assert jargon not in hint_lower, f"{code} hint 에 jargon '{jargon}' 잔존: {issue.hint!r}"
        # 모든 hint 가 구체 예시 또는 탭 이름 포함
        has_example = "예:" in issue.hint or "예시:" in issue.hint
        has_tab_name = any(t in issue.hint for t in ("Overview", "Epic & Story", "Screens", "NFR"))
        assert has_example or has_tab_name, (
            f"{code} hint 가 구체적이지 않음 (예/탭 둘 다 없음): {issue.hint!r}"
        )


def test_story_specific_issue_carries_target_section_epic():
    """Story 별 issue (STORY_NO_INPUT 등) 도 target_section='epic' 표시."""
    text = "# Overview\n" + "x" * 600 + "\n[Story 1.1] " + "x" * 60
    report = lint_prd(text)
    story_issue_codes = {"STORY_NO_INPUT", "STORY_NO_OUTPUT", "STORY_NO_VALIDATION"}
    story_issues = [i for i in report.issues if i.code in story_issue_codes]
    assert story_issues, "Story-specific issue 가 적어도 1개 발생해야 함"
    for issue in story_issues:
        assert issue.detail.get("target_section") == "epic", (
            f"{issue.code} target_section 누락 또는 잘못: {issue.detail!r}"
        )
        assert issue.detail.get("story_id"), f"{issue.code} story_id 누락"


def test_score_monotonic_empty_to_full():
    """빈 PRD < 부분 < 완전 PRD 점수 단조 증가."""
    empty_score = lint_prd("").score
    partial = (
        "# Product Overview\n" + "x" * 600
        + "\n[Story 1.1] 등록\n입력: x\n"
        + "\n# NFR\n응답 시간 500ms"
    )
    partial_score = lint_prd(partial).score
    full_score = lint_prd(_full_plant_prd()).score
    assert empty_score < partial_score < full_score


def test_full_plant_prd_high_score():
    """plant fixture 의 충실 PRD 는 점수 80%+ (warning 거의 없음)."""
    report = lint_prd(_full_plant_prd())
    assert report.score >= 0.80


# ─── 실 fixture 채점 ───────────────────────────────────────────────────


def test_evals_plant_prd_input_passes_lint():
    """evals/scenarios/plant/prd_input.md 가 lint 통과."""
    prd_path = (
        Path(__file__).resolve().parent.parent.parent
        / "evals"
        / "scenarios"
        / "plant"
        / "prd_input.md"
    )
    if not prd_path.exists():
        return  # fixture 없으면 skip (재배치된 경우 대비)
    text = prd_path.read_text(encoding="utf-8")
    report = lint_prd(text)
    # 충실히 작성된 PRD 는 0.70 이상
    assert report.score >= 0.70, (
        f"plant PRD 가 lint score {report.score} — "
        f"issues: {[i.code for i in report.issues]}"
    )


# ─── 픽스처 ─────────────────────────────────────────────────────────────


def _full_plant_prd() -> str:
    return """
# Product Overview
식물 모니터링 시스템.

# Epic & Story Map

## Epic 01: 식물 정보 관리

[Story 1.1] 사용자는 식물을 등록할 수 있다.
- 입력: name (필수, 최대 100자), species (선택)
- 출력: 생성된 식물 id 반환
- 권한: 인증된 사용자만

[Story 1.2] 사용자는 식물을 조회할 수 있다.
- 입력: plantId 필수
- 출력: 식물 정보 응답
- 권한: 본인 소유만 (403 차단)
- 식물 미존재 시 404

# Non-Functional Requirements
- 응답 시간 500ms 이내
- OAuth 2.0 + JWT 인증
- 401/403/404/422 에러 케이스 명시
- 동시 사용자 100명, 가용성 99.9%
"""


# ─── [2026-06-10] 검증 어휘 계약 — lint hint 예시가 자기 정규식을 통과해야 한다 ──


def _story_text(body: str) -> str:
    return (
        "# Overview\n" + "x" * 600
        + f"\n[Story 1.1] 사용자 등록 기능 {body} 입력으로 이름을 받아 처리하고 "
        + "출력으로 식별자가 반환된다 동작 끝\n"
        + "\n# NFR\nOAuth + JWT, 응답 시간 500ms, 401 처리"
    )


def test_validation_range_tilde_passes():
    """hint 예시 'username 4~20자' 가 검증으로 인정돼야 한다 (자기모순 해소)."""
    report = lint_prd(_story_text("제약은 username 4~20자 형식"))
    assert "STORY_NO_VALIDATION" not in _codes(report)


def test_validation_length_unit_passes():
    """길이 단위(8자/64바이트) 표현도 검증으로 인정."""
    report = lint_prd(_story_text("비밀번호는 8자 형식으로 제한"))
    assert "STORY_NO_VALIDATION" not in _codes(report)


def test_validation_date_alone_still_flags():
    """날짜(2026-06-10)는 검증 아님 — 하이픈 범위를 의도적으로 제외한 오탐 방지 확인."""
    report = lint_prd(_story_text("기한은 2026-06-10 까지 진행"))
    assert "STORY_NO_VALIDATION" in _codes(report)
