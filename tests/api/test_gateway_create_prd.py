"""
gateway `createPRD` 핸들러 — 검수 모드의 [PRD 생성] 버튼 + PRD error 복구 경로 (P0-6).

[배경]
FE(plan.vue)는 POST /api/gateway/createPRD {project_name, version} 을 호출하지만
dispatch 에 핸들러가 없어 검수 모드 사용자가 CPS 검토 후 **다음 단계로 갈 수 없었다**
(과거 `cps` 핸들러 누락 410 버그와 동일 패턴). prd.mode='error' 강등 후 재생성 경로도
이 버튼이므로, 부재 시 사용자가 갇힌다.

계약: 저장된 해당 버전 CPS delta 의 full_markdown 으로 PRD 파이프라인 단독 실행.
"""
from __future__ import annotations

import pytest

from app.api import gateway_compat_routes as gw

pytestmark = pytest.mark.asyncio


def test_create_prd_registered_and_classified():
    """dispatch 등록 + ownership ACCESS 분류 (미분류면 dispatcher 가 500 — 정합성 가드)."""
    assert "createPRD" in gw._DISPATCH
    assert "createPRD" in gw._OWNERSHIP_ACCESS


async def test_create_prd_requires_version():
    with pytest.raises(Exception) as exc_info:
        await gw._h_create_prd({"project_name": "x"}, {}, user_email="u@x.com")
    assert getattr(exc_info.value, "status_code", None) == 400


async def test_create_prd_404_when_cps_delta_missing(monkeypatch):
    async def _none(*a, **k):
        return None
    monkeypatch.setattr(gw.query_repository, "get_cps_delta_markdown", _none, raising=False)

    async def _no_quota_block(_email):
        return None
    monkeypatch.setattr(gw.quota, "assert_tokens_within_limit", _no_quota_block)

    with pytest.raises(Exception) as exc_info:
        await gw._h_create_prd({"project_name": "x", "version": "V3"}, {}, user_email="u@x.com")
    assert getattr(exc_info.value, "status_code", None) == 404


async def test_create_prd_runs_pipeline_with_synthesized_graph(monkeypatch):
    """delta markdown → CPS_Document 1-노드 graph 합성 → run_prd_pipeline 호출 → 결과 반환."""
    async def _md(project_name, version, team_id=""):
        assert project_name == "x" and version == "V3"
        return "## 📄 CPS 명세서\n### 2. Problem\n- [PRB-01] 수기 오류"
    monkeypatch.setattr(gw.query_repository, "get_cps_delta_markdown", _md, raising=False)

    async def _no_quota_block(_email):
        return None
    monkeypatch.setattr(gw.quota, "assert_tokens_within_limit", _no_quota_block)

    captured = {}

    class _R:
        delta_prd_id = "doc_prd_x_v3"
        master_prd_id = "doc_prd_master_x"
        mode = "incremental"
        diagnostic = {}

    async def _fake_pipeline(ctx, payload):
        captured["payload"] = payload
        return _R()
    monkeypatch.setattr(gw, "run_prd_pipeline", _fake_pipeline)

    out = await gw._h_create_prd({"project_name": "x", "version": "V3"}, {}, user_email="u@x.com")

    payload = captured["payload"]
    nodes = payload.cps_graph["nodes"]
    assert nodes[0]["label"] == "CPS_Document"
    assert "PRB-01" in nodes[0]["properties"]["full_markdown"]   # delta 본문이 입력으로
    assert out["result"]["mode"] == "incremental"
    assert out["result"]["master_prd_id"] == "doc_prd_master_x"
