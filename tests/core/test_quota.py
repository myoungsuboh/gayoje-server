"""
quota 정책 단위 테스트 — 한도 상수 + QuotaExceeded 응답 형식.

cypher 가 없는 순수 정책 모듈이라 빠른 테스트만.
"""
from __future__ import annotations

import pytest

from app.core import quota
from app.core.quota import (
    ERROR_CODE_QUOTA_EXCEEDED,
    QuotaExceeded,
    get_limit,
    get_limits,
    get_max_projects,
)
from app.core.subscription import (
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    SUBSCRIPTION_PRO_MAX,
    SUBSCRIPTION_PRO_PLUS,
)


# ─── get_limits / get_limit ──────────────────────────────────


def test_free_limits_have_expected_types():
    limits = get_limits(SUBSCRIPTION_FREE)
    # 한도 종류 — Skill Library 도입 (2026-05) 으로 library_skills 추가.
    assert set(limits.keys()) == {
        "meeting_logs",
        "summary_chars",
        "total_tokens",
        "library_skills",
    }
    # 사용자 결정한 한도값 — 변경 시 README/FE Pricing 도 갱신
    assert limits["meeting_logs"] == 5
    assert limits["summary_chars"] == 10_000
    assert limits["total_tokens"] == 1_000_000   # [2026-06] 메인 쿼터 (100K → 1M)
    assert limits["library_skills"] == 100


def test_pro_limits_are_strictly_higher_than_free():
    free = get_limits(SUBSCRIPTION_FREE)
    pro = get_limits(SUBSCRIPTION_PRO)
    for k in free:
        assert pro[k] > free[k], f"pro[{k}]={pro[k]} should be > free[{k}]={free[k]}"


def test_pro_plus_limits_are_double_pro():
    """Pro+ 는 Pro 의 정확히 2배 (요금제 정책)."""
    pro = get_limits(SUBSCRIPTION_PRO)
    pro_plus = get_limits(SUBSCRIPTION_PRO_PLUS)
    for k in pro:
        assert pro_plus[k] == pro[k] * 2, (
            f"pro_plus[{k}]={pro_plus[k]} should be 2x pro[{k}]={pro[k]}"
        )


def test_pro_max_limits_are_quadruple_pro():
    """Pro Max 는 Pro 의 정확히 4배 (요금제 정책).

    예외: total_tokens(메인 쿼터)는 마진/오버플로우 정책으로 별도 곡선 (2026-06).
    Free 1M / Pro 2M / Pro+ 4M / Pro Max 8M (2026-06-11 마진 재조정). 소진 후엔 Lite 오버플로우.
    """
    pro = get_limits(SUBSCRIPTION_PRO)
    pro_max = get_limits(SUBSCRIPTION_PRO_MAX)
    for k in pro:
        if k == "total_tokens":
            assert pro_max[k] == 8_000_000 and pro[k] == 2_000_000
            continue
        assert pro_max[k] == pro[k] * 4, (
            f"pro_max[{k}]={pro_max[k]} should be 4x pro[{k}]={pro[k]}"
        )


def test_pro_plus_lt_pro_max():
    """등급 순서: Free < Pro < Pro+ < Pro Max."""
    pro_plus = get_limits(SUBSCRIPTION_PRO_PLUS)
    pro_max = get_limits(SUBSCRIPTION_PRO_MAX)
    for k in pro_plus:
        assert pro_max[k] > pro_plus[k], (
            f"pro_max[{k}] should be > pro_plus[{k}]"
        )


def test_unknown_subscription_falls_back_to_free():
    """알 수 없는 등급값은 보수적으로 free 한도. (DB 에 비정상 값이 박혀도 차단 동작 유지.)"""
    assert get_limits("unknown") == get_limits(SUBSCRIPTION_FREE)
    assert get_limits("") == get_limits(SUBSCRIPTION_FREE)
    # None 은 dict key 가 아니라 type error 가 자연 — 호출자가 str 보장.


def test_get_limits_returns_copy():
    """호출자가 mutate 해도 다음 호출에 영향 없어야 함."""
    a = get_limits(SUBSCRIPTION_FREE)
    a["meeting_logs"] = 9999
    b = get_limits(SUBSCRIPTION_FREE)
    assert b["meeting_logs"] == 5  # 원본 보존


def test_get_limit_returns_single_value():
    # [2026-06-11 마진 재조정] 메인(Flash) 월간 쿼터 1M/2M/4M/8M. 소진 후엔 Lite 오버플로우.
    assert get_limit(SUBSCRIPTION_FREE, "meeting_logs") == 5
    assert get_limit(SUBSCRIPTION_FREE, "total_tokens") == 1_000_000
    assert get_limit(SUBSCRIPTION_PRO, "total_tokens") == 2_000_000
    assert get_limit(SUBSCRIPTION_PRO_PLUS, "total_tokens") == 4_000_000
    assert get_limit(SUBSCRIPTION_PRO_MAX, "total_tokens") == 8_000_000


# ─── get_max_projects (동시 보유 가능 프로젝트 수) ──────────


def test_max_projects_by_tier():
    """등급별 max_projects 정책 — Free 1 / Pro 3 / Pro+ 6 / Pro Max 12."""
    assert get_max_projects(SUBSCRIPTION_FREE) == 1
    assert get_max_projects(SUBSCRIPTION_PRO) == 3
    assert get_max_projects(SUBSCRIPTION_PRO_PLUS) == 6
    assert get_max_projects(SUBSCRIPTION_PRO_MAX) == 12


def test_max_projects_unknown_falls_back_to_free():
    """알 수 없는 등급은 보수적으로 free 한도 (1개)."""
    assert get_max_projects("unknown") == 1
    assert get_max_projects("") == 1
    assert get_max_projects("enterprise") == 1


# ─── QuotaExceeded ──────────────────────────────────────────


def test_quota_exceeded_dict_shape():
    """FE 가 의존하는 응답 키 — 변경 시 vue axios interceptor 도 갱신."""
    d = QuotaExceeded(
        limit_type="meeting_logs",
        current=5,
        limit=5,
        subscription_type=SUBSCRIPTION_FREE,
    ).to_dict()
    # 필수 키
    assert d["code"] == ERROR_CODE_QUOTA_EXCEEDED
    assert d["limit_type"] == "meeting_logs"
    assert d["current"] == 5
    assert d["limit"] == 5
    assert d["subscription_type"] == SUBSCRIPTION_FREE
    assert d["upgrade_url"] == "/pricing"
    # lifetime 정책 — reset_at 은 항상 None
    assert d["reset_at"] is None
    # message 가 default 로 채워짐
    assert "Pro" in d["message"] or "한도" in d["message"]


def test_quota_exceeded_custom_message_and_upgrade_url():
    d = QuotaExceeded(
        limit_type="total_tokens",
        current=100_001,
        limit=100_000,
        subscription_type=SUBSCRIPTION_FREE,
        message="커스텀 메시지",
        upgrade_url="/custom",
    ).to_dict()
    assert d["message"] == "커스텀 메시지"
    assert d["upgrade_url"] == "/custom"


# ─── 2026-05 월간 reset — reset_at 응답 ──────────────


def test_quota_exceeded_with_reset_at():
    """월간 reset 도입 — reset_at 명시 시 응답에 그대로 포함."""
    d = QuotaExceeded(
        limit_type="total_tokens",
        current=100_001,
        limit=100_000,
        subscription_type=SUBSCRIPTION_FREE,
        reset_at="2026-06-17T00:00:00.000000000Z",
    ).to_dict()
    assert d["reset_at"] == "2026-06-17T00:00:00.000000000Z"


def test_quota_exceeded_reset_at_default_none():
    """reset_at 미지정 시 None (예: max_projects 같이 reset 무관 한도)."""
    d = QuotaExceeded(
        limit_type="max_projects",
        current=3,
        limit=3,
        subscription_type=SUBSCRIPTION_FREE,
    ).to_dict()
    assert d["reset_at"] is None


def test_default_message_differs_by_limit_type():
    """한도 종류마다 사용자 친화 메시지가 다름."""
    messages = set()
    for limit_type in ("meeting_logs", "summary_chars", "total_tokens"):
        d = QuotaExceeded(
            limit_type=limit_type,  # type: ignore[arg-type]
            current=0,
            limit=0,
            subscription_type=SUBSCRIPTION_FREE,
        ).to_dict()
        messages.add(d["message"])
    # 3가지 메시지가 모두 다름 (사용자가 어떤 한도에 걸렸는지 알 수 있어야 함)
    assert len(messages) == 3


def test_default_message_for_unknown_limit_type_falls_back():
    """타입 강제 우회한 비정상 값에도 메시지가 비어있지 않게."""
    d = QuotaExceeded(
        limit_type="not_a_type",  # type: ignore[arg-type]
        current=0,
        limit=0,
        subscription_type=SUBSCRIPTION_FREE,
    ).to_dict()
    assert isinstance(d["message"], str) and d["message"]


def test_quota_exceeded_message_mentions_pro_upgrade():
    """FE 의 'Pro 알아보기' 버튼 흐름과 일관 — 메시지 자체에 Pro 언급."""
    d = QuotaExceeded(
        limit_type="meeting_logs",
        current=5,
        limit=5,
        subscription_type=SUBSCRIPTION_FREE,
    ).to_dict()
    assert "Pro" in d["message"]


def test_error_code_constant_exposed():
    """admin/FE 가 import 해서 매칭 — 문자열 변경 가드."""
    assert quota.ERROR_CODE_QUOTA_EXCEEDED == "QUOTA_EXCEEDED"


# ─── token_usage_summary (관리자 토큰 % 표시 — 2026-05-27) ───────────
def test_token_usage_summary_basic_pct():
    """free 등급(메인 1M) 에서 500K 사용 → 50%."""
    out = quota.token_usage_summary(500_000, SUBSCRIPTION_FREE)
    assert out["token_used"] == 500_000
    assert out["token_limit"] == get_limit(SUBSCRIPTION_FREE, "total_tokens")
    assert out["token_pct"] == 50.0


def test_token_usage_summary_zero_used():
    out = quota.token_usage_summary(0, SUBSCRIPTION_PRO_MAX)
    assert out["token_pct"] == 0.0


def test_token_usage_summary_over_limit_not_capped():
    """초과 사용은 100% 초과로 그대로 — 관리자가 초과 인지 가능."""
    limit = get_limit(SUBSCRIPTION_FREE, "total_tokens")
    out = quota.token_usage_summary(limit * 2, SUBSCRIPTION_FREE)
    assert out["token_pct"] == 200.0


def test_token_usage_summary_zero_limit_safe(monkeypatch):
    """한도 0(방어) → ZeroDivision 없이 pct=None. (실제 등급 한도는 >0 이지만 방어.)"""
    monkeypatch.setattr(quota, "get_limit", lambda *_a, **_k: 0)
    out = quota.token_usage_summary(100, SUBSCRIPTION_FREE)
    assert out["token_pct"] is None
    assert out["token_limit"] == 0
