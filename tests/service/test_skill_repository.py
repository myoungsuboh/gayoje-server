"""
skill_repository 단위 테스트 — Skill CRUD 5개 엔드포인트 동작 검증.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import skill_repository as skills
from app.service.skill_repository import SkillFull, SkillInput, SkillOut, SkillSummary


pytestmark = pytest.mark.asyncio


class _Fake:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(
        self,
        cypher: str,
        params: Optional[Dict[str, Any]] = None,
        database: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self.calls.append(
            {"cypher": cypher, "params": params or {}, "database": database}
        )
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(
        responses: Optional[List[List[Dict[str, Any]]]] = None,
    ) -> _Fake:
        fake = _Fake(responses=responses)
        monkeypatch.setattr(
            "app.service.skill_repository.neo4j_client.run_cypher", fake
        )
        return fake

    return _setup


# ─── create_skills ──────────────────────────────────────────────


async def test_create_skills_bulk_upsert_with_param_binding(fake_run):
    fake = fake_run([[{"ids": ["SKL-01", "SKL-02"]}]])
    out = await skills.create_skills(
        "harness",
        [
            SkillInput(
                id="SKL-01",
                name="Java Naming",
                priority="High",
                tags=["Spring Boot"],
                instructions=["use camelCase"],
            ),
            SkillInput(
                id="SKL-02",
                name="Vue SFC",
                priority="Medium",
                tags=["Vue.js"],
            ),
        ],
    )
    assert out == {"ids": ["SKL-01", "SKL-02"]}

    # Cypher / 파라미터 검증
    call = fake.calls[0]
    assert "UNWIND $skills AS sData" in call["cypher"]
    assert "MERGE (s:Skill {id: sData.id, project: $project})" in call["cypher"]
    # injection 안전: project 이름이 Cypher 본문에 보간되지 않음
    assert "harness" not in call["cypher"]
    assert call["params"]["project"] == "harness"
    assert len(call["params"]["skills"]) == 2
    assert call["params"]["skills"][0]["id"] == "SKL-01"
    assert call["params"]["skills"][0]["tags"] == ["Spring Boot"]


async def test_create_skills_empty_returns_empty_without_call(fake_run):
    fake = fake_run([])
    out = await skills.create_skills("harness", [])
    assert out == {"ids": []}
    # 빈 입력이면 Cypher 호출도 안 해야 함
    assert fake.calls == []


# ─── get_skill ──────────────────────────────────────────────────


async def test_get_skill_returns_skill_out_with_services(fake_run):
    fake = fake_run(
        [
            [
                {
                    "id": "SKL-01",
                    "name": "Java Naming",
                    "scope": "naming",
                    "priority": "High",
                    "trigger": "PR commit",
                    "instructions": ["use camelCase", "PascalCase for class"],
                    "tags": ["Spring Boot"],
                    "applied_services": ["Backend API"],
                }
            ]
        ]
    )
    out = await skills.get_skill("harness", "SKL-01")
    assert isinstance(out, SkillOut)
    assert out.id == "SKL-01"
    assert out.priority == "High"
    assert out.trigger == "PR commit"
    assert out.applied_services == ["Backend API"]
    # 파라미터 바인딩
    assert fake.calls[0]["params"] == {"project": "harness", "id": "SKL-01"}


async def test_get_skill_returns_none_when_not_found(fake_run):
    fake_run([[]])
    out = await skills.get_skill("harness", "missing")
    assert out is None


# ─── get_all_skills ─────────────────────────────────────────────


async def test_get_all_skills_returns_summary_list(fake_run):
    fake_run(
        [
            [
                {
                    "id": "SKL-01",
                    "name": "Java Naming",
                    "scope": "naming",
                    "priority": "High",
                    "tags": ["Spring Boot"],
                    "rule_count": 3,
                    "applied_services": ["Backend API"],
                },
                {
                    "id": "SKL-02",
                    "name": "Vue SFC",
                    "scope": "component",
                    "priority": "Medium",
                    "tags": ["Vue.js"],
                    "rule_count": 2,
                    "applied_services": [],
                },
            ]
        ]
    )
    out = await skills.get_all_skills("harness")
    assert len(out) == 2
    assert isinstance(out[0], SkillSummary)
    assert out[0].rule_count == 3
    assert out[1].applied_services == []


async def test_get_all_skills_empty_project(fake_run):
    fake_run([[]])
    out = await skills.get_all_skills("empty_project")
    assert out == []


# ─── get_all_skills_full (getAllSkillDetail) ────────────────────


async def test_get_all_skills_full_includes_instructions_and_trigger(fake_run):
    # 다운로드용 — 규칙 본문(instructions)과 trigger 가 반드시 실려야 한다.
    fake_run(
        [
            [
                {
                    "id": "SKL-01",
                    "name": "Java Naming",
                    "scope": "naming",
                    "priority": "High",
                    "trigger_condition": "백엔드 클래스 작성 시",
                    "instructions": ["PascalCase 사용", "약어 금지"],
                    "tags": ["backEnd", "Spring Boot"],
                },
            ]
        ]
    )
    out = await skills.get_all_skills_full("harness")
    assert len(out) == 1
    assert isinstance(out[0], SkillFull)
    assert out[0].instructions == ["PascalCase 사용", "약어 금지"]   # 규칙 본문 보존
    assert out[0].trigger_condition == "백엔드 클래스 작성 시"


async def test_get_all_skills_full_handles_missing_fields(fake_run):
    fake_run([[{"id": "SKL-X", "name": "n"}]])
    out = await skills.get_all_skills_full("p")
    assert out[0].instructions == []
    assert out[0].trigger_condition == ""


# ─── delete_skill ───────────────────────────────────────────────


async def test_delete_skill_returns_true_on_deleted(fake_run):
    fake = fake_run([[{"deleted_id": "SKL-01"}]])
    ok = await skills.delete_skill("harness", "SKL-01")
    assert ok is True
    assert "DETACH DELETE s" in fake.calls[0]["cypher"]
    assert fake.calls[0]["params"] == {"project": "harness", "id": "SKL-01"}


async def test_delete_skill_returns_false_when_not_found(fake_run):
    fake_run([[]])
    ok = await skills.delete_skill("harness", "missing")
    assert ok is False


# ─── find_duplicate_skill ───────────────────────────────────────


async def test_find_duplicate_returns_true_with_existing_ids(fake_run):
    fake_run(
        [
            [
                {
                    "is_duplicate": True,
                    "existing_ids": ["SKL-old-1", "SKL-old-2"],
                }
            ]
        ]
    )
    out = await skills.find_duplicate_skill("harness", "Java Naming")
    assert out == {
        "is_duplicate": True,
        "existing_ids": ["SKL-old-1", "SKL-old-2"],
    }


async def test_find_duplicate_returns_false(fake_run):
    fake_run([[{"is_duplicate": False, "existing_ids": []}]])
    out = await skills.find_duplicate_skill("harness", "new name")
    assert out == {"is_duplicate": False, "existing_ids": []}


# ─── Cypher injection safety ────────────────────────────────────


async def test_skill_id_with_special_chars_is_parameterized(fake_run):
    fake = fake_run([[]])
    dangerous = "x') DETACH DELETE s //"
    await skills.get_skill("harness", dangerous)
    # 입력값이 Cypher 본문에 보간되면 안 됨
    assert dangerous not in fake.calls[0]["cypher"]
    assert fake.calls[0]["params"]["id"] == dangerous
