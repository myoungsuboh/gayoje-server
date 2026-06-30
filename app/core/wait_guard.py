"""
`?wait=true` 동기 모드 admin 가드 — 공용.

[배경]
큐 우회 + LLM 30~120s 동기 호출은 web worker 점거 → DoS 표면. 운영에선
admin 만 허용, 개발 환경에선 누구나 허용 (디버깅 편의).

[적용 라우트]
- v2_routes: cps / post_meeting / prd / design  (Sprint 1)
- lineage_routes: analyze_lineage  (Sprint 8 P1)
"""
from __future__ import annotations

from fastapi import HTTPException, status

from app.core.config import settings


def guard_wait_mode(wait: bool, user) -> None:
    """
    Raises:
        HTTPException 403 — 운영 + 비관리자가 wait=true 호출 시.
    """
    if not wait:
        return
    if not settings.is_production:
        return
    if getattr(user, "is_admin", False):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "?wait=true 동기 모드는 운영 환경에서 관리자만 사용할 수 있습니다. "
            "일반 사용자는 큐 기반 비동기 모드(기본)를 사용하세요."
        ),
    )
