"""
quota 라우트 가드 단위 테스트 — assert_summary_within_limit / acquire_meeting_quota.

[검증 범위]
- 한도 안 → 통과 (예외 없음)
- 한도 초과 → HTTPException(402) + detail.code == QUOTA_EXCEEDED
- 사용자 없음 → HTTPException(404)
- Pro 등급은 더 높은 한도 적용
- 빈 입력 → 통과 (다른 가드가 처리)

[모킹 패턴]
quota.py 가 `from app.service import usage_repository` 를 함수 안에서 import.
즉 함수 호출 시점에 모듈 객체를 lookup. 따라서 monkeypatch.setattr 로
`app.service.usage_repository.get_usage` 함수 자체를 교체하면 동작.
sys.modules 통째 치환은 (지연 import 라도) 동작 안 함 — 이미 import 된 캐시가 우선.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import pytest
from fastapi import HTTPException

from app.core import quota
from app.core.quota import (
    ERROR_CODE_QUOTA_EXCEEDED,
    acquire_meeting_quota,
    assert_summary_within_limit,
    assert_tokens_within_limit,
)
from app.service.usage_repository import IncrementResult, Usage
from app.service.user_repository import SUBSCRIPTION_FREE, SUBSCRIPTION_PRO
from app.core.subscription import SUBSCRIPTION_PRO_PLUS

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_repo(monkeypatch):
    """app.service.usage_repository 의 함수들을 fake 로 교체.

    Returns:
        헬퍼 함수 — setup(usage=, increment=) 호출 시 fake state 주입 후
        (get_calls, inc_calls) 두 list 반환 → 테스트가 호출 내역 검증.
    """

    def _setup(
        *,
        usage: Optional[Usage] = None,
        increment: Optional[IncrementResult] = None,
    ) -> Tuple[List[str], List[Tuple[str, int]]]:
        get_calls: List[str] = []
        inc_calls: List[Tuple[str, int]] = []

        async def fake_get_usage(email: str) -> Optional[Usage]:
            get_calls.append(email)
            return usage

        async def fake_try_increment(email: str, limit: int) -> Optional[IncrementResult]:
            inc_calls.append((email, limit))
            return increment

        monkeypatch.setattr(
            "app.service.usage_repository.get_usage", fake_get_usage
        )
        monkeypatch.setattr(
            "app.service.usage_repository.try_increment_meeting_count",
            fake_try_increment,
        )
        return get_calls, inc_calls

    return _setup


# ─── assert_summary_within_limit ─────────────────────────────


async def test_summary_within_free_limit_passes(fake_repo):
    """글자수 4,999 (free 한도 5,000 이하) → 통과."""
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        )
    )
    # 예외 없으면 통과
    await assert_summary_within_limit("a@b.com", "a" * 4_999)


async def test_summary_at_free_limit_passes(fake_repo):
    """경계값 — 정확히 10,000 (한도 이하) 도 통과."""
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        )
    )
    await assert_summary_within_limit("a@b.com", "a" * 10_000)


async def test_summary_over_free_limit_raises_402(fake_repo):
    """10,001 자 → 402."""
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        )
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_summary_within_limit("a@b.com", "a" * 10_001)
    assert exc_info.value.status_code == 402
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == ERROR_CODE_QUOTA_EXCEEDED
    assert detail["limit_type"] == "summary_chars"
    assert detail["current"] == 10_001
    assert detail["limit"] == 10_000
    assert detail["subscription_type"] == SUBSCRIPTION_FREE
    assert detail["upgrade_url"] == "/pricing"
    # lifetime — reset 없음
    assert detail["reset_at"] is None


async def test_summary_over_pro_limit_raises(fake_repo):
    """Pro 등급은 50,000 까지 OK, 50,001 부터 거부."""
    fake_repo(
        usage=Usage(
            email="pro@b.com",
            subscription_type=SUBSCRIPTION_PRO,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        )
    )
    # 5,001 → Pro 는 통과
    await assert_summary_within_limit("pro@b.com", "a" * 5_001)
    # 50,001 → Pro 도 거부
    with pytest.raises(HTTPException) as exc_info:
        await assert_summary_within_limit("pro@b.com", "a" * 50_001)
    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["subscription_type"] == SUBSCRIPTION_PRO
    assert exc_info.value.detail["limit"] == 50_000


async def test_empty_content_passes(fake_repo):
    """빈 입력은 다른 가드(Field min_length) 영역 — 여기서는 silent 통과."""
    get_calls, _ = fake_repo()  # get_usage 호출 안 됨
    await assert_summary_within_limit("a@b.com", "")
    # 빈 입력은 get_usage 도 호출 안 함 (cypher 라운드트립 절약)
    assert len(get_calls) == 0


async def test_missing_user_falls_back_to_free_limit(fake_repo):
    """User 노드 없음 (비정상) → 보수적으로 free 한도 적용."""
    fake_repo(usage=None)
    # free 한도 10,000 기준 → 16,000 자는 거부
    with pytest.raises(HTTPException) as exc_info:
        await assert_summary_within_limit("ghost@b.com", "a" * 16_000)
    assert exc_info.value.detail["subscription_type"] == SUBSCRIPTION_FREE


# ─── acquire_meeting_quota ───────────────────────────────────


async def test_acquire_under_limit_returns_result(fake_repo):
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=2,
            total_tokens=0,
            total_chars=0,
        ),
        increment=IncrementResult(
            exceeded=False, current=3, limit=5, subscription_type=SUBSCRIPTION_FREE
        ),
    )
    result = await acquire_meeting_quota("a@b.com")
    assert result.exceeded is False
    assert result.current == 3


async def test_acquire_passes_free_limit_to_repo(fake_repo):
    """free 사용자의 limit param 으로 5 가 전달되는지 회귀 가드."""
    _, inc_calls = fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        increment=IncrementResult(
            exceeded=False, current=1, limit=5, subscription_type=SUBSCRIPTION_FREE
        ),
    )
    await acquire_meeting_quota("a@b.com")
    # try_increment_meeting_count 가 (email, limit=5) 로 호출됨
    assert inc_calls == [("a@b.com", 5)]


async def test_acquire_passes_pro_limit_to_repo(fake_repo):
    """pro 사용자의 limit param 으로 50 이 전달되는지 회귀 가드.

    값 50 의 출처: app/core/quota.py::_PRO_LIMITS["meeting_logs"] = 50
    (2026-05 운영 조정 — 실측 헤비유저 ≤ 월 30건 기준 100→50 하향).
    상수 변경 시 이 테스트도 따라가야 함.
    """
    _, inc_calls = fake_repo(
        usage=Usage(
            email="pro@b.com",
            subscription_type=SUBSCRIPTION_PRO,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        increment=IncrementResult(
            exceeded=False, current=1, limit=50, subscription_type=SUBSCRIPTION_PRO
        ),
    )
    await acquire_meeting_quota("pro@b.com")
    assert inc_calls == [("pro@b.com", 50)]


async def test_acquire_exceeded_raises_402(fake_repo):
    """한도 도달 → 402 + QUOTA_EXCEEDED."""
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=5,
            total_tokens=0,
            total_chars=0,
        ),
        increment=IncrementResult(
            exceeded=True, current=5, limit=5, subscription_type=SUBSCRIPTION_FREE
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        await acquire_meeting_quota("a@b.com")
    assert exc_info.value.status_code == 402
    detail = exc_info.value.detail
    assert detail["code"] == ERROR_CODE_QUOTA_EXCEEDED
    assert detail["limit_type"] == "meeting_logs"
    assert detail["current"] == 5
    assert detail["limit"] == 5


async def test_acquire_no_user_returns_404(fake_repo):
    """User 노드 없음 → 404 (라우트 인증 통과한 비정상 상태 — 사용자에게 재로그인 안내)."""
    fake_repo(usage=None)
    with pytest.raises(HTTPException) as exc_info:
        await acquire_meeting_quota("ghost@b.com")
    assert exc_info.value.status_code == 404


async def test_acquire_race_during_increment_returns_404(fake_repo):
    """get_usage 후 try_increment 사이에 노드가 사라진 race — 404 동일 응답."""
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
        ),
        increment=None,  # try_increment 가 None 반환 = 노드 사라짐
    )
    with pytest.raises(HTTPException) as exc_info:
        await acquire_meeting_quota("a@b.com")
    assert exc_info.value.status_code == 404


# ─── assert_tokens_within_limit ──────────────────────────────


async def test_tokens_under_free_limit_passes(fake_repo):
    """누적 999,999 토큰 (free 메인 한도 1,000,000 미만) → 통과."""
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=999_999,
            total_chars=0,
        )
    )
    # 예외 없으면 통과
    await assert_tokens_within_limit("a@b.com")


async def test_tokens_at_free_limit_raises_402(fake_repo):
    """경계값 — 정확히 1,000,000 토큰 (free 메인 한도) → 하드월 차단 (오버플로우 없음)."""
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=1_000_000,
            total_chars=0,
        )
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_tokens_within_limit("a@b.com")
    assert exc_info.value.status_code == 402
    detail = exc_info.value.detail
    assert detail["code"] == ERROR_CODE_QUOTA_EXCEEDED
    assert detail["limit_type"] == "total_tokens"
    assert detail["current"] == 1_000_000
    assert detail["limit"] == 1_000_000
    assert detail["subscription_type"] == SUBSCRIPTION_FREE


async def test_tokens_over_free_limit_raises(fake_repo):
    """Free 메인 초과 (1,000,001) → 하드월 차단."""
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=1_000_001,
            total_chars=0,
        )
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_tokens_within_limit("a@b.com")
    assert exc_info.value.detail["current"] == 1_000_001


async def test_tokens_within_pro_limit_passes(fake_repo):
    """Pro 사용자는 메인 한도(2,000,000) 직전까지 통과."""
    fake_repo(
        usage=Usage(
            email="pro@b.com",
            subscription_type=SUBSCRIPTION_PRO,
            meeting_count=0,
            total_tokens=100_000,
            total_chars=0,
        )
    )
    await assert_tokens_within_limit("pro@b.com")
    # 메인 한도(2M) 직전 1.9M 도 OK
    fake_repo(
        usage=Usage(
            email="pro@b.com",
            subscription_type=SUBSCRIPTION_PRO,
            meeting_count=0,
            total_tokens=1_900_000,
            total_chars=0,
        )
    )
    await assert_tokens_within_limit("pro@b.com")


async def test_tokens_over_pro_main_overflows_not_blocked(fake_repo):
    """[2026-06] Pro 가 메인(2M) 소진해도 차단 아님 — Lite 오버플로우로 통과.

    주간 Lite 캡(1.5M) 잔여가 있으므로 mode=overflow → 가드 통과.
    """
    fake_repo(
        usage=Usage(
            email="pro@b.com",
            subscription_type=SUBSCRIPTION_PRO,
            meeting_count=0,
            total_tokens=2_000_000,
            total_chars=0,
            lite_daily_tokens=0,
        )
    )
    decision = await assert_tokens_within_limit("pro@b.com")
    assert decision.mode == "overflow"
    assert decision.bucket == "lite"


async def test_tokens_over_pro_main_and_daily_cap_blocks(fake_repo):
    """Pro 가 메인 소진 + 주간 Lite 캡(1.5M)도 소진 → 차단 (엔터프라이즈 넛지)."""
    fake_repo(
        usage=Usage(
            email="pro@b.com",
            subscription_type=SUBSCRIPTION_PRO,
            meeting_count=0,
            total_tokens=2_500_000,
            total_chars=0,
            lite_daily_tokens=1_500_000,
        )
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_tokens_within_limit("pro@b.com")
    detail = exc_info.value.detail
    assert detail["subscription_type"] == SUBSCRIPTION_PRO
    assert detail["limit"] == 1_500_000        # 주간 Lite 캡
    assert "엔터프라이즈" in detail["message"]
    # [2026-06-13] 일→주 전환 후속 — 메시지가 '오늘/내일' 같은 일일 표현을 쓰면 안 됨
    # (롤링 7일인데 하루 기다려도 안 풀려 혼란). 주간 표현이어야.
    assert "내일" not in detail["message"] and "오늘" not in detail["message"]
    assert "주" in detail["message"] or "7일" in detail["message"]


async def test_tokens_pro_plus_over_main_overflows(fake_repo):
    """Pro+ 메인(4M) 소진 → Lite 무제한(공정사용) 오버플로우 통과."""
    fake_repo(
        usage=Usage(
            email="pp@b.com",
            subscription_type=SUBSCRIPTION_PRO_PLUS,
            meeting_count=0,
            total_tokens=4_000_000,
            total_chars=0,
            lite_daily_tokens=400_000,     # 주간캡 3M 미달
        )
    )
    decision = await assert_tokens_within_limit("pp@b.com")
    assert decision.mode == "overflow"


async def test_tokens_missing_user_passes_silently(fake_repo):
    """User 없음 → silent 통과 (다른 가드/인증이 처리)."""
    fake_repo(usage=None)
    # 예외 없이 통과
    await assert_tokens_within_limit("ghost@b.com")


# ─── 2026-05 월간 reset — reset_at 가 가드 응답에 포함되는지 ────


async def test_summary_exceeded_includes_reset_at(fake_repo):
    """summary 한도 초과 응답에 reset_at 포함 — FE 가 N일 후 reset 안내."""
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=0,
            total_chars=0,
            reset_at="2026-06-17T00:00:00.000000000Z",
        )
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_summary_within_limit("a@b.com", "a" * 10_001)
    assert exc_info.value.detail["reset_at"] == "2026-06-17T00:00:00.000000000Z"


async def test_tokens_exceeded_includes_reset_at(fake_repo):
    """token 한도 초과 응답에 reset_at 포함 (Free 메인 하드월)."""
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0,
            total_tokens=1_000_000,
            total_chars=0,
            reset_at="2026-06-17T00:00:00.000000000Z",
        )
    )
    with pytest.raises(HTTPException) as exc_info:
        await assert_tokens_within_limit("a@b.com")
    assert exc_info.value.detail["reset_at"] == "2026-06-17T00:00:00.000000000Z"


async def test_meeting_exceeded_includes_reset_at(fake_repo):
    """meeting 한도 초과 응답에 reset_at 포함 (IncrementResult 경유)."""
    fake_repo(
        usage=Usage(
            email="a@b.com",
            subscription_type=SUBSCRIPTION_FREE,
            meeting_count=5,
            total_tokens=0,
            total_chars=0,
            reset_at="2026-06-17T00:00:00.000000000Z",
        ),
        increment=IncrementResult(
            exceeded=True,
            current=5,
            limit=5,
            subscription_type=SUBSCRIPTION_FREE,
            reset_at="2026-06-17T00:00:00.000000000Z",
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        await acquire_meeting_quota("a@b.com")
    assert exc_info.value.detail["reset_at"] == "2026-06-17T00:00:00.000000000Z"


# ─── [2026-06-11] admin 한도 변경 멀티프로세스 전파 — 가드가 재로드를 호출하는지 ───
# 토큰 가드만 ensure_overrides_fresh 를 부르고 나머지 가드는 부팅 캐시를 읽어,
# 다른 프로세스에서 "관리자 변경이 일괄 반영 안 되는" 갭이 있었다 (회귀 가드).


async def test_summary_guard_refreshes_overrides(fake_repo, monkeypatch):
    calls = []

    async def _spy(force=False):
        calls.append(1)

    monkeypatch.setattr(quota, "ensure_overrides_fresh", _spy)
    fake_repo(usage=Usage(
        email="a@b.com", subscription_type=SUBSCRIPTION_FREE,
        meeting_count=0, total_tokens=0, total_chars=0,
    ))
    await assert_summary_within_limit("a@b.com", "짧은 회의록")
    assert calls, "assert_summary_within_limit 이 ensure_overrides_fresh 를 호출해야"


async def test_meeting_quota_guard_refreshes_overrides(fake_repo, monkeypatch):
    calls = []

    async def _spy(force=False):
        calls.append(1)

    monkeypatch.setattr(quota, "ensure_overrides_fresh", _spy)
    fake_repo(
        usage=Usage(
            email="a@b.com", subscription_type=SUBSCRIPTION_FREE,
            meeting_count=0, total_tokens=0, total_chars=0,
        ),
        increment=IncrementResult(exceeded=False, current=1, limit=5,
                                  subscription_type=SUBSCRIPTION_FREE),
    )
    await acquire_meeting_quota("a@b.com")
    assert calls, "acquire_meeting_quota 가 ensure_overrides_fresh 를 호출해야"
