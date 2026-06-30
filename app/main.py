"""gayoje-server 앱 팩토리 (BE-E01-T01).

create_app() 으로 FastAPI 앱을 구성한다:
- 보안/관측 미들웨어 (body size, request-id, metrics, rate limit, CORS) — app/core 재사용
- /api/v1 도메인 라우터 자동 등록 (app.api.v1.build_v1_router)
- 헬스(/health, /health/deep, /metrics)

DB 커넥션 풀·표준응답 envelope·구조화 로깅/레디니스는 후속 task
(BE-E01-T03/T05/T06)에서 확장한다. uvicorn 엔트리포인트는 run.py → app.main:app.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.v1 import build_v1_router
from app.clients import neo4j_client
from app.common import APP_VERSION
from app.common.exception_handlers import install_exception_handlers
from app.core.body_size_limit import install_body_size_limit
from app.core.config import settings
from app.core.limiter import limiter
from app.core.metrics import MetricsMiddleware, render_metrics
from app.core.observability import init_sentry, setup_logging
from app.core.request_context import RequestIdMiddleware
from app.infra.db import check_db, dispose_engine
from app.queue import client as queue_client

setup_logging()
logger = logging.getLogger("gayoje.app")
init_sentry(component="backend")


# ===== Lifespan =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[boot] gayoje-server 부팅 중...")
    # DB 제약/시드·레디니스 프로브는 BE-E01-T03/T04/T06 에서 추가.
    try:
        yield
    finally:
        # lazy 생성이라 열린 적 없으면 no-op.
        await neo4j_client.close_driver()
        await queue_client.close_pool()
        await dispose_engine()
    logger.info("[boot] gayoje-server 종료")


# ===== 헬스 엔드포인트 (모듈 레벨 — 단위 테스트가 직접 호출/패치) =====
@limiter.exempt
async def health() -> dict:
    """프로세스 생존만 검증 — 외부 모니터링용 (1초 내 응답)."""
    return {"status": "healthy"}


@limiter.exempt
async def health_deep() -> dict:
    """의존성(PostgreSQL + Neo4j + Redis)까지 검증. 모두 OK→200, 하나라도 실패→503."""
    results: dict = {"postgres": "unknown", "neo4j": "unknown", "redis": "unknown"}
    failures: list[str] = []

    try:
        if await check_db():
            results["postgres"] = "ok"
        else:
            results["postgres"] = "unexpected_response"
            failures.append("postgres")
    except Exception as e:  # noqa: BLE001
        results["postgres"] = f"error: {type(e).__name__}"
        failures.append("postgres")

    try:
        rows = await neo4j_client.run_cypher("RETURN 1 AS ok")
        if rows and rows[0].get("ok") == 1:
            results["neo4j"] = "ok"
        else:
            results["neo4j"] = "unexpected_response"
            failures.append("neo4j")
    except Exception as e:  # noqa: BLE001
        results["neo4j"] = f"error: {type(e).__name__}"
        failures.append("neo4j")

    try:
        pool = await queue_client.get_pool()
        await pool.ping()
        results["redis"] = "ok"
    except Exception as e:  # noqa: BLE001
        results["redis"] = f"error: {type(e).__name__}"
        failures.append("redis")

    body = {"status": "healthy" if not failures else "degraded", "checks": results}
    if failures:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=body
        )
    return body


@limiter.exempt
async def metrics_endpoint() -> Response:
    """Prometheus 텍스트 노출. METRICS_ENABLED=false 면 404, prometheus 미설치면 빈 응답."""
    if not settings.METRICS_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="metrics disabled"
        )
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


def create_app() -> FastAPI:
    """FastAPI 앱 팩토리."""
    app = FastAPI(
        title="gayoje-server",
        description="가요제 통합 플랫폼 백엔드",
        version=APP_VERSION,
        lifespan=lifespan,
    )

    # ===== Rate limiting =====
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ===== 표준 에러 envelope 핸들러 (BE-E01-T05) =====
    install_exception_handlers(app)

    # ===== 미들웨어 (Starlette LIFO — 마지막 add 가 최외곽 → CORS 최외곽) =====
    if settings.MAX_REQUEST_BODY_BYTES > 0:
        install_body_size_limit(app, max_bytes=settings.MAX_REQUEST_BODY_BYTES)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(SlowAPIMiddleware)
    # GZip — 1KB 이상 응답 압축.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ===== 헬스 =====
    app.add_api_route("/health", health, methods=["GET"], tags=["Health"])
    app.add_api_route("/health/deep", health_deep, methods=["GET"], tags=["Health"])
    app.add_api_route(
        "/metrics", metrics_endpoint, methods=["GET"], tags=["Health"],
        include_in_schema=False,
    )

    # ===== /api/v1 도메인 라우터 자동 등록 =====
    app.include_router(build_v1_router())

    return app


app = create_app()
