"""
POST /api/v2/{cps,prd}/resynthesize 회귀 — Phase 3.5b LLM graph→markdown 재합성.

[보호하는 동작]
- LLM 호출 성공 → markdown 저장 + 200 응답
- master 없음 / 그래프 노드 없음 → 404
- LLM 출력 형식 실패 (header 누락) → 404 + markdown 변경 안 됨
- LLM 호출 자체 실패 (GeminiError) → 적절한 HTTP 코드
- non-owner → 403
- 토큰 한도 초과 → 429
- rate limit decorator 존재
- prompt 가 current_markdown + graph_nodes 치환을 거쳐 LLM 에 전달
- 형식 검증기 (_looks_like_cps/prd_markdown) 가 nonempty + header 보장
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.api import query_routes
from app.clients.gemini_client import GeminiError
from app.service import query_repository as q
from app.service.query_repository import CpsMaster, PrdMaster
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


def _user(email: str = "owner@x.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="t",
        subscription_type="free", is_admin=False,
    )


def _fake_request(path: str = "/api/v2/cps/resynthesize"):
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"type": "http"},
        headers={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path=path),
        method="POST",
    )


@pytest.fixture
def allow_ownership(monkeypatch):
    async def fake(email, project): return None
    monkeypatch.setattr(
        "app.api.query_routes.ownership_repository.assert_owns", fake
    )


@pytest.fixture
def deny_ownership(monkeypatch):
    async def fake(email, project):
        raise HTTPException(status_code=403, detail="권한이 없습니다.")
    monkeypatch.setattr(
        "app.api.query_routes.ownership_repository.assert_owns", fake
    )


@pytest.fixture
def allow_quota(monkeypatch):
    async def fake(email): return None
    monkeypatch.setattr(
        "app.api.query_routes.quota.assert_tokens_within_limit", fake
    )


@pytest.fixture
def deny_quota(monkeypatch):
    async def fake(email):
        raise HTTPException(status_code=429, detail="quota exceeded")
    monkeypatch.setattr(
        "app.api.query_routes.quota.assert_tokens_within_limit", fake
    )


@pytest.fixture
def mock_tracked_ctx(monkeypatch):
    """tracked_pipeline_context 를 patch — 가짜 ctx (gemini mock 가능)."""
    from contextlib import asynccontextmanager

    fake_gemini = SimpleNamespace()
    state: Dict[str, Any] = {"gemini": fake_gemini}

    @asynccontextmanager
    async def fake_ctx(*, user_email, idempotency_key):
        state["last_user_email"] = user_email
        state["last_key"] = idempotency_key
        yield SimpleNamespace(gemini=fake_gemini, neo4j=None, idempotency_key=idempotency_key)

    monkeypatch.setattr(
        "app.api.query_routes.tracked_pipeline_context", fake_ctx
    )
    return state


# ─── helpers — service-level fakes ─────────────────────────────


def _patch_resync(monkeypatch, name: str, returnv):
    async def fake(ctx, project, team_id=""):
        return returnv() if callable(returnv) else returnv
    monkeypatch.setattr(f"app.api.query_routes.q.{name}", fake)


# ─── CPS resynth route ─────────────────────────────────────


async def test_resynth_cps_success(allow_ownership, allow_quota, mock_tracked_ctx, monkeypatch):
    new_md = "## 📄 CPS 명세서\n\n### 1. Context\n...\n### 2. Problem\n- **[PRB-01] X**: Y\n"
    _patch_resync(monkeypatch, "resync_cps_markdown_from_graph", new_md)
    async def fake_get(p, team_id=""): return CpsMaster(master_id="cps-1", content=new_md)
    monkeypatch.setattr("app.api.query_routes.q.get_master_cps", fake_get)

    payload = query_routes.ResynthesizeRequest(project_name="p")
    out = await query_routes.resynthesize_cps_route.__wrapped__(
        request=_fake_request(),
        payload=payload,
        current_user=_user(),
    )
    assert out.project_name == "p"
    assert out.markdown.startswith("## 📄 CPS")
    assert out.master_id == "cps-1"


async def test_resynth_cps_404_when_resync_returns_none(allow_ownership, allow_quota, mock_tracked_ctx, monkeypatch):
    """[회귀] master 없음 / 그래프 빈 / LLM 형식 실패 모두 None → 404."""
    _patch_resync(monkeypatch, "resync_cps_markdown_from_graph", None)

    payload = query_routes.ResynthesizeRequest(project_name="p")
    with pytest.raises(HTTPException) as exc:
        await query_routes.resynthesize_cps_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user(),
        )
    assert exc.value.status_code == 404


async def test_resynth_cps_denies_when_not_owner(deny_ownership, allow_quota):
    payload = query_routes.ResynthesizeRequest(project_name="victim")
    with pytest.raises(HTTPException) as exc:
        await query_routes.resynthesize_cps_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user("attacker@evil.com"),
        )
    assert exc.value.status_code == 403


async def test_resynth_cps_429_when_quota_exceeded(allow_ownership, deny_quota):
    payload = query_routes.ResynthesizeRequest(project_name="p")
    with pytest.raises(HTTPException) as exc:
        await query_routes.resynthesize_cps_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user(),
        )
    assert exc.value.status_code == 429


async def test_resynth_cps_propagates_gemini_error(allow_ownership, allow_quota, mock_tracked_ctx, monkeypatch):
    """[회귀] LLM 호출 자체 실패 시 HTTPException 으로 변환."""
    async def boom(ctx, project, team_id=""):
        raise GeminiError("rate limit", kind="rate_limited")
    monkeypatch.setattr("app.api.query_routes.q.resync_cps_markdown_from_graph", boom)

    payload = query_routes.ResynthesizeRequest(project_name="p")
    with pytest.raises(HTTPException) as exc:
        await query_routes.resynthesize_cps_route.__wrapped__(
            request=_fake_request(),
            payload=payload,
            current_user=_user(),
        )
    # gemini_error_to_http 가 적절한 status 매핑.
    assert exc.value.status_code in (429, 503, 502, 500)


def test_resynth_cps_route_has_rate_limit():
    assert hasattr(query_routes.resynthesize_cps_route, "__wrapped__")


# ─── PRD resynth route ─────────────────────────────────────


async def test_resynth_prd_success(allow_ownership, allow_quota, mock_tracked_ctx, monkeypatch):
    new_md = "# PRD - MyProject\n\n## 1. Product Overview\n...\n## 2. Epic & User Story\n..."
    _patch_resync(monkeypatch, "resync_prd_markdown_from_graph", new_md)
    async def fake_get(p, team_id=""): return PrdMaster(master_prd_id="prd-1", prd_content=new_md)
    monkeypatch.setattr("app.api.query_routes.q.get_master_prd", fake_get)

    payload = query_routes.ResynthesizeRequest(project_name="p")
    out = await query_routes.resynthesize_prd_route.__wrapped__(
        request=_fake_request("/api/v2/prd/resynthesize"),
        payload=payload,
        current_user=_user(),
    )
    assert out.project_name == "p"
    assert out.markdown.startswith("# PRD")
    assert out.master_id == "prd-1"


async def test_resynth_prd_404_when_none(allow_ownership, allow_quota, mock_tracked_ctx, monkeypatch):
    _patch_resync(monkeypatch, "resync_prd_markdown_from_graph", None)
    payload = query_routes.ResynthesizeRequest(project_name="p")
    with pytest.raises(HTTPException) as exc:
        await query_routes.resynthesize_prd_route.__wrapped__(
            request=_fake_request("/api/v2/prd/resynthesize"),
            payload=payload,
            current_user=_user(),
        )
    assert exc.value.status_code == 404


def test_resynth_prd_route_has_rate_limit():
    assert hasattr(query_routes.resynthesize_prd_route, "__wrapped__")


# ─── service helpers — format / validate ───────────────────


def test_format_nodes_for_prompt_groups_by_label():
    nodes = [
        {"id": "prb_01_1", "label": "Problem", "summary": "p1"},
        {"id": "res_01_1", "label": "Solution", "summary": "r1"},
        {"id": "prb_02_1", "label": "Problem", "summary": "p2"},
    ]
    txt = q._format_nodes_for_prompt(nodes)
    assert "## Problem" in txt
    assert "## Solution" in txt
    # Problem 섹션이 Solution 보다 먼저 (alphabetical).
    assert txt.index("## Problem") < txt.index("## Solution")
    assert "prb_01_1" in txt and "prb_02_1" in txt
    assert "r1" in txt


def test_format_nodes_for_prompt_handles_empty():
    assert "노드 없음" in q._format_nodes_for_prompt([])


def test_looks_like_cps_markdown_accepts_valid():
    assert q._looks_like_cps_markdown("## 📄 CPS 명세서\n\n### 1. Context\n" + "x" * 100)


def test_looks_like_cps_markdown_rejects_empty_or_short():
    assert not q._looks_like_cps_markdown("")
    assert not q._looks_like_cps_markdown("short")


def test_looks_like_cps_markdown_rejects_wrong_format():
    """[회귀] LLM 이 사과말이나 JSON 으로 응답할 때 거부."""
    assert not q._looks_like_cps_markdown("죄송합니다, 형식을 따를 수 없습니다.")
    assert not q._looks_like_cps_markdown('{"error": "..."}')


def test_looks_like_prd_markdown_accepts_valid():
    assert q._looks_like_prd_markdown("# PRD - MyProj\n\n## 1. Product Overview\n" + "y" * 100)


def test_looks_like_prd_markdown_rejects_wrong():
    assert not q._looks_like_prd_markdown("")
    assert not q._looks_like_prd_markdown("hello world")


# ─── service-level: resync_cps_markdown_from_graph ─────────


async def test_resync_cps_returns_none_when_no_master(monkeypatch):
    async def no_master(p): return None
    monkeypatch.setattr("app.service.query_repository.get_master_cps", no_master)
    ctx = SimpleNamespace(gemini=SimpleNamespace())
    out = await q.resync_cps_markdown_from_graph(ctx, "p")
    assert out is None


async def test_resync_cps_returns_none_when_no_nodes(monkeypatch):
    async def has_master(p): return CpsMaster(master_id="m", content="old")
    async def no_nodes(p): return []
    monkeypatch.setattr("app.service.query_repository.get_master_cps", has_master)
    monkeypatch.setattr("app.service.query_repository.list_cps_nodes", no_nodes)
    ctx = SimpleNamespace(gemini=SimpleNamespace())
    out = await q.resync_cps_markdown_from_graph(ctx, "p")
    assert out is None


async def test_resync_cps_rejects_bad_llm_output(monkeypatch):
    """[회귀] LLM 출력이 markdown 형식 아니면 None 반환 (Phase 3.5c: save 안 함)."""
    async def has_master(p): return CpsMaster(master_id="m", content="old md")
    async def has_nodes(p): return [
        {"id": "prb_01", "label": "Problem", "summary": "x"}
    ]
    monkeypatch.setattr("app.service.query_repository.get_master_cps", has_master)
    monkeypatch.setattr("app.service.query_repository.list_cps_nodes", has_nodes)

    # [회귀] preview 모드 — save 함수 절대 호출되면 안 됨 (LLM 결과 좋든 나쁘든).
    save_called = {"v": False}
    async def fake_save(p, c):
        save_called["v"] = True
        return {"master_id": "m"}
    monkeypatch.setattr("app.service.query_repository.update_master_cps_markdown", fake_save)

    async def bad_gen(prompt, temperature=None, **kwargs):
        return SimpleNamespace(text="죄송합니다 형식을 못 지킵니다")
    ctx = SimpleNamespace(gemini=SimpleNamespace(generate=bad_gen))

    out = await q.resync_cps_markdown_from_graph(ctx, "p")
    assert out is None
    assert save_called["v"] is False


async def test_resync_cps_returns_preview_without_saving(monkeypatch):
    """[Phase 3.5c — 핵심 회귀] valid LLM 출력도 service 단에서 save 안 함 (preview)."""
    async def has_master(p): return CpsMaster(master_id="m", content="old md")
    async def has_nodes(p): return [
        {"id": "prb_01", "label": "Problem", "summary": "x"}
    ]
    monkeypatch.setattr("app.service.query_repository.get_master_cps", has_master)
    monkeypatch.setattr("app.service.query_repository.list_cps_nodes", has_nodes)

    save_called = {"v": False}
    async def fake_save(p, c):
        save_called["v"] = True
        return {"master_id": "m"}
    monkeypatch.setattr("app.service.query_repository.update_master_cps_markdown", fake_save)

    valid_md = "## 📄 CPS 명세서\n\n### 1. Context\n다양한 배경 텍스트입니다." + "x" * 100
    async def good_gen(prompt, temperature=None, **kwargs):
        return SimpleNamespace(text=valid_md)
    ctx = SimpleNamespace(gemini=SimpleNamespace(generate=good_gen))

    out = await q.resync_cps_markdown_from_graph(ctx, "p")
    assert out == valid_md  # preview 반환
    assert save_called["v"] is False, "service 단에서 save 호출되면 안 됨 (preview)"


async def test_resync_cps_strips_code_block_fence(monkeypatch):
    """[회귀] LLM 이 ```markdown ... ``` 감싸면 strip 후 검증 후 preview 반환."""
    async def has_master(p): return CpsMaster(master_id="m", content="old")
    async def has_nodes(p): return [
        {"id": "prb_01", "label": "Problem", "summary": "x"}
    ]
    monkeypatch.setattr("app.service.query_repository.get_master_cps", has_master)
    monkeypatch.setattr("app.service.query_repository.list_cps_nodes", has_nodes)

    fenced = "```markdown\n## 📄 CPS 명세서\n\n### 1. Context\n" + "y" * 100 + "\n```"
    async def gen(prompt, temperature=None, **kwargs):
        return SimpleNamespace(text=fenced)
    ctx = SimpleNamespace(gemini=SimpleNamespace(generate=gen))

    out = await q.resync_cps_markdown_from_graph(ctx, "p")
    assert out is not None
    assert not out.startswith("```")
    assert out.startswith("## 📄 CPS")


# ─── service-level: PRD nested Epic↔Story 검증 (Phase 3.5c) ──


def test_prd_has_nested_epic_story_passes_for_valid():
    """[회귀] 올바른 nested 형식은 통과."""
    md = "# PRD\n\n#### 📦 Epic 1: 인증\n- 📝 Story 1.1: 로그인\n- 📝 Story 1.2: 로그아웃\n#### 📦 Epic 2: 프로필\n- 📝 Story 2.1: 수정"
    assert q._prd_has_nested_epic_story(md, {"Epic": 2, "Story": 3})


def test_prd_has_nested_epic_story_rejects_no_epic_in_output():
    """[회귀] 그래프에 Epic 있는데 출력에 Epic 마커 없으면 거부."""
    md = "# PRD\n\n어떤 텍스트\n- Story 1.1\n- Story 2.1\n" + "x" * 200
    assert not q._prd_has_nested_epic_story(md, {"Epic": 2, "Story": 2})


def test_prd_has_nested_epic_story_rejects_epic_count_mismatch():
    """[회귀] 그래프에 5개 Epic 있는데 출력에 1개 → 거부 (절반 이하)."""
    md = "# PRD\n\n#### 📦 Epic 1: only"
    assert not q._prd_has_nested_epic_story(md, {"Epic": 5})


def test_prd_has_nested_epic_story_allows_minor_diff():
    """[Phase 3.5c] LLM 이 1개 정도 머지하는 건 허용 (1/2 이상)."""
    md = "# PRD\n\n#### 📦 Epic 1: A\n#### 📦 Epic 2: B\n#### 📦 Epic 3: C"
    # 그래프에 4개, 출력에 3개 — 1개 적지만 절반 이상이라 통과.
    assert q._prd_has_nested_epic_story(md, {"Epic": 4})


def test_prd_has_nested_epic_story_rejects_when_story_missing():
    """[회귀] 그래프에 Story 있는데 출력에 'Story' 단어 없으면 거부."""
    md = "# PRD\n\n#### 📦 Epic 1: only\n#### 📦 Epic 2: another"
    assert not q._prd_has_nested_epic_story(md, {"Epic": 2, "Story": 5})


def test_prd_has_nested_epic_story_skips_when_no_epic_in_graph():
    """그래프에 Epic 없으면 검증 의미 없음 — 통과."""
    md = "# PRD\n\n어떤 내용"
    assert q._prd_has_nested_epic_story(md, {})


async def test_resync_prd_rejects_when_epic_count_off(monkeypatch):
    """[Phase 3.5c — 핵심 회귀] PRD resync 가 nested 검증 실패 시 None 반환."""
    async def has_master(p): return PrdMaster(master_prd_id="m", prd_content="old")
    async def many_nodes(p): return [
        {"id": f"epic_{i:02d}", "label": "Epic", "summary": f"e{i}"} for i in range(1, 6)
    ]
    monkeypatch.setattr("app.service.query_repository.get_master_prd", has_master)
    monkeypatch.setattr("app.service.query_repository.list_prd_nodes", many_nodes)

    # LLM 이 1개 Epic 만 반환 (5개 중 1개 — 머지 over)
    too_few = "# PRD\n\n## 1. Product Overview\n많은 텍스트.\n## 2. Epic & User Story\n\n#### 📦 Epic 1: 단일\n" + "y" * 100
    async def gen(prompt, temperature=None, **kwargs):
        return SimpleNamespace(text=too_few)
    ctx = SimpleNamespace(gemini=SimpleNamespace(generate=gen))

    out = await q.resync_prd_markdown_from_graph(ctx, "p")
    assert out is None, "Epic 개수가 그래프보다 크게 다르면 거부"


async def test_resync_prd_returns_preview_on_valid(monkeypatch):
    """[Phase 3.5c] valid PRD nested 출력 → preview 반환, save 호출 안 함."""
    async def has_master(p): return PrdMaster(master_prd_id="m", prd_content="old")
    async def two_epics(p): return [
        {"id": "epic_01", "label": "Epic", "summary": "e1"},
        {"id": "epic_02", "label": "Epic", "summary": "e2"},
        {"id": "story_01_1", "label": "Story", "summary": "s1.1"},
    ]
    monkeypatch.setattr("app.service.query_repository.get_master_prd", has_master)
    monkeypatch.setattr("app.service.query_repository.list_prd_nodes", two_epics)

    save_called = {"v": False}
    async def fake_save(p, c):
        save_called["v"] = True
        return {"master_id": "m"}
    monkeypatch.setattr("app.service.query_repository.update_master_prd_markdown", fake_save)

    good = "# PRD\n\n## 1. Product Overview\n많은 텍스트.\n## 2. Epic & User Story\n\n#### 📦 Epic 1: A\n- 📝 Story 1.1: x\n#### 📦 Epic 2: B\n" + "y" * 100
    async def gen(prompt, temperature=None, **kwargs):
        return SimpleNamespace(text=good)
    ctx = SimpleNamespace(gemini=SimpleNamespace(generate=gen))

    out = await q.resync_prd_markdown_from_graph(ctx, "p")
    assert out == good
    assert save_called["v"] is False
