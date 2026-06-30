"""
MCP spec tools 단위 테스트.

검증:
- get_api_spec: list(compact)/단일(full) 모드, keyword 필터, service enrich,
  endpoint 매칭, limit 클램프, 없는 id 가드
- get_screen_spec: calls_apis 를 method/endpoint 로 enrich, 미존재 api id graceful,
  단일/list 모드
- get_lint_findings: only_unapplied 필터, evidence 노출, no_lint 가드, limit 클램프

repository 는 monkeypatch 로 fake 응답 주입. 인증 가드는 no-op 모킹
(auth 자체는 test_mcp_auth.py 가 검증).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.mcp import spec_tools


# ─── 공용 fixture ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _bypass_auth(monkeypatch):
    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(
        "app.mcp.spec_tools.require_mcp_user_and_assert_owns", _noop
    )


def _fake_spack():
    """SpackGraph 흉내 — apis/screens 는 plain dict(실제 decode 결과 형태),
    api_service_rels 는 CrossMappingRel."""
    from app.service.query_repository import CrossMappingRel

    return SimpleNamespace(
        apis=[
            {
                "id": "api_1", "name": "주문생성", "method": "POST",
                "endpoint": "/orders", "description": "주문 생성",
                "related_story_id": "story_01_1",
                "path_params": [], "query_params": [],
                "request_body": {"fields": [{"name": "item"}]},
                "response_body": {"status": 201},
                "error_cases": [{"code": 400}], "auth": {"type": "bearer"},
                "lineage_confidence": "direct",
            },
            {
                "id": "api_2", "name": "주문조회", "method": "GET",
                "endpoint": "/orders/{id}", "description": "단건 조회",
                "related_story_id": None,
                "path_params": [{"name": "id"}], "query_params": [],
                "request_body": {}, "response_body": {},
                "error_cases": [], "auth": {}, "lineage_confidence": "none",
            },
        ],
        screens=[
            {
                "id": "scr_1", "name": "주문화면", "path": "/order",
                "description": "주문 페이지", "next_screens": ["scr_2"],
                "calls_apis": ["api_1", "api_unknown"],
                "related_story_id": "story_01_1",
            },
        ],
        api_service_rels=[
            CrossMappingRel(source_id="api_1", target_id="svc_1",
                            target_name="OrderService", type="HANDLED_BY"),
        ],
    )


@pytest.fixture
def _patch_spack(monkeypatch):
    async def _fake(_project, *_a, **_kw):
        return _fake_spack()
    monkeypatch.setattr(
        "app.service.query_repository.get_spack_graph", _fake
    )


# ─── get_api_spec ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_spec_list_compact_with_service(_patch_spack):
    out = await spec_tools.get_api_spec.fn("proj")
    assert out["mode"] == "list"
    assert out["count"] == 2 and out["total"] == 2
    first = out["apis"][0]
    assert first["service"] == "OrderService"
    # list 모드는 compact — body/auth 미포함
    assert "request_body" not in first and "auth" not in first


@pytest.mark.asyncio
async def test_api_spec_keyword_filter(_patch_spack):
    out = await spec_tools.get_api_spec.fn("proj", keyword="get")
    assert out["count"] == 1 and out["apis"][0]["id"] == "api_2"


@pytest.mark.asyncio
async def test_api_spec_single_full(_patch_spack):
    out = await spec_tools.get_api_spec.fn("proj", api_id="api_1")
    assert out["mode"] == "single" and out["count"] == 1
    a = out["apis"][0]
    assert a["request_body"]["fields"][0]["name"] == "item"
    assert a["auth"]["type"] == "bearer"
    assert a["error_cases"] == [{"code": 400}]


@pytest.mark.asyncio
async def test_api_spec_endpoint_match(_patch_spack):
    out = await spec_tools.get_api_spec.fn("proj", api_id="/orders")
    assert out["count"] == 1 and out["apis"][0]["id"] == "api_1"


@pytest.mark.asyncio
async def test_api_spec_missing_id(_patch_spack):
    out = await spec_tools.get_api_spec.fn("proj", api_id="nope")
    assert out["count"] == 0 and out["apis"] == []


@pytest.mark.asyncio
async def test_api_spec_limit_truncates(_patch_spack):
    out = await spec_tools.get_api_spec.fn("proj", limit=1)
    assert out["count"] == 1 and out["truncated"] is True


# ─── get_screen_spec ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_screen_spec_enriches_calls_apis(_patch_spack):
    out = await spec_tools.get_screen_spec.fn("proj")
    s = out["screens"][0]
    assert s["calls_apis"][0] == {"id": "api_1", "method": "POST", "endpoint": "/orders"}
    # 미존재 api id 는 method/endpoint None 으로 graceful
    assert s["calls_apis"][1] == {"id": "api_unknown", "method": None, "endpoint": None}
    assert s["next_screens"] == ["scr_2"]


@pytest.mark.asyncio
async def test_screen_spec_single(_patch_spack):
    out = await spec_tools.get_screen_spec.fn("proj", screen_id="scr_1")
    assert out["mode"] == "single" and out["count"] == 1
    out2 = await spec_tools.get_screen_spec.fn("proj", screen_id="nope")
    assert out2["count"] == 0


# ─── get_lint_findings ───────────────────────────────────────


def _fake_lint():
    from app.service.lint_repository import (
        LintCase, LintCaseRule, LintEvidence, LintResult,
    )
    return LintResult(
        score=72, violations=1, rules_checked=3, sampled_files=8,
        total_code_files=20, coverage_truncated=True, saved_at=1700000000000,
        cases=[
            LintCase(title="주문 API", convergence=50, rules=[
                LintCaseRule(
                    rule="api:POST /orders", description="주문 생성 API",
                    applied=True, detection_method="deterministic",
                    evidence=[LintEvidence(file="src/api/order.py", line=12,
                                           snippet="@router.post", kind="endpoint")],
                ),
                LintCaseRule(
                    rule="api:GET /orders/{id}", description="주문 조회 API",
                    applied=False, detection_method="fallback", evidence=[],
                ),
            ]),
        ],
    )


@pytest.fixture
def _patch_lint(monkeypatch):
    async def _fake(_project, *_a, **_kw):
        return _fake_lint()
    monkeypatch.setattr(
        "app.service.lint_repository.get_last_lint_for_project", _fake
    )


@pytest.mark.asyncio
async def test_lint_only_unapplied_default(_patch_lint):
    out = await spec_tools.get_lint_findings.fn("proj")
    assert out["status"] == "ok" and out["score"] == 72
    assert out["coverage_truncated"] is True
    assert len(out["findings"]) == 1
    assert out["findings"][0]["rule"] == "api:GET /orders/{id}"
    assert out["findings"][0]["applied"] is False


@pytest.mark.asyncio
async def test_lint_all_with_evidence(_patch_lint):
    out = await spec_tools.get_lint_findings.fn("proj", only_unapplied=False)
    assert len(out["findings"]) == 2
    applied = [f for f in out["findings"] if f["applied"]][0]
    ev = applied["evidence"][0]
    assert ev["file"] == "src/api/order.py" and ev["line"] == 12


@pytest.mark.asyncio
async def test_lint_limit_truncates(_patch_lint):
    out = await spec_tools.get_lint_findings.fn("proj", only_unapplied=False, limit=1)
    assert len(out["findings"]) == 1 and out["findings_truncated"] is True


@pytest.mark.asyncio
async def test_lint_no_result(monkeypatch):
    async def _none(_project, *_a, **_kw):
        return None
    monkeypatch.setattr(
        "app.service.lint_repository.get_last_lint_for_project", _none
    )
    out = await spec_tools.get_lint_findings.fn("proj")
    assert out == {"status": "no_lint"}
