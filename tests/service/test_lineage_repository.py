"""lineage_repository 단위 테스트 — base64 인코딩/디코딩 + Cypher 호환성."""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

import pytest

from app.service import lineage_repository
from app.service.lineage_repository import (
    LineageArtifact,
    LineageImpl,
    LineageResult,
    LineageResultData,
    LineageStats,
)


pytestmark = pytest.mark.asyncio


class _Fake:
    def __init__(self, responses=None):
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
            "app.service.lineage_repository.neo4j_client.run_cypher", fake
        )
        return fake
    return _setup


async def test_save_lineage_encodes_data_b64(fake_run):
    fake = fake_run([[{"saved_id": "lineage-x-1"}]])
    data = LineageResultData(
        summary="1개 매칭",
        aggregates=[
            LineageArtifact(
                id="A1",
                name="Ticket",
                implementations=[
                    LineageImpl(
                        repoUrl="https://github.com/o/r",
                        filePath="Ticket.java",
                        confidence="high",
                        reason="exact",
                    )
                ],
            )
        ],
        stats=LineageStats(aggregatesCount=1, totalImpls=1, verifiedImpls=1),
    )
    lid = await lineage_repository.save_lineage_result("x", data)
    assert lid.startswith("lineage-x-")
    call = fake.calls[0]
    # base64 인코딩 확인
    decoded = json.loads(base64.b64decode(call["params"]["data_b64"]).decode())
    assert decoded["summary"] == "1개 매칭"
    assert decoded["aggregates"][0]["implementations"][0]["filePath"] == "Ticket.java"
    # 메타 필드 확인
    assert call["params"]["aggregates_count"] == 1
    assert call["params"]["total_impls"] == 1


async def test_get_last_lineage_decodes_data(fake_run):
    payload = LineageResultData(summary="X", stats=LineageStats(totalImpls=3))
    b64 = base64.b64encode(
        json.dumps(payload.model_dump(), ensure_ascii=False).encode()
    ).decode()
    fake_run(
        [
            [
                {
                    "lineage": {
                        "id": "lineage-x-1",
                        "summary": "X",
                        "storiesCount": 0,
                        "aggregatesCount": 0,
                        "apisCount": 0,
                        "servicesCount": 0,
                        "totalImpls": 3,
                        "missingCount": 0,
                        "dataB64": b64,
                        "savedAt": 1700000000000,
                    }
                }
            ]
        ]
    )
    result = await lineage_repository.get_last_lineage("x")
    assert isinstance(result, LineageResult)
    assert result.totalImpls == 3
    assert result.data is not None
    assert result.data.stats.totalImpls == 3


async def test_get_last_lineage_returns_none_when_not_found(fake_run):
    fake_run([[]])
    assert await lineage_repository.get_last_lineage("missing") is None


async def test_get_last_lineage_handles_corrupt_b64(fake_run):
    fake_run(
        [
            [
                {
                    "lineage": {
                        "id": "lineage-x-1",
                        "summary": "X",
                        "storiesCount": 0,
                        "aggregatesCount": 0,
                        "apisCount": 0,
                        "servicesCount": 0,
                        "totalImpls": 0,
                        "missingCount": 0,
                        "dataB64": "###broken###",
                        "savedAt": 1700000000000,
                    }
                }
            ]
        ]
    )
    result = await lineage_repository.get_last_lineage("x")
    assert result is not None
    # data 디코딩 실패 → None 으로 graceful
    assert result.data is None


# ─── Lineage History ────────────────────────────────────────────


async def test_get_lineage_history_returns_latest_first(fake_run):
    fake_run(
        [
            [
                {
                    "lineage": {
                        "id": "lineage-x-3",
                        "summary": "third",
                        "storiesCount": 3,
                        "aggregatesCount": 0,
                        "apisCount": 0,
                        "servicesCount": 0,
                        "totalImpls": 5,
                        "missingCount": 0,
                        "driftCount": 0,
                        "savedAt": 1700000000300,
                    }
                },
                {
                    "lineage": {
                        "id": "lineage-x-2",
                        "summary": "second",
                        "storiesCount": 2,
                        "aggregatesCount": 0,
                        "apisCount": 0,
                        "servicesCount": 0,
                        "totalImpls": 4,
                        "missingCount": 0,
                        "driftCount": 0,
                        "savedAt": 1700000000200,
                    }
                },
            ]
        ]
    )
    out = await lineage_repository.get_lineage_history("x", limit=5)
    assert len(out) == 2
    assert out[0].id == "lineage-x-3"
    assert out[0].saved_at == 1700000000300
    assert out[1].id == "lineage-x-2"


async def test_get_lineage_history_caps_limit(fake_run):
    """limit 가 50 초과면 50 으로 clamp."""
    fake = fake_run([[]])
    await lineage_repository.get_lineage_history("x", limit=999)
    assert fake.calls[0]["params"]["limit"] == 50


async def test_get_lineage_history_skips_rows_missing_id(fake_run):
    fake_run([[{"lineage": {"id": None, "summary": "broken"}}]])
    out = await lineage_repository.get_lineage_history("x")
    assert out == []


async def test_get_lineage_by_id_returns_with_data_decoded(fake_run):
    data = LineageResultData(summary="x", stats=LineageStats(totalImpls=1))
    encoded = base64.b64encode(
        json.dumps(data.model_dump(), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    fake_run(
        [
            [
                {
                    "lineage": {
                        "id": "lineage-x-1",
                        "summary": "x",
                        "storiesCount": 0,
                        "aggregatesCount": 0,
                        "apisCount": 0,
                        "servicesCount": 0,
                        "totalImpls": 1,
                        "missingCount": 0,
                        "driftCount": 0,
                        "dataB64": encoded,
                        "savedAt": 1700000000100,
                    }
                }
            ]
        ]
    )
    out = await lineage_repository.get_lineage_by_id("x", "lineage-x-1")
    assert out is not None
    assert out.id == "lineage-x-1"
    assert out.data is not None
    assert out.data.stats.totalImpls == 1


async def test_get_lineage_by_id_filters_by_project(fake_run):
    """다른 project 의 id 로 우회 조회 불가 — Cypher 가 project 필터."""
    fake = fake_run([[]])
    out = await lineage_repository.get_lineage_by_id("alice-proj", "lineage-x-1")
    assert out is None
    assert fake.calls[0]["params"] == {
        "project": "alice-proj",
        "id": "lineage-x-1",
    }


# ─── Lineage Truth ──────────────────────────────────────────────


async def test_save_lineage_truth_upserts_and_returns_record(fake_run):
    fake = fake_run(
        [
            [
                {
                    "project": "x",
                    "itemType": "aggregate",
                    "itemId": "agg_order",
                    "expectedFiles": ["src/Order.ts", "src/Order.test.ts"],
                    "updatedAt": 1700000000000,
                }
            ]
        ]
    )
    out = await lineage_repository.save_lineage_truth(
        "x", "aggregate", "agg_order", ["src/Order.ts", "src/Order.test.ts"]
    )
    assert out.itemType == "aggregate"
    assert out.itemId == "agg_order"
    assert out.expectedFiles == ["src/Order.ts", "src/Order.test.ts"]
    # MERGE 패턴 + 4 param
    params = fake.calls[0]["params"]
    assert params["project"] == "x"
    assert params["item_type"] == "aggregate"
    assert params["item_id"] == "agg_order"
    assert params["expected_files"] == ["src/Order.ts", "src/Order.test.ts"]


async def test_save_lineage_truth_rejects_missing_required(fake_run):
    fake_run([[]])
    with pytest.raises(ValueError):
        await lineage_repository.save_lineage_truth("", "type", "id", [])
    with pytest.raises(ValueError):
        await lineage_repository.save_lineage_truth("x", "", "id", [])
    with pytest.raises(ValueError):
        await lineage_repository.save_lineage_truth("x", "type", "", [])


async def test_save_lineage_truth_normalizes_files_to_strings(fake_run):
    """expected_files 안의 non-string 도 str() 처리."""
    fake = fake_run(
        [
            [
                {
                    "project": "x",
                    "itemType": "api",
                    "itemId": "api_1",
                    "expectedFiles": ["a.py", "123"],
                    "updatedAt": 1700000000000,
                }
            ]
        ]
    )
    await lineage_repository.save_lineage_truth("x", "api", "api_1", ["a.py", 123])
    assert fake.calls[0]["params"]["expected_files"] == ["a.py", "123"]


async def test_list_lineage_truth_without_filter(fake_run):
    fake = fake_run(
        [
            [
                {"project": "x", "itemType": "api", "itemId": "a1", "expectedFiles": ["f.py"], "updatedAt": 1},
                {"project": "x", "itemType": "aggregate", "itemId": "agg_1", "expectedFiles": [], "updatedAt": 2},
            ]
        ]
    )
    out = await lineage_repository.list_lineage_truth("x")
    assert len(out) == 2
    assert "item_type" not in fake.calls[0]["params"]


async def test_list_lineage_truth_with_type_filter(fake_run):
    fake = fake_run(
        [
            [
                {"project": "x", "itemType": "api", "itemId": "a1", "expectedFiles": ["f.py"], "updatedAt": 1},
            ]
        ]
    )
    out = await lineage_repository.list_lineage_truth("x", item_type="api")
    assert len(out) == 1
    assert out[0].itemType == "api"
    assert fake.calls[0]["params"]["item_type"] == "api"


async def test_list_lineage_truth_skips_invalid_rows(fake_run):
    """itemType / itemId 누락 row 는 응답에서 drop."""
    fake_run(
        [
            [
                {"project": "x", "itemType": None, "itemId": "a", "expectedFiles": [], "updatedAt": 1},
                {"project": "x", "itemType": "api", "itemId": None, "expectedFiles": [], "updatedAt": 2},
                {"project": "x", "itemType": "api", "itemId": "a", "expectedFiles": [], "updatedAt": 3},
            ]
        ]
    )
    out = await lineage_repository.list_lineage_truth("x")
    assert len(out) == 1
    assert out[0].itemId == "a"


async def test_delete_lineage_truth_returns_true_when_deleted(fake_run):
    fake_run([[{"deleted": 1}]])
    assert await lineage_repository.delete_lineage_truth("x", "api", "a1") is True


async def test_delete_lineage_truth_returns_false_when_missing(fake_run):
    fake_run([[{"deleted": 0}]])
    assert await lineage_repository.delete_lineage_truth("x", "api", "missing") is False


async def test_import_lineage_truth_skip_existing_without_override(fake_run):
    """override=False — 이미 존재하는 (itemType, itemId) skip."""
    # 1차 list (existing 조회) → [{type=api, id=a1}], 2차 = upsert (api/a2 1개)
    existing = [
        {"project": "x", "itemType": "api", "itemId": "a1", "expectedFiles": [], "updatedAt": 1},
    ]
    fake = fake_run(
        [
            existing,
            [{"project": "x", "itemType": "api", "itemId": "a2", "expectedFiles": ["b.py"], "updatedAt": 2}],
        ]
    )
    result = await lineage_repository.import_lineage_truth(
        "x",
        [
            {"itemType": "api", "itemId": "a1", "expectedFiles": ["other.py"]},   # 이미 존재 → skip
            {"itemType": "api", "itemId": "a2", "expectedFiles": ["b.py"]},        # 신규
        ],
        override=False,
    )
    assert result == {"written": 1, "skipped": 1}


async def test_import_lineage_truth_override_replaces(fake_run):
    """override=True — 존재 여부 무시하고 전체 upsert."""
    # 1차 = upsert, 2차 = upsert
    fake = fake_run(
        [
            [{"project": "x", "itemType": "api", "itemId": "a1", "expectedFiles": ["new.py"], "updatedAt": 1}],
            [{"project": "x", "itemType": "api", "itemId": "a2", "expectedFiles": ["b.py"], "updatedAt": 2}],
        ]
    )
    result = await lineage_repository.import_lineage_truth(
        "x",
        [
            {"itemType": "api", "itemId": "a1", "expectedFiles": ["new.py"]},
            {"itemType": "api", "itemId": "a2", "expectedFiles": ["b.py"]},
        ],
        override=True,
    )
    assert result == {"written": 2, "skipped": 0}
    # override=True 면 list 조회 안 함 — 호출 2건(upsert 만)
    assert len(fake.calls) == 2


async def test_import_lineage_truth_skips_invalid_items(fake_run):
    """itemType / itemId 누락 / non-dict — skipped."""
    fake_run([[]])
    result = await lineage_repository.import_lineage_truth(
        "x",
        [
            None,
            "not-a-dict",
            {},
            {"itemType": "api"},                         # itemId 누락
            {"itemId": "a1"},                            # itemType 누락
        ],
        override=True,
    )
    assert result == {"written": 0, "skipped": 5}


async def test_import_lineage_truth_empty_items(fake_run):
    fake_run([[]])
    result = await lineage_repository.import_lineage_truth("x", [], override=True)
    assert result == {"written": 0, "skipped": 0}


async def test_import_lineage_truth_rejects_empty_project(fake_run):
    fake_run([[]])
    with pytest.raises(ValueError):
        await lineage_repository.import_lineage_truth(
            "", [{"itemType": "api", "itemId": "a"}], override=True
        )
