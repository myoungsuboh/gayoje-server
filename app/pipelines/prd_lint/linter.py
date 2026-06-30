"""PRD lint 핵심 구현 — 정규식 + 키워드 기반."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List


SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


@dataclass
class PrdLintIssue:
    code: str
    severity: str               # error / warning / info
    message: str                # 한국어 한 줄
    hint: str = ""              # 사용자가 어떻게 고치는지 (구체 액션)
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PrdLintReport:
    score: float                # 0.0 ~ 1.0
    issues: List[PrdLintIssue] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=dict)


# ─── 정규식 패턴 ────────────────────────────────────────────────────────

# Story 표기 — "[Story 1.1]" / "Story 1.1" / "Story-01.1" / "## Story 1.1" 등
# [2026-06-01 fix] 선행 경계에 `*`·백틱 추가. PRD 합성/autofix 가 권장 형식인
# `**[Story 1.1] 기능명**`(bold) 으로, Screen 참조는 `` `[Story 1.1]` ``(backtick) 으로
# 출력하는데, 기존 `(?:^|\s)` 는 `[Story` 바로 앞의 `*`/백틱을 공백으로 안 봐서 매칭
# 실패 → 스토리 13개를 0개로 오판("Story 0개 — 기능 명세 없음" 거짓 경고 + 충실도 저평가).
# 장식 문자(* `)도 경계로 허용해 bold/backtick 형식을 인식한다.
_STORY_PATTERN = re.compile(
    r"(?:^|[\s*`])(?:\[?Story[\s\-]?(\d+)\.(\d+)\]?|##\s+Story[\s\-]?(\d+)\.(\d+))",
    re.IGNORECASE | re.MULTILINE,
)

# [2026-05-26 P2] Section 2/3 reconcile 검사용 — ### N. 형태 헤더.
_SECTION_HEADER_PATTERN = re.compile(r"^###\s+(\d+)[\.\s]", re.MULTILINE)

# Overview / 개요 / Product Overview / 시스템 개요
_OVERVIEW_KEYWORDS = re.compile(
    r"(Product\s+Overview|Overview|개요|시스템\s*개요|제품\s*개요)",
    re.IGNORECASE,
)

# NFR / 비기능 / Non-Functional / 응답 시간 / 동시 사용자 / 가용성
_NFR_KEYWORDS = re.compile(
    r"(NFR|Non[\-\s]?Functional|비기능|응답\s*시간|동시\s*사용자|가용성|"
    r"availability|performance|scalability|99\.\d+%)",
    re.IGNORECASE,
)

# 인증 / 권한 — JWT / OAuth / 인증 / 권한 / RBAC
_AUTH_KEYWORDS = re.compile(
    r"(JWT|OAuth|인증|권한|RBAC|authentication|authorization|session|token)",
    re.IGNORECASE,
)

# 에러 케이스 — HTTP 4xx/5xx 코드 또는 한국어 키워드.
# \b word boundary 로 "500ms" 같은 false positive 차단.
_ERROR_KEYWORDS = re.compile(
    r"(\b4\d\d\b|\b5\d\d\b|권한\s*없음|검증\s*실패|찾을\s*수\s*없|미존재|"
    r"잘못된\s*(요청|입력)|만료|invalid|forbidden|not\s*found|unauthorized)",
    re.IGNORECASE,
)

# Story 본문의 입력 키워드
_INPUT_KEYWORDS = re.compile(
    r"(입력|요청|body|payload|파라미터|필드|input|request|args)",
    re.IGNORECASE,
)

# Story 본문의 출력 키워드
_OUTPUT_KEYWORDS = re.compile(
    r"(출력|응답|결과|반환|output|response|returns|yields)",
    re.IGNORECASE,
)

# 검증 표현 — 비교 연산자, 한국어 필수/제약, regex 등
# [2026-06-10 어휘 계약] 이 lint 의 hint 예시('username 4~20자')가 자기 정규식을
# 통과하지 못하는 자기모순이 있었다 — autofix 가 hint 대로 충실히 보완해도 같은
# 이슈가 영원히 남아 충실도가 100% 에 도달 불가('보완이 안 되네?' 이탈 원인).
# 범위(4~20)·길이 단위(8자/20글자/64바이트) 표현을 검증으로 인정. 하이픈 범위
# (\d+-\d+)는 날짜(2026-06-10)·버전 오탐 때문에 의도적으로 제외.
_VALIDATION_KEYWORDS = re.compile(
    r"(>=?|<=?|필수|선택|최대|최소|이상|이하|미만|초과|"
    r"len\s*[<>=]|regex|enum|required|optional|nullable|min\s*[<>=]?|max\s*[<>=]?|"
    r"\d+\s*[~∼〜]\s*\d+|\d+\s*(?:자|글자|문자|바이트|byte))",
    re.IGNORECASE,
)


# ─── Story 추출 ─────────────────────────────────────────────────────────


def _split_sections_by_number(text: str) -> Dict[int, str]:
    """### N. 헤더 기준으로 markdown 을 섹션별로 분할 (PRD 구조 가정).

    Returns:
        {1: "Section 1 본문", 2: "...", ...}. 헤더 없으면 빈 dict.

    [2026-05-26 P2 — Section 2/3 reconcile lint] PRD lint 가 Section 2 (Epic Map)
    와 Section 3 (Screen Architecture) 의 Story 정합성을 비교할 때 사용.
    """
    if not text:
        return {}
    matches = list(_SECTION_HEADER_PATTERN.finditer(text))
    if not matches:
        return {}
    out: Dict[int, str] = {}
    for idx, m in enumerate(matches):
        n = int(m.group(1))
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        out[n] = text[start:end]
    return out


def _normalize_story_ids_in_text(text: str) -> set:
    """텍스트에서 Story 패턴을 찾아 정규화 set 반환 ('Story-XX.Y')."""
    out: set = set()
    for m in _STORY_PATTERN.finditer(text):
        epic = (m.group(1) or m.group(3) or "").lstrip("0") or "0"
        story = (m.group(2) or m.group(4) or "").lstrip("0") or "0"
        out.add(f"Story-{int(epic):02d}.{int(story)}")
    return out


def _extract_story_bodies(text: str) -> List[Dict[str, str]]:
    """Story X.Y 시작 ~ 다음 Story 시작 또는 다음 섹션 전까지의 본문 추출.

    Returns: [{"id": "Story-01.1", "body": "..."}, ...]
    """
    # 모든 Story 시작 위치 찾기
    matches = list(_STORY_PATTERN.finditer(text))
    if not matches:
        return []

    bodies: List[Dict[str, str]] = []
    for i, m in enumerate(matches):
        # Story id 정규화 (Story-XX.Y)
        epic = (m.group(1) or m.group(3) or "").lstrip("0") or "0"
        story = (m.group(2) or m.group(4) or "").lstrip("0") or "0"
        story_id = f"Story-{int(epic):02d}.{int(story)}"

        start = m.end()
        # 다음 Story 또는 다음 "## " 헤더 또는 문서 끝
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            # 다음 헤더 (#) 찾기 — 없으면 끝
            tail = text[start:]
            header_match = re.search(r"\n#", tail)
            end = start + header_match.start() if header_match else len(text)

        body = text[start:end].strip()
        bodies.append({"id": story_id, "body": body})

    # [2026-06-01] 같은 Story ID 가 Section 2(정의)와 Section 3(화면의 `[Story X.Y]` 참조)에
    # 모두 등장하면, #106(백틱 매칭) 이후 둘 다 추출돼 짧은 "화면 참조 줄"이 "본문 너무 짧음"
    # false positive 를 냈다(autofix 는 '보완할 것 없음'인데 lint 만 경고 → 사용자 혼란).
    # ID별로 가장 긴 본문(=실제 정의)만 남겨 참조 줄을 제외한다. distinct 카운트라 점수 정합도 ↑.
    by_id: Dict[str, Dict[str, str]] = {}
    for b in bodies:
        cur = by_id.get(b["id"])
        if cur is None or len(b["body"]) > len(cur["body"]):
            by_id[b["id"]] = b
    return list(by_id.values())


# ─── 메인 lint ──────────────────────────────────────────────────────────


# [2026-05-28] 각 issue code 가 어느 FE 탭(섹션) 으로 사용자를 안내할지 매핑.
# detail['target_section'] 로 노출 → PrdLintBadge 의 '보러가기' 링크가 해당 탭으로
# switchSection(). 사용자가 PRD 어디를 손봐야 하는지 한 번에 점프.
_SECTION_OVERVIEW = "overview"
_SECTION_EPIC = "epic"
_SECTION_SCREEN = "screen"
_SECTION_NFR = "nfr"

_SECTION_LABEL = {
    _SECTION_OVERVIEW: "Overview 탭",
    _SECTION_EPIC: "Epic & Story 탭",
    _SECTION_SCREEN: "Screens 탭",
    _SECTION_NFR: "NFR 탭",
}


def _issue(
    code: str, severity: str, message: str, hint: str,
    target_section: str = "", **extra_detail: Any,
) -> PrdLintIssue:
    """detail 에 target_section 자동 주입한 PrdLintIssue 빌더.

    [2026-05-28] hint 가 추상적 ('변환 LLM 이 핵심 입력') 이라 사용자가 어디를 손볼지
    모르겠다는 피드백. 각 issue 가 어느 탭으로 안내할지 명시 → FE 가 '보러가기' 노출.
    """
    detail = {"target_section": target_section} if target_section else {}
    detail.update(extra_detail)
    return PrdLintIssue(
        code=code, severity=severity, message=message, hint=hint, detail=detail,
    )


_MIN_BYTES = 500
_PENALTY_PER_ERROR = 0.20
_PENALTY_PER_WARNING = 0.08
_PENALTY_PER_INFO = 0.02

# [2026-06-01] per-story INFO(STORY_NO_INPUT/OUTPUT/VALIDATION/TOO_ABSTRACT)는 스토리
# 마다 1~3개씩 나와, 개수로 선형 합산하면 "스토리가 많을수록 점수가 낮아지는" 역설을
# 만든다(스토리 13개 = 37 INFO → 0.02*37 = 0.74 감점 → 26%). 같은 품질이면 스토리 2개든
# 13개든 점수가 같아야 공정하다. 그래서 per-story gap 은 개수 합산이 아니라 "gap 비율
# (gaps / (stories*3))"로 환산해 이 상한 안에서만 감점한다. 문서 단위 INFO 는 기존대로.
_STORY_GAP_CODES = frozenset({
    "STORY_NO_INPUT", "STORY_NO_OUTPUT", "STORY_NO_VALIDATION", "STORY_TOO_ABSTRACT",
})
_MAX_STORY_GAP_PENALTY = 0.12  # 모든 스토리가 spec gap 이어도 최대 12%만 감점 (스토리 수 무관)


def lint_prd(text: str) -> PrdLintReport:
    """PRD 텍스트 lint. 점수 + 위반 list 반환.

    score 계산:
      - 1.0 에서 시작
      - error 1건당 0.20 감점
      - warning 1건당 0.08 감점
      - 문서 단위 info 1건당 0.02 감점
      - per-story gap info(입력/출력/검증/추상)는 개수 합산이 아니라 "gap 비율
        × 0.12 상한" 으로 감점 → 스토리가 많아도(풍부한 PRD) 점수가 부당하게
        폭락하지 않게 (스토리 수 무관, 비율 기반)
      - 0.0 floor
    """
    if not isinstance(text, str):
        text = str(text or "")

    issues: List[PrdLintIssue] = []

    # 1) PRD_TOO_SHORT
    if len(text.encode("utf-8")) < _MIN_BYTES:
        issues.append(_issue(
            code="PRD_TOO_SHORT", severity=SEVERITY_ERROR,
            message=f"PRD 전체 내용이 너무 짧음 ({len(text.encode('utf-8'))} / 최소 {_MIN_BYTES} 자)",
            hint="4개 탭(Overview/Epic & Story/Screens/NFR) 본문을 모두 작성하세요. "
                 "각 탭 우측 상단 '편집' 으로 직접 입력 또는 회의록을 추가해 재생성.",
            target_section=_SECTION_OVERVIEW,
            size=len(text.encode("utf-8")), min=_MIN_BYTES,
        ))

    # 2) PRD_NO_OVERVIEW
    if not _OVERVIEW_KEYWORDS.search(text):
        issues.append(_issue(
            code="PRD_NO_OVERVIEW", severity=SEVERITY_WARNING,
            message="Overview 섹션이 비어있음 (제품 비전·역할 정의 없음)",
            hint="Overview 탭에 'Product Vision: ...', 'Success Metrics: ...', "
                 "'Role A: ...' 3가지를 한 줄씩 작성. 예: 'Product Vision: 사용자가 회의록을 PRD 로 변환'.",
            target_section=_SECTION_OVERVIEW,
        ))

    # 3) PRD_NO_STORY
    stories = _extract_story_bodies(text)
    if not stories:
        issues.append(_issue(
            code="PRD_NO_STORY", severity=SEVERITY_ERROR,
            message="Story 0개 — 기능 명세 없음",
            hint="Epic & Story 탭에 '**[Story 1.1] 기능명**' 형식으로 각 기능을 작성. "
                 "예: '**[Story 1.1] 사용자가 회의록 파일을 업로드한다**'.",
            target_section=_SECTION_EPIC,
        ))

    # 4) PRD_NO_NFR
    if not _NFR_KEYWORDS.search(text):
        issues.append(_issue(
            code="PRD_NO_NFR", severity=SEVERITY_WARNING,
            message="NFR (비기능 요구사항) 섹션이 비어있음",
            hint="NFR 탭에 측정 가능한 수치 추가. 예: '응답시간 500ms 이하', "
                 "'가용성 99.9%', '동시 사용자 100명'.",
            target_section=_SECTION_NFR,
        ))

    # 5) PRD_NO_AUTH
    if not _AUTH_KEYWORDS.search(text):
        issues.append(_issue(
            code="PRD_NO_AUTH", severity=SEVERITY_WARNING,
            message="로그인/권한 방식이 명시되지 않음",
            hint="NFR 탭에 인증 방식 한 줄 추가. 예: 'Google OAuth 로그인' 또는 "
                 "'ID/PW + JWT 토큰', 'RBAC owner/admin/viewer'.",
            target_section=_SECTION_NFR,
        ))

    # 6) PRD_NO_ERROR_CASE
    if not _ERROR_KEYWORDS.search(text):
        issues.append(_issue(
            code="PRD_NO_ERROR_CASE", severity=SEVERITY_WARNING,
            message="에러 상황 (실패 케이스) 명세가 없음",
            hint="Epic & Story 탭의 각 Story 본문에 실패 케이스 추가. 예: "
                 "'권한 없으면 401', '데이터 없으면 404', '입력 검증 실패 시 422'.",
            target_section=_SECTION_EPIC,
        ))

    # [2026-05-26 P2] 6.5) PRD_S2_S3_STORY_MISMATCH —
    # Section 2 (Epic Map) 의 Story 정의 vs Section 3 (Screen Architecture) 의
    # Story 참조 불일치. 다음 케이스가 SPACK underextract 의 가장 흔한 root cause.
    #   Section 3 에서 [Story-02.1] 참조 → Section 2 엔 Epic-01/Story-01.1 만 정의
    #   → SPACK 이 Story 1개만 보고 API 1~2개 추출.
    # cleanup_master_prd 도 이 mismatch 가 잡혀야 cleanup 책임 명확해짐.
    sections = _split_sections_by_number(text)
    if 2 in sections and 3 in sections:
        s2_stories = _normalize_story_ids_in_text(sections[2])
        s3_stories = _normalize_story_ids_in_text(sections[3])
        missing = sorted(s3_stories - s2_stories)
        if missing:
            preview = ", ".join(missing[:5])
            more = f" 외 {len(missing) - 5}개" if len(missing) > 5 else ""
            issues.append(_issue(
                code="PRD_S2_S3_STORY_MISMATCH", severity=SEVERITY_WARNING,
                message=(
                    f"Screens 탭이 참조하는 Story {len(missing)}개가 "
                    f"Epic & Story 탭에 정의되지 않음: {preview}{more}"
                ),
                hint=(
                    "상단 'AI 로 정리' 버튼을 클릭하면 자동으로 정합성 맞춰줍니다. "
                    "수동 수정 시: Epic & Story 탭에 누락된 Story 를 추가."
                ),
                target_section=_SECTION_EPIC,
                s2_story_count=len(s2_stories),
                s3_story_count=len(s3_stories),
                missing_in_s2=missing[:50],
                missing_count=len(missing),
            ))

    # 7) Story 별 lint
    for s in stories:
        body = s["body"]
        # 본문이 50자 미만이면 너무 추상적
        if len(body) < 50:
            issues.append(_issue(
                code="STORY_TOO_ABSTRACT", severity=SEVERITY_INFO,
                message=f"{s['id']} 본문이 너무 짧음 ({len(body)}자)",
                hint=f"Epic & Story 탭의 {s['id']} 본문에 입력/출력/제약 추가. "
                     "예: '- 입력: 사용자명, 비밀번호 - 출력: JWT 토큰 - 제약: 비밀번호 8자 이상'.",
                target_section=_SECTION_EPIC,
                story_id=s["id"], body_length=len(body),
            ))
            continue

        has_input = bool(_INPUT_KEYWORDS.search(body))
        has_output = bool(_OUTPUT_KEYWORDS.search(body))
        has_validation = bool(_VALIDATION_KEYWORDS.search(body))

        if not has_input:
            issues.append(_issue(
                code="STORY_NO_INPUT", severity=SEVERITY_INFO,
                message=f"{s['id']}: 입력 명세 없음",
                hint=f"Epic & Story 탭의 {s['id']} 본문에 '- 입력: ...' 한 줄 추가. "
                     "예: '- 입력: { username: 문자열, password: 8자 이상 }'.",
                target_section=_SECTION_EPIC,
                story_id=s["id"],
            ))
        if not has_output:
            issues.append(_issue(
                code="STORY_NO_OUTPUT", severity=SEVERITY_INFO,
                message=f"{s['id']}: 출력/응답 명세 없음",
                hint=f"Epic & Story 탭의 {s['id']} 본문에 '- 출력: ...' 한 줄 추가. "
                     "예: '- 출력: { token: JWT 문자열, expiresIn: 3600 }'.",
                target_section=_SECTION_EPIC,
                story_id=s["id"],
            ))
        if not has_validation:
            issues.append(_issue(
                code="STORY_NO_VALIDATION", severity=SEVERITY_INFO,
                message=f"{s['id']}: 검증 규칙 없음",
                hint=f"Epic & Story 탭의 {s['id']} 본문에 제약 한 줄 추가. "
                     "예: 'username 4~20자', 'password 8자 이상', 'enum: A | B | C'.",
                target_section=_SECTION_EPIC,
                story_id=s["id"],
            ))

    # ─ 점수 계산 ─
    errors = sum(1 for i in issues if i.severity == SEVERITY_ERROR)
    warnings = sum(1 for i in issues if i.severity == SEVERITY_WARNING)
    infos = sum(1 for i in issues if i.severity == SEVERITY_INFO)

    # [2026-06-01] per-story gap INFO 는 개수 합산 대신 "gap 비율 × 상한" 으로 감점.
    # 스토리가 많아도(풍부한 PRD) 점수가 부당하게 폭락하지 않도록 — 같은 품질이면
    # 스토리 2개든 13개든 동일 감점. 문서 단위 INFO 는 기존대로 1건당 감점.
    story_gap_infos = sum(1 for i in issues if i.code in _STORY_GAP_CODES)
    other_infos = infos - story_gap_infos
    n_stories = len(stories)
    if n_stories > 0:
        # 스토리당 input/output/validation 3종 기준 gap 비율 (0~1).
        gap_ratio = min(1.0, story_gap_infos / (n_stories * 3))
        story_gap_penalty = gap_ratio * _MAX_STORY_GAP_PENALTY
    else:
        story_gap_penalty = 0.0

    score = max(
        0.0,
        1.0
        - errors * _PENALTY_PER_ERROR
        - warnings * _PENALTY_PER_WARNING
        - other_infos * _PENALTY_PER_INFO
        - story_gap_penalty,
    )

    return PrdLintReport(
        score=round(score, 4),
        issues=issues,
        summary={
            "errors": errors,
            "warnings": warnings,
            "infos": infos,
            "stories_found": len(stories),
            "size_bytes": len(text.encode("utf-8")),
        },
    )
