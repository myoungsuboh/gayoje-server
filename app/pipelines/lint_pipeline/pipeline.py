from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List, Optional

from app.clients.github_client import GitHubClient, GitHubError, filter_code_files
from app.pipelines.base import PipelineContext
from app.pipelines.lint_evidence import FileSample
from app.pipelines.lint_pipeline.evaluator import _build_cases, _compute_score, _empty_result
from app.pipelines.lint_pipeline.residual import _apply_residual_verdicts, _residual_llm_pass
from app.pipelines.lint_pipeline.sampler import (
    _extract_spec_tokens,
    _fetch_full_bodies,
    _select_sample_paths,
)
from app.pipelines.lint_pipeline.specs import _fetch_repo_tree, _fetch_specs, _parse_input
from app.pipelines.lint_pipeline.types import LintInput
from app.service.lint_repository import LintResult

logger = logging.getLogger(__name__)


def _summarize_non_code_repo(tree_response: Dict[str, Any]) -> str:
    """[2026-05-29] 코드 0개 repo 의 친절한 에러 메시지용 분포 요약.

    예: ".md 22개 / .yml 4개 / .png 4개 (총 31개 비코드 파일)".
    문서/명세 위주 repo 식별을 돕고 Onboard 흐름으로 안내하기 위함.
    """
    tree = (tree_response or {}).get("tree") or []
    ext_counter: Counter = Counter()
    total = 0
    for item in tree:
        if (item or {}).get("type") != "blob":
            continue
        path = item.get("path") or ""
        if not path:
            continue
        total += 1
        base = path.rsplit("/", 1)[-1]
        if "." in base:
            ext = "." + base.rsplit(".", 1)[-1].lower()
        else:
            ext = "(확장자 없음)"
        ext_counter[ext] += 1
    if not total:
        return ""
    top = ext_counter.most_common(3)
    parts = [f"{ext} {n}개" for ext, n in top]
    return f"{' / '.join(parts)} (총 {total}개 비코드 파일)"


async def run_lint_pipeline(
    ctx: PipelineContext,
    payload: LintInput,
    *,
    github_client: Optional[GitHubClient] = None,
    user_token: Optional[str] = None,
    save: bool = True,
    enable_residual_llm: bool = True,
) -> LintResult:
    logger.info(
        "lint pipeline start: project=%s github=%s key=%s",
        payload.project_name,
        payload.github_url,
        ctx.idempotency_key,
    )
    parsed = _parse_input(payload)

    # [멀티테넌시] SPACK/DDD/Arch spec fetch + lint 결과 저장은 스코프 키 기준
    # (개인=이름). result.project 표시는 깨끗한 이름 유지.
    from app.core.project_scope import scoped_project
    db_project = scoped_project(parsed["project_name"], payload.team_id)

    specs = await _fetch_specs(ctx, db_project)
    fetch_error: Optional[str] = None
    tree_response = {"tree": []}
    # 패키지 네임스페이스의 GitHubClient 를 lookup — test 가
    # app.pipelines.lint_pipeline.GitHubClient 를 monkeypatch 하기 때문.
    import app.pipelines.lint_pipeline as _pkg
    _GitHubClient = getattr(_pkg, "GitHubClient", GitHubClient)
    gh = github_client or _GitHubClient(user_token=user_token)
    try:
        tree_response = await _fetch_repo_tree(gh, parsed["_ident"])
    except GitHubError as e:
        logger.warning("lint pipeline GitHub error: %s", e)
        fetch_error = str(e)

    code_files = (
        filter_code_files({"tree": tree_response.get("tree") or []})
        if not fetch_error else []
    )
    if fetch_error is None and not code_files:
        # [2026-05-29] 친절한 에러 — 분포 요약 + Onboard 안내. 사용자가 "왜 안 돼?"
        # 보다 "이 repo 는 문서 위주라 Lint 가 아니라 Onboard 가 적합" 즉시 파악 가능.
        breakdown = _summarize_non_code_repo(tree_response)
        msg_parts = [
            "저장소에 분석 가능한 코드 파일이 없습니다 "
            "(지원 확장자: vue, ts, tsx, js, jsx, java, kt, py, go, rs, rb, php).",
        ]
        if breakdown:
            msg_parts.append(f"발견된 파일: {breakdown}.")
        msg_parts.append(
            "이 저장소가 문서·명세 위주이거나 미지원 언어라면 '시스템 그리기' 단계의 "
            "GitHub Onboard 로 시도해 보세요(코드 없이 문서·매니페스트로 프로젝트 시작 가능)."
        )
        fetch_error = " ".join(msg_parts)

    if fetch_error:
        return _empty_result(len(code_files), fetch_error)

    tokens = _extract_spec_tokens(specs)
    all_tree_files = tree_response.get("tree") or []
    target_files = _select_sample_paths(code_files, all_tree_files, tokens)

    samples: List[FileSample] = []
    try:
        samples = await _fetch_full_bodies(gh, parsed["_ident"], target_files)
    except GitHubError as e:
        logger.warning("lint sampler error (fallback to spec-only): %s", e)
        samples = []

    cases, residual_items = _build_cases(specs, samples)

    if enable_residual_llm and residual_items and samples:
        verdicts = await _residual_llm_pass(ctx, residual_items, samples)
        _apply_residual_verdicts(cases, verdicts, samples)

    score = _compute_score(cases)
    total_rules = sum(len(c.rules) for c in cases)
    total_violations = sum(1 for c in cases for r in c.rules if not r.applied)

    # [Sprint1 1.2] 커버리지 정직화. scanned_files 는 레거시 의미(전체 코드 파일
    # 수)를 유지하되, 실제로 본문을 fetch 해 검사한 파일은 samples 뿐이다. 전체보다
    # 적게 검사했으면 coverage_truncated=True → FE 가 점수의 한계를 사용자에게 고지.
    total_code = len(code_files)
    sampled = len(samples)
    result = LintResult(
        score=score,
        scanned_files=total_code,
        total_code_files=total_code,
        sampled_files=sampled,
        coverage_truncated=sampled < total_code,
        rules_checked=total_rules,
        violations=total_violations,
        cases=cases,
    )

    if save:
        from app.service import lint_repository

        result.id = await lint_repository.save_lint_result(
            project=db_project,
            github_url=parsed["github_url"],
            score=result.score,
            scanned_files=result.scanned_files,
            total_code_files=result.total_code_files,
            sampled_files=result.sampled_files,
            coverage_truncated=result.coverage_truncated,
            rules_checked=result.rules_checked,
            violations=result.violations,
            cases=result.cases,
        )
        result.project = parsed["project_name"]
        result.github_url = parsed["github_url"]

    return result
