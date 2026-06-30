"""
Paddle 부속 라우트 — 구독 스냅샷 조회 + 고객포털 세션 생성.

라우트(main 경유)는 fastmcp 의존이라 라우트 함수를 직접 호출해 검증
(test_my_usage_route.py 패턴).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
from fastapi import HTTPException

from app.api import paddle_billing_routes as pb
from app.core.config import settings
from app.service.user_repository import UserPublic

pytestmark = pytest.mark.asyncio


def _user() -> UserPublic:
    return UserPublic(
        id="u-1",
        email="u@example.com",
        name="Tester",
        subscription_type="pro",
        is_admin=False,
    )


def _patch_snapshot(monkeypatch, snapshot: Optional[Dict[str, Any]]) -> None:
    async def fake_get_by_email(email: str):
        assert email == "u@example.com"
        return snapshot

    monkeypatch.setattr(pb.paddle_subscription_repository, "get_by_email", fake_get_by_email)


# ─── GET /api/paddle/subscription ────────────────────────────


async def test_get_subscription_returns_snapshot(monkeypatch):
    snap = {"subscription_id": "sub_1", "customer_id": "ctm_1", "status": "active"}
    _patch_snapshot(monkeypatch, snap)
    resp = await pb.get_paddle_subscription_route(current_user=_user())
    assert resp == {"subscription": snap}


async def test_get_subscription_null_when_missing(monkeypatch):
    _patch_snapshot(monkeypatch, None)
    resp = await pb.get_paddle_subscription_route(current_user=_user())
    assert resp == {"subscription": None}


# ─── POST /api/paddle/portal-session ─────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, body: Dict[str, Any]):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> Dict[str, Any]:
        return self._body


class _FakeAsyncClient:
    """httpx.AsyncClient 대역 — 호출 인자 캡처 + 고정 응답.

    post_queue 가 있으면 post 가 순차 소비(self-heal: 저장 id 실패→재조회 id 재시도 2회).
    get_response 는 이메일→customer_id 재조회용(기본 빈 목록 = 재조회 실패)."""

    captured: Dict[str, Any] = {}
    captured_get: Dict[str, Any] = {}
    response: _FakeResponse = _FakeResponse(200, {})
    post_queue: list = []
    get_response: _FakeResponse = _FakeResponse(200, {"data": []})

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.captured = {"url": url, "headers": headers, "json": json}
        if _FakeAsyncClient.post_queue:
            return _FakeAsyncClient.post_queue.pop(0)
        return _FakeAsyncClient.response

    async def patch(self, url, headers=None, json=None):
        _FakeAsyncClient.captured = {"url": url, "headers": headers, "json": json}
        return _FakeAsyncClient.response

    async def get(self, url, headers=None, params=None):
        _FakeAsyncClient.captured_get = {"url": url, "headers": headers, "params": params}
        return _FakeAsyncClient.get_response


@pytest.fixture(autouse=True)
def _reset_fake_client(monkeypatch):
    """테스트 간 _FakeAsyncClient 상태 누수 차단 + set_customer_id(self-heal DB보정) no-op 캡처."""
    _FakeAsyncClient.captured = {}
    _FakeAsyncClient.captured_get = {}
    _FakeAsyncClient.response = _FakeResponse(200, {})
    _FakeAsyncClient.post_queue = []
    _FakeAsyncClient.get_response = _FakeResponse(200, {"data": []})
    _set_customer_calls.clear()

    async def _fake_set_customer_id(email: str, customer_id: str):
        _set_customer_calls.append((email, customer_id))
        return True

    monkeypatch.setattr(pb.paddle_subscription_repository, "set_customer_id", _fake_set_customer_id)


_set_customer_calls: list = []


async def test_portal_session_503_when_api_key_missing(monkeypatch):
    monkeypatch.setattr(settings, "PADDLE_API_KEY", None)
    with pytest.raises(HTTPException) as e:
        await pb.create_portal_session_route(current_user=_user())
    assert e.value.status_code == 503


async def test_portal_session_404_without_subscription(monkeypatch):
    monkeypatch.setattr(settings, "PADDLE_API_KEY", "pdl_key")
    _patch_snapshot(monkeypatch, None)
    # 저장 구독 없음 + 이메일 재조회(기본 빈 목록)도 못 찾음 → 404.
    monkeypatch.setattr(pb.httpx, "AsyncClient", _FakeAsyncClient)
    with pytest.raises(HTTPException) as e:
        await pb.create_portal_session_route(current_user=_user())
    assert e.value.status_code == 404


async def test_portal_session_success(monkeypatch):
    monkeypatch.setattr(settings, "PADDLE_API_KEY", "pdl_key")
    monkeypatch.setattr(settings, "PADDLE_ENV", "sandbox")
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "customer_id": "ctm_1"})
    _FakeAsyncClient.response = _FakeResponse(201, {
        "data": {"urls": {"general": {"overview": "https://sandbox-customer-portal.paddle.com/x"}}}
    })
    monkeypatch.setattr(pb.httpx, "AsyncClient", _FakeAsyncClient)

    resp = await pb.create_portal_session_route(current_user=_user())

    assert resp == {"url": "https://sandbox-customer-portal.paddle.com/x"}
    cap = _FakeAsyncClient.captured
    assert cap["url"] == "https://sandbox-api.paddle.com/customers/ctm_1/portal-sessions"
    assert cap["headers"]["Authorization"] == "Bearer pdl_key"
    assert cap["json"] == {}


async def test_portal_session_502_on_paddle_error(monkeypatch):
    monkeypatch.setattr(settings, "PADDLE_API_KEY", "pdl_key")
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "customer_id": "ctm_1"})
    _FakeAsyncClient.response = _FakeResponse(403, {"error": {"code": "forbidden"}})
    monkeypatch.setattr(pb.httpx, "AsyncClient", _FakeAsyncClient)

    with pytest.raises(HTTPException) as e:
        await pb.create_portal_session_route(current_user=_user())
    assert e.value.status_code == 502
    # Paddle 상태코드를 detail 에 노출 — 네트워크 탭만으로 401/403/404 진단 가능.
    assert e.value.detail == "paddle_api_error:403"


async def test_portal_session_502_unreachable_on_network_error(monkeypatch):
    """httpx 네트워크 오류(egress 차단·타임아웃)는 클린 502 로 변환 — CORS 보존."""
    monkeypatch.setattr(settings, "PADDLE_API_KEY", "pdl_key")
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "customer_id": "ctm_1"})

    class _BoomClient(_FakeAsyncClient):
        async def post(self, url, headers=None, json=None):
            raise pb.httpx.ConnectError("egress blocked")

    monkeypatch.setattr(pb.httpx, "AsyncClient", _BoomClient)

    with pytest.raises(HTTPException) as e:
        await pb.create_portal_session_route(current_user=_user())
    assert e.value.status_code == 502
    assert e.value.detail == "paddle_api_unreachable"


async def test_portal_session_502_on_unexpected_body(monkeypatch):
    monkeypatch.setattr(settings, "PADDLE_API_KEY", "pdl_key")
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "customer_id": "ctm_1"})
    _FakeAsyncClient.response = _FakeResponse(201, {"data": {}})
    monkeypatch.setattr(pb.httpx, "AsyncClient", _FakeAsyncClient)

    with pytest.raises(HTTPException) as e:
        await pb.create_portal_session_route(current_user=_user())
    assert e.value.status_code == 502


async def test_portal_session_self_heals_stale_customer_id(monkeypatch):
    """저장된 customer_id 가 거부(403)되면 이메일로 운영 customer_id 재조회→DB보정→재시도→성공.
    (샌드박스 시절 stale customer_id 가 운영 키로 403 나던 사례의 자가복구)"""
    monkeypatch.setattr(settings, "PADDLE_API_KEY", "pdl_key")
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "customer_id": "ctm_stale"})
    # post 1회차(저장 id) → 403, 2회차(재조회 id) → 201 + URL
    _FakeAsyncClient.post_queue = [
        _FakeResponse(403, {"error": {"code": "entity_not_found"}}),
        _FakeResponse(201, {"data": {"urls": {"general": {"overview": "https://customer-portal.paddle.com/ok"}}}}),
    ]
    # 이메일 재조회 → 운영 active customer
    _FakeAsyncClient.get_response = _FakeResponse(200, {"data": [{"id": "ctm_new", "status": "active"}]})
    monkeypatch.setattr(pb.httpx, "AsyncClient", _FakeAsyncClient)

    resp = await pb.create_portal_session_route(current_user=_user())

    assert resp == {"url": "https://customer-portal.paddle.com/ok"}
    assert _FakeAsyncClient.captured["url"].endswith("/customers/ctm_new/portal-sessions")  # 재조회 id 로 재시도
    assert ("u@example.com", "ctm_new") in _set_customer_calls                              # DB 보정됨
    assert _FakeAsyncClient.captured_get["params"] == {"email": "u@example.com"}            # 이메일로 조회


# ─── POST /api/paddle/change-subscription (이중청구 방지 — 기존 구독 등급 변경) ───


def _req(tier="pro_max", cycle="monthly") -> pb.ChangeSubscriptionRequest:
    return pb.ChangeSubscriptionRequest(tier=tier, cycle=cycle)


def _set_prices(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PADDLE_API_KEY", "pdl_key")
    monkeypatch.setattr(settings, "PADDLE_ENV", "sandbox")
    monkeypatch.setattr(settings, "PADDLE_PRICE_PRO", "pri_pro")
    monkeypatch.setattr(settings, "PADDLE_PRICE_PRO_PLUS", "pri_plus")
    monkeypatch.setattr(settings, "PADDLE_PRICE_PRO_MAX", "pri_max")


async def test_change_subscription_503_when_api_key_missing(monkeypatch):
    monkeypatch.setattr(settings, "PADDLE_API_KEY", None)
    with pytest.raises(HTTPException) as e:
        await pb.change_subscription_route(payload=_req(), current_user=_user())
    assert e.value.status_code == 503


async def test_change_subscription_400_on_invalid_tier(monkeypatch):
    _set_prices(monkeypatch)
    with pytest.raises(HTTPException) as e:
        await pb.change_subscription_route(payload=_req(tier="free"), current_user=_user())
    assert e.value.status_code == 400


async def test_change_subscription_409_when_no_active_subscription(monkeypatch):
    """활성 구독 없음 → 409. FE 는 이 신호로 체크아웃(신규결제)으로 폴백한다."""
    _set_prices(monkeypatch)
    _patch_snapshot(monkeypatch, None)
    with pytest.raises(HTTPException) as e:
        await pb.change_subscription_route(payload=_req(), current_user=_user())
    assert e.value.status_code == 409
    assert e.value.detail == "no_active_subscription"


async def test_change_subscription_409_when_subscription_canceled(monkeypatch):
    """끝난(canceled) 구독은 subscription_id 가 스냅샷에 남아도 '활성 없음' 으로 본다 →
    FE 가 409 로 신규 결제(체크아웃)로 폴백. 죽은 구독 PATCH 로 인한 502 혼선 방지."""
    _set_prices(monkeypatch)
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_old", "customer_id": "ctm_1", "price_id": "pri_pro", "status": "canceled"})
    with pytest.raises(HTTPException) as e:
        await pb.change_subscription_route(payload=_req(tier="pro_max"), current_user=_user())
    assert e.value.status_code == 409
    assert e.value.detail == "no_active_subscription"


async def test_change_subscription_409_when_already_on_target(monkeypatch):
    """이미 같은 price → 409 (불필요한 PATCH=불필요한 proration 청구 방지)."""
    _set_prices(monkeypatch)
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "customer_id": "ctm_1", "price_id": "pri_max", "status": "active"})
    with pytest.raises(HTTPException) as e:
        await pb.change_subscription_route(payload=_req(tier="pro_max"), current_user=_user())
    assert e.value.status_code == 409
    assert e.value.detail == "already_on_target_tier"


async def test_change_subscription_success_patches_existing_sub(monkeypatch):
    """핵심: 새 체크아웃이 아니라 기존 구독을 PATCH — items.price 교체 + 즉시 proration."""
    _set_prices(monkeypatch)
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "customer_id": "ctm_1", "price_id": "pri_pro", "status": "active"})
    _FakeAsyncClient.response = _FakeResponse(200, {"data": {"id": "sub_1", "status": "active"}})
    monkeypatch.setattr(pb.httpx, "AsyncClient", _FakeAsyncClient)

    resp = await pb.change_subscription_route(payload=_req(tier="pro_max"), current_user=_user())

    assert resp == {"status": "ok", "subscription_id": "sub_1", "tier": "pro_max"}
    cap = _FakeAsyncClient.captured
    # 기존 구독 PATCH (신규 구독 생성 아님)
    assert cap["url"] == "https://sandbox-api.paddle.com/subscriptions/sub_1"
    assert cap["headers"]["Authorization"] == "Bearer pdl_key"
    assert cap["json"]["items"] == [{"price_id": "pri_max", "quantity": 1}]
    assert cap["json"]["proration_billing_mode"] == "prorated_immediately"
    # 보류 예약(기말 해지 등) 해제도 함께 — "결제했는데 곧 취소됨" 모순 방지.
    assert cap["json"]["scheduled_change"] is None


async def test_change_subscription_clears_pending_cancellation(monkeypatch):
    """해지 예약(scheduled_change=cancel) 상태에서 업그레이드 → PATCH 가 예약을 해제(null)한다.

    실관측 시나리오: 사용자가 '기말 해지'를 걸어둔 active 구독을 그대로 두고 상위 등급으로
    업그레이드하면, 가격은 바뀌어도 해지 예약이 남아 '결제했는데 곧 취소됨' 모순이 생긴다.
    등급 변경은 곧 '계속 쓰겠다'는 의사이므로 같은 PATCH 에서 scheduled_change=null 로 해제한다.
    """
    _set_prices(monkeypatch)
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "customer_id": "ctm_1", "price_id": "pri_pro", "status": "active"})
    _FakeAsyncClient.response = _FakeResponse(200, {"data": {"id": "sub_1", "status": "active"}})
    monkeypatch.setattr(pb.httpx, "AsyncClient", _FakeAsyncClient)

    await pb.change_subscription_route(payload=_req(tier="pro_max"), current_user=_user())

    cap = _FakeAsyncClient.captured
    assert "scheduled_change" in cap["json"]
    assert cap["json"]["scheduled_change"] is None


async def test_change_subscription_502_on_paddle_error(monkeypatch):
    _set_prices(monkeypatch)
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "customer_id": "ctm_1", "price_id": "pri_pro", "status": "active"})
    _FakeAsyncClient.response = _FakeResponse(403, {"error": {"code": "forbidden"}})
    monkeypatch.setattr(pb.httpx, "AsyncClient", _FakeAsyncClient)

    with pytest.raises(HTTPException) as e:
        await pb.change_subscription_route(payload=_req(tier="pro_max"), current_user=_user())
    assert e.value.status_code == 502
    assert e.value.detail == "paddle_api_error:403"


async def test_change_subscription_502_unreachable_on_network_error(monkeypatch):
    _set_prices(monkeypatch)
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "customer_id": "ctm_1", "price_id": "pri_pro", "status": "active"})

    class _BoomClient(_FakeAsyncClient):
        async def patch(self, url, headers=None, json=None):
            raise pb.httpx.ConnectError("egress blocked")

    monkeypatch.setattr(pb.httpx, "AsyncClient", _BoomClient)

    with pytest.raises(HTTPException) as e:
        await pb.change_subscription_route(payload=_req(tier="pro_max"), current_user=_user())
    assert e.value.status_code == 502
    assert e.value.detail == "paddle_api_unreachable"


# ─── [2026-06 감사 하드닝] 신규 케이스 ──────────────────────────


async def test_change_subscription_400_on_invalid_cycle(monkeypatch):
    """오타 cycle 은 400 — 조용히 monthly 로 청구되는 silent-fail 방지."""
    _set_prices(monkeypatch)
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "price_id": "pri_pro", "status": "active"})
    with pytest.raises(HTTPException) as e:
        await pb.change_subscription_route(payload=_req(tier="pro_max", cycle="weekly"), current_user=_user())
    assert e.value.status_code == 400
    assert e.value.detail.startswith("invalid_cycle")


async def test_change_subscription_409_when_past_due(monkeypatch):
    """past_due(미납 처리 중)는 변경 불가 — 정산 충돌 방지. FE 는 409 로 폴백."""
    _set_prices(monkeypatch)
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "price_id": "pri_pro", "status": "past_due"})
    with pytest.raises(HTTPException) as e:
        await pb.change_subscription_route(payload=_req(tier="pro_max"), current_user=_user())
    assert e.value.status_code == 409
    assert e.value.detail == "no_active_subscription"


async def test_change_subscription_sends_idempotency_key(monkeypatch):
    """PATCH 에 (구독,price) 결정적 Idempotency-Key — 재시도/더블클릭 중복청구 방지."""
    _set_prices(monkeypatch)
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "price_id": "pri_pro", "status": "active"})
    _FakeAsyncClient.response = _FakeResponse(200, {"data": {"id": "sub_1"}})
    monkeypatch.setattr(pb.httpx, "AsyncClient", _FakeAsyncClient)
    await pb.change_subscription_route(payload=_req(tier="pro_max"), current_user=_user())
    cap = _FakeAsyncClient.captured
    # 결정적 — 같은 변경의 재시도는 동일 키 → Paddle 이 캐시 응답으로 dedup
    assert cap["headers"]["Idempotency-Key"] == "chgsub:sub_1:pri_max"


async def test_change_subscription_downgrade_defers_proration(monkeypatch):
    """다운그레이드(Pro Max→Pro)는 즉시청구가 아니라 다음 주기 정산 — 선결제분에 추가과금 방지."""
    _set_prices(monkeypatch)
    # 현재 pri_max(Pro Max) → pro(pri_pro) = 다운그레이드
    _patch_snapshot(monkeypatch, {"subscription_id": "sub_1", "price_id": "pri_max", "status": "active"})
    _FakeAsyncClient.response = _FakeResponse(200, {"data": {"id": "sub_1"}})
    monkeypatch.setattr(pb.httpx, "AsyncClient", _FakeAsyncClient)
    await pb.change_subscription_route(payload=_req(tier="pro"), current_user=_user())
    cap = _FakeAsyncClient.captured
    assert cap["json"]["items"] == [{"price_id": "pri_pro", "quantity": 1}]
    assert cap["json"]["proration_billing_mode"] == "prorated_next_billing_period"
    assert cap["json"]["scheduled_change"] is None  # 방향 무관 — 항상 예약 해제 동반
