"""
eval_score_routes.py 회귀 가드.

검증:
- ownership 미보유 시 403
- 그래프 fetch + scorer 호출 → 4-tier 응답
- 빈 그래프 (legacy project) 도 깨지지 않음
- LLM 호출 0건 (이 endpoint 의 핵심 — 빠른 응답)
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import eval_score_routes
from app.service.user_repository import UserPublic


pytestmark = pytest.mark.asyncio


def _user(email: str = "owner@example.com") -> UserPublic:
    return UserPublic(
        id="u-1", email=email, name="t",
        subscription_type="free", is_admin=False,
    )


@pytest.fixture
def deny_ownership(monkeypatch):
    async def fake_assert(email: str, project: str):
        raise HTTPException(status_code=403, detail="권한 없음")
    monkeypatch.setattr(
        "app.api.eval_score_routes.ownership_repository.assert_owns", fake_assert
    )


@pytest.fixture
def allow_ownership(monkeypatch):
    async def fake_assert(email: str, project: str):
        return None
    monkeypatch.setattr(
        "app.api.eval_score_routes.ownership_repository.assert_owns", fake_assert
    )


@pytest.fixture
def stub_graphs_fully_specified(monkeypatch):
    """A-1/A-2/A-3 + D-1/D-2 모두 채워진 그래프 stub."""
    from app.service.query_repository import (
        ArchitectureGraph,
        DddGraph,
        SpackGraph,
    )

    async def fake_spack(name, team_id=""):
        return SpackGraph(
            apis=[{
                "id": "API-01", "name": "POST", "method": "POST",
                "endpoint": "/plants/{plantId}/growth",
                "description": "기록",
                "related_story_id": "Story-03.1",
                "path_params": [{"name": "plantId", "type": "uuid"}],
                "request_body": {
                    "content_type": "application/json",
                    "fields": [{"name": "height", "type": "double"}],
                    "example": "",
                },
                "response_body": {
                    "status": 201, "content_type": "application/json",
                    "fields": [{"name": "id", "type": "uuid"}], "example": "",
                },
                "error_cases": [
                    {"status": 401}, {"status": 404}, {"status": 422},
                ],
                "auth": {
                    "required": True, "required_roles": ["owner"],
                    "ownership_check": "Plant.ownerId == requester.userId",
                    "description": "본인 식물",
                },
            }],
            entities=[{
                "id": "ENT-01", "name": "Plant",
                "attributes": [
                    {"name": "id", "type": "uuid", "required": True,
                     "constraint": "", "description": ""}
                ],
                "lineage": {
                    "confidence": "direct",
                    "related_stories": [{"story_id": "Story-01.1", "quote": "식물 등록"}],
                },
            }],
            policies=[{"id": "POL-01", "category": "Security", "description": "JWT"}],
        )

    async def fake_ddd(name, team_id=""):
        return DddGraph(
            contexts=[{"id": "CTX-01"}],
            aggregates=[{
                "id": "AGG-01", "name": "Plant",
                "invariants": ["leafCount >= 0"],
                "lineage": {"confidence": "direct",
                            "related_stories": [{"story_id": "Story-01.1", "quote": "q"}]},
            }],
            domain_entities=[{
                "id": "DENT-01", "name": "X", "aggregate_id": "AGG-01",
                "attributes": [{"name": "a", "type": "uuid"}],
            }],
            domain_events=[{
                "id": "EVT-01", "name": "Ev",
                "payload_fields": [{"name": "id", "type": "uuid"}],
            }],
        )

    async def fake_arch(name, team_id=""):
        return ArchitectureGraph(
            services=[{
                "id": "SVC-01", "name": "Backend", "type": "Backend API",
                "deployment": {"port": 8080, "replicas": 2,
                               "env_vars": ["DATABASE_URL"], "scaling_policy": "manual",
                               "health_check_path": "/health"},
                "external_dependencies": [],
            }],
            databases=[{"id": "DB-01", "name": "DB", "tech_stack": "PostgreSQL"}],
            connections=[],
        )

    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_spack_graph", fake_spack)
    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_ddd_graph", fake_ddd)
    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_architecture_graph", fake_arch)


@pytest.fixture
def stub_graphs_empty_detail(monkeypatch):
    """[2026-05-28] 노드는 있으나 본문(error_cases/attributes 등)이 빈약한 그래프.
    fix_targets 가 구체 항목을 콕 집어야 하는 케이스."""
    from app.service.query_repository import (
        ArchitectureGraph,
        DddGraph,
        SpackGraph,
    )

    async def fake_spack(name, team_id=""):
        return SpackGraph(
            apis=[
                {"id": "API-01", "name": "작업 생성", "method": "POST", "endpoint": "/tasks"},
                {"id": "API-02", "name": "작업 조회", "method": "GET", "endpoint": "/tasks"},
            ],
            entities=[{"id": "ENT-01", "name": "Task"}],
            policies=[],
        )

    async def fake_ddd(name, team_id=""):
        return DddGraph(contexts=[], aggregates=[])

    async def fake_arch(name, team_id=""):
        return ArchitectureGraph(services=[], databases=[], connections=[])

    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_spack_graph", fake_spack)
    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_ddd_graph", fake_ddd)
    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_architecture_graph", fake_arch)


@pytest.fixture
def stub_graphs_empty(monkeypatch):
    """빈 그래프 stub (legacy / 신규 프로젝트)."""
    from app.service.query_repository import (
        ArchitectureGraph,
        DddGraph,
        SpackGraph,
    )

    async def fake_spack(name, team_id=""):
        return SpackGraph(apis=[], entities=[], policies=[])

    async def fake_ddd(name, team_id=""):
        return DddGraph(contexts=[], aggregates=[])

    async def fake_arch(name, team_id=""):
        return ArchitectureGraph(services=[], databases=[], connections=[])

    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_spack_graph", fake_spack)
    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_ddd_graph", fake_ddd)
    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_architecture_graph", fake_arch)


async def test_eval_score_denies_when_not_owner(deny_ownership):
    """ownership 미보유 시 403 — IDOR 회귀 가드."""
    with pytest.raises(HTTPException) as exc:
        await eval_score_routes.get_eval_score(
            project_name="victim_project",
            current_user=_user(email="attacker@evil.com"),
        )
    assert exc.value.status_code == 403


async def test_eval_score_empty_project_name_rejected(allow_ownership):
    """빈 project_name → 400."""
    with pytest.raises(HTTPException) as exc:
        await eval_score_routes.get_eval_score(
            project_name="",
            current_user=_user(),
        )
    assert exc.value.status_code == 400


async def test_eval_score_fully_specified_returns_high_overall(
    allow_ownership, stub_graphs_fully_specified
):
    """모든 contract 충실 → overall 75%+ (validation Tier 4 가 실 위반 반영하므로
    fixture 의 small 위반 — DDD ↔ SPACK 매핑 미충족 등 — 으로 ~84% 예상)."""
    resp = await eval_score_routes.get_eval_score(
        project_name="plant", current_user=_user(),
    )
    assert resp.project_name == "plant"
    assert resp.overall > 0.75
    # Tier 1 (구조) 만점
    assert resp.tier1.score == 1.0
    # Tier 2 (디테일) 도 충실
    assert resp.tier2.score > 0.90
    # sub_metrics 가 dict (FE 가 표시할 수 있는 형태)
    assert isinstance(resp.tier2.sub_metrics, dict)
    assert "entity_attributes_present_ratio" in resp.tier2.sub_metrics


async def test_eval_score_empty_project_returns_low_overall(
    allow_ownership, stub_graphs_empty
):
    """빈 그래프 → Tier 1 = 0 (구조 부재), 나머지 N/A 만점 처리."""
    resp = await eval_score_routes.get_eval_score(
        project_name="empty", current_user=_user(),
    )
    # Tier 1 (구조) 가 0 — apis/entities/policies 모두 비어있음
    assert resp.tier1.score == 0.0
    # [2026-05-25 fix] 빈 그래프 = overall 0 (사용자 오해 방지). 이전엔 다른
    # Tier 의 N/A 만점이 합산돼 0.90 잘못 표시.
    assert resp.overall == 0.0


async def test_eval_score_response_schema_serializable(
    allow_ownership, stub_graphs_fully_specified
):
    """응답이 JSON 직렬화 가능 — FE 가 그대로 받을 수 있는지."""
    import json
    resp = await eval_score_routes.get_eval_score(
        project_name="plant", current_user=_user(),
    )
    # pydantic model → dict → json
    data = resp.model_dump()
    json_str = json.dumps(data)
    assert "overall" in json_str
    assert "tier1" in json_str
    # 점수가 소숫점 4자리로 반올림돼 응답 크기 작음
    assert "0.000000" not in json_str


async def test_eval_score_returns_top_violation_codes(
    allow_ownership, stub_graphs_fully_specified
):
    """[k — 2026-05-25] 응답에 top_violation_codes 포함 — FE 가 위반 상세 표시."""
    resp = await eval_score_routes.get_eval_score(
        project_name="plant", current_user=_user(),
    )
    # list 가 비어있어도 (위반 없음) field 자체는 존재
    assert isinstance(resp.top_violation_codes, list)
    # 각 항목은 {code, count}
    for item in resp.top_violation_codes:
        assert isinstance(item.code, str) and item.code
        assert isinstance(item.count, int) and item.count > 0


async def test_eval_score_returns_fix_targets(
    allow_ownership, stub_graphs_empty_detail
):
    """[2026-05-28] 응답에 fix_targets 포함 — 어느 항목이 무엇이 빠졌는지 이름까지."""
    resp = await eval_score_routes.get_eval_score(
        project_name="needs_fix", current_user=_user(),
    )
    assert isinstance(resp.fix_targets, list)
    assert len(resp.fix_targets) > 0
    # 각 target 은 구체적 missing 항목 이름을 가진다
    ft = resp.fix_targets[0]
    assert ft.label
    assert ft.fix  # 액션 문구
    assert ft.missing  # 빠진 항목 list
    assert all(m.get("id") and m.get("name") for m in ft.missing)
    # FE 점프용 prd_section 존재
    assert ft.prd_section in ("epic", "nfr", "screen", "")


# ─── ARCH_API_UNMAPPED false-positive fix (2026-06) ──────────────


async def _eval_with(monkeypatch, *, api_service_rels, entities=None, entity_mapping_rels=None):
    """spack(api_service_rels/entities/entity_mapping_rels 주입 가능) + 최소 arch stub."""
    from app.service.query_repository import ArchitectureGraph, DddGraph, SpackGraph

    async def fake_spack(name, team_id=""):
        return SpackGraph(
            apis=[
                {"id": "API-01", "name": "a", "method": "GET", "endpoint": "/a",
                 "related_story_id": "Story-01.1"},
                {"id": "API-02", "name": "b", "method": "POST", "endpoint": "/b",
                 "related_story_id": "Story-01.2"},
            ],
            entities=entities or [], policies=[],
            api_service_rels=api_service_rels,
            entity_mapping_rels=entity_mapping_rels or [],
        )

    async def fake_ddd(name, team_id=""):
        return DddGraph()

    async def fake_arch(name, team_id=""):
        return ArchitectureGraph(
            services=[{"id": "SVC-01", "name": "S", "type": "Backend API"}],
        )

    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_spack_graph", fake_spack)
    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_ddd_graph", fake_ddd)
    monkeypatch.setattr(
        "app.api.eval_score_routes.query_repository.get_architecture_graph", fake_arch)
    return await eval_score_routes.get_eval_score(project_name="p", current_user=_user())


async def test_arch_api_unmapped_not_flagged_when_handled_by_edges_exist(
    monkeypatch, allow_ownership,
):
    """HANDLED_BY 엣지(api_service_rels)가 있으면 ARCH_API_UNMAPPED false-positive 없어야."""
    resp = await _eval_with(monkeypatch, api_service_rels=[
        {"source_id": "API-01", "target_id": "SVC-01", "type": "HANDLED_BY"},
        {"source_id": "API-02", "target_id": "SVC-01", "type": "HANDLED_BY"},
    ])
    codes = [v.code for v in resp.top_violation_codes]
    assert "ARCH_API_UNMAPPED" not in codes


async def test_arch_api_unmapped_still_flagged_when_truly_unmapped(
    monkeypatch, allow_ownership,
):
    """대조군 — 매핑이 진짜 없으면 ARCH_API_UNMAPPED 는 정상적으로 잡혀야(검증 살아있음)."""
    resp = await _eval_with(monkeypatch, api_service_rels=[])
    codes = [v.code for v in resp.top_violation_codes]
    assert "ARCH_API_UNMAPPED" in codes


# ─── DDD_MAPPING_MISSING_ENTITY false-positive fix (2026-06) ─────


async def test_ddd_mapping_not_flagged_when_mapped_to_edges_exist(
    monkeypatch, allow_ownership,
):
    """MAPPED_TO 엣지(entity_mapping_rels)가 있으면 DDD_MAPPING_MISSING_ENTITY false-positive 없어야."""
    resp = await _eval_with(
        monkeypatch,
        api_service_rels=[],
        entities=[{"id": "ENT-01", "name": "Ticket",
                   "attributes": [{"name": "id", "type": "uuid"}]}],
        entity_mapping_rels=[
            {"source_id": "ENT-01", "target_id": "AGG-01", "type": "MAPPED_TO"},
        ],
    )
    codes = [v.code for v in resp.top_violation_codes]
    assert "DDD_MAPPING_MISSING_ENTITY" not in codes


def test_ddd_mapping_validator_still_flags_when_truly_unmapped():
    """대조군(검증기 직접) — spack_entity_mapping 이 비면 DDD_MAPPING_MISSING_ENTITY 정상 발생.

    라우트 top5 랭킹과 무관하게 검증 로직 자체가 살아있음을 보장.
    """
    from app.pipelines.design_validator.ddd import normalize_ddd

    norm_spack = {"entities": [{"id": "ENT-01", "name": "Ticket"}]}
    _, report = normalize_ddd({"spack_entity_mapping": []}, norm_spack)
    codes = {v.code for v in report.violations}
    assert "DDD_MAPPING_MISSING_ENTITY" in codes


# ─── ARCH_AGG_UNOWNED false-positive fix (owned_aggregate_names, 2026-06) ─────


async def _eval_agg(monkeypatch, *, owned_names):
    from app.service.query_repository import ArchitectureGraph, DddGraph, SpackGraph

    async def fake_spack(name, team_id=""):
        return SpackGraph(apis=[], entities=[], policies=[])

    async def fake_ddd(name, team_id=""):
        return DddGraph(aggregates=[
            {"id": "AGG-01", "name": "Plant", "invariants": ["leaf >= 0"]},
        ])

    async def fake_arch(name, team_id=""):
        svc = {"id": "SVC-01", "name": "Backend", "type": "Backend API"}
        if owned_names is not None:
            svc["owned_aggregate_names"] = owned_names
        return ArchitectureGraph(services=[svc])

    monkeypatch.setattr("app.api.eval_score_routes.query_repository.get_spack_graph", fake_spack)
    monkeypatch.setattr("app.api.eval_score_routes.query_repository.get_ddd_graph", fake_ddd)
    monkeypatch.setattr("app.api.eval_score_routes.query_repository.get_architecture_graph", fake_arch)
    return await eval_score_routes.get_eval_score(project_name="p", current_user=_user())


async def test_arch_agg_unowned_not_flagged_when_owned_aggregate_names_present(
    monkeypatch, allow_ownership,
):
    """owned_aggregate_names(노드 저장명)에 Aggregate 가 있으면 backfill 되어 false ARCH_AGG_UNOWNED 없어야."""
    resp = await _eval_agg(monkeypatch, owned_names=["Plant"])
    codes = [v.code for v in resp.top_violation_codes]
    assert "ARCH_AGG_UNOWNED" not in codes


async def test_arch_agg_unowned_flagged_when_truly_unowned(
    monkeypatch, allow_ownership,
):
    """대조군 — 소유 정보가 전혀 없으면 ARCH_AGG_UNOWNED 정상 발생(검증 유지)."""
    resp = await _eval_agg(monkeypatch, owned_names=None)
    codes = [v.code for v in resp.top_violation_codes]
    assert "ARCH_AGG_UNOWNED" in codes


# ─── DDD_MISSING_SPACK_ENTITY false-positive (domain_entities 별칭, 2026-06) ──


async def test_ddd_missing_spack_entity_not_flagged_when_modeled_as_domain_entity(
    monkeypatch, allow_ownership,
):
    """SPACK Entity 가 DDD domain_entity 로 모델링됐으면 DDD_MISSING_SPACK_ENTITY false 없어야.

    get_ddd_graph 는 domain_entities 로 주는데 검증기는 entities 를 읽어, 별칭이 없으면
    domain_entity 로 매핑된 엔티티가 전부 '누락'으로 false 처리됨.
    """
    from app.service.query_repository import ArchitectureGraph, DddGraph, SpackGraph

    async def fake_spack(name, team_id=""):
        return SpackGraph(
            apis=[], policies=[],
            entities=[{"id": "ENT-01", "name": "Ticket",
                       "attributes": [{"name": "id", "type": "uuid"}]}],
        )

    async def fake_ddd(name, team_id=""):
        # 'Ticket' 은 Aggregate 가 아니라 domain_entity 로 모델링됨
        return DddGraph(
            aggregates=[{"id": "AGG-01", "name": "Board"}],
            domain_entities=[{"id": "DENT-01", "name": "Ticket",
                              "attributes": [{"name": "id", "type": "uuid"}]}],
        )

    async def fake_arch(name, team_id=""):
        return ArchitectureGraph(services=[])

    monkeypatch.setattr("app.api.eval_score_routes.query_repository.get_spack_graph", fake_spack)
    monkeypatch.setattr("app.api.eval_score_routes.query_repository.get_ddd_graph", fake_ddd)
    monkeypatch.setattr("app.api.eval_score_routes.query_repository.get_architecture_graph", fake_arch)

    resp = await eval_score_routes.get_eval_score(project_name="p", current_user=_user())
    codes = [v.code for v in resp.top_violation_codes]
    assert "DDD_MISSING_SPACK_ENTITY" not in codes
