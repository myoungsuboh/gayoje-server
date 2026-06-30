from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from app.clients.github_client import GitHubClient, GitHubError, RepoIdentifier
from app.pipelines.lint_evidence import MANIFEST_FILES, FileSample
from app.pipelines.lint_pipeline.types import (
    _DEFAULT_MAX_SAMPLE_FILES,
    _DEFAULT_PER_FILE_BYTES,
    _DEFAULT_TOTAL_BUDGET,
)

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")

_ANCHOR_PATTERNS = [
    re.compile(
        r"(^|/)(main|index|app|server)\.(py|ts|tsx|js|jsx|go|rs|java|kt)$",
        re.IGNORECASE,
    ),
    re.compile(r"(^|/)README(\.md)?$", re.IGNORECASE),
]


def _extract_spec_tokens(specs: Dict[str, Any]) -> List[str]:
    """spec 의 name/id/endpoint/path/tech_stack/tags 에서 영문 토큰 추출 (3+자)."""
    tokens: List[str] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                # [2026-06] "path" — Screen 의 route 경로. 라우터/페이지 파일이
                # 샘플로 뽑히게 해 기획 카테고리의 검증 정확도를 올린다.
                if k in ("name", "id", "endpoint", "path", "tech_stack", "tags"):
                    _walk(v)
                elif isinstance(v, (dict, list)):
                    _walk(v)
        elif isinstance(obj, list):
            for x in obj:
                _walk(x)
        elif isinstance(obj, str):
            for m in _TOKEN_RE.findall(obj):
                t = m.lower()
                if len(t) >= 3:
                    tokens.append(t)

    _walk(specs.get("spack") or {})
    _walk(specs.get("ddd") or {})
    _walk(specs.get("architecture") or {})
    _walk(specs.get("plan") or {})
    for r in specs.get("rules") or []:
        _walk({"name": r.get("name"), "tags": r.get("tags")})

    stop = {
        "api", "apis", "the", "and", "for", "with", "type", "name",
        "service", "services", "entity", "entities", "policy", "policies",
        "database", "databases", "domain",
    }
    return sorted({t for t in tokens if t not in stop})


def _score_file(path: str, tokens: List[str]) -> int:
    p = path.lower()
    return sum(1 for t in tokens if t in p)


def _is_anchor(path: str) -> bool:
    return any(rx.search(path) for rx in _ANCHOR_PATTERNS)


def _is_manifest_path(path: str) -> bool:
    return os.path.basename(path) in MANIFEST_FILES


def _select_sample_paths(
    code_files: List[Dict[str, Any]],
    all_tree_files: List[Dict[str, Any]],
    tokens: List[str],
    *,
    max_files: int = _DEFAULT_MAX_SAMPLE_FILES,
) -> List[Dict[str, Any]]:
    eligible_code = [
        f for f in code_files if f.get("sha") and (f.get("size") or 0) > 0
    ]

    manifests = [
        f for f in all_tree_files
        if f.get("type") == "blob" and f.get("sha")
        and _is_manifest_path(f.get("path") or "")
    ]
    manifests = sorted(manifests, key=lambda f: f.get("size") or 0)[:8]

    seen_paths = {f["path"] for f in manifests}

    anchors = [f for f in eligible_code if _is_anchor(f["path"])]
    anchor_budget = max(2, max_files // 5)
    anchors = sorted(anchors, key=lambda f: f.get("size") or 0)[:anchor_budget]
    anchors = [f for f in anchors if f["path"] not in seen_paths]
    seen_paths.update(f["path"] for f in anchors)

    scored: List[Tuple[int, int, Dict[str, Any]]] = []
    for f in eligible_code:
        if f["path"] in seen_paths:
            continue
        s = _score_file(f["path"], tokens)
        if s == 0:
            continue
        scored.append((s, -(f.get("size") or 0), f))
    scored.sort(key=lambda x: (-x[0], -x[1]))

    rest_budget = max(0, max_files - len(manifests) - len(anchors))
    rest = [f for _, _, f in scored[:rest_budget]]

    return manifests + anchors + rest


async def _fetch_full_bodies(
    github: GitHubClient,
    ident: RepoIdentifier,
    targets: List[Dict[str, Any]],
    *,
    per_file_bytes: int = _DEFAULT_PER_FILE_BYTES,
    total_budget: int = _DEFAULT_TOTAL_BUDGET,
) -> List[FileSample]:
    """선정된 파일들의 full body 를 병렬 fetch + total_budget 으로 잘라냄."""
    if not targets:
        return []

    async def _fetch(f: Dict[str, Any]) -> Optional[FileSample]:
        try:
            text = await github.get_blob_text(
                ident, f["sha"], max_bytes=per_file_bytes
            )
        except GitHubError as e:
            logger.debug("blob fetch failed: %s (%s)", f.get("path"), e)
            return None
        if not text:
            return None
        return FileSample(path=f["path"], content=text, size=f.get("size") or 0)

    fetched = await asyncio.gather(*[_fetch(f) for f in targets])
    samples: List[FileSample] = []
    used = 0
    for s in fetched:
        if s is None:
            continue
        c_len = len(s.content)
        if used + c_len > total_budget:
            remaining = total_budget - used
            if remaining <= 200:
                break
            samples.append(
                FileSample(path=s.path, content=s.content[:remaining], size=s.size)
            )
            used = total_budget
            break
        samples.append(s)
        used += c_len
    return samples
