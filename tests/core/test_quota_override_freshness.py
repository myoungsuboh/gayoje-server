"""
워커 신선도(override freshness) — admin 한도 변경이 워커 결정에 반영되는지.

[배경 — 2026-06]
admin 이 한도(예: Pro+ total_tokens 500K→5M)를 변경하면 API 프로세스의
_LIMITS_OVERRIDE 는 즉시 갱신되지만, 별도 워커 프로세스 메모리는 부팅 시
값으로 stale → 워커가 옛 한도로 결정을 내려 메인이 폭주(1.4M / 500K).
quota.ensure_overrides_fresh() 가 15s TTL 로 DB 재로드해 stale 창을 제한.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.core import quota
from app.core.subscription import SUBSCRIPTION_PRO_PLUS


@dataclass
class _FakeRow:
    tier: str
    meeting_logs: int
    summary_chars: int
    total_tokens: int
    library_skills: int
    max_projects: int
    lite_daily_cap: int


@pytest.fixture(autouse=True)
def _isolate_quota_state(monkeypatch):
    """각 테스트마다 _LIMITS_OVERRIDE + 마지막 로드 타임스탬프 초기화."""
    quota.clear_limits_override()
    quota._reset_override_cache_for_test()
    yield
    quota.clear_limits_override()
    quota._reset_override_cache_for_test()


def _make_fake_repo(rows):
    """quota_config_repository.list_quota_config 가 rows 반환하도록 모킹."""
    calls = {"n": 0}

    async def _list():
        calls["n"] += 1
        return list(rows)

    return calls, _list


@pytest.mark.asyncio
async def test_ensure_overrides_fresh_loads_from_db_on_first_call(monkeypatch):
    """첫 호출 시 DB 에서 한도를 로드해 _LIMITS_OVERRIDE 갱신."""
    rows = [_FakeRow(
        tier=SUBSCRIPTION_PRO_PLUS, meeting_logs=40, summary_chars=600_000,
        total_tokens=5_000_000, library_skills=50, max_projects=6,
        lite_daily_cap=1_400_000,
    )]
    calls, fake_list = _make_fake_repo(rows)
    import app.service.quota_config_repository as repo
    monkeypatch.setattr(repo, "list_quota_config", fake_list)

    # 첫 호출 — DB 로드 발생.
    await quota.ensure_overrides_fresh()
    assert calls["n"] == 1
    assert quota.get_limit(SUBSCRIPTION_PRO_PLUS, "total_tokens") == 5_000_000


@pytest.mark.asyncio
async def test_ensure_overrides_fresh_caches_within_ttl(monkeypatch):
    """TTL 안의 두 번째 호출은 DB 재호출 안 함 (메모리 캐시)."""
    rows = [_FakeRow(
        tier=SUBSCRIPTION_PRO_PLUS, meeting_logs=40, summary_chars=600_000,
        total_tokens=5_000_000, library_skills=50, max_projects=6,
        lite_daily_cap=1_400_000,
    )]
    calls, fake_list = _make_fake_repo(rows)
    import app.service.quota_config_repository as repo
    monkeypatch.setattr(repo, "list_quota_config", fake_list)

    await quota.ensure_overrides_fresh()
    await quota.ensure_overrides_fresh()  # TTL 안 — 재호출 없음
    await quota.ensure_overrides_fresh()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_ensure_overrides_fresh_reloads_after_ttl_expiry(monkeypatch):
    """TTL 만료 후 호출은 다시 DB 로드 → admin 변경이 워커에 전파."""
    # 1차 — 옛 한도 (워커 부팅 시점 값).
    rows = [_FakeRow(
        tier=SUBSCRIPTION_PRO_PLUS, meeting_logs=40, summary_chars=600_000,
        total_tokens=500_000,  # 옛 값
        library_skills=50, max_projects=6, lite_daily_cap=1_400_000,
    )]
    calls, fake_list = _make_fake_repo(rows)
    import app.service.quota_config_repository as repo
    monkeypatch.setattr(repo, "list_quota_config", fake_list)

    await quota.ensure_overrides_fresh()
    assert quota.get_limit(SUBSCRIPTION_PRO_PLUS, "total_tokens") == 500_000

    # admin 이 한도를 5M 으로 변경 (DB 업데이트만, 워커 메모리는 stale).
    rows[0] = _FakeRow(
        tier=SUBSCRIPTION_PRO_PLUS, meeting_logs=40, summary_chars=600_000,
        total_tokens=5_000_000,  # 새 값
        library_skills=50, max_projects=6, lite_daily_cap=1_400_000,
    )

    # TTL 만료 시뮬레이션 — 캐시 타임스탬프 초기화.
    quota._reset_override_cache_for_test()
    await quota.ensure_overrides_fresh()
    assert calls["n"] == 2
    # 새 한도 반영 — 다음 결정부터 5M 적용.
    assert quota.get_limit(SUBSCRIPTION_PRO_PLUS, "total_tokens") == 5_000_000


@pytest.mark.asyncio
async def test_ensure_overrides_fresh_swallows_db_errors(monkeypatch):
    """DB 재로드 실패 시 이전 캐시로 계속 진행 (가용성 우선)."""
    # 정상 1회 로드.
    rows = [_FakeRow(
        tier=SUBSCRIPTION_PRO_PLUS, meeting_logs=40, summary_chars=600_000,
        total_tokens=5_000_000, library_skills=50, max_projects=6,
        lite_daily_cap=1_400_000,
    )]
    _, fake_list = _make_fake_repo(rows)
    import app.service.quota_config_repository as repo
    monkeypatch.setattr(repo, "list_quota_config", fake_list)
    await quota.ensure_overrides_fresh()
    assert quota.get_limit(SUBSCRIPTION_PRO_PLUS, "total_tokens") == 5_000_000

    # DB 장애.
    async def _fail():
        raise RuntimeError("neo4j down")
    monkeypatch.setattr(repo, "list_quota_config", _fail)
    quota._reset_override_cache_for_test()

    # 예외 swallow + 이전 캐시 유지.
    await quota.ensure_overrides_fresh()
    assert quota.get_limit(SUBSCRIPTION_PRO_PLUS, "total_tokens") == 5_000_000


@pytest.mark.asyncio
async def test_resolve_quota_decision_uses_fresh_override(monkeypatch):
    """resolve_quota_decision 진입 시 ensure_overrides_fresh 가 자동 호출 → 신선한 결정."""
    # 옛 한도 500K 로드.
    rows = [_FakeRow(
        tier=SUBSCRIPTION_PRO_PLUS, meeting_logs=40, summary_chars=600_000,
        total_tokens=500_000, library_skills=50, max_projects=6,
        lite_daily_cap=1_400_000,
    )]
    _, fake_list = _make_fake_repo(rows)
    import app.service.quota_config_repository as repo
    monkeypatch.setattr(repo, "list_quota_config", fake_list)

    # usage_repository mock — 누적 1M (옛 한도 500K 기준 초과, 새 한도 5M 기준 미만).
    @dataclass
    class _U:
        subscription_type: str = SUBSCRIPTION_PRO_PLUS
        total_tokens: int = 1_000_000
        lite_daily_tokens: int = 0
        reset_at: str = None
        lite_daily_reset_at: str = None

    import app.service.usage_repository as ur
    async def _get(_e): return _U()
    monkeypatch.setattr(ur, "get_usage", _get)

    # admin 이 5M 으로 즉시 변경 (DB 업데이트만).
    rows[0] = _FakeRow(
        tier=SUBSCRIPTION_PRO_PLUS, meeting_logs=40, summary_chars=600_000,
        total_tokens=5_000_000, library_skills=50, max_projects=6,
        lite_daily_cap=1_400_000,
    )

    # 캐시 만료 시뮬레이션 후 결정 호출 — 워커가 신선한 5M 한도 기준으로 main 통과해야.
    quota._reset_override_cache_for_test()
    decision = await quota.resolve_quota_decision("user@x")
    assert decision.mode == "main", f"신선한 한도 5M 기준 1M 누적은 main 통과해야: {decision}"
