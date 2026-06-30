"""
FastAPI 앱 진입점
- REST API 라우터 등록 (auth, 도메인 라우트, 파이프라인 v2)
- FastMCP 서버를 /mcp 경로에 마운트
- CORS, lifespan(Neo4j driver + arq pool 정리) 설정
"""
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastmcp.server.http import create_streamable_http_app
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.admin_billing_routes import router as admin_billing_router
from app.api.admin_routes import router as admin_router
from app.api.auth_routes import router as auth_router
from app.api.paddle_webhook_routes import router as paddle_webhook_router
from app.api.paddle_billing_routes import router as paddle_billing_router
from app.api.create_md_routes import router as create_md_router
from app.api.delete_routes import router as delete_router
from app.api.gateway_compat_routes import router as gateway_compat_router
from app.api.gateway_routes import router as gateway_router
from app.api.github_proxy_routes import router as github_proxy_router
from app.api.inquiry_routes import admin_router as inquiry_admin_router
from app.api.inquiry_routes import user_router as inquiry_user_router
from app.api.mcp_token_routes import router as mcp_token_router
from app.api.eval_score_routes import router as eval_score_router
from app.api.interview_routes import router as interview_router
from app.api.lineage_routes import router as lineage_router
from app.api.prd_lint_routes import router as prd_lint_router
from app.api.lint_routes import router as lint_router
from app.api.notion_routes import router as notion_router
from app.api.pricing_routes import admin_router as pricing_admin_router
from app.api.pricing_routes import public_router as pricing_public_router
from app.api.coupon_routes import admin_router as coupon_admin_router
from app.api.coupon_routes import user_router as coupon_user_router
from app.api.quota_config_routes import admin_router as quota_config_admin_router
from app.api.quota_config_routes import public_router as quota_config_public_router
from app.api.query_routes import router as query_router
from app.api.revenue_routes import router as revenue_router
from app.api.setup_routes import router as setup_router
from app.api.skill_library_routes import router as skill_library_router
from app.api.skill_routes import router as skill_router
from app.api.trace_routes import router as trace_router
from app.api.team_routes import invites_router as invites_router
from app.api.team_routes import router as team_router
from app.api.v2_routes import router as v2_router
from app.clients import neo4j_client
from app.core.config import settings
from app.core.limiter import limiter
from app.mcp.auth import MCPAuthMiddleware
from app.mcp.harness_mcp import harness_mcp
# side-effect import — lineage tools 가 harness_mcp 에 자동 등록.
# Cursor / Claude Code 같은 AI 에이전트가 spec ↔ code 추적을 직접 조회 가능 (lock-in).
import app.mcp.lineage_tools  # noqa: F401
# side-effect import — spec tools (API/Screen 계약 + Lint 결과) 자동 등록.
import app.mcp.spec_tools  # noqa: F401
from app.queue import client as queue_client
from app.service import (
    admin_repository,
    audit_repository,
    coupon_repository,
    domain_indexes,
    infra_cost_repository,
    inquiry_repository,
    ownership_repository,
    payment_repository,
    pricing_repository,
    quota_config_repository,
    skill_library_repository,
    subscription_repository,
    team_repository,
    user_repository,
    webhook_event_repository,
)

# ===== 로깅 + 관측성 =====
# [2026-05] observability.setup_logging() 이 LOG_FORMAT(text|json) / LOG_LEVEL 토글,
# request_id/user_email 컨텍스트 필터 부착, 노이즈 로거 억제를 일괄 처리.
# (이전엔 여기서 basicConfig + install_request_context_logging 를 직접 호출했음.)
from app.core.observability import init_sentry, setup_logging  # noqa: E402

setup_logging()
logger = logging.getLogger("harness.app")

# Sentry 에러 추적 — SENTRY_DSN 설정 시에만 활성. 미설정이면 no-op.
init_sentry(component="backend")

# ===== MCP 앱 미리 생성 (lifespan 관리용) =====
# `middleware=` 로 JWT Bearer 인증 강제 — 이전엔 누구나 /mcp/sse 호출해서
# 임의 project_name 으로 타 테넌트 데이터 조회 가능했음. 미들웨어 통과 후
# tool 함수 안에서 require_mcp_user_and_assert_owns() 로 소유권까지 검증.
from starlette.middleware import Middleware

mcp_app = create_streamable_http_app(
    server=harness_mcp,
    streamable_http_path="/sse",
    debug=not settings.is_production,
    middleware=[Middleware(MCPAuthMiddleware)],
)


# ===== FastAPI Lifespan =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    # logger 사용 — Windows cp949 콘솔에서도 이모지/한글 안전 (uvicorn 직접 호출 호환)
    logger.info("[boot] Harness Backend 부팅 중...")

    # User.email + Project.name UNIQUE 제약 idempotent ensure
    # (Neo4j 미연결이면 warning 만 찍고 진행)
    if settings.NEO4J_URI:
        await user_repository.ensure_user_constraints()
        await ownership_repository.ensure_project_constraint()
        # 기존 사용자에 subscription_type / is_admin default 채우기 (idempotent).
        await user_repository.migrate_user_defaults()
        # SubscriptionChange 인덱스 ensure.
        await admin_repository.ensure_subscription_history_index()
        # AuditLog 인덱스 ensure (created_at desc 조회 + actor/target 검색).
        await audit_repository.ensure_audit_indexes()
        # [2026-05] PricingConfig 제약 + 4개 등급 default seed (idempotent — 기존 값 보존).
        await pricing_repository.ensure_pricing_constraint()
        await pricing_repository.ensure_pricing_seeded()
        # [2026-06] KRW → USD 전환 마이그레이션 (멱등) — legacy 원화 행을 USD 캐노니컬로 1회 교체.
        await pricing_repository.ensure_pricing_usd_migration()
        # [2026-05] Coupon.code UNIQUE 제약 ensure (베타 신청자 무료 코드용).
        await coupon_repository.ensure_coupon_constraint()
        # [2026-05-17] QuotaConfig 제약 + 4개 등급 default seed.
        await quota_config_repository.ensure_quota_config_constraint()
        await quota_config_repository.ensure_quota_config_seeded()
        # DB 값을 quota._LIMITS_OVERRIDE 에 load — 다음 가드 호출부터 새 값 반영.
        from app.api.quota_config_routes import load_quota_overrides_into_memory
        await load_quota_overrides_into_memory()
        # [2026-05-18] 결제/구독 — Payment / Subscription / WebhookEvent 제약 + 인덱스
        # (BillingMethod 제약은 Toss 빌링키 시절 잔재 — 2026-06 Paddle MoR 전환으로 제거)
        await subscription_repository.ensure_subscription_constraints()
        await payment_repository.ensure_payment_constraints()
        await webhook_event_repository.ensure_webhook_constraints()
        # [2026-05-18] NotificationLog 제약 + 인덱스 — 이메일 발송 audit
        from app.service import notification_log_repository
        await notification_log_repository.ensure_notification_log_constraints()
        # [2026-05] InfraCost (year, month) UNIQUE 제약 ensure.
        await infra_cost_repository.ensure_infra_cost_constraint()
        # [2026-05] Inquiry 제약 + 인덱스
        await inquiry_repository.ensure_inquiry_constraints()
        # 핵심 도메인 노드 인덱스 (CPS/PRD/Skill/Meeting/Lint/Lineage on project).
        # 데이터 증가 시 풀스캔 방지 — 멱등 (IF NOT EXISTS).
        await domain_indexes.ensure_domain_indexes()
        # Skill Library (유저 단위 스킬 보관함) — SkillFolder/LibrarySkill 제약 + 인덱스.
        await skill_library_repository.ensure_constraints()
        # [2026-05-18] MCP 전용 토큰 — McpToken 제약 + 인덱스.
        from app.service import mcp_token_repository
        await mcp_token_repository.ensure_constraints()
        # [2026-05-31] Team/Invite 제약 + 인덱스 + 만료 초대 정리.
        await team_repository.ensure_team_constraints()
        try:
            cleaned = await team_repository.cleanup_expired_invites()
            if cleaned:
                logger.info("team: 만료 초대 %d건 정리", cleaned)
        except Exception as e:  # noqa: BLE001
            logger.warning("team: 만료 초대 정리 실패 (%s)", e)
        # .env ADMIN_EMAILS 의 사용자를 admin 으로 승격 (가입 후이면 즉시 적용).
        # 승격 결과는 audit log 에도 기록 — 시스템 자동 액션 추적용.
        if settings.admin_emails_list:
            promoted = await user_repository.promote_admins_by_emails(
                settings.admin_emails_list
            )
            for email in promoted:
                await audit_repository.write(
                    actor_email=audit_repository.SYSTEM_ACTOR,
                    action=audit_repository.ACTION_SYSTEM_ADMIN_GRANT,
                    target_email=email,
                    payload={"reason": "ADMIN_EMAILS env auto-promote on boot"},
                )

    async with mcp_app.lifespan(app):
        logger.info("[boot] 모든 시스템 준비 완료: REST API + MCP + v2 pipelines (queue: arq)")
        try:
            yield
        finally:
            await neo4j_client.close_driver()
            await queue_client.close_pool()

    logger.info("[boot] Harness Backend 종료")


# ===== FastAPI 인스턴스 =====
# 운영 환경에서 DOCS_PASSWORD 가 설정되어 있으면 기본 /docs 비활성화하고
# Basic Auth 로 보호된 커스텀 엔드포인트로 노출한다.
_docs_protected = settings.is_production and bool(settings.DOCS_PASSWORD)

app = FastAPI(
    title="Harness Backend",
    description="Harness Backend — 인증 + 도메인 라우트 + MCP 서버 + v2 파이프라인 (arq queue)",
    version="0.3.0",
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
# gateway_compat dispatcher 처럼 GeminiError 를 명시적으로 catch 안 하는 라우트의
# 안전망. v2 라우트들은 자체 try/except 로 gemini_error_to_http 호출 — 동일 결과.
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
# 2026-05 보안 점검 — Content-Length 가 임계 초과면 본문 읽기 전 413.
from app.core.body_size_limit import install_body_size_limit

if settings.MAX_REQUEST_BODY_BYTES > 0:
    install_body_size_limit(app, max_bytes=settings.MAX_REQUEST_BODY_BYTES)

# ===== Request ID + context logging =====
# 2026-05 보안 점검 — 모든 로그 라인에 req_id / user_email 자동 첨부.
# 외부 X-Request-ID 헤더 우선, 없으면 UUID 발급. 응답 헤더에도 echo.
# Body size 와 CORS 사이에 등록 — context 가 모든 요청에 attach 되어야 하기에
# 가능한 outer 쪽 (Starlette 는 add_middleware 가 LIFO 라 마지막 add 가 가장 outer).
from app.core.request_context import RequestIdMiddleware

app.add_middleware(RequestIdMiddleware)

# ===== 메트릭 (Prometheus 계측) =====
# 요청 수/지연/동시성 집계. RequestIdMiddleware 보다 안쪽(나중 add)이라 라우트
# 매칭이 끝난 뒤 path 템플릿을 읽을 수 있음. prometheus_client 미설치 시 통과만.
from app.core.metrics import MetricsMiddleware

app.add_middleware(MetricsMiddleware)

# ===== 전역 rate limit (DoS/버스트 방어) =====
# [2026-06-04 보안] limiter.default_limits(300/분, 라우트별·사용자별)를 모든 라우트에
# 적용하는 ASGI 미들웨어. 데코레이터 없는 라우트도 자동 보호되고, 명시 @limiter.limit
# 가 있으면 그 값이 우선(더 빡빡). per-route 카운트라 폴링/페이지 fan-out 은 합산되지
# 않아 정상 흐름엔 영향 0. CORS 보다 안쪽(먼저 add)에 둬서 preflight(OPTIONS)는 CORS
# 가 단락 처리하고, 실제 요청만 rate limit + 429 응답에도 CORS 헤더가 입혀지도록 한다.
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
# [2026-06-04] 모니터링/헬스/메트릭은 전역 rate limit(300/분) 면제 — Uptime Kuma·
# Prometheus 등 외부 스크래퍼가 잦게 호출해도 차단되지 않도록 (@limiter.exempt).
@app.get("/", tags=["Health"])
@limiter.exempt
async def root():
    return {
        "status": "ok",
        "service": "Harness Backend",
        "env": settings.ENV,
    }


@app.get("/health", tags=["Health"])
@limiter.exempt
async def health():
    """프로세스 살아있음만 검증 — Uptime Kuma 등 외부 모니터링용 (1초 안에 응답)."""
    return {"status": "healthy"}


@app.get("/metrics", tags=["Health"], include_in_schema=False)
@limiter.exempt
async def metrics():
    """Prometheus 텍스트 노출 — Grafana/Prometheus 스크랩용.

    METRICS_ENABLED=false 면 404. prometheus_client 미설치면 빈 응답.
    8000 포트는 외부 비공개(Caddy 뒤)라 별도 인증 없이 노출 — 같은 docker
    네트워크의 스크래퍼만 접근. 외부 공개가 필요하면 Caddy 단에서 /metrics 보호.
    """
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
    의존성 (Neo4j + Redis) 까지 검증하는 깊은 헬스체크.

    [정책]
    - 200: 모든 의존성 OK
    - 503: 어느 하나라도 실패 (어느 게 실패했는지 응답 body 에 명시)

    [용도]
    - 운영 알람 전용 — /health 가 200 이어도 DB 끊겼을 때 사용자 영향 큼.
    - load balancer probe 로는 비권장 (latency 변동, 일시 장애에 민감).
    - 호출 빈도: 1~5분에 1회 정도 — 매 1초는 부담.
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
app.include_router(auth_router)
app.include_router(gateway_router)
app.include_router(setup_router)
app.include_router(gateway_compat_router)
app.include_router(v2_router)
app.include_router(skill_router)
app.include_router(skill_library_router)
app.include_router(lint_router)
app.include_router(lineage_router)
app.include_router(eval_score_router)
app.include_router(interview_router)
app.include_router(prd_lint_router)
app.include_router(query_router)
app.include_router(trace_router)
app.include_router(delete_router)
app.include_router(create_md_router)
app.include_router(github_proxy_router)
# [2026-05-18] Notion 페이지 검색 / 미리보기 / 미팅 import.
app.include_router(notion_router)
app.include_router(admin_router)
# [2026-05] 가격 라우트 — 공개(/api/pricing) + admin(/api/admin/pricing).
app.include_router(pricing_public_router)
app.include_router(pricing_admin_router)
# [2026-05] 쿠폰 라우트 — 사용자(/api/coupons/validate) + admin(/api/admin/coupons/*).
app.include_router(coupon_user_router)
app.include_router(coupon_admin_router)
# [2026-05-17] 한도 라우트 — 공개(/api/quota-config) + admin(/api/admin/quota-config).
app.include_router(quota_config_public_router)
app.include_router(quota_config_admin_router)
# [2026-06] 결제 라우트 — Paddle(MoR) webhook + 구독 스냅샷/고객포털(/api/paddle/*) + admin(/api/admin/billing/*).
# Toss 시절 사용자 결제(/api/billing/*)·webhook·내부 cron 라우트는 Paddle 전환으로 제거.
app.include_router(paddle_webhook_router)
app.include_router(paddle_billing_router)
app.include_router(admin_billing_router)
# [2026-05] 수익 대시보드 — admin(/api/admin/revenue/* + /api/admin/infra-cost).
app.include_router(revenue_router)
# [2026-05] 문의 시스템 — 사용자(/api/inquiries) + admin(/api/admin/inquiries).
app.include_router(inquiry_user_router)
app.include_router(inquiry_admin_router)
# [2026-05-18] MCP 전용 토큰 — 사용자별 발급/조회/회수.
app.include_router(mcp_token_router)
# [2026-05-31] 팀 관리 — 팀 CRUD / 멤버 / 초대.
app.include_router(team_router)
app.include_router(invites_router)

# ===== MCP 마운트 =====
app.mount("/mcp", mcp_app)
