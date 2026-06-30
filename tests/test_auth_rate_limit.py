"""레이트 리밋 — login(5/min) / signup(3/min) IP당 횟수 제한 검증."""
import pytest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.main import app
from app.core.limiter import limiter


@pytest.fixture(autouse=True)
def _reset_limiter():
    """각 테스트 전 in-memory 카운터 초기화 → 테스트 간 상태 격리."""
    limiter._storage.reset()
    yield


_LOGIN_PAYLOAD = {"email": "test@example.com", "password": "anypassword"}


@pytest.mark.asyncio
async def test_login_allows_five_then_blocks():
    """5회 허용 → 6번째 요청에서 429."""
    with patch("app.api.auth_routes.login", new_callable=AsyncMock) as mock_login:
        mock_login.side_effect = HTTPException(status_code=401, detail="bad creds")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            for i in range(5):
                r = await client.post("/auth/login", json=_LOGIN_PAYLOAD)
                assert r.status_code != 429, f"요청 {i + 1}번이 예상보다 일찍 차단됨"
            r = await client.post("/auth/login", json=_LOGIN_PAYLOAD)
            assert r.status_code == 429


@pytest.mark.asyncio
async def test_login_429_response_has_error_field():
    """429 응답 본문에 error 또는 detail 키 포함."""
    with patch("app.api.auth_routes.login", new_callable=AsyncMock) as mock_login:
        mock_login.side_effect = HTTPException(status_code=401, detail="bad creds")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            for _ in range(5):
                await client.post("/auth/login", json=_LOGIN_PAYLOAD)
            r = await client.post("/auth/login", json=_LOGIN_PAYLOAD)
    assert r.status_code == 429
    body = r.json()
    assert "error" in body or "detail" in body


# ─── [2026-06-04] 전역 기본 rate limit (300/분, 라우트별·사용자별) ───────────


def test_global_default_limit_is_configured():
    """limiter 에 전역 기본 한도가 걸려 있어 데코 없는 라우트도 보호됨.
    (값 300/분 자체는 limiter.py 의 default_limits 로 명시 — 여기선 '설정 존재'만 가드.)"""
    assert limiter._default_limits, "전역 default_limits 가 비어 있음 — 데코 없는 라우트 무방비"


@pytest.mark.asyncio
async def test_health_endpoint_exempt_from_global_limit():
    """헬스체크는 @limiter.exempt — 전역 300/분을 초과해도(모니터링 잦은 호출) 안 막힘."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        codes = [(await client.get("/health")).status_code for _ in range(320)]
    assert codes.count(429) == 0, "헬스체크가 전역 rate limit 에 막히면 안 됨(exempt)"
    assert all(c == 200 for c in codes)
