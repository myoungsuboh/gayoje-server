from __future__ import annotations

import os as _os
from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class LintInput:
    project_name: str
    github_url: str
    team_id: str = ""


def _int_env(key: str, default: int) -> int:
    """env 정수 파싱 — 잘못된 값은 default."""
    try:
        return int(_os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


_DEFAULT_MAX_SAMPLE_FILES = _int_env("LINT_MAX_SAMPLE_FILES", 40)
_DEFAULT_PER_FILE_BYTES = _int_env("LINT_PER_FILE_BYTES", 64_000)
_DEFAULT_TOTAL_BUDGET = _int_env("LINT_TOTAL_BUDGET_BYTES", 400_000)
_RESIDUAL_LLM_BUDGET = _int_env("LINT_RESIDUAL_LLM_BUDGET", 80_000)


_LINT_RESIDUAL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category_idx": {"type": "integer"},
                    "rule_idx": {"type": "integer"},
                    "applied": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "evidence_file": {"type": "string"},
                    "evidence_line": {"type": "integer"},
                },
                "required": ["category_idx", "rule_idx", "applied"],
            },
        },
    },
}
