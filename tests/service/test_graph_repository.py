"""
graph_repository 단위 테스트 — trace_upstream 의 cypher dispatch + 결과 변환.

각 시작 노드 kind 별로:
  - 정상 흐름 (target + collections 채워짐)
  - 시작 노드 미발견 (not_found=True)
  - dedup (cps_raw / logs_raw 의 중복 노드 1개로 줄어듦)
  - 미지원 라벨 silently drop
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest


pytestmark = pytest.mark.asyncio


# ===== Fake Neo4j node =====
#
# graph_repository._node_labels() 가 `node.labels` 또는 dict 의 _label 키를 본다.
# Neo4j driver 의 Node 와 호환되도록 두 가지 표현 모두 지원하는 fake 만든다.


class _FakeNode:
    """Neo4j.graph.Node 와 비슷한 모양 — properties + labels."""

    def __init__(self, labels: List[str], props: Dict[str, Any]):
        self.labels = frozenset(labels)
        self._props = props

    def keys(self):
        return self._props.keys()

    def __getitem__(self, k):
        return self._props[k]

    def get(self, k, default=None):
        return self._props.get(k, default)


def api(id: str, method: str = "POST", endpoint: str = "/x", project: str = "p1") -> _FakeNode:
    return _FakeNode(
        ["API"],
        {"id": id, "method": method, "endpoint": endpoint, "project": project},
    )


def story(id: str, summary: str = "story summary", project: str = "p1") -> _FakeNode:
    return _FakeNode(
        ["Story"], {"id": id, "summary": summary, "priority": "High", "project": project}
    )


def epic(id: str, summary: str = "epic summary", project: str = "p1") -> _FakeNode:
    return _FakeNode(["Epic"], {"id": id, "summary": summary, "project": project})


def problem(id: str, summary: str = "problem summary", project: str = "p1") -> _FakeNode:
    return _FakeNode(["Problem"], {"id": id, "summary": summary, "project": project})


def resolution(id: str, summary: str = "res summary", project: str = "p1") -> _FakeNode:
    return _FakeNode(["Solution"], {"id": id, "summary": summary, "project": project})


def prd(id: str, version: str = "v1.0", project: str = "p1") -> _FakeNode:
    return _FakeNode(["PRD_Document"], {"id": id, "version": version, "project": project})


def cps(id: str, version: str = "v1.0", project: str = "p1") -> _FakeNode:
    return _FakeNode(["CPS_Document"], {"id": id, "version": version, "project": project})


def meeting(id: str, version: str = "v1.0", date: str = "2026-04-15", project: str = "p1") -> _FakeNode:
    return _FakeNode(
        ["Meeting_Log"],
        {"id": id, "version": version, "date": date, "project": project},
    )


# ===== Fake run_cypher =====


class _Fake:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(self, cypher, params=None, database=None):
        self.calls.append({"cypher": cypher, "params": params or {}})
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses=None):
        fake = _Fake(responses=responses)
        monkeypatch.setattr(
            "app.service.graph_repository.neo4j_client.run_cypher", fake
        )
        return fake
    return _setup


# ===== trace_upstream — kind=api =====


async def test_trace_from_api_happy_path(fake_run):
    """API 시작 — 전체 chain (Story/Epic/Problem/PRD/CPS/Meeting) 채워짐."""
    from app.service import graph_repository

    fake = fake_run(
        [
            [
                {
                    "target": api("API-03", "POST", "/tickets/{id}/refund"),
                    "stories": [story("story_03_2", "환불 신청")],
                    "epics": [epic("epic_03", "환불 관리")],
                    "problems": [problem("prb_05", "결제 취소 후 환불 지연")],
                    "resolutions": [resolution("res_05", "즉시 환불 처리")],
                    "prds": [prd("doc_prd_p1_v1_3", "v1.3")],
                    "cps_raw": [
                        cps("doc_cps_p1_v1_3", "v1.3"),
                        cps("doc_cps_p1_v1_3", "v1.3"),  # 중복 — dedup 검증
                    ],
                    "logs_raw": [
                        meeting("log_p1_v1_3", "v1.3", "2026-04-15"),
                    ],
                }
            ]
        ]
    )

    out = await graph_repository.trace_upstream(
        kind="api", start_id="API-03", project="p1"
    )

    assert out.not_found is False
    assert out.target is not None
    assert out.target.kind == "api"
    assert out.target.id == "API-03"
    assert out.target.label == "POST /tickets/{id}/refund"

    assert len(out.stories) == 1 and out.stories[0].label == "환불 신청"
    assert len(out.epics) == 1
    assert len(out.problems) == 1 and out.problems[0].label == "결제 취소 후 환불 지연"
    assert len(out.resolutions) == 1
    assert len(out.prd_documents) == 1
    assert len(out.cps_documents) == 1, "중복 CPS dedup 안 됨"
    assert len(out.meetings) == 1
    assert out.meetings[0].label == "v1.3 (2026-04-15)"

    # parameter binding 검증
    assert fake.calls[0]["params"] == {"start_id": "API-03", "project": "p1"}


async def test_trace_from_api_not_found(fake_run):
    """neo4j 가 빈 결과 → not_found=True."""
    from app.service import graph_repository

    fake_run([[]])
    out = await graph_repository.trace_upstream(
        kind="api", start_id="API-99", project="p1"
    )
    assert out.not_found is True
    assert out.target is None
    assert out.stories == []


async def test_trace_target_label_unmappable_returns_not_found(fake_run):
    """row 는 있지만 target 라벨 매핑 안 되면 not_found 같음."""
    from app.service import graph_repository

    # 알 수 없는 라벨의 target — _to_artifact_ref 가 None 반환
    fake_run(
        [
            [
                {
                    "target": _FakeNode(["UnknownLabel"], {"id": "x"}),
                    "stories": [],
                    "epics": [],
                    "problems": [],
                    "resolutions": [],
                    "prds": [],
                    "cps_raw": [],
                    "logs_raw": [],
                }
            ]
        ]
    )
    out = await graph_repository.trace_upstream(
        kind="api", start_id="x", project="p1"
    )
    assert out.not_found is True
    assert out.target is None


# ===== trace_upstream — 다른 kind =====


async def test_trace_from_story(fake_run):
    from app.service import graph_repository

    fake_run(
        [
            [
                {
                    "target": story("story_01_1", "로그인"),
                    "stories": [],  # FROM_STORY 의 cypher RETURN 은 stories=[]
                    "epics": [epic("epic_01", "인증")],
                    "problems": [],
                    "resolutions": [],
                    "prds": [prd("doc_prd_p1_v1_1")],
                    "cps_raw": [cps("doc_cps_p1_v1_1")],
                    "logs_raw": [meeting("log_p1_v1_1")],
                }
            ]
        ]
    )
    out = await graph_repository.trace_upstream(
        kind="story", start_id="story_01_1", project="p1"
    )
    assert out.target.kind == "story"
    assert out.target.label == "로그인"
    assert len(out.epics) == 1
    assert out.stories == []


async def test_trace_from_epic(fake_run):
    from app.service import graph_repository

    fake_run(
        [
            [
                {
                    "target": epic("epic_01", "결제"),
                    "stories": [],
                    "epics": [],
                    "problems": [problem("prb_03")],
                    "resolutions": [resolution("res_03")],
                    "prds": [prd("doc_prd_p1_v1_1")],
                    "cps_raw": [cps("doc_cps_p1_v1_1")],
                    "logs_raw": [meeting("log_p1_v1_1")],
                }
            ]
        ]
    )
    out = await graph_repository.trace_upstream(
        kind="epic", start_id="epic_01", project="p1"
    )
    assert out.target.kind == "epic"


async def test_trace_from_problem(fake_run):
    from app.service import graph_repository

    fake_run(
        [
            [
                {
                    "target": problem("prb_01", "주문 누락"),
                    "stories": [],
                    "epics": [],
                    "problems": [],
                    "resolutions": [resolution("res_01")],
                    "prds": [],
                    "cps_raw": [cps("doc_cps_p1_v1_1")],
                    "logs_raw": [meeting("log_p1_v1_1")],
                }
            ]
        ]
    )
    out = await graph_repository.trace_upstream(
        kind="problem", start_id="prb_01", project="p1"
    )
    assert out.target.kind == "problem"
    assert len(out.cps_documents) == 1
    assert len(out.meetings) == 1


async def test_trace_from_resolution(fake_run):
    from app.service import graph_repository

    fake_run(
        [
            [
                {
                    "target": resolution("res_01", "환불 자동화"),
                    "stories": [],
                    "epics": [],
                    "problems": [problem("prb_01")],
                    "resolutions": [],
                    "prds": [],
                    "cps_raw": [cps("doc_cps_p1_v1_1")],
                    "logs_raw": [meeting("log_p1_v1_1")],
                }
            ]
        ]
    )
    out = await graph_repository.trace_upstream(
        kind="resolution", start_id="res_01", project="p1"
    )
    assert out.target.kind == "resolution"
    assert len(out.problems) == 1


# ===== input validation =====


async def test_trace_invalid_kind_raises():
    from app.service import graph_repository

    with pytest.raises(ValueError, match="지원하지 않는"):
        await graph_repository.trace_upstream(
            kind="aggregate", start_id="AGG-01", project="p1"
        )


async def test_trace_empty_start_id_raises():
    from app.service import graph_repository

    with pytest.raises(ValueError, match="start_id"):
        await graph_repository.trace_upstream(kind="api", start_id="", project="p1")


async def test_trace_empty_project_raises():
    from app.service import graph_repository

    with pytest.raises(ValueError, match="project"):
        await graph_repository.trace_upstream(kind="api", start_id="API-01", project="")


# ===== dedup 검증 =====


async def test_cps_raw_dedup_by_id(fake_run):
    """cps_raw 가 [cps_p, cps_d] concat 이라 같은 id 중복 가능 — 응답에서 dedup."""
    from app.service import graph_repository

    fake_run(
        [
            [
                {
                    "target": api("API-01"),
                    "stories": [story("s1")],
                    "epics": [epic("e1")],
                    "problems": [problem("p1")],
                    "resolutions": [],
                    "prds": [prd("d1")],
                    "cps_raw": [
                        cps("doc_cps_v1_1"),
                        cps("doc_cps_v1_1"),  # via PRD.BASED_ON — 같은 노드
                        cps("doc_cps_v1_2"),  # via Problem.EXTRACTED_FROM
                    ],
                    "logs_raw": [
                        meeting("log_v1_1"),
                        meeting("log_v1_1"),  # 중복
                    ],
                }
            ]
        ]
    )
    out = await graph_repository.trace_upstream(
        kind="api", start_id="API-01", project="p1"
    )
    cps_ids = sorted(c.id for c in out.cps_documents)
    assert cps_ids == ["doc_cps_v1_1", "doc_cps_v1_2"], cps_ids
    assert len(out.meetings) == 1


# ===== 미지원 라벨 silently drop =====


async def test_unknown_label_silently_dropped(fake_run):
    """collect 안에 들어온 미지원 라벨 노드는 응답에서 빠짐."""
    from app.service import graph_repository

    fake_run(
        [
            [
                {
                    "target": api("API-01"),
                    "stories": [
                        story("s1"),
                        _FakeNode(["UnknownLabel"], {"id": "x1"}),  # drop
                    ],
                    "epics": [],
                    "problems": [],
                    "resolutions": [],
                    "prds": [],
                    "cps_raw": [],
                    "logs_raw": [],
                }
            ]
        ]
    )
    out = await graph_repository.trace_upstream(
        kind="api", start_id="API-01", project="p1"
    )
    assert len(out.stories) == 1
    assert out.stories[0].id == "s1"


# ===== Cypher 보안 검증 =====


async def test_cypher_uses_param_binding_only(fake_run):
    """LLM/사용자 입력이 cypher 문자열에 인터폴되지 않고 항상 $param 으로만 들어감."""
    from app.service import graph_repository

    fake = fake_run([[]])
    await graph_repository.trace_upstream(
        kind="api",
        start_id="API-01'; DROP DATABASE;//",  # 인젝션 시도
        project="p1",
    )
    # cypher 본문은 변하지 않고, 위험한 값은 params 에만 들어가야 함
    call = fake.calls[0]
    assert "DROP DATABASE" not in call["cypher"]
    assert call["params"]["start_id"] == "API-01'; DROP DATABASE;//"
