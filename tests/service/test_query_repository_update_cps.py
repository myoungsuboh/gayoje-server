"""
query_repository.update_master_cps_markdown 회귀 가드.

[배경 — 2026-05 검수 게이트 Phase 2]
사용자가 LLM 생성 CPS 를 직접 markdown 으로 수정 가능. 정책:
- Master CPS (is_latest=true) 의 full_markdown 만 덮어쓰기
- Problem/Solution 그래프 노드는 그대로 (markdown 은 display only)
- 없는 project → None (404 매핑)
- user_edited_at 필드 갱신 (운영 추적)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import query_repository as q


pytestmark = pytest.mark.asyncio


class _FakeRun:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher: str, params: Optional[Dict[str, Any]] = None,
                       database: Optional[str] = None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        return self._responses.pop(0) if self._responses else []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses=None):
        fake = _FakeRun(responses)
        monkeypatch.setattr(
            "app.service.query_repository.neo4j_client.run_cypher", fake
        )
        return fake
    return _setup


# ─── 정상 경로 ──────────────────────────────────────────


async def test_update_returns_master_id_and_last_updated(fake_run):
    fake = fake_run([
        [{"master_id": "doc_cps_master_p", "last_updated": 1700000000000}]
    ])
    out = await q.update_master_cps_markdown("p", "# new markdown")
    assert out == {
        "master_id": "doc_cps_master_p",
        "last_updated": 1700000000000,
    }
    # cypher 파라미터 바인딩 검증
    params = fake.calls[0]["params"]
    assert params["project"] == "p"
    assert params["content"] == "# new markdown"


async def test_update_cypher_filters_master_and_latest(fake_run):
    """[회귀] master 아닌 노드 / 옛 master 는 매칭 안 됨."""
    fake_run([[{"master_id": "x", "last_updated": 0}]])
    await q.update_master_cps_markdown("p", "x")
    cypher = fake_run.__self__ if False else None  # noqa: F841
    # 직접 cypher 검증
    cypher_str = q._UPDATE_CPS_MARKDOWN_CYPHER
    assert "type: 'Master'" in cypher_str
    assert "is_latest: true" in cypher_str
    # full_markdown 만 덮어쓰기 — Problem/Solution 노드 안 건드림
    assert "SET m.full_markdown" in cypher_str
    assert "DELETE" not in cypher_str.upper().replace("DELETED", "")
    # user_edited_at 추적 필드
    assert "user_edited_at" in cypher_str


async def test_update_returns_none_when_no_master(fake_run):
    """master 없으면 빈 응답 → None (404 매핑)."""
    fake_run([[]])
    out = await q.update_master_cps_markdown("ghost", "x")
    assert out is None


async def test_update_returns_none_when_response_missing_id(fake_run):
    """응답에 master_id 누락 → None (방어)."""
    fake_run([[{"last_updated": 123}]])
    out = await q.update_master_cps_markdown("p", "x")
    assert out is None


async def test_update_preserves_long_markdown(fake_run):
    """긴 markdown (수백KB) 도 parameter binding 으로 안전 전달."""
    fake = fake_run([[{"master_id": "m1", "last_updated": 0}]])
    long_md = "# " + "a" * 50000
    await q.update_master_cps_markdown("p", long_md)
    assert fake.calls[0]["params"]["content"] == long_md
    # cypher 본문에 long_md 가 직접 인터폴되지 않음 ($content 만)
    assert long_md not in fake.calls[0]["cypher"]


# ─── [2026-05-26] 데이터 무결성 가드 — master full_markdown wipe 차단 ───


async def test_update_cps_refuses_empty_content(fake_run):
    """빈 content → ValueError. master 데이터 손실 방지. cypher 호출 자체 미발생."""
    fake = fake_run()
    with pytest.raises(ValueError, match="비어있습니다"):
        await q.update_master_cps_markdown("p", "")
    assert fake.calls == [], "cypher 호출되면 안 됨 (가드가 사전 차단)"


async def test_update_cps_refuses_whitespace_only(fake_run):
    fake = fake_run()
    with pytest.raises(ValueError, match="비어있습니다"):
        await q.update_master_cps_markdown("p", "   \n\t   ")
    assert fake.calls == []


async def test_update_prd_refuses_empty_content(fake_run):
    """PRD update 도 동일 정책. 동일 negative 케이스."""
    fake = fake_run()
    with pytest.raises(ValueError, match="비어있습니다"):
        await q.update_master_prd_markdown("p", "")
    assert fake.calls == []


async def test_update_prd_refuses_whitespace_only(fake_run):
    fake = fake_run()
    with pytest.raises(ValueError, match="비어있습니다"):
        await q.update_master_prd_markdown("p", "   ")
    assert fake.calls == []
