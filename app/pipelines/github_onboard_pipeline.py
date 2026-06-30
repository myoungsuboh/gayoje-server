"""
GitHub Onboard Pipeline — 회의록 없이 GitHub repo URL 만으로 V1 + CPS 자동 생성.

[배경 — 2026-05-26 Vibe Coding entry Phase 1]
회의를 안 하는 Vibe Coding 사용자 (Cursor / Claude Code / Cline 등) 가 시스템에 첫
진입할 때 회의록 업로드 진입장벽 차단. GitHub URL 한 줄 → AI 가 코드 분석 → V1
"프로젝트 설명" 자동 생성 → 기존 CPS pipeline 자연 합류.

[Stages]
1. parse + fetch — github URL → tree (기존 GitHubClient 재활용)
2. select files — D2 패턴 샘플링 (README + manifest + entry + 코드, 40개 한도)
3. LLM V1 — prompts/onboard_from_github.md 로 V1 markdown 생성
4. CPS pipeline 위임 — run_cps_pipeline(V1 markdown)
5. PRD pipeline 위임 — run_prd_pipeline(cps_graph) — postMeeting 과 동일 체이닝
   GitHub URL 한 번으로 V1 + CPS + PRD 완성 → 사용자가 바로 design 진입.

[원자성]
LLM 실패 시 다음 stage 진입 안 함 → DB 변경 없음. 각 stage(CPS/PRD)의 트랜잭션은
해당 pipeline 이 보장. onboard 의 추가 트랜잭션 없음.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.clients.github_client import (
    GitHubClient,
    GitHubError,
    RepoIdentifier,
    parse_github_url,
)
from app.pipelines.base import (
    PipelineContext,
    strip_code_blocks,
    strip_template_placeholders,
)
from app.pipelines.cps_pipeline import run_cps_pipeline
from app.pipelines.github_onboard_code_evidence import (
    extract_entry_signals,
    extract_manifest_facts,
    extract_repo_stats,
    format_code_evidence_block,
)
from app.pipelines.cps_pipeline.types import CpsInput, CpsResult
from app.pipelines.prd_pipeline import PrdInput, PrdResult, run_prd_pipeline

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


# ─── Config ─────────────────────────────────────────────────────────────────


def _int_env(key: str, default: int) -> int:
    """env 정수 파싱 — 잘못된 값은 default."""
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


# Lint 의 LINT_* env 패턴과 동일 default — token 한도 / 응답 일관성 유지.
# onboard 만의 특수성 (README 우선 등) 은 코드로 표현 (env 변수 신설 X).
_MAX_SAMPLE_FILES = _int_env("ONBOARD_MAX_SAMPLE_FILES", 40)
_PER_FILE_BYTES = _int_env("ONBOARD_PER_FILE_BYTES", 64_000)
_TOTAL_BUDGET_BYTES = _int_env("ONBOARD_TOTAL_BUDGET_BYTES", 400_000)

# Stage 3 V1 markdown 가드 — LLM 환각 / 빈 응답 차단.
# 200자 미만이면 5 sections 중 하나도 채워지지 않은 셈 → raise.
# 50000자 초과면 CPS pipeline 입력 한계 보호 (truncate + warning).
_MIN_V1_LENGTH = 200
_MAX_V1_LENGTH = 50_000


# ─── Schemas ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GithubOnboardInput:
    project_name: str
    github_url: str
    user_email: str  # quota / ownership tracking 용
    team_id: str = ""  # 멀티테넌시 스코프 (개인=빈 값)


@dataclass
class GithubOnboardResult:
    project_name: str
    github_url: str
    repo_full_name: str             # "owner/repo"
    v1_markdown: str
    v1_markdown_size: int
    sampled_file_count: int
    sampled_file_paths: List[str]   # 사용자/디버깅 용 — 어떤 파일 봤는지
    cps_result: Optional[CpsResult] = None
    # [2026-05-27] PRD 도 자동 생성 — postMeeting 흐름과 동일. GitHub URL 한 번으로
    # V1 + CPS + PRD 까지 완성되어 사용자가 바로 design 단계로 진행 가능.
    prd_result: Optional[PrdResult] = None
    diagnostic: Dict[str, Any] = field(default_factory=dict)


# ─── Stage 2: file selection (D2 sampling) ─────────────────────────────────


# 우선순위 1: README — V1 의 모든 section 의 원천.
_README_PATTERNS = [
    re.compile(r"^README\.md$", re.IGNORECASE),
    re.compile(r"^README\.rst$", re.IGNORECASE),
    re.compile(r"^README\.txt$", re.IGNORECASE),
    re.compile(r"^README$", re.IGNORECASE),
]

# 우선순위 2: 매니페스트 (기술 스택 추출).
_MANIFEST_FILES = {
    "package.json", "package-lock.json",
    "pyproject.toml", "requirements.txt", "Pipfile",
    "Cargo.toml", "Cargo.lock",
    "go.mod", "go.sum",
    "build.gradle", "build.gradle.kts", "pom.xml",
    "composer.json",
    "Gemfile", "Gemfile.lock",
    ".python-version", ".nvmrc", ".node-version",
}

# 우선순위 3: entry 파일 — 사용자 시나리오 / 기능 추론.
_ENTRY_BASENAMES = {
    "main.py", "app.py", "__main__.py", "manage.py", "asgi.py", "wsgi.py",
    "index.ts", "index.tsx", "index.js", "index.jsx",
    "main.ts", "main.tsx", "main.js", "main.jsx",
    "App.vue", "App.tsx", "App.jsx", "App.js",
    "server.js", "server.ts", "server.py",
    "Main.kt", "Main.java", "Application.kt", "Application.java",
    "main.go", "lib.rs", "main.rs",
}

# 우선순위 4: 인프라 / 설정.
_CONFIG_BASENAMES = {
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "vite.config.js", "vite.config.ts",
    "next.config.js", "next.config.mjs", "next.config.ts",
    "nuxt.config.js", "nuxt.config.ts",
    "vue.config.js",
    "tsconfig.json", "tsconfig.base.json",
    ".env.example",
    "Makefile",
}

# 차단 — 바이너리 / 빌드 산출물 / 노이즈.
_BLOCKED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".mp4", ".mp3", ".wav", ".mov", ".avi",
    ".ttf", ".woff", ".woff2", ".eot",
    ".pyc", ".pyo",
    ".class", ".jar", ".war",
    ".o", ".so", ".dll", ".exe", ".dylib",
    ".lock",  # package-lock.json 은 _MANIFEST_FILES 로 별도 화이트리스트
    ".min.js", ".min.css",
}

_BLOCKED_PATH_PREFIXES = (
    "node_modules/", ".git/", "dist/", "build/", ".next/", ".nuxt/",
    "out/", "target/", "vendor/", "__pycache__/", ".venv/", "venv/",
    "coverage/", ".pytest_cache/", ".mypy_cache/", ".cache/",
    ".idea/", ".vscode/", ".gradle/",
)

# 코드 파일로 인정되는 확장자 (우선순위 5 의 후보).
_CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala",
    ".rb", ".php", ".cs", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".md",  # 추가 docs 들도 흥미 있음 (다만 README 가 1순위라 후순위)
    ".yml", ".yaml",  # config 의 일부
    ".sql", ".graphql",
}


def _is_blocked(path: str) -> bool:
    p = path.lower()
    if any(p.startswith(prefix) for prefix in _BLOCKED_PATH_PREFIXES):
        return True
    # _BLOCKED_EXTENSIONS 단순 확장자 매칭 (예: .lock 이지만 package-lock.json 같은 매니페스트는
    # 위에서 화이트리스트 통과되므로 여기 매칭 시 빠짐).
    for ext in _BLOCKED_EXTENSIONS:
        if p.endswith(ext):
            return True
    return False


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


# [Phase D] 핵심 코드 디렉토리 — 기능 추론에 중요한 파일을 일반 코드보다 우선 샘플링.
# 디렉토리 '세그먼트' 정확 매칭(substring X)으로 microservice 같은 오탐을 막는다.
_CORE_CODE_SEGMENTS = {
    "controllers", "controller", "routes", "router", "handlers", "handler",
    "services", "service", "usecases", "usecase", "domain", "api",
    "views", "view", "endpoints", "resources",
}


def _is_core_code_path(path: str) -> bool:
    segs = path.lower().split("/")[:-1]  # 파일명 제외한 디렉토리 세그먼트
    return any(seg in _CORE_CODE_SEGMENTS for seg in segs)


def _classify(path: str) -> Tuple[float, str]:
    """파일 분류 — (priority, category). 0=README … 4=기타 코드.

    [Phase D] 핵심 코드 디렉토리(controllers/services/domain 등)는 priority 2.5 로 상향해
    일반 코드(4)보다 먼저 샘플링(기능 추론 핵심 파일 누락 방지). category 는 'code' 로 유지
    (기존 동작/진단 회귀 방지)."""
    base = _basename(path)
    if any(rx.match(base) for rx in _README_PATTERNS):
        return (0, "readme")
    if base in _MANIFEST_FILES:
        return (1, "manifest")
    if base in _ENTRY_BASENAMES:
        return (2, "entry")
    if base in _CONFIG_BASENAMES:
        return (3, "config")
    # 코드 파일 — 확장자 기준.
    ext = ""
    if "." in base:
        ext = "." + base.rsplit(".", 1)[-1].lower()
    if ext in _CODE_EXTENSIONS:
        return (2.5, "code") if _is_core_code_path(path) else (4, "code")
    return (99, "other")


def select_onboard_files(
    tree_blobs: List[Dict[str, Any]],
    *,
    max_files: int = _MAX_SAMPLE_FILES,
) -> List[Dict[str, Any]]:
    """
    GitHub tree 의 blob 목록에서 onboard 입력으로 쓸 파일 선택.

    Args:
        tree_blobs: GitHub API `git/trees?recursive=1` 의 tree 항목 list.
            각 item: {"path": str, "type": "blob"|"tree", "sha": str, "size": int}
        max_files: 선정 한도.

    Returns:
        선정된 blob list. 우선순위 (priority) 오름차순 → size 작은 순으로 정렬.
        priority 동률은 size 작은 것 우선 (다양성 ↑, budget 효율 ↑).

    [D2 패턴]
    1. README → 무조건 포함 (있으면).
    2. 매니페스트 (package.json 등) → 다음 우선.
    3. entry 파일 (main.py, App.vue 등) → 시나리오 추론용.
    4. config — Dockerfile / 빌드 설정 → 운영 컨텍스트.
    5. 그 외 코드 — size 작은 순 (큰 파일이 budget 잠식 방지).
    """
    candidates: List[Dict[str, Any]] = []
    for item in tree_blobs:
        if item.get("type") != "blob":
            continue
        path = item.get("path") or ""
        sha = item.get("sha")
        if not path or not sha:
            continue
        if _is_blocked(path):
            continue
        priority, category = _classify(path)
        if priority == 99:
            continue  # 분류 불가 = 후보 X
        candidates.append({
            "path": path,
            "sha": sha,
            "size": int(item.get("size") or 0),
            "_priority": priority,
            "_category": category,
        })

    # 우선순위 오름차순 → 동일 priority 안에선 size 작은 순 → path 알파벳 순 (결정성).
    candidates.sort(key=lambda f: (f["_priority"], f["size"], f["path"]))
    return candidates[:max_files]


# ─── Stage 3: LLM V1 generation ─────────────────────────────────────────────


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


def _format_files_block(samples: List[Dict[str, Any]]) -> str:
    """LLM 입력용 파일 블록 — `### path\\n```\\n{content}\\n```` 반복."""
    parts: List[str] = []
    for s in samples:
        path = s["path"]
        content = s.get("content") or ""
        # 너무 긴 단일 파일은 truncate (per_file_bytes 가드는 fetch 단계에서 적용됐지만 안전망).
        if len(content) > _PER_FILE_BYTES:
            content = content[:_PER_FILE_BYTES] + "\n... (truncated)"
        parts.append(f"### `{path}`\n```\n{content}\n```")
    return "\n\n".join(parts)


async def _fetch_sample_contents(
    github: GitHubClient,
    ident: RepoIdentifier,
    selected: List[Dict[str, Any]],
    *,
    per_file_bytes: int = _PER_FILE_BYTES,
    total_budget: int = _TOTAL_BUDGET_BYTES,
) -> List[Dict[str, Any]]:
    """선정된 파일들의 텍스트 fetch + total_budget 으로 자름.

    LINT sampler 와 유사하지만 onboard 는 ref 가 default branch 라 별도 인자 없이 sha 만 사용.
    실패한 파일은 silently skip (logger debug). 성공한 것만 반환.
    """
    if not selected:
        return []
    out: List[Dict[str, Any]] = []
    used = 0
    for item in selected:
        try:
            text = await github.get_blob_text(
                ident, item["sha"], max_bytes=per_file_bytes,
            )
        except GitHubError as e:
            logger.debug("onboard blob fetch failed: %s (%s)", item["path"], e)
            continue
        if not text:
            continue
        c_len = len(text)
        if used + c_len > total_budget:
            remaining = total_budget - used
            if remaining <= 500:  # 너무 적게 남으면 의미 없음 — 중단.
                break
            # 마지막 파일은 잘라서라도 포함 (README 가 마지막에 잘리는 케이스 방어).
            out.append({**item, "content": text[:remaining]})
            used = total_budget
            break
        out.append({**item, "content": text})
        used += c_len
    return out


async def call_onboard_llm(
    ctx: PipelineContext,
    repo_full_name: str,
    project_name: str,
    samples: List[Dict[str, Any]],
    tree_blobs: List[Dict[str, Any]],
) -> str:
    """Stage: LLM V1 markdown 생성.

    temperature 0.1 — 결정성 우선 (CPS 입력 일관성).
    출력은 5 sections 의 markdown.

    [2026-05-28 코드 단서 주입] manifest deps + entry signals + repo stats 를 결정적
    추출해 프롬프트의 새 input section 으로 주입 → LLM 이 README 회귀 못 함
    (L1-2 #70 동일 함정 방지, L1-1 backstop 디자인과 동일 철학).
    """
    code_evidence = format_code_evidence_block(
        extract_manifest_facts(samples),
        extract_entry_signals(samples),
        extract_repo_stats(tree_blobs),
    )
    prompt = _render(
        _load_prompt("onboard_from_github.md"),
        repo_full_name=repo_full_name,
        project_name=project_name,
        files_block=_format_files_block(samples),
        file_count=str(len(samples)),
        code_evidence=code_evidence,
    )
    result = await ctx.gemini.generate(prompt, temperature=0.1)
    return strip_template_placeholders(strip_code_blocks(result.text)).strip()


# ─── Stage 4+5: 위임 (CPS → PRD 체이닝) ─────────────────────────────────────


async def _delegate_to_cps_and_prd(
    ctx: PipelineContext,
    project_name: str,
    v1_markdown: str,
    team_id: str = "",
) -> Tuple[CpsResult, PrdResult]:
    """V1 markdown 을 기존 CPS → PRD pipeline 에 순차 위임 (postMeeting 패턴).

    [2026-05-27] 이전엔 CPS 만 생성 → 사용자가 "미팅로그/CPS 는 있는데 PRD 없음"
    호소 (design 단계 진입 불가). 일반 postMeeting 흐름과 동일하게 CPS 결과의
    cps_graph 를 PRD pipeline 에 넘겨 PRD 까지 자동 생성.

    기존 흐름 100% 재활용:
      - CPS pipeline: Meeting_Log 저장 + CPS 추출 + master merge (단일 트랜잭션)
      - PRD pipeline: cps_graph → Epic/Story 추출 + PRD master merge
    각 pipeline 의 환각 가드 / 빈값 거부 / master wipe 차단 그대로 적용.
    """
    cps_result = await run_cps_pipeline(
        ctx,
        CpsInput(
            project_name=project_name,
            version="v1.0",
            date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            meeting_content=v1_markdown,
            previous_cps_id=None,  # 새 프로젝트
            team_id=team_id,
        ),
    )
    prd_result = await run_prd_pipeline(
        ctx,
        PrdInput(
            project_name=project_name,
            version="v1.0",
            cps_graph=cps_result.cps_graph,
            previous_prd_id=None,  # 새 프로젝트
            team_id=team_id,
            # [2026-06-04] CPS delta 가 비어도 PRD 가 v1 본문으로 생성되도록 raw fallback.
            meeting_content=v1_markdown,
        ),
    )
    return cps_result, prd_result


# ─── Main entry ─────────────────────────────────────────────────────────────


async def run_github_onboard_pipeline(
    ctx: PipelineContext,
    payload: GithubOnboardInput,
    github_client: Optional[GitHubClient] = None,
) -> GithubOnboardResult:
    """
    GitHub URL → V1 + CPS + PRD 자동 생성.

    Args:
        ctx: PipelineContext (gemini + neo4j + idempotency_key).
        payload: GithubOnboardInput.
        github_client: 테스트 주입용 (fake GitHub). None 이면 real client 생성.

    Raises:
        GitHubError: URL 파싱 실패 / repo 404 / private 권한 부족 등.
        ValueError: V1 markdown 가드 (너무 짧음) 실패.
        그 외 LLM/Neo4j 오류는 그대로 전파.

    Returns:
        GithubOnboardResult — V1 본문 + 샘플링된 파일 + CPS + PRD 결과.
    """
    logger.info(
        "github_onboard start: project=%s url=%s key=%s",
        payload.project_name, payload.github_url, ctx.idempotency_key,
    )

    # ── Stage 1: parse + fetch ──────────────────────────────────────────
    ident = parse_github_url(payload.github_url)
    github = github_client or GitHubClient()
    repo_info = await github.get_repo(ident)
    default_branch = repo_info.get("default_branch") or "main"
    is_private = bool(repo_info.get("private"))

    # NOTE: private repo 의 인증은 GitHubClient 의 user_token (호출자가 주입) 으로 처리.
    # 이 시점에 401/403 이 나면 GitHubError 로 전파 → API route 가 사용자 친화 메시지.

    tree_data = await github.get_tree(ident, default_branch, recursive=True)
    tree_blobs: List[Dict[str, Any]] = tree_data.get("tree") or []

    # ── Stage 2: select files ───────────────────────────────────────────
    selected = select_onboard_files(tree_blobs, max_files=_MAX_SAMPLE_FILES)
    if not selected:
        # README/manifest/entry 가 하나도 없는 repo (사실상 빈 repo 또는 텍스트 X) →
        # V1 생성 불가능. 친화적 메시지.
        raise ValueError(
            f"'{ident.full_name}' 에서 분석 가능한 텍스트 파일을 찾지 못했습니다. "
            "README / manifest / 코드 파일이 있는 repo 인지 확인해주세요."
        )

    # ── Stage 3: LLM V1 ─────────────────────────────────────────────────
    samples = await _fetch_sample_contents(
        github, ident, selected,
        per_file_bytes=_PER_FILE_BYTES,
        total_budget=_TOTAL_BUDGET_BYTES,
    )
    if not samples:
        # blob fetch 모두 실패 — rate limit / 권한 / 네트워크. 친화적 메시지.
        raise ValueError(
            f"'{ident.full_name}' 의 파일 내용을 가져오지 못했습니다. "
            "잠시 후 다시 시도해주세요."
        )

    v1_markdown = await call_onboard_llm(
        ctx, ident.full_name, payload.project_name, samples, tree_blobs,
    )

    # V1 가드 — 너무 짧으면 LLM 환각 의심 → raise (DB 변경 없음).
    if len(v1_markdown) < _MIN_V1_LENGTH:
        raise ValueError(
            f"AI 가 '{ident.full_name}' 에서 V1 항목을 충분히 추출하지 못했습니다 "
            f"({len(v1_markdown)} 자, 최소 {_MIN_V1_LENGTH} 자 필요). "
            "잠시 후 다시 시도하거나 회의록을 직접 입력해주세요."
        )
    # 상한 — CPS pipeline 입력 보호.
    if len(v1_markdown) > _MAX_V1_LENGTH:
        logger.warning(
            "github_onboard V1 markdown 이 %d 자로 너무 큼 — %d 자로 truncate "
            "(project=%s)", len(v1_markdown), _MAX_V1_LENGTH, payload.project_name,
        )
        v1_markdown = v1_markdown[:_MAX_V1_LENGTH]

    # ── Stage 4+5: CPS → PRD pipeline 위임 ──────────────────────────────
    cps_result, prd_result = await _delegate_to_cps_and_prd(
        ctx, payload.project_name, v1_markdown, team_id=payload.team_id,
    )

    logger.info(
        "github_onboard done: project=%s repo=%s v1_size=%d cps_id=%s prd_id=%s prd_mode=%s",
        payload.project_name, ident.full_name, len(v1_markdown),
        cps_result.delta_cps_id, prd_result.master_prd_id, prd_result.mode,
    )

    return GithubOnboardResult(
        project_name=payload.project_name,
        github_url=payload.github_url,
        repo_full_name=ident.full_name,
        v1_markdown=v1_markdown,
        v1_markdown_size=len(v1_markdown),
        sampled_file_count=len(samples),
        sampled_file_paths=[s["path"] for s in samples],
        cps_result=cps_result,
        prd_result=prd_result,
        diagnostic={
            "default_branch": default_branch,
            "is_private": is_private,
            "tree_blob_count": sum(1 for b in tree_blobs if b.get("type") == "blob"),
            "selected_count": len(selected),
            "fetched_count": len(samples),
            "sample_categories": _category_counts(samples),
            "prd_mode": prd_result.mode,
        },
    )


def _category_counts(samples: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for s in samples:
        cat = s.get("_category") or "unknown"
        out[cat] = out.get(cat, 0) + 1
    return out
