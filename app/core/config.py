"""
환경설정 (Settings)
.env에서 값을 읽어 타입 검증된 설정 객체로 노출.
"""
import os
from typing import List, Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# JWT_SECRET_KEY 의 default 값. 운영(production) 환경에서 이 값 그대로면 부팅 거부.
_DEFAULT_JWT_SECRET_PLACEHOLDER = "change-me-to-a-long-random-secret-string"

# 운영에서 요구되는 JWT secret 최소 길이 (256 bit / 32 byte 이상 권장).
_JWT_SECRET_MIN_LENGTH = 32

# ===== 시크릿 마스킹 (BE-E01-T02) =====
# 로그/디버그 요약에서 평문 시크릿이 새지 않도록. 필드 이름에 아래 토큰이 들어가면
# 시크릿으로 간주해 마스킹.
_SECRET_FIELD_HINTS = ("KEY", "SECRET", "PASSWORD", "TOKEN", "DSN")


def mask_secret(value: Optional[str], show: int = 4) -> str:
    """시크릿 값을 로그용으로 마스킹 — 앞 show 자만 남기고 나머지 ***."""
    if not value:
        return ""
    s = str(value)
    if len(s) <= show:
        return "***"
    return s[:show] + "***"


def mask_db_url(url: str) -> str:
    """DB/Redis URL 의 자격증명(password) 만 마스킹.

    예: postgresql+asyncpg://user:secret@host:5432/db → .../user:***@host:5432/db
    자격증명이 없으면(sqlite, http://localhost 등) 원본 그대로.
    """
    import re

    return re.sub(r"(://[^:/@]+:)[^@]+@", r"\1***@", str(url))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env에 정의 안 된 변수가 있어도 무시
    )

    # 실행 환경 — development | staging | production (is_* 헬퍼로 분기).
    ENV: str = "development"
    PORT: int = 8000

    # ===== PostgreSQL (사용자/결제/정형 데이터 주력 SOR) — BE-E01-T02/T03 =====
    # 예: postgresql+asyncpg://user:pw@host:5432/gayoje
    # 로컬 개발은 sqlite 폴백(편의). 운영/수집(INGEST)은 PG 필수.
    DATABASE_URL: str = "sqlite+aiosqlite:///./gayoje_dev.db"

    # JWT
    JWT_SECRET_KEY: str = "change-me-to-a-long-random-secret-string"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # [2026-06] 인증 정책 — OAuth(Google/GitHub) 전용 전환.
    # 가짜 이메일로 만든 계정이 많아 이메일/비번 신규 가입을 차단(기본 False).
    # 기존 이메일/비번 계정의 '로그인'은 그대로 유지(잠금 방지) — 가입(/auth/signup)만 게이트.
    # env(ALLOW_PASSWORD_SIGNUP=true)로 임시 재개 가능.
    ALLOW_PASSWORD_SIGNUP: bool = False

    # CORS
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # [2026-05 보안 점검] 요청 본문 최대 크기 (bytes).
    # 회의록 텍스트가 가장 큰 본문 — 보통 100KB 이하. 10MB 면 충분히 여유.
    # 100MB 같은 거대 페이로드 DoS 방어. 0 이하면 비활성화.
    # [2026-05-18] STT (음성 파일 업로드 → /api/gateway/transcribeAudio) 가
    # 최대 30MB 파일을 multipart 로 받으므로 30MB 로 상향.
    MAX_REQUEST_BODY_BYTES: int = 30 * 1024 * 1024  # 30MB

    # ===== Feature flags (재배포 없이 env 토글) — BE-E01-T02 =====
    # 기능을 점진 활성/긴급 차단. payments=Phase 2, notifications=Phase 1+,
    # 크롤러=INGEST-E3. 공공 API 수집(북극성 PoC 대상)만 기본 on.
    FEATURE_PAYMENTS: bool = False
    FEATURE_NOTIFICATIONS: bool = False
    FEATURE_INGEST_CRAWLER: bool = False
    FEATURE_INGEST_PUBLIC_API: bool = True

    # ===== 공공데이터 수집 (INGEST) — BE-E01-T02 =====
    # 공공데이터포털(data.go.kr) 서비스키 — 표준데이터/문화축제 OpenAPI 인증.
    # 다중키는 콤마 구분(쿼터 로테이션 — INGEST-E2-T2). 미설정 시 수집 잡이 호출 시점에 실패.
    DATA_GO_KR_SERVICE_KEY: Optional[str] = None
    PUBLIC_API_TIMEOUT_SEC: int = 20

    # Pipelines — Neo4j + Gemini.
    # 모두 Optional: 값이 없으면 파이프라인 라우트는 import 시점에 실패하지 않고
    # 호출 시점에 RuntimeError 로 명확히 실패함 (테스트 환경 호환성).
    NEO4J_URI: Optional[str] = None
    NEO4J_USERNAME: str = "neo4j"
    NEO4J_PASSWORD: Optional[str] = None
    NEO4J_DATABASE: str = "neo4j"

    GEMINI_API_KEY: Optional[str] = None

    # ===== Gemini 모델 — 등급별 분기 =====
    # 정책 (2026-05): Free 는 저비용 모델 (flash-lite), Pro 는 한 단계 위 (flash).
    # 모델 가격 차이: flash-lite (input $0.10/M, output $0.40/M)
    #                vs flash (input $0.30/M, output $2.50/M) — output 약 6배 차이.
    # 사용자 등급별 quota 한도는 별도 (`app/core/quota.py` 의 _FREE_LIMITS/_PRO_LIMITS).
    #
    # [Fallback]
    # GEMINI_MODEL_FREE / GEMINI_MODEL_PRO 가 비어 있으면 GEMINI_MODEL (legacy) 사용.
    # 즉 두 변수 모두 미설정 시 모든 등급이 GEMINI_MODEL 단일 모델로 동작 (기존 동작 호환).
    GEMINI_MODEL: str = "gemini-2.5-flash"          # legacy / global default
    GEMINI_MODEL_FREE: Optional[str] = "gemini-2.5-flash-lite"  # Free·저비용 기본 (env override)
    GEMINI_MODEL_PRO: Optional[str] = None          # 비어있으면 GEMINI_MODEL 로 fallback
    # [2026-06] Lite 오버플로우 모델 — 유료 등급이 메인(Flash) 월간 쿼터를 소진하면
    # 이 저비용 모델로 강등해 작업을 이어간다 (Pro=일일캡 소프트랜딩, Pro+/Max=무제한).
    # 기본값을 live 저비용 모델로 고정 — env 미설정이어도 비싼 Flash 로 새지 않게
    # (과거 None→GEMINI_MODEL=Flash fallback 으로 오버플로우가 5배 청구된 버그 재발 방지).
    GEMINI_MODEL_LITE: Optional[str] = "gemini-2.5-flash-lite"

    @property
    def gemini_model_for_free(self) -> str:
        """Free 등급 사용자에게 적용할 Gemini 모델."""
        return self.GEMINI_MODEL_FREE or self.GEMINI_MODEL

    @property
    def gemini_model_for_pro(self) -> str:
        """Pro 등급 사용자에게 적용할 Gemini 모델."""
        return self.GEMINI_MODEL_PRO or self.GEMINI_MODEL

    @property
    def gemini_model_lite(self) -> str:
        """메인 쿼터 소진 후 오버플로우(Lite) 모델. 미설정 시 free 모델 → legacy fallback."""
        return self.GEMINI_MODEL_LITE or self.GEMINI_MODEL_FREE or self.GEMINI_MODEL

    # Swagger UI Basic Auth (운영 환경에서 /docs 보호)
    DOCS_USERNAME: str = "admin"
    DOCS_PASSWORD: Optional[str] = None

    # 컬럼 암호화 — OAuth access_token 등을 저장할 때 Fernet 으로 암호화.
    # base64 urlsafe 32 byte. 생성: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
    # 미설정 시 평문 저장 (개발 편의) — 운영에서는 반드시 설정.
    TOKEN_ENCRYPTION_KEY: Optional[str] = None

    # OAuth state 토큰 서명용 별도 비밀키 (2026-05 H2 — 키 분리 정책).
    #
    # 이전: github_oauth 의 state 토큰을 JWT_SECRET_KEY 로 서명 → access/refresh 와
    #     같은 키 공유. JWT_SECRET 누설 시 OAuth state 도 위조 가능 → 영향 범위
    #     축소를 위해 키 분리.
    # 이후: OAUTH_STATE_SECRET_KEY 별도 설정 권장. 미설정 시 JWT_SECRET_KEY 로 fallback
    #     (기존 환경 호환). 운영 환경 부팅 시 미설정이면 warning 로그 (강제 fail X
    #     — 기존 배포 깨지 않게).
    # 생성: `openssl rand -hex 32`.
    OAUTH_STATE_SECRET_KEY: Optional[str] = None

    @property
    def oauth_state_secret(self) -> str:
        """OAuth state 토큰 서명 키. 별도 키가 없으면 JWT_SECRET 로 fallback."""
        return self.OAUTH_STATE_SECRET_KEY or self.JWT_SECRET_KEY

    # GitHub OAuth (https://github.com/settings/developers 에서 OAuth App 등록).
    # 모두 Optional — 미설정 시 /auth/github/* 라우트가 503 으로 응답하여
    # 기능만 비활성화되고 다른 라우트는 정상 동작.
    GITHUB_OAUTH_CLIENT_ID: Optional[str] = None
    GITHUB_OAUTH_CLIENT_SECRET: Optional[str] = None
    # GitHub OAuth App 등록 시 입력한 "Authorization callback URL" 과 정확히 일치해야 함.
    # 운영: https://api.your-domain.com/auth/github/callback
    # 로컬: http://localhost:8000/auth/github/callback
    GITHUB_OAUTH_REDIRECT_URI: Optional[str] = None
    # 사용자 GitHub 권한 범위. private repo 까지 다루려면 `repo` 포함.
    # 콤마 또는 공백 구분 (예: "read:user,user:email,repo").
    GITHUB_OAUTH_SCOPES: str = "read:user,user:email,repo"
    # BE 가 OAuth 처리 후 FE 의 어디로 redirect 할지.
    # FE 가 /auth/callback 라우트에서 token query 를 받아 저장.
    # 운영: https://your-frontend.vercel.app/auth/callback
    # 로컬: http://localhost:5173/auth/callback
    FRONTEND_OAUTH_CALLBACK_URL: Optional[str] = None

    # ===== Google OAuth (2026-05 추가) =====
    # Google Cloud Console (https://console.cloud.google.com) > APIs & Services > Credentials
    # > Create Credentials > OAuth client ID > Web application 으로 등록.
    # Authorized redirect URIs 에 아래 GOOGLE_OAUTH_REDIRECT_URI 값 정확히 입력.
    GOOGLE_OAUTH_CLIENT_ID: Optional[str] = None
    GOOGLE_OAUTH_CLIENT_SECRET: Optional[str] = None
    # 운영: https://api.example.com/auth/google/callback
    # 로컬: http://localhost:8000/auth/google/callback
    GOOGLE_OAUTH_REDIRECT_URI: Optional[str] = None
    # Google scopes — 콤마/공백 구분. openid + email + profile 표준 (최소 권한).
    GOOGLE_OAUTH_SCOPES: str = "openid,email,profile"

    # ===== Paddle (MoR) — 2026-06 결제 전환 =====
    # (Toss Payments + Internal Cron Secret 설정은 Paddle MoR 전환으로 제거 —
    #  결제 시작/갱신/대사/환불을 Paddle 이 처리하므로 자체 PG 키·cron 인증이 불필요.)
    # https://developer.paddle.com — Billing v2. 한국 사업자 셀러 가입 후 발급.
    #   - VITE_PADDLE_CLIENT_TOKEN (공개) 는 FE(.env)만. 여기 BE 에는 시크릿만.
    #   - PADDLE_API_KEY : 서버 전용 (구독 조회/취소 등 Paddle API 호출). 절대 노출 금지.
    #   - PADDLE_WEBHOOK_SECRET : 웹훅 서명(Paddle-Signature) 검증용. Notifications 등록 시 발급.
    #   - PADDLE_PRICE_* : Price ID → 등급 매핑 (웹훅이 price.id 로 등급 판별).
    # [환경] sandbox 에서 가짜카드로 전체 흐름 검증 후 production 키로 교체.
    PADDLE_API_KEY: Optional[str] = None
    PADDLE_WEBHOOK_SECRET: Optional[str] = None
    PADDLE_ENV: str = "sandbox"  # 'sandbox' | 'production' — production 으로 set 시 API base 자동 전환
    # Price ID(pri_...) → 등급. FE 의 VITE_PADDLE_PRICE_* 와 동일 값.
    PADDLE_PRICE_PRO: Optional[str] = None
    PADDLE_PRICE_PRO_PLUS: Optional[str] = None
    PADDLE_PRICE_PRO_MAX: Optional[str] = None
    # 연간 Price ID — 월간과 같은 등급으로 매핑 (FE env 계약은 월/연 6개. 누락 시 연간 결제 등급부여 실패).
    PADDLE_PRICE_PRO_Y: Optional[str] = None
    PADDLE_PRICE_PRO_PLUS_Y: Optional[str] = None
    PADDLE_PRICE_PRO_MAX_Y: Optional[str] = None

    # ===== Notion OAuth (2026-05-17 추가) =====
    # https://www.notion.so/my-integrations 에서 Public Integration 등록.
    # Redirect URI 는 아래 NOTION_OAUTH_REDIRECT_URI 와 정확히 일치 필요.
    # 권한: Read content, Read user information (사용자 인증 + 페이지 조회).
    NOTION_OAUTH_CLIENT_ID: Optional[str] = None
    NOTION_OAUTH_CLIENT_SECRET: Optional[str] = None
    # 운영: https://api.example.com/auth/notion/callback
    # 로컬: http://localhost:8000/auth/notion/callback
    NOTION_OAUTH_REDIRECT_URI: Optional[str] = None

    # ===== Resend 이메일 (2026-05 — 비밀번호 찾기 reset 링크 발송) =====
    # https://resend.com 에서 API key 발급 (무료 100통/월). 운영 도메인 인증 필요
    # (재 발송 도메인 SPF/DKIM 설정 — Resend 대시보드에서 가이드 제공).
    RESEND_API_KEY: Optional[str] = None
    # 발송자 이메일 — 운영 도메인의 검증된 주소 (예: "Harness <no-reply@example.com>").
    # 미설정 시 default 운영 fallback. 도메인 미검증이면 발송 실패 가능.
    RESEND_FROM_EMAIL: str = "Harness <onboarding@resend.dev>"
    # 비밀번호 찾기 reset 링크가 가리킬 FE URL. 예: "https://example.com/reset-password"
    # 미설정 시 FRONTEND_OAUTH_CALLBACK_URL 의 origin + /reset-password 로 추정.
    PASSWORD_RESET_URL: Optional[str] = None
    # 팀 초대 링크 base URL — "http://localhost:5173" 형태. 미설정 시 동일 fallback.
    FRONTEND_URL: str = "http://localhost:5173"

    # ===== 관측성 (Observability) — 2026-05 B2C 운영 강화 =====
    # 로그 포맷: "text"(사람이 grep, default) | "json"(운영 로그 수집기/구조화).
    LOG_FORMAT: str = "text"
    # root 로그 레벨 — DEBUG/INFO/WARNING/ERROR.
    LOG_LEVEL: str = "INFO"

    # Sentry 에러 추적 — DSN 미설정 시 완전 비활성(no-op). https://sentry.io
    SENTRY_DSN: Optional[str] = None
    # APM 트레이스 샘플링 비율(0.0~1.0). 비용 고려 기본 0 (에러만 수집, 트레이스 X).
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    # 배포 식별용 release 태그 (예: git sha). 미설정 시 sentry 자동 추정.
    SENTRY_RELEASE: Optional[str] = None

    # [2026-06 완성도 자동화] design 파이프라인(createDesign)이 SPACK 생성 직후
    # error_cases/auth 가 빈 API 를 자동으로 채울지 여부. 켜면 사용자가 "AI로 채우기"
    # 버튼을 따로 누르지 않아도 디자인 업데이트 한 번으로 API 에러/인증 명세 초안까지
    # 채워진다(빈 것만, 사람이 적은 명세는 보존). 끄면 기존처럼 수동 버튼만.
    # 실패해도 design 결과는 보존 — autofill 은 best-effort 후처리다.
    DESIGN_AUTOFILL_API_SPECS: bool = True
    # design 안에 녹인 autofill 의 전체 시간 예산(초). design 3-stage 자체가 대형 PRD 시
    # 10~20분 걸릴 수 있고 arq job_timeout(기본 1200s)을 공유하므로, autofill 이
    # 무한정 늘어 design 잡 전체를 timeout 으로 죽이지 않도록 상한을 둔다. 초과 시
    # autofill 만 잘리고(design 결과는 이미 DB 저장됨) 잡은 정상 성공한다.
    DESIGN_AUTOFILL_BUDGET_SEC: int = 240
    # [2026-06-10 autofill 고도화] 병렬 LLM 동시 호출 상한 — 이전 하드코딩 5.
    # 유료 tier RPM 여유가 커서 기본 8 (예: 대상 14개 기준 3웨이브→2웨이브, ~33% 단축).
    # 무료 키 위주 운영이거나 rate limit(429)가 잦으면 env 로 낮춘다.
    AUTOFILL_LLM_CONCURRENCY: int = 8
    # autofill 초안 생성 모델 강제(예: "gemini-2.5-flash-lite"). 미설정이면 기존 동작
    # (구독 모델 — Pro=flash). 초안은 source=ai_draft·reviewed=False(0.5점)로 사람
    # 검토가 전제라 경량 모델이어도 무방 — lite 는 비-thinking 이라 호출당 시간 절반
    # 이하 + 비용 ~1/6. 운영에서 품질 확인(A/B) 후 켜는 노브.
    AUTOFILL_DRAFT_MODEL: Optional[str] = None

    # Prometheus /metrics 노출 여부. 끄면 /metrics 가 404.
    METRICS_ENABLED: bool = True
    # 워커 메트릭 노출 포트 — arq 워커는 HTTP 서버가 없어 backend 의 /metrics 로
    # worker_jobs_total 등을 볼 수 없다. on_startup 에서 이 포트로 독립
    # prometheus 노출 서버(데몬 스레드)를 띄워 같은 docker 네트워크의 Prometheus 가
    # 스크랩한다. METRICS_ENABLED=false 또는 prometheus_client 미설치 시 미기동.
    WORKER_METRICS_PORT: int = 9100

    @property
    def sentry_enabled(self) -> bool:
        return bool(self.SENTRY_DSN)

    @property
    def sentry_environment(self) -> str:
        """Sentry 이벤트 환경 태그 — ENV 그대로 사용."""
        return self.ENV

    # 부팅 시 자동으로 is_admin=true 로 승격할 이메일들. 콤마/공백 구분.
    # 예: "admin@example.com,other@example.com"
    # 가입 전 이메일이면 skip — 가입 후 다음 부팅에 적용.
    ADMIN_EMAILS: str = ""

    @property
    def admin_emails_list(self) -> List[str]:
        raw = (self.ADMIN_EMAILS or "").replace(",", " ").split()
        return [e.strip().lower() for e in raw if e.strip()]

    @property
    def github_oauth_enabled(self) -> bool:
        return bool(
            self.GITHUB_OAUTH_CLIENT_ID
            and self.GITHUB_OAUTH_CLIENT_SECRET
            and self.GITHUB_OAUTH_REDIRECT_URI
            and self.FRONTEND_OAUTH_CALLBACK_URL
        )

    @property
    def github_oauth_scopes_list(self) -> List[str]:
        raw = self.GITHUB_OAUTH_SCOPES.replace(",", " ").split()
        return [s.strip() for s in raw if s.strip()]

    @property
    def google_oauth_enabled(self) -> bool:
        return bool(
            self.GOOGLE_OAUTH_CLIENT_ID
            and self.GOOGLE_OAUTH_CLIENT_SECRET
            and self.GOOGLE_OAUTH_REDIRECT_URI
            and self.FRONTEND_OAUTH_CALLBACK_URL
        )

    @property
    def google_oauth_scopes_list(self) -> List[str]:
        raw = self.GOOGLE_OAUTH_SCOPES.replace(",", " ").split()
        return [s.strip() for s in raw if s.strip()]

    @property
    def notion_oauth_enabled(self) -> bool:
        return bool(
            self.NOTION_OAUTH_CLIENT_ID
            and self.NOTION_OAUTH_CLIENT_SECRET
            and self.NOTION_OAUTH_REDIRECT_URI
            and self.FRONTEND_OAUTH_CALLBACK_URL
        )

    @property
    def paddle_enabled(self) -> bool:
        """Paddle 웹훅 처리 활성 — webhook secret 설정 시. (서명 검증 필수라 secret 없으면 비활성.)"""
        return bool(self.PADDLE_WEBHOOK_SECRET)

    @property
    def paddle_api_base(self) -> str:
        """Paddle REST API base — PADDLE_ENV 에서 자동 도출 (production 전환은 env 한 줄로)."""
        return "https://api.paddle.com" if self.PADDLE_ENV == "production" else "https://sandbox-api.paddle.com"

    @property
    def paddle_price_to_tier(self) -> dict[str, str]:
        """Paddle Price ID → 구독 등급 (월간+연간). 웹훅이 price.id 로 등급 판별 (미설정 항목은 제외)."""
        from app.core.subscription import (
            SUBSCRIPTION_PRO,
            SUBSCRIPTION_PRO_MAX,
            SUBSCRIPTION_PRO_PLUS,
        )
        pairs = [
            (self.PADDLE_PRICE_PRO, SUBSCRIPTION_PRO),
            (self.PADDLE_PRICE_PRO_Y, SUBSCRIPTION_PRO),
            (self.PADDLE_PRICE_PRO_PLUS, SUBSCRIPTION_PRO_PLUS),
            (self.PADDLE_PRICE_PRO_PLUS_Y, SUBSCRIPTION_PRO_PLUS),
            (self.PADDLE_PRICE_PRO_MAX, SUBSCRIPTION_PRO_MAX),
            (self.PADDLE_PRICE_PRO_MAX_Y, SUBSCRIPTION_PRO_MAX),
        ]
        return {pid: tier for pid, tier in pairs if pid}

    def paddle_price_for_tier(self, tier: str, cycle: str = "monthly") -> Optional[str]:
        """등급+주기 → Paddle Price ID. 기존 구독자의 등급 변경(change-subscription)이
        PATCH /subscriptions 에 넘길 '새 price' 를 고를 때 사용 (price_to_tier 의 역방향).
        미설정/미지원 등급(free 등)이면 None."""
        from app.core.subscription import (
            SUBSCRIPTION_PRO,
            SUBSCRIPTION_PRO_MAX,
            SUBSCRIPTION_PRO_PLUS,
        )
        monthly = {
            SUBSCRIPTION_PRO: self.PADDLE_PRICE_PRO,
            SUBSCRIPTION_PRO_PLUS: self.PADDLE_PRICE_PRO_PLUS,
            SUBSCRIPTION_PRO_MAX: self.PADDLE_PRICE_PRO_MAX,
        }
        yearly = {
            SUBSCRIPTION_PRO: self.PADDLE_PRICE_PRO_Y,
            SUBSCRIPTION_PRO_PLUS: self.PADDLE_PRICE_PRO_PLUS_Y,
            SUBSCRIPTION_PRO_MAX: self.PADDLE_PRICE_PRO_MAX_Y,
        }
        table = yearly if cycle == "yearly" else monthly
        return table.get(tier)

    @property
    def email_enabled(self) -> bool:
        """Resend API 키 설정 시 이메일 발송 가능."""
        return bool(self.RESEND_API_KEY)

    @property
    def password_reset_url(self) -> str:
        """비밀번호 reset 링크의 base URL — FE 페이지 경로."""
        if self.PASSWORD_RESET_URL:
            return self.PASSWORD_RESET_URL
        # FRONTEND_OAUTH_CALLBACK_URL 의 origin 추출 + /reset-password 결합
        cb = self.FRONTEND_OAUTH_CALLBACK_URL or ""
        if cb:
            from urllib.parse import urlparse
            p = urlparse(cb)
            if p.scheme and p.netloc:
                return f"{p.scheme}://{p.netloc}/reset-password"
        return "http://localhost:5173/reset-password"

    @property
    def cors_origins_list(self) -> List[str]:
        """콤마 구분 문자열을 리스트로 변환.

        [Fail-fast — '*' 거부]
        운영 환경에서 `CORS_ORIGINS='*'` 는 거부.
        FastAPI 의 CORSMiddleware 는 `allow_origins=['*']` + `allow_credentials=True`
        조합 시 모든 origin 으로 credentialed 요청을 허용하려 시도한다.
        브라우저가 거부하지만 운영 envvar 실수로 한 번이라도 토큰 탈취 표면이 열리는
        것을 차단하기 위해 부팅 시점에 명시적으로 reject.

        - 운영(production) + '*'  → ValueError (boot 중단).
        - 로컬/dev + '*'          → ['*'] 허용 (편의).

        [Fail-fast — 운영에서 빈 list / localhost-only]
        2026-05-18: 운영 docker-compose 에서 `CORS_ORIGINS=${CORS_ORIGINS}` 가 unset 이면
        default localhost 만 들어가고, 브라우저가 `example.com` → API 호출 시
        `No 'Access-Control-Allow-Origin' header` 로 silently 차단되던 문제. 백엔드는
        200 OK 를 반환하지만 CORS 헤더가 없어 클라이언트는 응답 활용 못 함.
        부팅 시점에 명확히 ValueError 로 알려 운영자가 envvar 설정하게 강제.
        """
        raw = self.CORS_ORIGINS.strip()
        if raw == "*":
            if self.is_production:
                raise ValueError(
                    "CORS_ORIGINS='*' 는 운영 환경에서 사용할 수 없습니다. "
                    "allow_credentials=True 와의 조합이 위험 표면을 만들기 때문에 "
                    "허용된 origin 목록을 콤마로 구분해 명시하세요 "
                    "(예: CORS_ORIGINS='https://gayoje.example')."
                )
            return ["*"]
        parsed = [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
        if self.is_production:
            if not parsed:
                raise ValueError(
                    "CORS_ORIGINS 가 운영 환경에서 비어 있습니다. "
                    "FastAPI CORSMiddleware 가 어떤 origin 에도 "
                    "`Access-Control-Allow-Origin` 헤더를 붙이지 않아 "
                    "브라우저가 API 응답을 차단합니다. "
                    "Portainer/배포 환경변수에 운영 도메인을 콤마로 구분해 설정하세요 "
                    "(예: CORS_ORIGINS='https://example.com,https://www.example.com')."
                )
            has_real_origin = any(
                "localhost" not in o and "127.0.0.1" not in o for o in parsed
            )
            if not has_real_origin:
                raise ValueError(
                    "CORS_ORIGINS 에 운영 도메인이 없고 localhost 만 포함돼 있습니다. "
                    "default 값이 그대로 사용된 상태로 추정됩니다 — "
                    "Portainer/배포 환경변수에 실제 운영 도메인을 추가하세요 "
                    f"(현재 값: {parsed!r})."
                )
        return parsed

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() == "production"

    @property
    def is_staging(self) -> bool:
        return self.ENV.lower() in ("staging", "stg")

    @property
    def is_development(self) -> bool:
        """production/staging 이 아니면 개발 환경."""
        return not (self.is_production or self.is_staging)

    @property
    def data_go_kr_service_keys(self) -> List[str]:
        """공공데이터 서비스키 목록 (다중키 로테이션용)."""
        raw = (self.DATA_GO_KR_SERVICE_KEY or "").replace(",", " ").split()
        return [k.strip() for k in raw if k.strip()]

    def feature_flags(self) -> dict:
        """현재 feature flag 스냅샷 (관측/디버그용)."""
        return {
            "payments": self.FEATURE_PAYMENTS,
            "notifications": self.FEATURE_NOTIFICATIONS,
            "ingest_crawler": self.FEATURE_INGEST_CRAWLER,
            "ingest_public_api": self.FEATURE_INGEST_PUBLIC_API,
        }

    def safe_summary(self) -> dict:
        """시크릿을 마스킹한 설정 요약 — 부팅 로그/디버그용(평문 시크릿 미노출).

        - *_URL: 자격증명(password)만 마스킹.
        - KEY/SECRET/PASSWORD/TOKEN/DSN 포함 필드: 앞 4자만 노출.
        properties/메서드(Paddle 등)는 호출하지 않으므로 dormant 지연 import 도 안전.
        """
        out: dict = {}
        for name, value in self.model_dump().items():
            upper = name.upper()
            if value and upper.endswith("_URL"):
                out[name] = mask_db_url(str(value))
            elif value and any(h in upper for h in _SECRET_FIELD_HINTS):
                out[name] = mask_secret(str(value))
            else:
                out[name] = value
        return out

    # ===== 부팅 시점 시크릿 검증 (운영 환경 한정) =====
    #
    # CORS '*' 거부 (cors_origins_list) 와 동일한 fail-fast 정책.
    # 차이점: CORS 는 첫 호출 시점 lazy 검증 — JWT 는 토큰 발급/검증 hot path 라
    # 매번 검증하면 비용. 그래서 Settings 인스턴스화 시점 (= 모듈 import 시점)에
    # 1회 검증.
    #
    # [거부 케이스]
    # - JWT_SECRET_KEY 가 default placeholder 그대로 → 토큰 위조 가능 (모두가 아는 값)
    # - JWT_SECRET_KEY 가 32 byte 미만 → 무차별 대입 가능
    #
    # [통과 케이스]
    # - 운영 외 환경 (development/local/staging…) — placeholder 허용 (편의)
    # - 운영 + 충분히 긴 secret — 정상
    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        if not self.is_production:
            return self
        # Worker context — worker 는 JWT 발급/검증을 수행하지 않음 (시크릿 표면 축소
        # 정책상 의도적으로 JWT_SECRET_KEY 미주입). 그러나 quota / pipelines import
        # chain 이 settings 평가를 강제 → worker 부팅 시 JWT_SECRET 부재로 fail.
        # docker-compose 의 worker 서비스에 HARNESS_WORKER_CONTEXT=1 설정 → 이 분기로
        # JWT validator skip. backend 는 flag 없으므로 정상 검증.
        if os.getenv("HARNESS_WORKER_CONTEXT") == "1":
            return self
        if self.JWT_SECRET_KEY == _DEFAULT_JWT_SECRET_PLACEHOLDER:
            raise ValueError(
                "JWT_SECRET_KEY 가 default placeholder 값이라 운영 환경에서 사용할 수 없습니다. "
                "`openssl rand -hex 32` 결과를 .env / Portainer Stack env 의 "
                "JWT_SECRET_KEY 에 설정하세요. 이 값이 노출되면 모든 사용자 토큰이 위조 가능합니다."
            )
        if len(self.JWT_SECRET_KEY) < _JWT_SECRET_MIN_LENGTH:
            raise ValueError(
                f"JWT_SECRET_KEY 가 너무 짧습니다 (현재 {len(self.JWT_SECRET_KEY)}자, "
                f"최소 {_JWT_SECRET_MIN_LENGTH}자 필요). 무차별 대입에 취약합니다. "
                "`openssl rand -hex 32` 로 재발급하세요."
            )
        # [2026-05 M1] TOKEN_ENCRYPTION_KEY 운영 강제 검증.
        # 미설정 시 token_encryption.py 가 평문 저장 + warning 로그 — 운영에서는 위험.
        # GitHub OAuth access_token 이 평문으로 Neo4j 에 저장되면 DB 덤프 유출 시
        # 사용자 GitHub 접근까지 노출됨. 운영에서는 반드시 키 설정 강제.
        if not self.TOKEN_ENCRYPTION_KEY:
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY 가 운영 환경에서 미설정입니다. "
                "OAuth access_token 등의 컬럼 암호화에 필요합니다. "
                "생성: `python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\"`. "
                "이 값이 노출되면 저장된 OAuth 토큰이 모두 평문 노출됩니다."
            )
        return self


# 싱글톤처럼 사용
settings = Settings()
