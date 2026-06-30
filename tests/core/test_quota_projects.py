"""
quota.assert_projects_within_limit 가드 단위 테스트 —
등급별 동시 보유 프로젝트 수 한도 검증.

[검증 범위]
- Free 1 / Pro 3 / Pro+ 6 / Pro Max 12 한도 적용 확인
- 한도 미만 → 통과
- 한도 도달 (>=) → 402 + QUOTA_EXCEEDED
- 사용자 없음 → 보수적으로 Free 한도 적용
- ownership_repository.claim_project 가 가드 호출 (멱등 케이스 우회)

[모킹 패턴]
test_quota_guards.py 와 동일 — `app.service.{usage,ownership}_repository`
함수를 monkeypatch.setattr 로 교체.
"""
from __future__ import annotations

from typing import Optional

import pytest
from fastapi import HTTPException

from app.core.quota import (
    ERROR_CODE_QUOTA_EXCEEDED,
    assert_projects_within_limit,
)
from app.core.subscription import (
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    SUBSCRIPTION_PRO_MAX,
    SUBSCRIPTION_PRO_PLUS,
)
from app.service.usage_repository import Usage

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_repos(monkeypatch):
    """usage + ownership repo 양쪽 모킹.

    Returns:
        setup(usage=, project_count=) 헬퍼.
    """

    def _setup(*, usage: Optional[Usage], project_count: int):
        async def fake_get_usage(email: str) -> Optional[Usage]:
            return usage

        async def fake_count_user_projects(email: str) -> int:
            return project_count

        monkeypatch.setattr(
            "app.service.usage_repository.get_usage", fake_get_usage
        )
        monkeypatch.setattr(
            "app.service.ownership_repository.count_user_projects",
            fake_count_user_projects,
        )

    return _setup


# ─── 한도 안 — 통과 ──────────────────────────────────────────


async def test_free_under_limit_passes(fake_repos):
    """Free 한도 1 — 0개 보유 시 통과."""
    fake_repos(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        project_count=0,
    )
    # 예외 없으면 통과
    await assert_projects_within_limit("a@b.com")


async def test_pro_under_limit_passes(fake_repos):
    """Pro 한도 3 — 2개 보유 시 통과."""
    fake_repos(
        usage=Usage(
            email="pro@b.com",
            subscription_type=SUBSCRIPTION_PRO,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        project_count=2,
    )
    await assert_projects_within_limit("pro@b.com")


async def test_pro_plus_under_limit_passes(fake_repos):
    """Pro+ 한도 6 — 5개 보유 시 통과."""
    fake_repos(
        usage=Usage(
            email="pp@b.com",
            subscription_type=SUBSCRIPTION_PRO_PLUS,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        project_count=5,
    )
    await assert_projects_within_limit("pp@b.com")


async def test_pro_max_under_limit_passes(fake_repos):
    """Pro Max 한도 12 — 11개 보유 시 통과."""
    fake_repos(
        usage=Usage(
            email="pm@b.com",
            subscription_type=SUBSCRIPTION_PRO_MAX,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        project_count=11,
    )
    await assert_projects_within_limit("pm@b.com")


# ─── 한도 도달 — 차단 ────────────────────────────────────────


async def test_free_at_limit_raises_402(fake_repos):
    """Free 한도 1 — 정확히 1개 보유 시 차단 (>= 비교)."""
    fake_repos(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        project_count=1,
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_projects_within_limit("a@b.com")
    assert exc_info.value.status_code == 402
    detail = exc_info.value.detail
    assert detail["code"] == ERROR_CODE_QUOTA_EXCEEDED
    assert detail["current"] == 1
    assert detail["limit"] == 1
    assert detail["subscription_type"] == SUBSCRIPTION_FREE


async def test_pro_at_limit_raises(fake_repos):
    """Pro 한도 3 — 3개 보유 시 차단."""
    fake_repos(
        usage=Usage(
            email="pro@b.com",
            subscription_type=SUBSCRIPTION_PRO,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        project_count=3,
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_projects_within_limit("pro@b.com")
    assert exc_info.value.detail["limit"] == 3
    assert exc_info.value.detail["subscription_type"] == SUBSCRIPTION_PRO


async def test_pro_plus_at_limit_raises(fake_repos):
    """Pro+ 한도 6 — 6개 보유 시 차단."""
    fake_repos(
        usage=Usage(
            email="pp@b.com",
            subscription_type=SUBSCRIPTION_PRO_PLUS,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        project_count=6,
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_projects_within_limit("pp@b.com")
    assert exc_info.value.detail["limit"] == 6
    assert exc_info.value.detail["subscription_type"] == SUBSCRIPTION_PRO_PLUS


async def test_pro_max_at_limit_raises(fake_repos):
    """Pro Max 한도 12 — 12개 보유 시 차단."""
    fake_repos(
        usage=Usage(
            email="pm@b.com",
            subscription_type=SUBSCRIPTION_PRO_MAX,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        project_count=12,
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_projects_within_limit("pm@b.com")
    assert exc_info.value.detail["limit"] == 12
    assert exc_info.value.detail["subscription_type"] == SUBSCRIPTION_PRO_MAX


# ─── Edge cases ───────────────────────────────────────────────


async def test_missing_user_falls_back_to_free_limit(fake_repos):
    """User 노드 없음 → 보수적으로 free 한도 (1개) 적용. 1개 이상이면 차단."""
    fake_repos(usage=None, project_count=2)
    with pytest.raises(HTTPException) as exc_info:
        await assert_projects_within_limit("ghost@b.com")
    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["limit"] == 1
    assert exc_info.value.detail["subscription_type"] == SUBSCRIPTION_FREE


async def test_message_includes_next_tier_suggestion(fake_repos):
    """402 응답의 메시지가 다음 등급 안내 포함 — UX 보조."""
    fake_repos(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        project_count=1,
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_projects_within_limit("a@b.com")
    msg = exc_info.value.detail["message"]
    assert "Pro" in msg  # Free → Pro / Pro+ / Pro Max 안내 포함


async def test_pro_max_message_has_no_upgrade_path(fake_repos):
    """Pro Max 한도 초과 시 메시지 — 추가 업그레이드 단계 없음 안내."""
    fake_repos(
        usage=Usage(
            email="pm@b.com",
            subscription_type=SUBSCRIPTION_PRO_MAX,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        project_count=12,
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_projects_within_limit("pm@b.com")
    msg = exc_info.value.detail["message"]
    # Pro Max 안내 — "추가 용량이 필요하시면 문의" 등의 다른 톤
    assert "문의" in msg or "Pro Max" in msg
