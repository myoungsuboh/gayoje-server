"""
extract 캐시 모듈 — batch 파이프라이닝의 prefetch 결과 저장/재사용.

핵심:
  - 키는 (project, version, content) 결정적 — prefetch 와 본 job 이 같은 키 산출.
  - set → get 라운드트립.
  - miss / Redis 부재 → None (graceful, 본 job 이 직접 계산하는 기존 동작으로 강등).
  - single-flight 락: 첫 획득만 True → prefetch 와 본 job 의 중복 추출(토큰 이중과금) 방지.
"""
from __future__ import annotations

import pytest

from app.queue.extract_cache import (
    extract_cache_key,
    get_cached_extract,
    set_cached_extract,
    try_acquire_extract_lock,
)
from tests.conftest import FakeRedis

pytestmark = pytest.mark.asyncio


def test_key_is_deterministic_and_scoped_by_project_version_content():
    k1 = extract_cache_key("food", "v1", "회의 내용")
    k2 = extract_cache_key("food", "v1", "회의 내용")
    assert k1 == k2                                  # 같은 입력 → 같은 키 (prefetch=본 job)
    assert extract_cache_key("food", "v2", "회의 내용") != k1   # version 다르면 다른 키
    assert extract_cache_key("food", "v1", "다른 내용") != k1   # content 다르면 다른 키
    assert extract_cache_key("other", "v1", "회의 내용") != k1  # project 다르면 다른 키


def test_key_ignores_surrounding_whitespace():
    """앞뒤 공백 차이로 캐시 미스가 나지 않도록 normalize."""
    assert extract_cache_key("food", "v1", "  내용\n") == extract_cache_key("food", "v1", "내용")


def test_key_includes_cache_version_for_invalidation():
    """키에 코드 버전이 포함돼야 — extract 로직/프롬프트 변경 시 _CACHE_VERSION bump 로
    옛 캐시 엔트리를 전부 무효화(배포 후 stale 결과 방지)."""
    from app.queue import extract_cache
    key = extract_cache.extract_cache_key("food", "v1", "내용")
    assert extract_cache._CACHE_VERSION in key


async def test_set_then_get_roundtrips_artifact():
    redis = FakeRedis()
    key = extract_cache_key("food", "v1", "내용")
    artifact = {"cps_graph": {"nodes": [1]}, "prd_markdown": "# md", "prd_graph": {"nodes": []}}

    await set_cached_extract(redis, key, artifact)
    got = await get_cached_extract(redis, key)

    assert got == artifact


async def test_get_returns_none_on_miss():
    redis = FakeRedis()
    assert await get_cached_extract(redis, extract_cache_key("x", "v9", "없음")) is None


async def test_get_and_set_are_graceful_without_redis():
    """Redis=None 이면 set 은 no-op, get 은 None — 캐시 없이도 안전 동작."""
    key = extract_cache_key("food", "v1", "내용")
    await set_cached_extract(None, key, {"a": 1})     # no error
    assert await get_cached_extract(None, key) is None


async def test_single_flight_lock_only_first_acquires():
    redis = FakeRedis()
    key = extract_cache_key("food", "v1", "내용")

    assert await try_acquire_extract_lock(redis, key) is True    # 첫 획득
    assert await try_acquire_extract_lock(redis, key) is False   # 두 번째는 거부 (이미 점유)


async def test_lock_allows_progress_without_redis():
    """Redis 부재 시 lock 은 True(=직접 진행 허용) — 캐시 인프라 없이도 동작."""
    assert await try_acquire_extract_lock(None, "k") is True
