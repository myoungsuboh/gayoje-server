"""
skill_library_repository 단위 테스트 — Neo4j Cypher 모킹.

[검증 범위]
- 폴더 CRUD: create / update / delete (cascade + move_to_unfiled)
- 스킬 CRUD: create / update (메타 수정 + 폴더 이동) / delete
- 라이브러리 조회 (list_library)
- 스킬 개수 (count_skills) — quota 검증용
- ensure_constraints 안전성
- Cypher injection 안전 — email / folder_id / skill_id 가 모두 param 바인딩
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import skill_library_repository as repo
from app.service.skill_library_repository import (
    LibraryEntry,
    LibrarySkillRow,
    SkillFolderRow,
)

pytestmark = pytest.mark.asyncio


# ─── Fake run_cypher ─────────────────────────────────────────


class _FakeRunCypher:
    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(
        self,
        cypher: str,
        params: Optional[Dict[str, Any]] = None,
        database: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self.calls.append({"cypher": cypher, "params": params or {}})
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    def _setup(responses: Optional[List[List[Dict[str, Any]]]] = None) -> _FakeRunCypher:
        fake = _FakeRunCypher(responses=responses)
        monkeypatch.setattr(
            "app.service.skill_library_repository.neo4j_client.run_cypher", fake
        )
        return fake

    return _setup


# ─── ensure_constraints ──────────────────────────────────────


async def test_ensure_constraints_runs_all_statements(fake_run):
    fake = fake_run([[]] * 10)
    await repo.ensure_constraints()
    # 4개 statement: folder UNIQUE / skill UNIQUE / folder index / skill index
    assert len(fake.calls) == 4
    all_cypher = "\n".join(c["cypher"] for c in fake.calls)
    assert "skill_folder_id_unique" in all_cypher
    assert "library_skill_id_unique" in all_cypher
    assert "skill_folder_owner_email" in all_cypher
    assert "library_skill_owner_email" in all_cypher


async def test_ensure_constraints_swallows_errors(monkeypatch):
    """Neo4j 미연결이어도 부팅 막지 않음."""
    async def _raise(*a, **kw):
        raise RuntimeError("NEO4J_URI not set")
    monkeypatch.setattr(
        "app.service.skill_library_repository.neo4j_client.run_cypher", _raise
    )
    # 예외 없이 종료
    await repo.ensure_constraints()


# ─── 폴더 CRUD ───────────────────────────────────────────────


async def test_create_folder_returns_folder_row(fake_run):
    fake = fake_run(
        [
            [
                {
                    "folder": {
                        "id": "f-1",
                        "name": "Frontend Standard",
                        "description": "",
                        "color": "#3b82f6",
                        "category": "frontEnd",
                        "owner_email": "a@b.com",
                        "created_at": "2026-05-16T00:00:00Z",
                        "updated_at": "2026-05-16T00:00:00Z",
                    }
                }
            ]
        ]
    )
    out = await repo.create_folder(
        owner_email="a@b.com",
        name="Frontend Standard",
        color="#3b82f6",
        category="frontEnd",
    )
    assert isinstance(out, SkillFolderRow)
    assert out.id == "f-1"
    assert out.name == "Frontend Standard"
    assert out.color == "#3b82f6"
    assert out.category == "frontEnd"
    assert out.owner_email == "a@b.com"
    # 파라미터 바인딩 검증
    params = fake.calls[0]["params"]
    assert params["owner_email"] == "a@b.com"
    assert params["name"] == "Frontend Standard"


async def test_create_folder_returns_none_when_user_missing(fake_run):
    """User 노드 없으면 MATCH 가 0건 → 빈 결과 → None."""
    fake_run([[]])
    out = await repo.create_folder(owner_email="ghost@x.com", name="X")
    assert out is None


async def test_update_folder_partial_fields(fake_run):
    """name / description / color / category 각각 None 이면 기존값 유지."""
    fake = fake_run(
        [
            [
                {
                    "folder": {
                        "id": "f-1",
                        "name": "Renamed",
                        "description": "desc",
                        "color": "#3b82f6",
                        "category": "frontEnd",
                        "owner_email": "a@b.com",
                    }
                }
            ]
        ]
    )
    out = await repo.update_folder(
        owner_email="a@b.com",
        folder_id="f-1",
        name="Renamed",
        description=None,  # 기존 유지
        color=None,
        category=None,
    )
    assert out.name == "Renamed"
    # cypher 가 owner_email + id 양쪽 매칭 (다른 사용자 폴더 수정 차단)
    cypher = fake.calls[0]["cypher"]
    assert "owner_email: $owner_email" in cypher
    assert "id: $id" in cypher


async def test_update_folder_returns_none_when_not_owned(fake_run):
    """다른 사용자 폴더 update 시도 → cypher MATCH 매칭 안 됨 → 빈 결과."""
    fake_run([[]])
    out = await repo.update_folder(
        owner_email="a@b.com", folder_id="f-other", name="hack"
    )
    assert out is None


async def test_delete_folder_cascade_returns_deleted_count(fake_run):
    fake = fake_run([[{"deleted_skill_count": 3}]])
    out = await repo.delete_folder(
        owner_email="a@b.com", folder_id="f-1", cascade=True
    )
    assert out["mode"] == "cascade"
    assert out["deleted_skill_count"] == 3
    # cascade cypher 사용
    cypher = fake.calls[0]["cypher"]
    assert "FOREACH (sk IN skills | DETACH DELETE sk)" in cypher


async def test_delete_folder_move_to_unfiled(fake_run):
    """cascade=False → 안 스킬을 미분류 폴더로 이동 + 폴더 삭제."""
    fake = fake_run(
        [[{
            "moved_skill_count": 5,
            "unfiled_folder_id": "f-unfiled",
            "is_self_target": False,
        }]]
    )
    out = await repo.delete_folder(
        owner_email="a@b.com", folder_id="f-1", cascade=False
    )
    assert out["mode"] == "moved"
    assert out["moved_skill_count"] == 5
    assert out["unfiled_folder_id"] == "f-unfiled"
    # 미분류 폴더 ensure cypher 사용 + is_system 플래그 (사용자 동명 폴더와 분리)
    cypher = fake.calls[0]["cypher"]
    assert "MERGE (unfiled:SkillFolder" in cypher
    assert "is_system: true" in cypher


async def test_delete_unfiled_folder_self_falls_back_to_cascade(fake_run):
    """[BUG FIX 회귀 가드] 시스템 미분류 폴더 자체를 cascade=False 로 삭제 시도하면
    cypher 가 자동으로 cascade 전환 (안 스킬도 삭제). 응답 mode='cascade'.
    이전 버그: 자기-자신 매칭으로 스킬들이 함께 사라짐 + 응답은 'moved' 라 거짓."""
    fake_run(
        [[{
            "moved_skill_count": 3,
            "unfiled_folder_id": "f-unfiled",
            "is_self_target": True,
        }]]
    )
    out = await repo.delete_folder(
        owner_email="a@b.com", folder_id="f-unfiled", cascade=False
    )
    assert out["mode"] == "cascade"
    assert out["deleted_skill_count"] == 3


async def test_delete_folder_not_found(fake_run):
    """폴더 없거나 다른 사용자 폴더 → mode='not_found'."""
    fake_run([[]])
    out = await repo.delete_folder(
        owner_email="a@b.com", folder_id="f-ghost", cascade=True
    )
    assert out["mode"] == "not_found"


# ─── 스킬 CRUD ───────────────────────────────────────────────


async def test_create_skill_returns_skill_row(fake_run):
    fake = fake_run(
        [
            [
                {
                    "skill": {
                        "id": "s-1",
                        "name": "ESLint React",
                        "scope": "frontend",
                        "priority": "High",
                        "trigger_condition": "PR open",
                        "instructions": ["use hooks", "no class"],
                        "tags": ["react", "eslint"],
                        "folder_id": "f-1",
                        "owner_email": "a@b.com",
                    }
                }
            ]
        ]
    )
    out = await repo.create_skill(
        owner_email="a@b.com",
        folder_id="f-1",
        name="ESLint React",
        scope="frontend",
        priority="High",
        trigger_condition="PR open",
        instructions=["use hooks", "no class"],
        tags=["react", "eslint"],
    )
    assert isinstance(out, LibrarySkillRow)
    assert out.id == "s-1"
    assert out.folder_id == "f-1"
    assert out.priority == "High"
    assert out.instructions == ["use hooks", "no class"]


async def test_create_skill_returns_none_when_folder_missing(fake_run):
    """폴더 없거나 다른 사용자 폴더 → 빈 결과."""
    fake_run([[]])
    out = await repo.create_skill(
        owner_email="a@b.com", folder_id="f-other", name="X"
    )
    assert out is None


async def test_update_skill_meta_only(fake_run):
    """folder_id None → 폴더 이동 없이 메타만 수정.

    [BUG FIX 회귀 가드 - Critical 2026-05]
    이전 cypher 는 CALL { ... WHERE new_folder_id IS NOT NULL } 패턴이라
    folder_id=None 시 CALL 안 row 0 → outer query 도 0 row → 함수 None 반환.
    수정: OPTIONAL MATCH + FOREACH 패턴으로 outer row 보존.
    """
    fake = fake_run(
        [
            [
                {
                    "skill": {
                        "id": "s-1",
                        "name": "Renamed",
                        "scope": "frontend",
                        "priority": "Medium",
                        "trigger_condition": "",
                        "instructions": [],
                        "tags": [],
                        "folder_id": "f-1",  # 폴더 그대로 유지
                        "owner_email": "a@b.com",
                    }
                }
            ]
        ]
    )
    out = await repo.update_skill(
        owner_email="a@b.com",
        skill_id="s-1",
        name="Renamed",
        folder_id=None,
    )
    # 핵심: None 아닌 정상 row 반환 (이전 버그는 None)
    assert out is not None
    assert out.name == "Renamed"
    # folder_id None 이면 폴더 이동 cypher 분기 안 탐
    params = fake.calls[0]["params"]
    assert params["folder_id"] is None
    # 새 cypher 는 OPTIONAL MATCH + FOREACH 패턴 (CALL subquery 안 씀)
    cypher = fake.calls[0]["cypher"]
    assert "OPTIONAL MATCH (newF:SkillFolder" in cypher
    assert "FOREACH (_ IN CASE WHEN newF IS NOT NULL" in cypher


async def test_update_skill_with_folder_move(fake_run):
    """folder_id 가 주어지면 폴더 이동 cypher 분기 실행."""
    fake = fake_run(
        [
            [
                {
                    "skill": {
                        "id": "s-1",
                        "name": "S",
                        "scope": "",
                        "priority": "Medium",
                        "trigger_condition": "",
                        "instructions": [],
                        "tags": [],
                        "folder_id": "f-2",  # 새 폴더
                        "owner_email": "a@b.com",
                    }
                }
            ]
        ]
    )
    out = await repo.update_skill(
        owner_email="a@b.com",
        skill_id="s-1",
        folder_id="f-2",
    )
    assert out.folder_id == "f-2"
    cypher = fake.calls[0]["cypher"]
    # cypher 안에 폴더 이동 분기 있어야 함
    assert "MERGE (newF)-[:CONTAINS]->(s)" in cypher


async def test_delete_skill_returns_true(fake_run):
    fake_run([[{"deleted_id": "s-1"}]])
    ok = await repo.delete_skill(owner_email="a@b.com", skill_id="s-1")
    assert ok is True


async def test_delete_skill_returns_false_when_not_found(fake_run):
    fake_run([[]])
    ok = await repo.delete_skill(owner_email="a@b.com", skill_id="s-ghost")
    assert ok is False


# ─── 조회 ────────────────────────────────────────────────────


async def test_list_library_returns_folder_tree(fake_run):
    fake_run(
        [
            [
                {
                    "entry": {
                        "folder": {
                            "id": "f-1",
                            "name": "Frontend",
                            "description": "",
                            "color": "",
                            "category": "frontEnd",
                            "owner_email": "a@b.com",
                        },
                        "skills": [
                            {
                                "id": "s-1",
                                "name": "ESLint",
                                "scope": "",
                                "priority": "High",
                                "trigger_condition": "",
                                "instructions": [],
                                "tags": ["react"],
                                "folder_id": "f-1",
                                "owner_email": "a@b.com",
                            },
                            {
                                "id": "s-2",
                                "name": "Prettier",
                                "scope": "",
                                "priority": "Medium",
                                "trigger_condition": "",
                                "instructions": [],
                                "tags": [],
                                "folder_id": "f-1",
                                "owner_email": "a@b.com",
                            },
                        ],
                    }
                },
                {
                    "entry": {
                        "folder": {
                            "id": "f-2",
                            "name": "Empty",
                            "description": "",
                            "color": "",
                            "category": "",
                            "owner_email": "a@b.com",
                        },
                        "skills": [],  # 빈 폴더 — 옵션 B 의 장점
                    }
                },
            ]
        ]
    )
    out = await repo.list_library("a@b.com")
    assert len(out) == 2
    assert isinstance(out[0], LibraryEntry)
    assert out[0].folder.name == "Frontend"
    assert len(out[0].skills) == 2
    # 빈 폴더 정상 표시
    assert out[1].folder.name == "Empty"
    assert out[1].skills == []


async def test_list_library_returns_empty_for_no_folders(fake_run):
    fake_run([[]])
    out = await repo.list_library("a@b.com")
    assert out == []


async def test_count_skills_returns_total(fake_run):
    fake_run([[{"total": 42}]])
    n = await repo.count_skills("a@b.com")
    assert n == 42


async def test_count_skills_returns_zero_for_empty(fake_run):
    fake_run([[]])
    n = await repo.count_skills("a@b.com")
    assert n == 0


# ─── Cypher injection safety ─────────────────────────────────


async def test_email_and_ids_are_parameterized(fake_run):
    """email / folder_id / skill_id 가 cypher 본문에 보간되지 않고 $param 으로만 전달."""
    dangerous = "x@y.com'} ) DETACH DELETE u //"
    fake = fake_run([[]])
    await repo.list_library(dangerous)
    call = fake.calls[0]
    assert dangerous not in call["cypher"]
    assert call["params"]["owner_email"] == dangerous


# ─── Import (프로젝트 → 라이브러리) ─────────────────────


async def test_copy_from_project_returns_imported_list(fake_run):
    """[2단계 패턴] 1) 폴더 체크 cypher → 2) import cypher → 3) count."""
    fake_run(
        [
            # 1. _CHECK_FOLDER_OWNED_CYPHER
            [{"folder_id": "f-1"}],
            # 2. _COPY_FROM_PROJECT_CYPHER
            [
                {
                    "imported": [
                        {"source_skill_id": "eslint", "library_skill_id": "lib-1", "name": "ESLint"},
                        {"source_skill_id": "prettier", "library_skill_id": "lib-2", "name": "Prettier"},
                    ]
                }
            ],
            # 3. count_skills
            [{"total": 2}],
        ]
    )
    result = await repo.copy_skills_from_project(
        owner_email="a@b.com",
        project_name="proj",
        skill_ids=["eslint", "prettier"],
        folder_id="f-1",
    )
    assert result is not None
    assert len(result.imported) == 2
    assert result.imported[0]["source_skill_id"] == "eslint"
    assert result.imported[0]["library_skill_id"] == "lib-1"
    assert result.new_total_skill_count == 2


async def test_copy_from_project_returns_none_when_folder_missing(fake_run):
    """폴더 없으면 1단계 check 가 0 row → None (import cypher 호출 안 함)."""
    fake_run([[]])  # check 만 0 row
    result = await repo.copy_skills_from_project(
        owner_email="a@b.com",
        project_name="proj",
        skill_ids=["eslint"],
        folder_id="f-ghost",
    )
    assert result is None


async def test_copy_from_project_empty_skill_ids(fake_run):
    """skill_ids 가 빈 list 면 check 만 + import cypher 호출 안 함."""
    fake_run(
        [
            [{"folder_id": "f-1"}],  # check pass
            [{"total": 0}],          # count_skills
        ]
    )
    result = await repo.copy_skills_from_project(
        owner_email="a@b.com",
        project_name="proj",
        skill_ids=[],
        folder_id="f-1",
    )
    assert result.imported == []
    assert result.new_total_skill_count == 0


async def test_copy_from_project_zero_match_returns_empty_not_404(fake_run):
    """[BUG FIX 회귀 가드 - Critical 2026-05]
    이전 버전: skill_ids 가 project 에 0개 매칭이면 cypher 0 row → 함수 None 반환
    → 라우트가 폴더 없음(404)으로 잘못 매핑.
    수정 후: 2단계 분리 — 폴더 있으면 import 0건이라도 ImportResult(imported=[]).
    """
    fake_run(
        [
            [{"folder_id": "f-1"}],  # check pass (폴더 있음)
            [],                      # import cypher 0 row (skill_ids 모두 매칭 안 됨)
            [{"total": 0}],          # count_skills
        ]
    )
    result = await repo.copy_skills_from_project(
        owner_email="a@b.com",
        project_name="proj",
        skill_ids=["nonexistent-1", "nonexistent-2"],
        folder_id="f-1",
    )
    # 핵심: None 아님. 빈 imported 리스트 + 폴더 존재 확인.
    assert result is not None
    assert result.imported == []


# ─── Export (라이브러리 → 프로젝트) ─────────────────────


async def test_copy_to_project_overwrite_strategy(fake_run):
    """overwrite — 충돌 ID 도 MERGE 로 덮어쓰기."""
    fake_run(
        [
            # _fetch_library_skill_ids_owned
            [{"owned": [{"id": "lib-1"}, {"id": "lib-2"}]}],
            # find_conflicting_skill_ids
            [{"conflicting_ids": ["lib-1"]}],
            # _COPY_TO_PROJECT_MERGE_CYPHER
            [{"created_ids": ["lib-1", "lib-2"]}],
        ]
    )
    result = await repo.copy_skills_to_project(
        owner_email="a@b.com",
        project_name="proj",
        library_skill_ids=["lib-1", "lib-2"],
        conflict_strategy="overwrite",
    )
    assert set(result.imported_ids) == {"lib-1", "lib-2"}
    assert result.skipped_ids == []
    assert result.renamed == []


async def test_copy_to_project_skip_strategy(fake_run):
    """skip — 충돌 ID 는 제외하고 나머지만 import."""
    fake_run(
        [
            [{"owned": [{"id": "lib-1"}, {"id": "lib-2"}]}],
            [{"conflicting_ids": ["lib-1"]}],
            # COPY 는 lib-2 만
            [{"created_ids": ["lib-2"]}],
        ]
    )
    result = await repo.copy_skills_to_project(
        owner_email="a@b.com",
        project_name="proj",
        library_skill_ids=["lib-1", "lib-2"],
        conflict_strategy="skip",
    )
    assert result.imported_ids == ["lib-2"]
    assert result.skipped_ids == ["lib-1"]
    assert result.renamed == []


async def test_copy_to_project_rename_strategy(fake_run):
    """rename — 충돌 ID 에 -copy suffix 부여."""
    fake_run(
        [
            [{"owned": [{"id": "lib-1"}, {"id": "lib-2"}]}],
            [{"conflicting_ids": ["lib-1"]}],
            # _allocate_renamed_id 의 conflict check: 'lib-1-copy' 가 비어있음
            [{"conflicting_ids": []}],
            # COPY 실행
            [{"created_ids": ["lib-1-copy", "lib-2"]}],
        ]
    )
    result = await repo.copy_skills_to_project(
        owner_email="a@b.com",
        project_name="proj",
        library_skill_ids=["lib-1", "lib-2"],
        conflict_strategy="rename",
    )
    assert len(result.renamed) == 1
    assert result.renamed[0]["old_id"] == "lib-1"
    assert result.renamed[0]["new_id"] == "lib-1-copy"


async def test_copy_to_project_invalid_strategy(fake_run):
    fake_run()
    with pytest.raises(ValueError, match="invalid conflict_strategy"):
        await repo.copy_skills_to_project(
            owner_email="a@b.com",
            project_name="proj",
            library_skill_ids=["lib-1"],
            conflict_strategy="explode",
        )


async def test_copy_to_project_filters_other_users_skills(fake_run):
    """다른 사용자 LibrarySkill id 시도 → _fetch_library_skill_ids_owned 가 owner 매칭으로 필터 → 빈 결과."""
    fake_run(
        [
            # owner 매칭 안 됨 → 빈 owned list
            [{"owned": []}],
        ]
    )
    result = await repo.copy_skills_to_project(
        owner_email="a@b.com",
        project_name="proj",
        library_skill_ids=["other-user-lib"],
        conflict_strategy="overwrite",
    )
    assert result.imported_ids == []
    assert result.skipped_ids == []
    assert result.renamed == []


async def test_find_conflicting_skill_ids(fake_run):
    fake_run([[{"conflicting_ids": ["a", "b"]}]])
    out = await repo.find_conflicting_skill_ids("proj", ["a", "b", "c"])
    assert set(out) == {"a", "b"}


async def test_find_conflicting_skill_ids_empty(fake_run):
    """빈 입력은 cypher 호출 없이 빈 list 반환."""
    fake_run()
    out = await repo.find_conflicting_skill_ids("proj", [])
    assert out == []
