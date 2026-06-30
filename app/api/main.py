"""
FastAPI 앱 진입점 (gayoje-server)

- 헬스체크(/ , /health, /health/deep, /metrics)
- 보안/관측 미들웨어 (body size, request-id, metrics, rate limit, CORS)
- setup 라우트 (Neo4j 제약 idempotent 셋업)
- lifespan: 종료 시 Neo4j driver / arq pool 정리

⚠️ strip 직후 "최소 부팅" 상태다. 인증(auth_routes)·도메인 라우터·잡(jobs)·MCP 는
   harness 도메인 제거 후 비워둔 상태이며, Phase 0 adapt 에서 가요제용으로
   재구성한다 (싱글 source: singaservertasklist/REUSE_FROM_HARNESS.md).
   - auth_routes: User SOR Neo4j→PostgreSQL, Google/GitHub→카카오/네이버 (adapt)
   - jobs/worker: 수집/정규화/알림/제출 잡으로 재작성 (adapt)
"""
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.setup_routes import router as setup_router
from app.clients import neo4j_client
from app.core.config import settings
from app.core.limiter import limiter
from app.queue import client as queue_client

# ===== 로깅 + 관측성 =====
# observability.setup_logging() 이 LOG_FORMAT(text|json)/LOG_LEVEL 토글,
# request_id/user_email 컨텍스트 필터 부착, 노이즈 로거 억제를 일괄 처리.
from app.core.observability import init_sentry, setup_logging  # noqa: E402

setup_logging()
logger = logging.getLogger("gayoje.app")

# Sentry 에러 추적 — SENTRY_DSN 설정 시에만 활성. 미설정이면 no-op.
init_sentry(component="backend")


# ===== FastAPI Lifespan =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[boot] gayoje-server 부팅 중...")
    # NOTE: DB 제약/인덱스 seeding 은 Phase 0 adapt 에서 가요제 스키마(PostgreSQL SOR
    #       + Neo4j 관계)로 재구성한다. strip 단계에서는 외부 의존 연결 없이 최소 부팅.
    try:
        yield
    finally:
        # 열린 적 없으면 no-op — lazy 생성이라 안전.
        await neo4j_client.close_driver()
        await queue_client.close_pool()

    logger.info("[boot] gayoje-server 종료")


# ===== FastAPI 인스턴스 =====
# 운영 환경에서 DOCS_PASSWORD 가 설정되어 있으면 기본 /docs 비활성화하고
# Basic Auth 로 보호된 커스텀 엔드포인트로 노출한다.
_docs_protected = settings.is_production and bool(settings.DOCS_PASSWORD)

app = FastAPI(
    title="gayoje-server",
    description="가요제 통합 플랫폼 백엔드 (strip 직후 최소 부팅 — Phase 0 adapt 예정)",
    version="0.0.1",
    lifespan=lifespan,
    docs_url=None if _docs_protected else "/docs",
    redoc_url=None if _docs_protected else "/redoc",
    openapi_url=None if _docs_protected else "/openapi.json",
)


# ===== Swagger UI Basic Auth =====
if _docs_protected:
    _basic = HTTPBasic()

    def _verify_docs_credentials(creds: HTTPBasicCredentials = Depends(_basic)) -> str:
        ok_user = secrets.compare_digest(creds.username, settings.DOCS_USERNAME)
        ok_pass = secrets.compare_digest(creds.password, settings.DOCS_PASSWORD or "")
        if not (ok_user and ok_pass):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid docs credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return creds.username

    @app.get("/openapi.json", include_in_schema=False)
    async def _protected_openapi(_: str = Depends(_verify_docs_credentials)):
        return app.openapi()

    @app.get("/docs", include_in_schema=False)
    async def _protected_swagger(_: str = Depends(_verify_docs_credentials)):
        return get_swagger_ui_html(openapi_url="/openapi.json", title=app.title + " - Swagger UI")

    @app.get("/redoc", include_in_schema=False)
    async def _protected_redoc(_: str = Depends(_verify_docs_credentials)):
        return get_redoc_html(openapi_url="/openapi.json", title=app.title + " - ReDoc")

# ===== Rate Limiting =====
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ===== GeminiError 전역 핸들러 =====
# LLM 호출 라우트가 GeminiError 를 명시적으로 안 잡았을 때의 안전망.
from fastapi import Request
from fastapi.responses import JSONResponse
from app.clients.gemini_client import GeminiError, gemini_error_to_http


@app.exception_handler(GeminiError)
async def _gemini_error_handler(_: Request, exc: GeminiError) -> JSONResponse:
    """라우트가 GeminiError 를 명시적으로 안 잡았을 때 사용자 친화 응답으로 변환."""
    http_exc = gemini_error_to_http(exc)
    return JSONResponse(
        status_code=http_exc.status_code, content={"detail": http_exc.detail}
    )

# ===== Body size limit (DoS 방어) =====
# Content-Length 가 임계 초과면 본문 읽기 전 413.
from app.core.body_size_limit import install_body_size_limit

if settings.MAX_REQUEST_BODY_BYTES > 0:
    install_body_size_limit(app, max_bytes=settings.MAX_REQUEST_BODY_BYTES)

# ===== Request ID + context logging =====
# 모든 로그 라인에 req_id / user_email 자동 첨부. 외부 X-Request-ID 우선, 없으면 UUID.
# Starlette 는 add_middleware 가 LIFO — 마지막 add 가 가장 outer.
from app.core.request_context import RequestIdMiddleware

app.add_middleware(RequestIdMiddleware)

# ===== 메트릭 (Prometheus 계측) =====
# RequestIdMiddleware 보다 안쪽(나중 add)이라 라우트 매칭 후 path 템플릿을 읽을 수 있음.
from app.core.metrics import MetricsMiddleware

app.add_middleware(MetricsMiddleware)

# ===== 전역 rate limit (DoS/버스트 방어) =====
# limiter.default_limits 를 모든 라우트에 적용. CORS 보다 안쪽(먼저 add)이라
# preflight(OPTIONS)는 CORS 가 단락 처리, 실제 요청만 rate limit.
from slowapi.middleware import SlowAPIMiddleware

app.add_middleware(SlowAPIMiddleware)

# ===== CORS =====
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== 헬스체크 =====
# 모니터링/헬스/메트릭은 전역 rate limit 면제 (@limiter.exempt) — 외부 스크래퍼가
# 잦게 호출해도 차단되지 않도록.
@app.get("/", tags=["Health"])
@limiter.exempt
async def root():
    return {
        "status": "ok",
        "service": "gayoje-server",
        "env": settings.ENV,
    }


@app.get("/health", tags=["Health"])
@limiter.exempt
async def health():
    """프로세스 살아있음만 검증 — 외부 모니터링용 (1초 안에 응답)."""
    return {"status": "healthy"}


@app.get("/metrics", tags=["Health"], include_in_schema=False)
@limiter.exempt
async def metrics():
    """Prometheus 텍스트 노출. METRICS_ENABLED=false 면 404, prometheus_client 미설치면 빈 응답."""
    if not settings.METRICS_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="metrics disabled")
    from fastapi.responses import Response as _Response
    from app.core.metrics import render_metrics
    body, content_type = render_metrics()
    return _Response(content=body, media_type=content_type)


@app.get("/health/deep", tags=["Health"])
@limiter.exempt
async def health_deep():
    """
    의존성(Neo4j + Redis)까지 검증하는 깊은 헬스체크.
    - 200: 모든 의존성 OK / 503: 하나라도 실패 (실패 항목 body 에 명시)
    - 운영 알람 전용 (load balancer probe 비권장).
    """
    from fastapi import HTTPException, status as http_status
    results: dict = {"neo4j": "unknown", "redis": "unknown"}
    failures: list[str] = []

    # Neo4j
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

    # Redis — arq pool 재사용
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
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=body,
        )
    return body


# ===== REST 라우터 =====
# strip 단계: setup 만 등록. 인증/도메인 라우터는 Phase 0 adapt 에서 추가.
app.include_router(setup_router)
