"""
LintResult CRUD — runLint 결과 저장 + getLastLintResult 조회.

[스테이지 매핑]
- Save LintResult / Prepare Lint Save → `save_lint_result`
- getLastLintResult / Get Last LintResult / Format Lint Result → `get_last_lint_result`

[Cypher 호환성]
cases 는 base64 로 인코딩해 Cypher 이스케이프 회피.
기존 데이터 호환 + 큰 JSON 안전.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.clients import neo4j_client
from app.core.project_scope import scoped_project

logger = logging.getLogger(__name__)


class LintEvidence(BaseModel):
    """
    rule 한 건이 코드에 적용된 근거 — file:line:snippet 인용.

    [목적]
      lint 점수의 신뢰성 + 사용자 검증 가능성.
      과거: LLM 만 봐서 "왜 applied=true 인지" 추적 불가 (블랙박스).
      현재: 매칭된 파일과 줄을 그대로 노출 → 사용자가 클릭해서 GitHub 으로 이동 검증.

    [필드]
      file:    'src/api/refund.py'  — repo 내 상대 경로 (FE 가 GitHub URL 빌드용)
      line:    42  — 1-based 라인 번호. 0 이면 line-less (예: manifest 매칭)
      snippet: "@router.post('/refund')"  — 매칭된 줄 (최대 200자 잘림)
      kind:    'endpoint'  — 매칭 종류 (FE 가 배지로 표시).
               'endpoint' | 'class' | 'manifest' | 'context_dir' | 'event_class' |
               'event_publish' | 'policy_token' | 'rule_token' | 'llm_quoted'
    """

    file: str
    line: int = 0
    snippet: str = ""
    kind: str = ""


class LintCaseRule(BaseModel):
    rule: str           # 예: 'api:POST /tickets' / 'entity:Ticket'
    description: str    # 사용자에게 보일 한글/영문 설명
    applied: bool       # True = 코드에 적용됨, False = 미적용

    # evidence: 코드 grep / LLM 인용이 찾은 file:line:snippet 목록.
    # 비어있으면 결정적 매칭 + LLM 검증 둘 다 실패 (applied=False 와 함께).
    evidence: List[LintEvidence] = []

    # detection_method: 어떻게 applied 가 결정됐는지. FE 가 신뢰도 배지로 표시.
    #   'deterministic' — 코드 grep 으로 직접 매칭 (가장 신뢰도 ↑)
    #   'llm'           — LLM residual pass 가 sample 본문 보고 인용
    #   'fallback'      — 둘 다 실패. 항상 applied=False 와 함께.
    detection_method: str = "deterministic"


class LintCase(BaseModel):
    title: str
    convergence: int
    rules: List[LintCaseRule] = []


class LintResult(BaseModel):
    """runLint 정규화 결과 + getLastLintResult 응답 공용."""

    id: Optional[str] = None
    project: Optional[str] = None
    github_url: Optional[str] = None
    score: int = 0
    scanned_files: int = 0
    # [Sprint1 1.2] 커버리지 정직화. scanned_files 는 레거시 의미(레포 전체 코드
    # 파일 수)를 유지하고, 실제로 본문을 가져와 검사한 파일 수는 sampled_files 로
    # 분리 노출한다. coverage_truncated=True 면 전체 중 일부만 검사한 것 →
    # FE 가 "전체 N개 중 M개 샘플 검사" 경고를 띄워 점수의 한계를 알린다.
    total_code_files: int = 0
    sampled_files: int = 0
    coverage_truncated: bool = False
    rules_checked: int = 0
    violations: int = 0
    cases: List[LintCase] = []
    saved_at: Optional[int] = None
    error: Optional[str] = None


_SAVE_LINT_CYPHER = """\
CREATE (l:LintResult {
    id: $id,
    project: $project,
    githubUrl: $github_url,
    score: $score,
    scannedFiles: $scanned_files,
    totalCodeFiles: $total_code_files,
    sampledFiles: $sampled_files,
    coverageTruncated: $coverage_truncated,
    rulesChecked: $rules_checked,
    violations: $violations,
    cases: $cases_b64,
    savedAt: $saved_at
})
RETURN l.id AS saved_id
"""


_GET_LAST_LINT_CYPHER = """\
MATCH (l:LintResult)
WHERE l.project = $project AND l.githubUrl = $github_url
RETURN l {
    .id, .project, .githubUrl, .score, .scannedFiles,
    .totalCodeFiles, .sampledFiles, .coverageTruncated, .rulesChecked,
    .violations, .cases, .savedAt
} AS lint
ORDER BY l.savedAt DESC
LIMIT 1
"""

# [T7] github_url 없이 project 만으로 최신 검증 결과 (빌드 검증 환류용).
_GET_LAST_LINT_BY_PROJECT_CYPHER = """\
MATCH (l:LintResult)
WHERE l.project = $project
RETURN l {
    .id, .project, .githubUrl, .score, .scannedFiles,
    .totalCodeFiles, .sampledFiles, .coverageTruncated, .rulesChecked,
    .violations, .cases, .savedAt
} AS lint
ORDER BY l.savedAt DESC
LIMIT 1
"""


def _encode_cases(cases: List[LintCase]) -> str:
    payload = [c.model_dump() for c in cases]
    return base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def _decode_cases(b64: str) -> List[LintCase]:
    if not b64:
        return []
    try:
        raw = base64.b64decode(b64).decode("utf-8")
        items = json.loads(raw)
        return [LintCase(**i) for i in items if isinstance(i, dict)]
    except Exception as e:  # noqa: BLE001
        logger.warning("LintResult cases base64 decode 실패: %s", e)
        return []


async def save_lint_result(
    project: str,
    github_url: str,
    score: int,
    scanned_files: int,
    rules_checked: int,
    violations: int,
    cases: List[LintCase],
    total_code_files: int = 0,
    sampled_files: int = 0,
    coverage_truncated: bool = False,
    team_id: str = "",
) -> str:
    """Neo4j 저장 + 새 id 반환."""
    project = scoped_project(project, team_id)
    lint_id = f"lint-{project}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    await neo4j_client.run_cypher(
        _SAVE_LINT_CYPHER,
        {
            "id": lint_id,
            "project": project,
            "github_url": github_url,
            "score": int(score),
            "scanned_files": int(scanned_files),
            "total_code_files": int(total_code_files),
            "sampled_files": int(sampled_files),
            "coverage_truncated": bool(coverage_truncated),
            "rules_checked": int(rules_checked),
            "violations": int(violations),
            "cases_b64": _encode_cases(cases),
            "saved_at": int(time.time() * 1000),
        },
    )
    return lint_id


def _row_to_lint_result(row: Dict[str, Any]) -> Optional[LintResult]:
    """LintResult 노드 row(dict) → LintResult. id 없으면 None. cases 는 base64 decode."""
    if not row or not row.get("id"):
        return None
    return LintResult(
        id=row.get("id"),
        project=row.get("project"),
        github_url=row.get("githubUrl"),
        score=int(row.get("score") or 0),
        scanned_files=int(row.get("scannedFiles") or 0),
        # 레거시 레코드(필드 없음)는 scannedFiles 로 폴백 → 전체=샘플로 보수적 표기.
        total_code_files=int(
            row.get("totalCodeFiles")
            if row.get("totalCodeFiles") is not None
            else (row.get("scannedFiles") or 0)
        ),
        sampled_files=int(
            row.get("sampledFiles")
            if row.get("sampledFiles") is not None
            else (row.get("scannedFiles") or 0)
        ),
        coverage_truncated=bool(row.get("coverageTruncated") or False),
        rules_checked=int(row.get("rulesChecked") or 0),
        violations=int(row.get("violations") or 0),
        cases=_decode_cases(row.get("cases") or ""),
        saved_at=int(row["savedAt"]) if row.get("savedAt") is not None else None,
    )


async def get_last_lint_result(
    project: str, github_url: str, team_id: str = ""
) -> Optional[LintResult]:
    """가장 최근 LintResult 조회 (project + github_url). cases 는 base64 decode."""
    records = await neo4j_client.run_cypher(
        _GET_LAST_LINT_CYPHER,
        {"project": scoped_project(project, team_id), "github_url": github_url},
    )
    if not records:
        return None
    return _row_to_lint_result(records[0].get("lint") or {})


async def get_last_lint_for_project(
    project: str, team_id: str = ""
) -> Optional[LintResult]:
    """[T7] github_url 없이 프로젝트의 최신 LintResult — 빌드 검증 환류용.

    build_plan 은 repo URL 을 모르므로 project 만으로 최근 검증 결과를 읽어
    '코드에 없던 설계 항목'을 다음 플랜에 되먹인다.
    """
    records = await neo4j_client.run_cypher(
        _GET_LAST_LINT_BY_PROJECT_CYPHER,
        {"project": scoped_project(project, team_id)},
    )
    if not records:
        return None
    return _row_to_lint_result(records[0].get("lint") or {})
