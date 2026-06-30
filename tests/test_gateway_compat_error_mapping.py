"""
/api/gateway/* compat dispatcher 의 예외 → HTTP 응답 매핑 회귀 테스트.

[배경 — 운영 버그]
FE 가 `POST /api/gateway/deleteMeeting` 호출 시 콘솔에 다음이 떴다:

    Access to XMLHttpRequest at '.../api/gateway/deleteMeeting' ...
    blocked by CORS policy: No 'Access-Control-Allow-Origin' header ...

실제 원인은 CORS 설정이 아니라, `run_delete_meeting_pipeline` 이 던지는
`RuntimeError`(LLM 빈 응답 / 데이터 손상 가드) · `ValueError`(입력 오류) 를
디스패처가 잡지 않아 Starlette `ServerErrorMiddleware`(CORSMiddleware 보다 바깥)
가 500 을 내보내고, 그 500 에는 CORS 헤더가 안 붙어 브라우저가 "CORS 오류" 로
오인 표시한 것. (정상 v2 라우트 delete_meeting_route 는 이 예외를 HTTPException
으로 변환해 CORS 통과 + 의미 있는 메시지를 돌려준다.)

[계약]
디스패처는 핸들러가 던진 ValueError/RuntimeError 를 4xx HTTPException 으로
변환해 정상 응답 경로(=CORS 미들웨어 통과)로 돌려보내야 한다. 그래야 (1) 브라우저가
CORS 오류로 오인하지 않고 (2) 사용자에게 실제 사유가 전달된다.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import gateway_compat_routes as pt
from app.api.main import app
from app.core.security import get_current_user
from app.service.user_repository import UserPublic


_FAKE_USER = UserPublic(
    id="u-1",
    email="alice@example.com",
    name="Alice",
    created_at="2025-01-01T00:00:00Z",
)

# CORS 미들웨어가 헤더를 echo 하도록 — 테스트 env 의 기본 허용 오리진.
_ALLOWED_ORIGIN = "http://localhost:5173"


@pytest.fixture(autouse=True)
def _bypass_auth():
    app.dependency_overrides[get_current_user] = lambda: _FAKE_USER
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def _bypass_ownership_and_quota(monkeypatch):
    """deleteMeeting 은 _OWNERSHIP_ACCESS + _LLM_HANDLERS → 핸들러 도달 전에
    assert_access / quota 가드를 통과시켜야 핸들러 예외 매핑을 검증할 수 있다."""

    async def _ok_access(email, project, team_id=None):
        return None

    async def _ok_quota(email):
        return None

    monkeypatch.setattr(
        pt.ownership_repository, "assert_access", _ok_access
    )
    monkeypatch.setattr(pt.quota, "assert_tokens_within_limit", _ok_quota)


@pytest.mark.asyncio
async def test_dispatcher_maps_runtime_error_to_422_with_cors(
    _bypass_ownership_and_quota, monkeypatch
):
    """핸들러가 RuntimeError 를 던지면 디스패처는 500 이 아니라 422 + detail 로 변환,
    그리고 응답에 CORS 헤더가 살아있어야 한다 (브라우저 'CORS 오류' 오인 방지)."""

    async def boom(body, query, **kwargs):
        raise RuntimeError(
            "CPS 재구성 실패: LLM 응답이 비어 있습니다. 잠시 후 다시 시도해주세요."
        )

    monkeypatch.setitem(pt._DISPATCH, "deleteMeeting", boom)

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/api/gateway/deleteMeeting",
            json={"projectName": "harness", "version": "V26"},
            headers={"Origin": _ALLOWED_ORIGIN},
        )

    assert r.status_code == 422, r.text
    assert "LLM 응답이 비어" in r.json().get("detail", "")
    # 핵심: 에러 응답에도 CORS 헤더가 있어야 브라우저가 응답을 활용 가능.
    assert r.headers.get("access-control-allow-origin") == _ALLOWED_ORIGIN


@pytest.mark.asyncio
async def test_dispatcher_maps_value_error_to_422(
    _bypass_ownership_and_quota, monkeypatch
):
    """ValueError(입력 오류)도 동일하게 422 로 변환."""

    async def boom(body, query, **kwargs):
        raise ValueError("project_name + version 필수.")

    monkeypatch.setitem(pt._DISPATCH, "deleteMeeting", boom)

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/api/gateway/deleteMeeting",
            json={"projectName": "harness", "version": "V26"},
            headers={"Origin": _ALLOWED_ORIGIN},
        )

    assert r.status_code == 422, r.text
    assert "필수" in r.json().get("detail", "")


@pytest.mark.asyncio
async def test_dispatcher_maps_unexpected_error_to_500_with_cors(
    _bypass_ownership_and_quota, monkeypatch
):
    """예상 못 한 예외도 CORS 통과하는 깔끔한 500 으로 (헤더 없는 raw 500 금지)."""

    async def boom(body, query, **kwargs):
        raise KeyError("unexpected")

    monkeypatch.setitem(pt._DISPATCH, "deleteMeeting", boom)

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/api/gateway/deleteMeeting",
            json={"projectName": "harness", "version": "V26"},
            headers={"Origin": _ALLOWED_ORIGIN},
        )

    assert r.status_code == 500, r.text
    assert r.headers.get("access-control-allow-origin") == _ALLOWED_ORIGIN
