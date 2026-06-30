"""
lineage_pipeline 테스트 — 핵심은 deterministic 매칭의 정확성.

'Build Lineage Context' 알고리즘 동작 검증:
- name → high/medium/low 매칭 (변형: PascalCase/snake_case/kebab-case)
- API endpoint path segment 매칭
- Service 이름 stopword 제외 후 단어별 매칭
- 매칭 0 건 → missingImpl 에 분류
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.lineage_pipeline import (
    LineageInput,
    _build_drift_list,
    _build_lineage_result,
    _build_spec_name_index,
    _extract_drift_candidates,
    _match_by_endpoint,
    _match_by_name,
    _match_by_service_name,
    _name_variants,
    run_lineage_pipeline,
)
from tests.conftest import FakeGemini, FakeNeo4j


# ─── _name_variants ─────────────────────────────────────────────


def test_name_variants_pascal_case():
    v = set(_name_variants("ToolApplication"))
    assert "toolapplication" in v
    assert "tool-application" in v
    assert "tool_application" in v


def test_name_variants_snake_case():
    v = set(_name_variants("tool_application"))
    assert "tool_application" in v
    assert "toolapplication" in v


def test_name_variants_empty():
    assert _name_variants("") == []
    assert _name_variants("   ") == []


# ─── _match_by_name ─────────────────────────────────────────────


def _repo(url: str, files: List[str], role: str = "primary") -> Dict[str, Any]:
    return {"url": url, "role": role, "label": "", "files": files}


def test_match_by_name_high_confidence_exact_filename():
    trees = [_repo("https://github.com/o/r", ["src/ToolApplication.java"])]
    out = _match_by_name("ToolApplication", trees)
    assert len(out) == 1
    assert out[0].confidence == "high"
    assert out[0].filePath == "src/ToolApplication.java"


def test_match_by_name_high_for_suffix_in_filename():
    """파일명 '...Service' 처럼 도메인명 이 포함되면 high."""
    trees = [_repo("https://github.com/o/r", ["src/ToolApplicationService.java"])]
    out = _match_by_name("ToolApplication", trees)
    assert len(out) >= 1
    assert out[0].confidence == "high"


def test_match_by_name_medium_folder_match():
    trees = [_repo("https://github.com/o/r", ["src/domain/ticket/Repository.java"])]
    out = _match_by_name("ticket", trees)
    assert len(out) == 1
    assert out[0].confidence == "medium"


def test_match_by_name_no_match_returns_empty():
    trees = [_repo("https://github.com/o/r", ["src/something.java"])]
    out = _match_by_name("CompletelyDifferent", trees)
    assert out == []


def test_match_by_name_skips_repos_with_error():
    """GitHub fetch 실패한 repo 는 매칭 skip."""
    trees = [
        {"url": "https://github.com/o/r", "error": "404", "role": "primary"},
    ]
    out = _match_by_name("Ticket", trees)
    assert out == []


def test_match_by_name_confidence_ordering():
    """동일 name 으로 high + medium + low 가 있으면 high 가 앞에."""
    trees = [
        _repo(
            "https://github.com/o/r",
            [
                "Ticket.java",  # high (exact)
                "src/ticket/utils.java",  # medium (folder)
                "src/internal/legacy_ticket_old.txt",  # low (path includes)
            ],
        )
    ]
    out = _match_by_name("Ticket", trees)
    assert out[0].confidence == "high"
    assert out[0].filePath == "Ticket.java"


def test_match_by_name_limits_to_8():
    files = [f"src/Ticket{i}.java" for i in range(20)]
    trees = [_repo("https://github.com/o/r", files)]
    out = _match_by_name("Ticket", trees)
    assert len(out) == 8


# ─── _match_by_endpoint ─────────────────────────────────────────


def test_match_by_endpoint_extracts_segments():
    trees = [_repo("https://github.com/o/r", ["src/TicketController.java"])]
    out = _match_by_endpoint("/api/v1/ticket/issue", trees)
    # 'ticket' (length 6) 로 'TicketController.java' 매칭
    assert any("Ticket" in m.filePath for m in out)


def test_match_by_endpoint_singular_plural_not_normalized():
    """단수/복수 자동 변환 없음 (의도된 한계 회귀 테스트)."""
    trees = [_repo("https://github.com/o/r", ["src/TicketController.java"])]
    out = _match_by_endpoint("/api/v1/tickets", trees)
    # 'tickets' 는 'ticketcontroller' 안에 포함 안 됨 (s 가 끝에 있음)
    # → 매칭 0개. 의도된 한계 (tech-debt 후보).
    assert out == []


def test_match_by_endpoint_skips_short_and_meta_segments():
    trees = [_repo("https://github.com/o/r", ["src/v1Handler.java"])]
    # 'v1', 'api' 같은 메타 segment 는 매칭 안 됨
    out = _match_by_endpoint("/api/v1/x", trees)
    assert out == []


# ─── _match_by_service_name ─────────────────────────────────────


def test_match_by_service_name_filters_stopwords():
    trees = [_repo("https://github.com/o/r", ["src/Governance.java"])]
    # 'Service' 는 stopword 라 'Governance' 만 매칭에 사용
    out = _match_by_service_name("Governance & Subscription Service", trees)
    assert any("Governance" in m.filePath for m in out)


def test_match_by_service_name_pure_stopwords_returns_empty():
    trees = [_repo("https://github.com/o/r", ["src/X.java"])]
    out = _match_by_service_name("Service API Module", trees)
    assert out == []


# ─── Multi-repo + 결합 매칭 시나리오 ───────────────────────────


def test_match_by_name_aggregates_across_repos():
    """동일 name 이 여러 repo 에 있으면 모두 결과에 포함."""
    trees = [
        _repo("https://github.com/o/backend", ["src/Ticket.java"], role="primary"),
        _repo("https://github.com/o/frontend", ["pages/Ticket.vue"], role="mirror"),
    ]
    out = _match_by_name("Ticket", trees)
    repos = {m.repoUrl for m in out}
    assert "https://github.com/o/backend" in repos
    assert "https://github.com/o/frontend" in repos
    # role 정보도 전파됨
    roles = {m.role for m in out}
    assert "primary" in roles
    assert "mirror" in roles


def test_pascalcase_variant_matches_kebab_case_file():
    """PR8 변경: PascalCase 입력이 kebab-case 파일과 매칭됨."""
    trees = [_repo("https://github.com/o/r", ["src/tool-application.ts"])]
    out = _match_by_name("ToolApplication", trees)
    # tool-application kebab variant 가 high 매칭
    assert len(out) >= 1
    assert out[0].confidence == "high"


def test_pascalcase_variant_matches_snake_case_file():
    """PR8 변경: PascalCase 입력이 snake_case 파일과 매칭됨."""
    trees = [_repo("https://github.com/o/r", ["src/tool_application.py"])]
    out = _match_by_name("ToolApplication", trees)
    assert len(out) >= 1
    assert out[0].confidence == "high"


# ─── _build_lineage_result ──────────────────────────────────────


def test_build_full_result_computes_stats_and_missing():
    sources = {
        "stories": [
            {"id": "Story-01", "name": "TicketIssue"},
            {"id": "Story-02", "name": "MissingFeature"},
        ],
        "aggregates": [{"id": "AGG-01", "name": "Ticket"}],
        "apis": [
            {
                "id": "API-01",
                "name": "issueTicket",
                "method": "POST",
                "endpoint": "/api/v1/tickets",
            }
        ],
        "services": [
            {
                "id": "SVC-01",
                "name": "Backend Ticket Service",
                "type": "Backend API",
                "tech_stack": "Spring Boot",
            }
        ],
    }
    repo_trees = [
        _repo(
            "https://github.com/o/r",
            [
                "src/TicketIssue.java",  # Story 매칭
                "src/Ticket.java",  # Aggregate / API endpoint segment 매칭
            ],
        )
    ]
    result = _build_lineage_result(sources, repo_trees)
    assert result.stats.storiesCount == 2
    assert result.stats.aggregatesCount == 1
    assert result.stats.apisCount == 1
    assert result.stats.servicesCount == 1
    # MissingFeature 는 매칭 없으므로 missingImpl 에
    missing_names = {m.name for m in result.missingImpl}
    assert "MissingFeature" in missing_names
    # Aggregate Ticket 은 매칭 있음
    agg = result.aggregates[0]
    assert len(agg.implementations) >= 1
    # 모든 implementations 는 verified=True
    for art in [*result.stories, *result.aggregates, *result.apis, *result.services]:
        for impl in art.implementations:
            assert impl.verified is True


def test_build_summary_includes_counts():
    sources = {"stories": [], "aggregates": [], "apis": [], "services": []}
    result = _build_lineage_result(sources, [])
    assert "0개 Aggregate" in result.summary
    assert "0개 API" in result.summary
    assert "0개 Service" in result.summary


def test_build_api_matches_via_both_name_and_endpoint():
    """API 는 name 매칭 + endpoint segment 매칭이 dedup 되어 결합됨."""
    sources = {
        "stories": [],
        "aggregates": [],
        "apis": [
            {
                "id": "API-01",
                "name": "issueTicket",
                "method": "POST",
                "endpoint": "/api/v1/ticket/issue",
            }
        ],
        "services": [],
    }
    # 파일은 endpoint 의 'ticket' 으로만 매칭 가능 (name 'issueTicket' 은 직접 매칭 안 됨)
    repo_trees = [_repo("https://github.com/o/r", ["src/TicketController.java"])]
    result = _build_lineage_result(sources, repo_trees)
    assert len(result.apis[0].implementations) >= 1
    # endpoint segment 'ticket' 으로 매칭
    paths = {i.filePath for i in result.apis[0].implementations}
    assert "src/TicketController.java" in paths


def test_build_service_fallback_to_word_split_when_name_no_match():
    """Service name 직접 매칭 0 → stopword 제외 단어별 매칭으로 fallback."""
    sources = {
        "stories": [],
        "aggregates": [],
        "apis": [],
        "services": [
            {
                "id": "SVC-01",
                "name": "Governance & Subscription Service",
                "type": "Backend",
            }
        ],
    }
    # 'Governance' 단어로 매칭 (Service 는 stopword)
    repo_trees = [_repo("https://github.com/o/r", ["src/Governance.java"])]
    result = _build_lineage_result(sources, repo_trees)
    assert len(result.services[0].implementations) >= 1
    assert "Governance" in result.services[0].implementations[0].filePath


# ─── e2e ────────────────────────────────────────────────────────


class _FakeGitHub:
    """fetch_repo_trees_bulk 의 GitHubClient 인터페이스 mock."""

    def __init__(self, *, meta: Dict[str, Any], tree: Dict[str, Any]):
        self.meta = meta
        self.tree = tree

    async def get_repo(self, ident):
        return self.meta

    async def get_tree(self, ident, ref, recursive=True):
        return self.tree


@pytest.mark.asyncio
async def test_run_lineage_full_flow_saves_result(monkeypatch):
    # 5개 Neo4j fetch + 1개 save
    neo = FakeNeo4j(
        responses=[
            [{"stories": [{"id": "S1", "name": "TicketIssue"}]}],
            [{"aggregates": [{"id": "A1", "name": "Ticket"}]}],
            [{"apis": []}],
            [{"services": []}],
            [{"repos": [{"url": "https://github.com/o/r", "role": "primary", "label": ""}]}],
        ]
    )

    save_calls = []

    async def fake_save_run(cypher, params=None, database=None):
        save_calls.append({"cypher": cypher, "params": params or {}})
        return [{"saved_id": "lineage-x-1"}]

    monkeypatch.setattr(
        "app.service.lineage_repository.neo4j_client.run_cypher", fake_save_run
    )

    github = _FakeGitHub(
        meta={"default_branch": "main"},
        tree={
            "tree": [
                {"path": "src/TicketIssue.java", "type": "blob"},
                {"path": "src/Ticket.java", "type": "blob"},
            ]
        },
    )

    ctx = PipelineContext(gemini=FakeGemini(lambda p: "no call"), neo4j=neo, idempotency_key="lg1")

    result = await run_lineage_pipeline(
        ctx,
        LineageInput(project_name="x"),
        github_client=github,  # type: ignore[arg-type]
    )

    assert result.stats.storiesCount == 1
    assert result.stats.aggregatesCount == 1
    # save 호출 발생
    assert len(save_calls) == 1
    assert "CREATE (l:LineageResult" in save_calls[0]["cypher"]
    # 매칭 결과 검증
    assert len(result.stories[0].implementations) >= 1
    assert result.stories[0].implementations[0].repoUrl == "https://github.com/o/r"


@pytest.mark.asyncio
async def test_run_lineage_raises_on_empty_project():
    ctx = PipelineContext(
        gemini=FakeGemini(lambda p: "x"), neo4j=FakeNeo4j(), idempotency_key="lg2"
    )
    with pytest.raises(ValueError, match="projectName"):
        await run_lineage_pipeline(ctx, LineageInput(project_name=""))


@pytest.mark.asyncio
async def test_run_lineage_handles_repo_fetch_failure():
    """GitHub 호출 실패해도 파이프라인은 진행, 매칭 결과만 빈 상태."""
    neo = FakeNeo4j(
        responses=[
            [{"stories": [{"id": "S1", "name": "Ticket"}]}],
            [{"aggregates": []}],
            [{"apis": []}],
            [{"services": []}],
            [{"repos": [{"url": "https://github.com/o/missing", "role": "primary", "label": ""}]}],
        ]
    )

    class _FailingGitHub:
        async def get_repo(self, ident):
            from app.clients.github_client import GitHubError
            raise GitHubError("404 not found", status=404)
        async def get_tree(self, ident, ref, recursive=True):
            from app.clients.github_client import GitHubError
            raise GitHubError("404", status=404)

    ctx = PipelineContext(
        gemini=FakeGemini(lambda p: "no call"), neo4j=neo, idempotency_key="lg3"
    )
    result = await run_lineage_pipeline(
        ctx,
        LineageInput(project_name="x"),
        github_client=_FailingGitHub(),  # type: ignore[arg-type]
        save=False,  # 저장 건너뛰기 (실제 Neo4j 안 거치는 테스트)
    )
    # Story 가 missingImpl 로 분류됨 (매칭할 파일이 없으므로)
    missing_names = {m.name for m in result.missingImpl}
    assert "Ticket" in missing_names


@pytest.mark.asyncio
async def test_run_lineage_emits_stage_markers_in_order():
    """[progress] FE 진행바용 stage 마커 — fetch→trees→match→saving 순서."""
    neo = FakeNeo4j(
        responses=[
            [{"stories": [{"id": "S1", "name": "Ticket"}]}],
            [{"aggregates": []}],
            [{"apis": []}],
            [{"services": []}],
            [{"repos": []}],  # repo 없음 → tree fetch 빈 결과, 단계는 그대로 진행
        ]
    )
    stages: List[str] = []

    async def _record(stage: str) -> None:
        stages.append(stage)

    ctx = PipelineContext(
        gemini=FakeGemini(lambda p: "no call"),
        neo4j=neo,
        idempotency_key="lg-stage",
        stage_callback=_record,
    )
    await run_lineage_pipeline(
        ctx, LineageInput(project_name="x"),
        github_client=_FakeGitHub(meta={"default_branch": "main"}, tree={"tree": []}),  # type: ignore[arg-type]
        save=False,
    )
    # save=False 라 saving 은 없음
    assert stages == ["lineage:fetch", "lineage:trees", "lineage:match"]


# ─── fetch_repo_trees_bulk: 병렬 + repo 별 타임아웃 (timeout fix) ──


@pytest.mark.asyncio
async def test_fetch_repo_trees_bulk_runs_in_parallel(monkeypatch):
    """여러 repo 를 병렬로 fetch — 순차 합산보다 빠름 (동시 실행 확인)."""
    import asyncio as _asyncio
    from app.clients import github_client as gc

    # 각 호출이 0.2s 자는 mock. 순차면 3 repo × (get_repo+get_tree)=6×0.2=1.2s,
    # 병렬이면 ~0.2~0.4s. 0.8s 안에 끝나면 병렬로 동작한 것.
    class _SlowGitHub:
        async def get_repo(self, ident):
            await _asyncio.sleep(0.2)
            return {"default_branch": "main"}
        async def get_tree(self, ident, ref, recursive=True):
            await _asyncio.sleep(0.2)
            return {"tree": [{"path": "src/App.java", "type": "blob"}]}

    repos = [
        {"url": "https://github.com/o/a", "role": "fe", "label": ""},
        {"url": "https://github.com/o/b", "role": "be", "label": ""},
        {"url": "https://github.com/o/c", "role": "db", "label": ""},
    ]
    start = _asyncio.get_event_loop().time()
    out = await gc.fetch_repo_trees_bulk(_SlowGitHub(), repos)  # type: ignore[arg-type]
    elapsed = _asyncio.get_event_loop().time() - start

    assert len(out) == 3
    assert all(r.get("files") == ["src/App.java"] for r in out)
    assert elapsed < 0.8, f"병렬이 아니면 ~1.2s — elapsed={elapsed:.2f}s"


@pytest.mark.asyncio
async def test_fetch_repo_trees_bulk_per_repo_timeout(monkeypatch):
    """느린 repo 하나가 전체를 막지 않고 그 repo 만 error 처리 (타임아웃 캡)."""
    import asyncio as _asyncio
    from app.clients import github_client as gc

    monkeypatch.setattr(gc, "_PER_REPO_TIMEOUT_SEC", 0.3)

    class _MixedGitHub:
        async def get_repo(self, ident):
            # 'slow' repo 는 타임아웃을 넘기도록 길게 잔다.
            if ident.repo == "slow":
                await _asyncio.sleep(5)
            return {"default_branch": "main"}
        async def get_tree(self, ident, ref, recursive=True):
            return {"tree": [{"path": "src/Ok.java", "type": "blob"}]}

    repos = [
        {"url": "https://github.com/o/fast", "role": "fe", "label": ""},
        {"url": "https://github.com/o/slow", "role": "be", "label": ""},
    ]
    out = await gc.fetch_repo_trees_bulk(_MixedGitHub(), repos)  # type: ignore[arg-type]
    by_url = {r["url"]: r for r in out}

    # 빠른 repo 는 정상, 느린 repo 는 error 로 분류 (전체는 막히지 않음)
    assert by_url["https://github.com/o/fast"].get("files") == ["src/Ok.java"]
    assert "error" in by_url["https://github.com/o/slow"]
    assert "넘겨" in by_url["https://github.com/o/slow"]["error"]


# ─── Drift detection ────────────────────────────────────────────


def test_extract_drift_candidates_filename_patterns():
    """Controller/Service/Repository/Aggregate/Event 파일명 패턴 인식."""
    trees = [
        _repo("https://github.com/o/r", [
            "src/OrderController.java",
            "src/PaymentService.ts",
            "src/UserRepository.java",
            "src/CartAggregate.kt",
            "src/OrderCreatedEvent.java",
            "src/utils/helpers.ts",   # spec 후보 아님
            "src/README.md",
        ]),
    ]
    candidates = _extract_drift_candidates(trees)
    kinds = sorted({c["kind"] for c in candidates})
    assert "controller" in kinds
    assert "service" in kinds
    assert "repository" in kinds
    assert "aggregate" in kinds
    assert "event" in kinds
    # symbol 추출 정확성
    symbols = {c["symbol"] for c in candidates}
    assert "Order" in symbols
    assert "Payment" in symbols


def test_extract_drift_candidates_folder_patterns():
    """routes/handlers/controllers 폴더 안의 파일은 route 후보."""
    trees = [
        _repo("https://github.com/o/r", [
            "src/routes/checkout.ts",
            "src/handlers/invoice.py",
            "src/routes/index.ts",   # index 는 스톱워드라 제외
        ]),
    ]
    candidates = _extract_drift_candidates(trees)
    route_syms = sorted([c["symbol"] for c in candidates if c["kind"] == "route"])
    assert route_syms == ["checkout", "invoice"]


def test_spec_name_index_collects_variants():
    sources = {
        "stories": [{"name": "User Login"}],
        "aggregates": [{"name": "Order"}],
        "apis": [{"name": "createOrder", "endpoint": "/api/v1/orders/new"}],
        "services": [{"name": "PaymentService"}],
    }
    idx = _build_spec_name_index(sources)
    # variants 가 잘 들어 갔는지
    assert "order" in idx
    assert "createorder" in idx
    assert "create-order" in idx or "create_order" in idx
    # endpoint segment 'orders' 도 포함
    assert "orders" in idx
    # api / v1 같은 stopword 는 제외
    assert "api" not in idx
    assert "v1" not in idx


def test_build_drift_list_matches_skipped():
    """spec name index 와 매칭되는 후보는 drift 아님."""
    sources = {
        "stories": [],
        "aggregates": [{"name": "Order"}],   # Order 는 spec 존재
        "apis": [],
        "services": [],
    }
    trees = [
        _repo("https://github.com/o/r", [
            "src/OrderController.java",       # 'Order' 매칭 → drift 아님
            "src/SecretInternalService.ts",   # 매칭 없음 → drift
        ]),
    ]
    drifts = _build_drift_list(sources, trees)
    assert len(drifts) == 1
    assert drifts[0].symbol == "SecretInternal"
    assert drifts[0].kind == "service"


def test_build_drift_list_dedupes_same_file():
    """같은 파일이 여러 패턴에 걸려도 한 번만 drift."""
    sources = {"stories": [], "aggregates": [], "apis": [], "services": []}
    trees = [
        _repo("https://github.com/o/r", [
            "src/handlers/MysteryService.ts",  # filename Service + folder route 둘 다 hit
        ]),
    ]
    drifts = _build_drift_list(sources, trees)
    assert len(drifts) == 1
