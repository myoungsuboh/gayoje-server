"""v1 API — 도메인 라우터 자동 등록 (BE-E01-T01).

각 도메인 패키지 app/api/v1/<domain>/router.py 가 `router: APIRouter` 를 노출하면
build_v1_router() 가 자동 수집해 prefix /api/v1 아래로 묶는다.
→ 도메인 추가 = 디렉터리(+router.py) 추가만으로 자동 등록.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

from fastapi import APIRouter

logger = logging.getLogger(__name__)

# 표시/등록 순서 고정(OpenAPI 가독성). 목록 밖 도메인도 발견되면 알파벳순으로 뒤에 붙는다.
_DOMAIN_ORDER = [
    "version",
    "festivals", "search", "geo", "calendar", "favorites",
    "reports", "subscriptions", "payments", "notifications",
    "instructors", "auth", "users", "admin", "intake", "ingestion",
]


def _discover_domains() -> list[str]:
    """app/api/v1 하위의 도메인 패키지 이름을 등록 순서대로 반환."""
    import app.api.v1 as pkg

    found = {m.name for m in pkgutil.iter_modules(pkg.__path__) if m.ispkg}
    ordered = [d for d in _DOMAIN_ORDER if d in found]
    ordered += sorted(found - set(_DOMAIN_ORDER))
    return ordered


def build_v1_router() -> APIRouter:
    """모든 도메인 라우터를 모아 /api/v1 라우터로 반환."""
    v1 = APIRouter(prefix="/api/v1")
    for domain in _discover_domains():
        try:
            mod = importlib.import_module(f"app.api.v1.{domain}.router")
        except ModuleNotFoundError:
            # router.py 가 아직 없는 도메인은 건너뜀(스캐폴딩 단계 허용).
            logger.debug("v1: 도메인 %s 에 router.py 없음 — skip", domain)
            continue
        router = getattr(mod, "router", None)
        if router is not None:
            v1.include_router(router)
    return v1
