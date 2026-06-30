"""
PipelineContext.user_email 전파 회귀 차단 — Phase 2D 멀티테넌시 핵심.

[배경]
Phase 2D 마이그레이션에서 `delete_pipeline._derive_ids` 와
`_DELETE_PROJECT_NODE_CYPHER` 가 `ctx.user_email` 을 사용해 master_id 에 email
prefix 를 포함하고 Project 노드를 (name, owner_email) 합성으로 매칭한다.

ctx 생성처가 user_email 을 채우지 않으면:
  1. master_id 가 옛 형식으로 회귀 → 다른 유저의 동명 프로젝트와 충돌(데이터 덮어쓰기).
  2. Project 노드 매칭이 owner_email='' 로 떨어져 운영 노드 0개 매치(좀비 재발).

이 모듈은 모든 ctx 생성처가 user_email 을 ctx 에 정확히 박아넣는지 명시 검증.
"""
from __future__ import annotations

from app.pipelines.base import PipelineContext


def test_pipeline_context_has_user_email_field():
    """PipelineContext 가 user_email 필드 노출. default '' (옛 코드 호환)."""
    ctx = PipelineContext(gemini=None, neo4j=None, idempotency_key="t")
    assert hasattr(ctx, "user_email")
    assert ctx.user_email == ""


def test_pipeline_context_accepts_user_email_kwarg():
    ctx = PipelineContext(
        gemini=None, neo4j=None, idempotency_key="t", user_email="alice@example.com"
    )
    assert ctx.user_email == "alice@example.com"


def test_delete_routes_build_context_propagates_user_email():
    """app/api/delete_routes.py 의 _build_context — Phase 2D 필수."""
    from app.api.delete_routes import _build_context
    ctx = _build_context(idempotency_key="t", user_email="alice@example.com")
    assert ctx.user_email == "alice@example.com"


def test_gateway_routes_ctx_no_llm_propagates_user_email():
    """app/api/gateway_routes.py 의 _ctx_no_llm — body 신뢰 X, 인증된 email 만."""
    from app.api.gateway_routes import _ctx_no_llm
    ctx = _ctx_no_llm(user_email="alice@example.com")
    assert ctx.user_email == "alice@example.com"


def test_lineage_routes_build_context_propagates_user_email():
    from app.api.lineage_routes import _build_context
    ctx = _build_context(idempotency_key="t", user_email="alice@example.com")
    assert ctx.user_email == "alice@example.com"
