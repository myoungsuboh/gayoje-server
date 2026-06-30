"""
[2026-06 회귀 가드] enqueue 를 감싼 broad `except Exception → 503` 블록이
의도된 HTTPException(특히 동시성 429)을 503 으로 가리지 않는지 정적 검증.

concurrency 게이트가 _enqueue 에서 HTTPException(429)을 raise 하는데, 라우트의
`except Exception as e: raise HTTPException(503, "queue unavailable")` 가 그걸
잡아 503 으로 변질시키던 버그를 수정(`except HTTPException: raise` 선행)했다.
이 테스트는 그 선행 re-raise 가 모든 enqueue 실패 핸들러에 유지되는지 가드한다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_API_DIR = Path(__file__).resolve().parents[2] / "app" / "api"
_FILES = [
    "v2_routes.py", "gateway_compat_routes.py", "notion_routes.py",
    "create_md_routes.py", "skill_routes.py", "eval_score_routes.py",
]


@pytest.mark.parametrize("fname", _FILES)
def test_queue_unavailable_handlers_reraise_httpexception(fname):
    """'queue unavailable' 503 핸들러 직전에 `except HTTPException: raise` 가 있어야."""
    text = (_API_DIR / fname).read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "queue unavailable" not in line:
            continue
        # 이 503 블록을 연 `except Exception` 라인을 위로 거슬러 찾는다.
        j = i
        while j >= 0 and "except Exception" not in lines[j]:
            j -= 1
        assert j >= 0, f"{fname}:{i+1} — except Exception 못 찾음"
        # 그 except Exception 바로 앞 2줄 안에 `except HTTPException:` 가 있어야 한다.
        preceding = "\n".join(lines[max(0, j - 2):j])
        assert "except HTTPException" in preceding, (
            f"{fname}: 'queue unavailable' 핸들러(line {j+1})가 HTTPException 을 "
            f"먼저 re-raise 하지 않음 — 동시성 429 가 503 으로 가려짐."
        )
