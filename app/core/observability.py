"""
관측성(Observability) — 구조화 로깅 + Sentry 에러 추적 일괄 설정.

[배경 — 2026-05 B2C 운영 강화]
이전 상태:
  - 로깅: main.py 의 logging.basicConfig text 포맷 1개 (req_id/user_email 첨부됨).
  - 에러추적: 없음 — worker/route 예외가 stdout 로그로만 흘러가 운영에서
    "어떤 사용자가 왜 이탈했는지" 사후 추적 불가.
  - 메트릭: 없음 (app/core/metrics.py 가 별도 담당).

이 모듈이 제공:
  1. setup_logging() — LOG_FORMAT=json|text, LOG_LEVEL 환경변수로 토글되는
     중앙 로깅 설정. JSON 포맷은 운영 로그 수집기(Loki/CloudWatch 등)용.
  2. init_sentry(component) — SENTRY_DSN 설정 시에만 활성. backend / worker
     양쪽에서 호출. DSN 미설정이거나 sentry_sdk 미설치면 완전 no-op (운영 안전).

[설계 원칙]
  - 의존성 graceful degradation: sentry_sdk 가 없거나 DSN 미설정이어도 import /
    호출이 절대 실패하지 않음 (기존 배포·테스트 환경 보호).
  - request_id / user_email 컨텍스트는 기존 request_context._ContextFilter 재사용 —
    text/json 양쪽 포맷에서 동일하게 첨부.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from typing import Optional

from app.core.config import settings
from app.core.request_context import _ContextFilter, install_request_context_logging


# ===== JSON 포맷터 =====


# JSON 라인에 그대로 옮길 표준 LogRecord 속성 (이미 별도 필드로 추출).
_RESERVED_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
        "request_id", "user_email", "taskName",
    }
)


class JsonLogFormatter(logging.Formatter):
    """LogRecord 를 한 줄 JSON 으로 직렬화 — 운영 로그 수집기 친화 포맷.

    필수 필드: ts(ISO8601 UTC), level, logger, msg, request_id, user_email.
    예외 발생 시 exc_type / exc 스택을 함께 첨부.
    logger.info(..., extra={...}) 로 넘긴 커스텀 키도 보존(예약 키 제외).
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = _dt.datetime.fromtimestamp(
            record.created, tz=_dt.timezone.utc
        ).isoformat()
        payload = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            # _ContextFilter 가 첨부 — 누락 시(필터 미부착) 안전 default.
            "request_id": getattr(record, "request_id", "-"),
            "user_email": getattr(record, "user_email", "-"),
        }
        if record.exc_info:
            payload["exc_type"] = getattr(record.exc_info[0], "__name__", "Exception")
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # extra={...} 로 넘긴 커스텀 필드 보존 (예약 키 / private 제외).
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            if key not in payload:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


_TEXT_FORMAT = (
    "%(asctime)s [%(levelname)s] %(name)s "
    "req=%(request_id)s user=%(user_email)s: %(message)s"
)


def setup_logging() -> None:
    """중앙 로깅 설정 — main.py / worker 부팅 시 1회 호출.

    LOG_FORMAT=json 이면 JSON 한 줄 포맷, 그 외(text)면 기존 사람-친화 포맷.
    LOG_LEVEL 로 root 레벨 조정 (기본 INFO).

    basicConfig 와 달리 root 의 기존 핸들러를 교체 — 중복 로깅/포맷 혼선 방지.
    """
    root = logging.getLogger()

    level_name = (settings.LOG_LEVEL or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    if (settings.LOG_FORMAT or "text").lower() == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    # request_id / user_email 첨부 필터 — text/json 양쪽 모두 필요.
    handler.addFilter(_ContextFilter())

    # 기존 핸들러 제거 후 교체 (basicConfig 잔재 / 재호출 멱등성).
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)

    # contextvar 필터를 root 의 모든 핸들러에 보장 (방어적 — 위에서 이미 부착).
    install_request_context_logging(root)

    # 노이즈 로거 억제 (기존 main.py 정책 유지).
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ===== Sentry 에러 추적 =====


def init_sentry(component: str = "backend") -> bool:
    """SENTRY_DSN 설정 시 Sentry 초기화. 반환값: 활성화 여부.

    component: "backend" | "worker" — Sentry 이벤트 태그로 붙어 어느 프로세스에서
    난 에러인지 구분. DSN 미설정 / sentry_sdk 미설치면 조용히 no-op(False 반환).
    """
    if not settings.sentry_enabled:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logging.getLogger("harness.app").warning(
            "SENTRY_DSN 설정됐으나 sentry_sdk 미설치 — 에러추적 비활성. "
            "`pip install sentry-sdk[fastapi]` 필요."
        )
        return False

    try:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.sentry_environment,
            release=settings.SENTRY_RELEASE,
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            # PII(이메일 등) 는 직접 태그로만 — 자동 IP/쿠키 수집 비활성.
            send_default_pii=False,
        )
        sentry_sdk.set_tag("component", component)
        logging.getLogger("harness.app").info(
            "Sentry 활성화 (component=%s, env=%s)", component, settings.sentry_environment
        )
        return True
    except Exception as e:  # noqa: BLE001 — 관측성 초기화가 부팅을 막으면 안 됨.
        logging.getLogger("harness.app").warning("Sentry 초기화 실패: %s", e)
        return False


def capture_exception(exc: BaseException, **tags: str) -> None:
    """예외를 Sentry 로 전송 — Sentry 미활성 시 no-op.

    worker job 처럼 미들웨어 밖에서 발생한 예외를 명시적으로 보고할 때 사용.
    """
    try:
        import sentry_sdk
    except ImportError:
        return
    if not settings.sentry_enabled:
        return
    # sentry-sdk 2.x: push_scope() 는 deprecated → new_scope() 사용 (운영 로그 경고 방지).
    with sentry_sdk.new_scope() as scope:
        for key, value in tags.items():
            scope.set_tag(key, value)
        sentry_sdk.capture_exception(exc)
