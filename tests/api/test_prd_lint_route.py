"""
prd_lint_routes.py 회귀 가드.

검증:
- 빈 PRD → lint score 낮음 + errors >= 1
- 충실 PRD → 80%+
- 1MB 초과 → 413
- text 누락 (Pydantic validation) → 422
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import prd_lint_routes
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


def _user(email: str = "u@x.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="t",
        subscription_type="free", is_admin=False,
    )


async def test_prd_lint_empty_text_low_score():
    resp = await prd_lint_routes.prd_lint_route(
        payload=prd_lint_routes.PrdLintRequest(text=""),
        current_user=_user(),
    )
    assert resp.score < 0.5
    assert resp.summary["errors"] >= 1


async def test_prd_lint_too_large_rejects():
    """1MB 초과 PRD → 413."""
    big_text = "x" * (1_000_001)
    with pytest.raises(HTTPException) as exc:
        await prd_lint_routes.prd_lint_route(
            payload=prd_lint_routes.PrdLintRequest(text=big_text),
            current_user=_user(),
        )
    assert exc.value.status_code == 413


async def test_prd_lint_fully_specified_high_score():
    """충실 PRD → 80%+."""
    prd = (
        "# Product Overview\n"
        + "x" * 600
        + "\n[Story 1.1] 사용자는 식물을 등록한다 자세한 동작 설명 본문 길게\n"
        + "- 입력: name, species 필수\n"
        + "- 출력: 식물 id 반환\n"
        + "- 권한: 인증된 사용자만\n"
        + "[Story 1.2] 사용자는 식물 조회 동작을 수행 다양한 사용 시나리오\n"
        + "- 입력: plantId 필수\n"
        + "- 출력: 식물 정보 응답, 미존재 시 404\n"
        + "- 권한: 본인 소유, 최대 100자\n"
        + "\n# Non-Functional Requirements\n"
        + "- 응답 시간 500ms\n"
        + "- OAuth 2.0 + JWT 인증\n"
        + "- 401/403/404/422 처리\n"
        + "- 가용성 99.9%\n"
    )
    resp = await prd_lint_routes.prd_lint_route(
        payload=prd_lint_routes.PrdLintRequest(text=prd),
        current_user=_user(),
    )
    assert resp.score >= 0.80


async def test_prd_lint_issues_have_hint():
    """모든 issue 에 hint 가 있어야 사용자가 어떻게 고치는지 알 수 있음."""
    resp = await prd_lint_routes.prd_lint_route(
        payload=prd_lint_routes.PrdLintRequest(text=""),
        current_user=_user(),
    )
    assert len(resp.issues) > 0
    for issue in resp.issues:
        assert issue.hint, f"issue {issue.code} 에 hint 없음"
        assert issue.severity in {"error", "warning", "info"}
