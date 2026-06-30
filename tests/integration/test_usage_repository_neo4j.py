"""
실제 Neo4j (testcontainers) 로 usage_repository 의 self-healing reset cypher
시맨틱 검증.

[활성화]
  RUN_TESTCONTAINERS=1 pytest tests/integration/ -m testcontainers
  + Docker 데몬 실행 중이어야 함.

[검증 목적 — FakeNeo4j 가 못 잡는 영역]
- FOREACH + CASE 조건부 SET 이 실제로 작동하는지
- duration({months: 1}) 산술이 실제 datetime + 1mo 결과를 내는지
- COALESCE / WHERE 조합의 시맨틱 (NULL OR datetime() >= reset_at)
- 하나의 cypher 가 atomic 실행되는지 (FOREACH 안 SET 가 같은 트랜잭션)

[skip 조건]
- RUN_TESTCONTAINERS!=1 (default)
- Docker daemon 미실행
- testcontainers 패키지 미설치
"""
from __future__ import annotations

import asyncio
import os

import pytest


pytestmark = [pytest.mark.asyncio, pytest.mark.testcontainers]


@pytest.fixture(scope="module")
def neo4j_container():
    """testcontainers Neo4j 5 컨테이너 1회 띄워 module 단위 재사용."""
    try:
        from testcontainers.neo4j import Neo4jContainer
    except ImportError:
        pytest.skip("testcontainers[neo4j] 미설치 — `pip install 'testcontainers[neo4j]'`")

    # neo4j:5.27 은 Docker Hub 에 없는 태그(manifest unknown) → 실재 태그로. (NEO4J_TEST_IMAGE override)
    # [2026-06] NEO4J_AUTH 를 강제하지 않는다 — testcontainers 4.x 의 readiness 프로브가
    # 자체 기본 비번으로 접속하는데 NEO4J_AUTH 를 덮으면 불일치로 반복 인증 실패
    # (AuthenticationRateLimit) → 컨테이너 기본 비번을 그대로 쓴다(impact/lineage 테스트와 동일).
    image = os.getenv("NEO4J_TEST_IMAGE", "neo4j:5.13")
    container = Neo4jContainer(image)
    try:
        container.start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Docker 데몬 없음 또는 컨테이너 시작 실패: {e}")
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def neo4j_env(neo4j_container, monkeypatch):
    """neo4j_client 가 testcontainers 인스턴스를 보도록 env 설정 + driver 재초기화."""
    # testcontainers 가 노출한 connection URL 사용
    bolt_url = neo4j_container.get_connection_url()  # 예: bolt://localhost:32768
    # 컨테이너가 실제로 설정한 비번(testcontainers 기본). 강제 NEO4J_AUTH 제거 → 기본값.
    pw = getattr(neo4j_container, "NEO4J_ADMIN_PASSWORD", None) or "password"
    monkeypatch.setenv("NEO4J_URI", bolt_url)
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", pw)

    from app.clients import neo4j_client
    # 이전 테스트가 driver 캐시했을 수 있으므로 close + 재초기화
    await neo4j_client.close_driver()
    yield
    await neo4j_client.close_driver()


@pytest.fixture
async def clean_user(neo4j_env):
    """각 테스트마다 단일 User 노드만 남김 (격리)."""
    from app.clients import neo4j_client
    await neo4j_client.run_cypher("MATCH (u:User) DETACH DELETE u")
    await neo4j_client.run_cypher(
        "CREATE (u:User {email: $email, subscription_type: 'free', "
        "usage_meeting_count: 0, usage_total_tokens: 0, usage_total_chars: 0})",
        {"email": "u@test.com"},
    )
    yield "u@test.com"


# ─── 핵심 시맨틱 검증 ──────────────────────────────────────────


async def test_first_get_usage_sets_reset_at_via_foreach(clean_user):
    """[FOREACH + CASE 조건부 SET] reset_at NULL → first call 이 atomic 으로 박음."""
    from app.service.usage_repository import get_usage
    out = await get_usage(clean_user)
    assert out is not None
    # FOREACH 가 작동해 reset_at 박힘. fake 에선 검증 불가능했던 부분.
    assert out.reset_at is not None, "self-healing FOREACH 가 작동 안 함"
    # ISO datetime 형식 — Neo4j toString 결과
    assert "T" in out.reset_at or "-" in out.reset_at


async def test_reset_at_is_one_month_from_now(clean_user):
    """[duration({months: 1})] reset_at 이 now + ~1mo 인지 검증."""
    from datetime import datetime, timezone, timedelta
    from app.service.usage_repository import get_usage
    before = datetime.now(timezone.utc)
    out = await get_usage(clean_user)
    after = datetime.now(timezone.utc)

    # Neo4j 의 datetime 응답 파싱 — "2026-06-17T01:23:45.123456789Z" 같은 형식
    raw = out.reset_at
    # 마지막 'Z' 처리 + nanosecond 잘라냄
    cleaned = raw.rstrip("Z")
    # 소수점 6자리까지만 (python 한계)
    if "." in cleaned:
        head, frac = cleaned.split(".", 1)
        cleaned = head + "." + frac[:6]
    parsed = datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)

    # 28~31 일 범위 (월말 변동 흡수)
    delta = parsed - before
    assert timedelta(days=27) < delta < timedelta(days=32), (
        f"reset_at 이 ~1mo 안에 안 들어옴: {parsed} vs now {before}"
    )


async def test_try_increment_atomic_check_and_set(clean_user):
    """[atomic check+SET] limit 도달 시 카운터 그대로, exceeded=True."""
    from app.service.usage_repository import try_increment_meeting_count
    # 1번째: 0→1 통과 (limit=2)
    r1 = await try_increment_meeting_count(clean_user, 2)
    assert r1 is not None and r1.exceeded is False
    assert r1.current == 1
    # 2번째: 1→2 통과
    r2 = await try_increment_meeting_count(clean_user, 2)
    assert r2.exceeded is False and r2.current == 2
    # 3번째: 한도 도달, exceeded=True, current 그대로
    r3 = await try_increment_meeting_count(clean_user, 2)
    assert r3.exceeded is True
    assert r3.current == 2, (
        "한도 도달 시 카운터가 증가됨 — atomic check+SET 실패 (FOREACH+CASE 시맨틱 깨짐)"
    )


# ─── [2026-06] 관리자 기간제 부여 만료 self-heal (실 Neo4j) ──────────────
# get_usage 는 핫패스(모든 quota 체크 + /auth/me/usage) → cypher 문법/시맨틱이
# 깨지면 전원 장애. fake 로는 FOREACH 미검증 → 실 Neo4j 로 강등 동작을 가드한다.


async def test_expired_subscription_self_heals_to_free(neo4j_env):
    """subscription_ends_at 지난 Pro 유저 → get_usage 시 free 강등 + ends_at 비움(재강등 방지)."""
    from app.service.usage_repository import get_usage
    from app.clients import neo4j_client
    await neo4j_client.run_cypher("MATCH (u:User) DETACH DELETE u")
    await neo4j_client.run_cypher(
        "CREATE (u:User {email: $e, subscription_type: 'pro', "
        "usage_meeting_count: 0, usage_total_tokens: 0, usage_total_chars: 0, "
        "subscription_ends_at: datetime() - duration({days: 1})})",
        {"e": "expired@test.com"},
    )
    out = await get_usage("expired@test.com")
    assert out is not None
    assert out.subscription_type == "free", "만료된 등급이 free 로 강등 안 됨"
    assert out.subscription_ends_at is None, "강등 후 ends_at 미정리 — 재강등/혼선 위험"


async def test_future_expiry_keeps_tier(neo4j_env):
    """아직 안 지난 만료 → 등급 유지 + ends_at 보존 (조기 강등 회귀 가드)."""
    from app.service.usage_repository import get_usage
    from app.clients import neo4j_client
    await neo4j_client.run_cypher("MATCH (u:User) DETACH DELETE u")
    await neo4j_client.run_cypher(
        "CREATE (u:User {email: $e, subscription_type: 'pro', "
        "usage_meeting_count: 0, usage_total_tokens: 0, usage_total_chars: 0, "
        "subscription_ends_at: datetime() + duration({days: 1})})",
        {"e": "future@test.com"},
    )
    out = await get_usage("future@test.com")
    assert out is not None
    assert out.subscription_type == "pro", "아직 안 지난 만료가 조기 강등됨"
    assert out.subscription_ends_at is not None, "유효 만료일이 사라짐"


async def test_permanent_grant_no_expiry(neo4j_env):
    """ends_at NULL(영구/Paddle) → 강등 없음 (need_expire=false no-op)."""
    from app.service.usage_repository import get_usage
    from app.clients import neo4j_client
    await neo4j_client.run_cypher("MATCH (u:User) DETACH DELETE u")
    await neo4j_client.run_cypher(
        "CREATE (u:User {email: $e, subscription_type: 'pro_max', "
        "usage_meeting_count: 0, usage_total_tokens: 0, usage_total_chars: 0})",
        {"e": "perm@test.com"},
    )
    out = await get_usage("perm@test.com")
    assert out is not None
    assert out.subscription_type == "pro_max", "영구 등급이 강등됨 — ends_at NULL no-op 깨짐"
    assert out.subscription_ends_at is None
