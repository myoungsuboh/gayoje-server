"""
lint_pipeline 테스트 — evidence-first hybrid 흐름.

[검증 포인트]
- _parse_input            : URL 파싱
- _select_sample_paths    : manifest + anchor + token-matched 우선순위
- _build_cases            : deterministic evidence 수집 → applied=true/false
- _apply_residual_verdicts: LLM verdict 의 hallucination 차단 (인용 검증)
- _compute_score          : convergence + 4 카테고리 가중평균
- run_lint_pipeline       : e2e with FakeGitHub providing full file bodies
- legacy _normalize_result: backward compat 유지
- 코드 파일 0개 → 친절한 error
- GitHub 404 → 친절한 error
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, List

import pytest

from app.clients.github_client import GitHubError, RepoIdentifier
from app.pipelines.base import PipelineContext
from app.pipelines.lint_evidence import FileSample
from app.pipelines.lint_pipeline import (
    LintInput,
    _apply_residual_verdicts,
    _build_cases,
    _compute_score,
    _normalize_result,
    _parse_input,
    _select_sample_paths,
    run_lint_pipeline,
)
from app.service.lint_repository import LintCase, LintCaseRule, LintEvidence, LintResult
from tests.conftest import FakeGemini, FakeNeo4j


# ─── _parse_input ──────────────────────────────────────────────


def test_parse_input_extracts_owner_repo():
    out = _parse_input(
        LintInput(project_name="x", github_url="https://github.com/owner/repo.git/")
    )
    assert out["owner"] == "owner"
    assert out["repo"] == "repo"
    assert out["github_url"] == "https://github.com/owner/repo"


# ─── _select_sample_paths ──────────────────────────────────────


def test_select_sample_paths_prioritizes_manifest_then_anchor_then_token_match():
    """manifest 가 코드 확장자 필터 무시하고 무조건 포함됨."""
    code_files = [
        {"path": "src/index.ts", "size": 100, "sha": "a"},
        {"path": "src/main.py", "size": 50, "sha": "b"},
        {"path": "src/ticket/refund.py", "size": 80, "sha": "c"},
        {"path": "src/unrelated.py", "size": 200, "sha": "d"},
    ]
    all_tree = code_files + [
        {"path": "package.json", "type": "blob", "size": 30, "sha": "p"},
        {"path": "pyproject.toml", "type": "blob", "size": 20, "sha": "q"},
    ]
    # 'ticket', 'refund' 토큰 매칭 → src/ticket/refund.py 선정
    selected = _select_sample_paths(
        code_files, all_tree, tokens=["ticket", "refund"]
    )
    paths = [f["path"] for f in selected]
    # manifest 가 먼저
    assert paths[0] in ("package.json", "pyproject.toml")
    assert "package.json" in paths
    assert "pyproject.toml" in paths
    # anchor (main.py / index.ts) 포함
    assert any(p in paths for p in ("src/main.py", "src/index.ts"))
    # token-matched 포함
    assert "src/ticket/refund.py" in paths
    # unrelated 는 score 0 → 제외
    assert "src/unrelated.py" not in paths


# ─── _build_cases (deterministic evidence) ─────────────────────


def test_build_cases_marks_api_applied_when_fastapi_endpoint_found():
    specs = {
        "spack": {
            "apis": [
                {
                    "id": "API-01",
                    "name": "Refund Ticket",
                    "method": "POST",
                    "endpoint": "/tickets/{id}/refund",
                    "description": "환불",
                }
            ],
            "entities": [],
            "policies": [],
        },
        "ddd": {"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []},
        "architecture": {"services": [], "databases": []},
        "rules": [],
    }
    samples = [
        FileSample(
            path="src/api/refund.py",
            content=(
                "from fastapi import APIRouter\n"
                "router = APIRouter()\n"
                "\n"
                "@router.post('/tickets/{id}/refund')\n"
                "async def refund(id: str):\n"
                "    return {}\n"
            ),
            size=200,
        )
    ]
    cases, residual = _build_cases(specs, samples)
    # SPACK 첫 rule = API → evidence 1건 이상
    spack_rules = cases[0].rules
    assert len(spack_rules) >= 1
    api_rule = spack_rules[0]
    assert api_rule.applied is True
    assert api_rule.detection_method == "deterministic"
    assert len(api_rule.evidence) == 1
    assert api_rule.evidence[0].file == "src/api/refund.py"
    assert api_rule.evidence[0].line == 4
    assert "refund" in api_rule.evidence[0].snippet.lower()
    # API 가 deterministic 으로 잡혔으니 residual 에 없음
    assert not any(r["rule_idx"] == 0 and r["category_idx"] == 0 for r in residual)


def test_build_cases_marks_api_unmatched_when_no_endpoint_in_samples():
    specs = {
        "spack": {
            "apis": [
                {"id": "API-99", "method": "POST", "endpoint": "/nonexistent"}
            ],
            "entities": [], "policies": [],
        },
        "ddd": {"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []},
        "architecture": {"services": [], "databases": []},
        "rules": [],
    }
    samples = [
        FileSample(path="src/other.py", content="print('hi')\n", size=12),
    ]
    cases, residual = _build_cases(specs, samples)
    api_rule = cases[0].rules[0]
    assert api_rule.applied is False
    assert api_rule.detection_method == "fallback"
    assert api_rule.evidence == []
    # residual 에 포함
    assert any(r["category_idx"] == 0 and r["rule_idx"] == 0 for r in residual)


def test_build_cases_matches_express_endpoint_with_path_param():
    """Express :id 변형 흡수."""
    specs = {
        "spack": {
            "apis": [{"id": "A", "method": "GET", "endpoint": "/users/:id"}],
            "entities": [], "policies": [],
        },
        "ddd": {"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []},
        "architecture": {"services": [], "databases": []},
        "rules": [],
    }
    samples = [
        FileSample(
            path="server/routes.js",
            content="router.get('/users/:id', handler)\n",
            size=50,
        )
    ]
    cases, _ = _build_cases(specs, samples)
    assert cases[0].rules[0].applied is True


def test_build_cases_matches_spring_postmapping():
    specs = {
        "spack": {
            "apis": [{"id": "A", "method": "POST", "endpoint": "/order"}],
            "entities": [], "policies": [],
        },
        "ddd": {"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []},
        "architecture": {"services": [], "databases": []},
        "rules": [],
    }
    samples = [
        FileSample(
            path="OrderController.java",
            content='@PostMapping("/order")\npublic void create() {}\n',
            size=50,
        )
    ]
    cases, _ = _build_cases(specs, samples)
    assert cases[0].rules[0].applied is True


def test_build_cases_matches_entity_class_python():
    specs = {
        "spack": {
            "apis": [],
            "entities": [{"id": "E", "name": "Ticket", "description": "티켓"}],
            "policies": [],
        },
        "ddd": {"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []},
        "architecture": {"services": [], "databases": []},
        "rules": [],
    }
    samples = [
        FileSample(
            path="models.py",
            content="from pydantic import BaseModel\nclass Ticket(BaseModel):\n    id: str\n",
            size=80,
        )
    ]
    cases, _ = _build_cases(specs, samples)
    # SPACK.entity 가 첫 rule (apis 가 없음)
    rule = cases[0].rules[0]
    assert rule.applied is True
    assert rule.detection_method == "deterministic"
    assert rule.evidence[0].kind == "entity_class"


def test_build_cases_matches_tech_stack_in_package_json():
    specs = {
        "spack": {"apis": [], "entities": [], "policies": []},
        "ddd": {"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []},
        "architecture": {
            "services": [
                {"id": "S1", "name": "Frontend", "tech_stack": "Vue.js"}
            ],
            "databases": [
                {"id": "D1", "name": "DB", "tech_stack": "PostgreSQL"}
            ],
        },
        "rules": [],
    }
    samples = [
        FileSample(
            path="package.json",
            content='{"dependencies": {"vue": "^3.4.0", "axios": "^1"}}\n',
            size=50,
        ),
        FileSample(
            path="requirements.txt",
            content="asyncpg==0.29.0\nfastapi==0.128.0\n",
            size=40,
        ),
    ]
    cases, _ = _build_cases(specs, samples)
    arch_rules = cases[2].rules
    assert len(arch_rules) == 2
    # Service: Vue.js → vue 키워드 매칭
    svc_rule = arch_rules[0]
    assert svc_rule.applied is True
    assert svc_rule.evidence[0].kind == "manifest"
    # Database: PostgreSQL → asyncpg 매칭
    db_rule = arch_rules[1]
    assert db_rule.applied is True


def test_build_cases_inserts_empty_placeholder_when_no_specs():
    specs = {
        "spack": {"apis": [], "entities": [], "policies": []},
        "ddd": {"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []},
        "architecture": {"services": [], "databases": []},
        "rules": [],
    }
    cases, residual = _build_cases(specs, [])
    # 4 카테고리 모두 'empty' placeholder rule 1개씩
    assert all(len(c.rules) == 1 and c.rules[0].applied is False for c in cases)
    assert all(c.rules[0].rule.endswith(":empty") for c in cases)
    # empty placeholder 는 residual 안 만듦
    assert residual == []


# ─── _apply_residual_verdicts (hallucination 차단) ─────────────


def test_apply_residual_verdicts_accepts_valid_quoted_evidence():
    cases = [
        LintCase(title="SPACK 준수율", convergence=0, rules=[
            LintCaseRule(rule="api:X", description="x", applied=False, detection_method="fallback"),
        ]),
        LintCase(title="DDD 준수율", convergence=0, rules=[]),
        LintCase(title="Architecture 준수율", convergence=0, rules=[]),
        LintCase(title="Rule Generator 준수율", convergence=0, rules=[]),
    ]
    samples = [FileSample(path="a.py", content="line1\n@router.post('/x')\nline3\n", size=30)]
    verdicts = [
        {
            "category_idx": 0, "rule_idx": 0, "applied": True,
            "reason": "found", "evidence_file": "a.py", "evidence_line": 2,
        }
    ]
    _apply_residual_verdicts(cases, verdicts, samples)
    rule = cases[0].rules[0]
    assert rule.applied is True
    assert rule.detection_method == "llm"
    assert rule.evidence[0].file == "a.py"
    assert rule.evidence[0].line == 2
    assert "router.post" in rule.evidence[0].snippet


def test_apply_residual_verdicts_rejects_hallucinated_file_path():
    """LLM 이 samples 에 없는 file 인용 → applied=true 거부."""
    cases = [
        LintCase(title="SPACK 준수율", convergence=0, rules=[
            LintCaseRule(rule="api:X", description="x", applied=False, detection_method="fallback"),
        ]),
        LintCase(title="DDD 준수율", convergence=0, rules=[]),
        LintCase(title="Architecture 준수율", convergence=0, rules=[]),
        LintCase(title="Rule Generator 준수율", convergence=0, rules=[]),
    ]
    samples = [FileSample(path="real.py", content="x\n", size=2)]
    verdicts = [
        {
            "category_idx": 0, "rule_idx": 0, "applied": True,
            "reason": "fabricated", "evidence_file": "made_up.py", "evidence_line": 999,
        }
    ]
    _apply_residual_verdicts(cases, verdicts, samples)
    rule = cases[0].rules[0]
    # 인용 검증 실패 → applied=false 로 강제
    assert rule.applied is False
    assert rule.detection_method == "llm"
    assert rule.evidence == []


def test_apply_residual_verdicts_rejects_out_of_range_line():
    cases = [
        LintCase(title="SPACK 준수율", convergence=0, rules=[
            LintCaseRule(rule="api:X", description="x", applied=False, detection_method="fallback"),
        ]),
        LintCase(title="DDD 준수율", convergence=0, rules=[]),
        LintCase(title="Architecture 준수율", convergence=0, rules=[]),
        LintCase(title="Rule Generator 준수율", convergence=0, rules=[]),
    ]
    samples = [FileSample(path="a.py", content="one line\n", size=10)]
    verdicts = [
        {
            "category_idx": 0, "rule_idx": 0, "applied": True,
            "reason": "out of range", "evidence_file": "a.py", "evidence_line": 999,
        }
    ]
    _apply_residual_verdicts(cases, verdicts, samples)
    # line 999 는 한 줄짜리 파일에 없음 → snippet 빈 문자열 → 거부
    assert cases[0].rules[0].applied is False


# ─── _compute_score ────────────────────────────────────────────


def test_compute_score_weighted_average_of_four_categories():
    cases = [
        LintCase(title="SPACK 준수율", convergence=0, rules=[
            LintCaseRule(rule="a", description="", applied=True),
            LintCaseRule(rule="b", description="", applied=True),
            LintCaseRule(rule="c", description="", applied=True),
            LintCaseRule(rule="d", description="", applied=False),
        ]),  # 75% applied
        LintCase(title="DDD 준수율", convergence=0, rules=[
            LintCaseRule(rule="x", description="", applied=True),
            LintCaseRule(rule="y", description="", applied=True),
        ]),  # 100%
        LintCase(title="Architecture 준수율", convergence=0, rules=[
            LintCaseRule(rule="z", description="", applied=False),
        ]),  # 0%
        LintCase(title="Rule Generator 준수율", convergence=0, rules=[
            LintCaseRule(rule="r", description="", applied=True),
        ]),  # 100%
    ]
    score = _compute_score(cases)
    assert cases[0].convergence == 75
    assert cases[1].convergence == 100
    assert cases[2].convergence == 0
    assert cases[3].convergence == 100
    # (75 + 100 + 0 + 100) / 4 = 68.75 → 69
    assert score == 69


def test_compute_score_handles_empty_case():
    cases = [
        LintCase(title="SPACK 준수율", convergence=0, rules=[]),
        LintCase(title="DDD 준수율", convergence=0, rules=[]),
        LintCase(title="Architecture 준수율", convergence=0, rules=[]),
        LintCase(title="Rule Generator 준수율", convergence=0, rules=[]),
    ]
    assert _compute_score(cases) == 0


# ─── e2e run_lint_pipeline ─────────────────────────────────────


class _FakeGitHub:
    """GitHubClient mock — repo / tree / blob fetch."""

    def __init__(
        self,
        *,
        repo_response: Dict[str, Any] | None = None,
        tree_response: Dict[str, Any] | None = None,
        blobs: Dict[str, str] | None = None,
        error: Exception | None = None,
    ):
        self.repo_response = repo_response or {"default_branch": "main"}
        self.tree_response = tree_response or {"tree": []}
        self.blobs = blobs or {}  # sha → text
        self.error = error
        self.blob_calls: List[str] = []

    async def get_repo(self, ident: RepoIdentifier) -> Dict[str, Any]:
        if self.error:
            raise self.error
        return self.repo_response

    async def get_tree(
        self, ident: RepoIdentifier, ref: str, recursive: bool = True
    ) -> Dict[str, Any]:
        if self.error:
            raise self.error
        return self.tree_response

    async def get_blob_text(
        self, ident: RepoIdentifier, file_sha: str, *, max_bytes: int = 32_000
    ) -> str:
        self.blob_calls.append(file_sha)
        return self.blobs.get(file_sha, "")


def _spec_neo_responses(
    *,
    apis: List[Dict[str, Any]] | None = None,
    entities: List[Dict[str, Any]] | None = None,
    services: List[Dict[str, Any]] | None = None,
    rules: List[Dict[str, Any]] | None = None,
    stories: List[Dict[str, Any]] | None = None,
    screens: List[Dict[str, Any]] | None = None,
) -> List[List[Dict[str, Any]]]:
    return [
        [{"apis": apis or [], "entities": entities or [], "policies": []}],
        [{"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []}],
        [{"services": services or [], "databases": []}],
        [{"rules": rules or []}],
        # [2026-06] 5번째 — 기획 항목 (Story/Screen)
        [{"stories": stories or [], "screens": screens or []}],
    ]


@pytest.mark.asyncio
async def test_run_lint_e2e_deterministic_match_skips_llm(monkeypatch):
    """API endpoint deterministic 매칭 성공 → LLM 호출 0회."""
    # spec: POST /tickets 1개. 토큰 'tickets' 가 path 매칭에 사용.
    apis = [{"id": "API-01", "name": "list", "method": "POST", "endpoint": "/tickets"}]

    blobs = {
        "sha-api": "@app.post('/tickets')\ndef create_ticket():\n    return {}\n",
        "sha-anchor": "print('main')\n",
    }
    tree = {
        "tree": [
            # anchor — 무조건 sample 포함
            {"path": "src/main.py", "type": "blob", "size": 50, "sha": "sha-anchor"},
            # token-matched — 'tickets' 가 spec endpoint 토큰
            {"path": "src/tickets.py", "type": "blob", "size": 80, "sha": "sha-api"},
        ]
    }
    github = _FakeGitHub(tree_response=tree, blobs=blobs)
    neo = FakeNeo4j(responses=_spec_neo_responses(apis=apis))
    gemini = FakeGemini(lambda p: "SHOULD NOT BE CALLED")

    # save_lint_result 가 Neo4j 직접 호출 → monkeypatch
    save_calls: List[Dict[str, Any]] = []

    async def fake_save_run(cypher, params=None, database=None):
        save_calls.append({"cypher": cypher, "params": params or {}})
        return [{"saved_id": (params or {}).get("id", "lint-x-1")}]

    monkeypatch.setattr(
        "app.service.lint_repository.neo4j_client.run_cypher", fake_save_run
    )

    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="lt-det")
    result = await run_lint_pipeline(
        ctx,
        LintInput(project_name="p", github_url="https://github.com/owner/repo"),
        github_client=github,  # type: ignore[arg-type]
    )

    # API rule applied=true, evidence 1건
    spack_case = result.cases[0]
    api_rule = spack_case.rules[0]
    assert api_rule.applied is True
    assert api_rule.detection_method == "deterministic"
    assert len(api_rule.evidence) == 1
    assert api_rule.evidence[0].file == "src/tickets.py"

    # SPACK convergence 100 (1/1) but DDD/Arch/Rules/기획 이 empty placeholder 라 0
    assert spack_case.convergence == 100
    # 전체 score = (100 + 0 + 0 + 0 + 0) / 5 = 20
    assert result.score == 20

    # LLM 호출 0회 — empty 카테고리 들은 residual 안 만들고, API 는 deterministic
    assert len(gemini.calls) == 0
    # save 1회
    assert len(save_calls) == 1

    # [Sprint1 1.2] 커버리지 정직화: 두 파일 모두 anchor/token 매칭 → 전체=샘플,
    # 잘림 없음. 저장 파라미터에도 새 필드가 실린다.
    assert result.total_code_files == 2
    assert result.sampled_files == 2
    assert result.coverage_truncated is False
    saved_params = save_calls[0]["params"]
    assert saved_params["total_code_files"] == 2
    assert saved_params["sampled_files"] == 2
    assert saved_params["coverage_truncated"] is False


@pytest.mark.asyncio
async def test_run_lint_e2e_reports_coverage_truncation(monkeypatch):
    """[Sprint1 1.2] 토큰/anchor 와 무관한 코드 파일은 샘플에서 빠진다 →
    sampled_files < total_code_files, coverage_truncated=True 로 노출."""
    apis = [{"id": "API-01", "method": "POST", "endpoint": "/tickets"}]

    blobs = {
        "sha-api": "@app.post('/tickets')\ndef create_ticket():\n    return {}\n",
        # 아래 파일들은 본문이 있어도 경로가 spec 토큰/anchor 와 무관 → 미샘플링.
        "sha-u1": "x = 1\n",
        "sha-u2": "y = 2\n",
    }
    tree = {
        "tree": [
            {"path": "src/tickets.py", "type": "blob", "size": 80, "sha": "sha-api"},
            {"path": "src/unrelated_one.py", "type": "blob", "size": 10, "sha": "sha-u1"},
            {"path": "src/unrelated_two.py", "type": "blob", "size": 10, "sha": "sha-u2"},
        ]
    }
    github = _FakeGitHub(tree_response=tree, blobs=blobs)
    neo = FakeNeo4j(responses=_spec_neo_responses(apis=apis))
    gemini = FakeGemini(lambda p: '{"verdicts": []}')

    async def fake_save_run(cypher, params=None, database=None):
        return [{"saved_id": (params or {}).get("id", "lint-x-1")}]

    monkeypatch.setattr(
        "app.service.lint_repository.neo4j_client.run_cypher", fake_save_run
    )

    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="lt-trunc")
    result = await run_lint_pipeline(
        ctx,
        LintInput(project_name="p", github_url="https://github.com/owner/repo"),
        github_client=github,  # type: ignore[arg-type]
    )

    # 코드 파일 3개 중 토큰 매칭된 1개만 샘플링됨 → 잘림 표기.
    assert result.total_code_files == 3
    assert result.sampled_files < result.total_code_files
    assert result.coverage_truncated is True


@pytest.mark.asyncio
async def test_run_lint_e2e_residual_llm_validates_quote(monkeypatch):
    """deterministic 실패 → LLM 호출 → 인용 검증 후 applied=true."""
    apis = [{"id": "API-01", "method": "POST", "endpoint": "/missing"}]

    # anchor 패턴(index.ts)로 sample 에 포함되도록
    blobs = {
        "sha-x": "// custom router DSL\nhandle('POST', '/missing', myFn);\n",
    }
    tree = {
        "tree": [
            {"path": "src/index.ts", "type": "blob", "size": 60, "sha": "sha-x"},
        ]
    }
    github = _FakeGitHub(tree_response=tree, blobs=blobs)
    neo = FakeNeo4j(responses=_spec_neo_responses(apis=apis))

    # LLM 이 line 2 (handle call) 을 인용하면 hallucination 차단 통과
    llm_response = json.dumps({
        "verdicts": [
            {
                "category_idx": 0, "rule_idx": 0, "applied": True,
                "reason": "custom DSL 로 POST /missing 라우트 정의",
                "evidence_file": "src/index.ts", "evidence_line": 2,
            }
        ]
    })
    gemini = FakeGemini(lambda p: llm_response)

    async def fake_save_run(cypher, params=None, database=None):
        return [{"saved_id": "lint-x-1"}]

    monkeypatch.setattr(
        "app.service.lint_repository.neo4j_client.run_cypher", fake_save_run
    )

    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="lt-llm")
    result = await run_lint_pipeline(
        ctx,
        LintInput(project_name="p", github_url="https://github.com/owner/repo"),
        github_client=github,  # type: ignore[arg-type]
    )

    api_rule = result.cases[0].rules[0]
    assert api_rule.applied is True
    assert api_rule.detection_method == "llm"
    assert api_rule.evidence[0].file == "src/index.ts"
    assert api_rule.evidence[0].line == 2
    assert "handle" in api_rule.evidence[0].snippet

    # LLM 1회 호출
    assert len(gemini.calls) == 1


@pytest.mark.asyncio
async def test_run_lint_e2e_llm_hallucination_rejected(monkeypatch):
    """LLM 이 존재하지 않는 file 인용 → applied=false 강제."""
    apis = [{"id": "A", "method": "POST", "endpoint": "/x"}]

    blobs = {"sha-1": "unrelated content\n"}
    tree = {
        # anchor 'src/index.ts' 로 sample 에 포함 → LLM residual pass 발동
        "tree": [{"path": "src/index.ts", "type": "blob", "size": 30, "sha": "sha-1"}]
    }
    github = _FakeGitHub(tree_response=tree, blobs=blobs)
    neo = FakeNeo4j(responses=_spec_neo_responses(apis=apis))

    # LLM 이 made-up file 인용
    llm_response = json.dumps({
        "verdicts": [
            {
                "category_idx": 0, "rule_idx": 0, "applied": True,
                "reason": "fabricated",
                "evidence_file": "nope/fake.py", "evidence_line": 1,
            }
        ]
    })
    gemini = FakeGemini(lambda p: llm_response)

    async def fake_save_run(cypher, params=None, database=None):
        return [{"saved_id": "lint-x-1"}]

    monkeypatch.setattr(
        "app.service.lint_repository.neo4j_client.run_cypher", fake_save_run
    )

    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="lt-hal")
    result = await run_lint_pipeline(
        ctx,
        LintInput(project_name="p", github_url="https://github.com/owner/repo"),
        github_client=github,  # type: ignore[arg-type]
    )

    api_rule = result.cases[0].rules[0]
    assert api_rule.applied is False
    assert api_rule.detection_method == "llm"
    assert api_rule.evidence == []


@pytest.mark.asyncio
async def test_run_lint_skips_llm_when_no_code_files():
    """README/LICENSE 만 있으면 코드 0개 → 친절한 error."""
    gemini = FakeGemini(lambda p: "SHOULD NOT BE CALLED")
    neo = FakeNeo4j(responses=_spec_neo_responses())
    tree = {
        "tree": [
            {"path": "README.md", "type": "blob"},
            {"path": "LICENSE", "type": "blob"},
        ]
    }
    github = _FakeGitHub(tree_response=tree)
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="lt-empty")

    result = await run_lint_pipeline(
        ctx,
        LintInput(project_name="x", github_url="https://github.com/owner/repo"),
        github_client=github,  # type: ignore[arg-type]
    )
    assert result.score == 0
    assert result.error is not None
    assert "코드 파일" in result.error
    assert len(gemini.calls) == 0


@pytest.mark.asyncio
async def test_run_lint_no_code_error_includes_extension_breakdown_and_onboard_hint():
    """[2026-05-29 UX] 코드 0개 에러에 (a) 발견된 파일 타입 분포 + (b) Onboard 안내
    포함 — 사용자가 "왜 안돼?" 보다 "이 repo 는 문서 위주라 Onboard 가 적합" 파악 가능."""
    gemini = FakeGemini(lambda p: "SHOULD NOT BE CALLED")
    neo = FakeNeo4j(responses=_spec_neo_responses())
    # revfactory/harness 실제 패턴 모사: .md 21 / .yml 4 / .png 4 / 코드 0.
    tree = {
        "tree": (
            [{"path": f"docs/spec-{i}.md", "type": "blob"} for i in range(21)]
            + [{"path": f"ci/.github/workflows/{n}.yml", "type": "blob"} for n in ["build", "test", "lint", "release"]]
            + [{"path": f"assets/diagram-{i}.png", "type": "blob"} for i in range(4)]
            + [{"path": "README.md", "type": "blob"}]
        )
    }
    github = _FakeGitHub(tree_response=tree)
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="lt-docs")

    result = await run_lint_pipeline(
        ctx,
        LintInput(project_name="x", github_url="https://github.com/owner/docs-repo"),
        github_client=github,  # type: ignore[arg-type]
    )

    err = result.error or ""
    # (a) 분포 힌트 — 가장 많은 타입(.md) 가 메시지에 등장
    assert ".md" in err
    # 22개 (docs/spec-0..20.md + README.md). 정확한 표기는 자유롭게.
    assert "22" in err
    # (b) Onboard 안내
    assert "Onboard" in err or "onboard" in err.lower() or "시스템 그리기" in err
    # 핵심 단서 — "코드 파일" 키워드 유지(backward compat).
    assert "코드 파일" in err
    assert len(gemini.calls) == 0


@pytest.mark.asyncio
async def test_run_lint_handles_github_404():
    gemini = FakeGemini(lambda p: "no call")
    neo = FakeNeo4j(responses=_spec_neo_responses())
    github = _FakeGitHub(error=GitHubError("repo not found", status=404))
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="lt-404")

    result = await run_lint_pipeline(
        ctx,
        LintInput(project_name="x", github_url="https://github.com/owner/missing"),
        github_client=github,  # type: ignore[arg-type]
    )
    assert result.score == 0
    assert "not found" in (result.error or "").lower() or "찾을 수 없" in (result.error or "")
    assert len(gemini.calls) == 0


@pytest.mark.asyncio
async def test_run_lint_invalid_github_url_raises():
    gemini = FakeGemini(lambda p: "x")
    neo = FakeNeo4j()
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="lt-url")

    with pytest.raises(GitHubError, match="GitHub URL 파싱"):
        await run_lint_pipeline(
            ctx,
            LintInput(project_name="x", github_url="not-a-url"),
        )


# ─── user_token wiring (private repo / rate-limit 확장) ──────────


@pytest.mark.asyncio
async def test_run_lint_passes_user_token_to_github_client(monkeypatch):
    """run_lint_pipeline 의 user_token 인자가 실제 GitHubClient 생성에 전달되는지.

    기존 e2e 테스트는 FakeGitHub 를 github_client 인자로 직접 주입해서 이 경로를
    안 건드렸음. 여기서는 github_client=None 으로 두고 GitHubClient 가
    user_token 으로 호출되는지 monkeypatch 로 검증.
    """
    captured: dict = {}

    class _CapturingGitHub:
        def __init__(self, *, timeout=30.0, user_token=None):
            captured["user_token"] = user_token

        async def get_repo(self, ident):
            return {"default_branch": "main"}

        async def get_tree(self, ident, ref, recursive=True):
            return {"tree": []}

        async def get_blob_text(self, ident, file_sha, *, max_bytes=32_000):
            return ""

    # lint_pipeline 이 import 한 GitHubClient 심볼 자체를 교체
    monkeypatch.setattr(
        "app.pipelines.lint_pipeline.GitHubClient", _CapturingGitHub
    )

    neo = FakeNeo4j(responses=_spec_neo_responses())
    gemini = FakeGemini(lambda p: "no-call")
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="lt-tok")

    await run_lint_pipeline(
        ctx,
        LintInput(project_name="x", github_url="https://github.com/owner/repo"),
        user_token="ghp_test_token_42",
        enable_residual_llm=False,
        save=False,
    )
    assert captured["user_token"] == "ghp_test_token_42"


@pytest.mark.asyncio
async def test_run_lint_github_client_override_ignores_user_token():
    """github_client 가 명시되면 user_token 은 무시 (테스트 호환)."""
    gemini = FakeGemini(lambda p: "no-call")
    neo = FakeNeo4j(responses=_spec_neo_responses())

    # FakeGitHub 는 user_token 안 받음 — github_client 우선 동작 검증
    github = _FakeGitHub(
        repo_response={"default_branch": "main"},
        tree_response={"tree": []},
    )
    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="lt-ovr")

    result = await run_lint_pipeline(
        ctx,
        LintInput(project_name="x", github_url="https://github.com/owner/repo"),
        github_client=github,  # type: ignore[arg-type]
        user_token="this-should-be-ignored",
        enable_residual_llm=False,
        save=False,
    )
    # 정상 실행 — TypeError 등 안 남
    assert result.score >= 0


# ─── Legacy _normalize_result (backward compat) ────────────────


def test_legacy_normalize_result_clamps_score():
    output = json.dumps({
        "score": 999, "cases": [
            {"title": "SPACK 준수율", "convergence": -10, "rules": []},
        ]
    })
    out = _normalize_result(output, scanned_files=5)
    assert out.score == 100
    assert out.cases[0].convergence == 0


def test_legacy_normalize_result_fallback_on_invalid_json():
    out = _normalize_result("garbage", scanned_files=3)
    assert out.score == 0
    titles = [c.title for c in out.cases]
    assert "SPACK 준수율" in titles
    assert "DDD 준수율" in titles
    assert "Architecture 준수율" in titles
    assert "Rule Generator 준수율" in titles


# ─── 기획 항목 카테고리 (2026-06 — 5번째 케이스) ─────────────────


def test_build_cases_plan_screen_deterministic_applied():
    """Screen 의 route 가 라우터 정의에 있으면 deterministic applied."""
    specs = {
        "spack": {"apis": [], "entities": [], "policies": []},
        "ddd": {"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []},
        "architecture": {"services": [], "databases": []},
        "rules": [],
        "plan": {
            "stories": [],
            "screens": [{"id": "SCR-01", "name": "로그인 화면", "path": "/login"}],
        },
    }
    samples = [FileSample(
        path="src/router.js",
        content="const routes = [\n  { path: '/login', component: Login },\n]\n",
        size=60,
    )]
    cases, residual = _build_cases(specs, samples)
    assert len(cases) == 5
    plan_case = cases[4]
    assert plan_case.title == "기획 항목 구현율"
    screen_rule = plan_case.rules[0]
    assert screen_rule.applied is True
    assert screen_rule.detection_method == "deterministic"
    assert screen_rule.evidence[0].kind == "screen_route"
    # 매칭됐으니 residual 에 없음
    assert all(r["category_idx"] != 4 for r in residual)


def test_build_cases_plan_story_goes_residual_with_description():
    """한국어 Story 는 결정적 매칭 불가 → 전부 residual (LLM 의미 매칭)로,
    description 본문도 extra 로 동봉."""
    specs = {
        "spack": {"apis": [], "entities": [], "policies": []},
        "ddd": {"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []},
        "architecture": {"services": [], "databases": []},
        "rules": [],
        "plan": {
            "stories": [{
                "id": "story_1_1", "name": "사용자가 이메일로 로그인한다",
                "description": "이메일+비밀번호 입력 → JWT 발급",
            }],
            "screens": [],
        },
    }
    cases, residual = _build_cases(specs, [FileSample(path="a.py", content="x\n", size=2)])
    story_rule = cases[4].rules[0]
    assert story_rule.applied is False
    assert story_rule.rule == "story:story_1_1"
    plan_residual = [r for r in residual if r["category_idx"] == 4]
    assert len(plan_residual) == 1
    assert plan_residual[0]["story_description"] == "이메일+비밀번호 입력 → JWT 발급"


def test_compute_score_five_categories_equal_weight():
    cases = [
        LintCase(title="SPACK 준수율", convergence=0, rules=[
            LintCaseRule(rule="a", description="", applied=True),
        ]),  # 100
        LintCase(title="DDD 준수율", convergence=0, rules=[
            LintCaseRule(rule="b", description="", applied=True),
        ]),  # 100
        LintCase(title="Architecture 준수율", convergence=0, rules=[
            LintCaseRule(rule="c", description="", applied=True),
        ]),  # 100
        LintCase(title="Rule Generator 준수율", convergence=0, rules=[
            LintCaseRule(rule="d", description="", applied=True),
        ]),  # 100
        LintCase(title="기획 항목 구현율", convergence=0, rules=[
            LintCaseRule(rule="s1", description="", applied=True),
            LintCaseRule(rule="s2", description="", applied=False),
        ]),  # 50
    ]
    # (100*4 + 50) / 5 = 90
    assert _compute_score(cases) == 90


@pytest.mark.asyncio
async def test_run_lint_e2e_story_verified_by_residual_llm(monkeypatch):
    """한국어 Story → deterministic 0건 → LLM 이 file:line 인용 → applied=true."""
    stories = [{"id": "story_1_1", "name": "사용자가 이메일로 로그인한다",
                "description": "이메일+비밀번호 → JWT"}]

    blobs = {
        "sha-login": "export async function loginWithEmail(email, pw) {\n  return jwt.sign(...)\n}\n",
    }
    tree = {
        "tree": [
            {"path": "src/index.ts", "type": "blob", "size": 90, "sha": "sha-login"},
        ]
    }
    github = _FakeGitHub(tree_response=tree, blobs=blobs)
    neo = FakeNeo4j(responses=_spec_neo_responses(stories=stories))

    llm_response = json.dumps({
        "verdicts": [
            {
                "category_idx": 4, "rule_idx": 0, "applied": True,
                "reason": "loginWithEmail 이 이메일 로그인 + JWT 발급 구현",
                "evidence_file": "src/index.ts", "evidence_line": 1,
            }
        ]
    })
    gemini = FakeGemini(lambda p: llm_response)

    async def fake_save_run(cypher, params=None, database=None):
        return [{"saved_id": "lint-x-1"}]
    monkeypatch.setattr(
        "app.service.lint_repository.neo4j_client.run_cypher", fake_save_run
    )

    ctx = PipelineContext(gemini=gemini, neo4j=neo, idempotency_key="lt-story")
    result = await run_lint_pipeline(
        ctx,
        LintInput(project_name="p", github_url="https://github.com/owner/repo"),
        github_client=github,  # type: ignore[arg-type]
    )

    plan_case = result.cases[4]
    story_rule = plan_case.rules[0]
    assert story_rule.applied is True
    assert story_rule.detection_method == "llm"
    assert story_rule.evidence[0].file == "src/index.ts"
    assert plan_case.convergence == 100
    # LLM 프롬프트에 story 본문(story_description)이 동봉됐는지
    assert any("이메일+비밀번호" in c["prompt"] for c in gemini.calls)


# ─── Policy — LLM 일원화 (2026-06 위양성 차단) ───────────────────


def test_build_cases_policy_always_goes_residual():
    """[2026-06] Policy 는 결정적 토큰 매칭 폐기 — 'audit' 가 주석에 있어도
    applied 가 되던 위양성 차단. 전부 LLM residual 로 (file:line 인용 강제)."""
    specs = {
        "spack": {
            "apis": [], "entities": [],
            "policies": [{"id": "POL-01", "name": "AuditAll", "category": "Audit",
                          "description": "모든 변경에 감사 로그"}],
        },
        "ddd": {"contexts": [], "aggregates": [], "domain_entities": [], "domain_events": []},
        "architecture": {"services": [], "databases": []},
        "rules": [],
    }
    # 예전 토큰 매칭이라면 applied 됐을 본문 — 주석에 'audit' 등장.
    samples = [FileSample(path="logger.py", content="# audit something someday\n", size=30)]
    cases, residual = _build_cases(specs, samples)
    pol_rule = cases[0].rules[0]
    assert pol_rule.applied is False
    assert pol_rule.detection_method == "fallback"
    assert pol_rule.evidence == []
    pol_residual = [r for r in residual if r["rule"].startswith("policy:")]
    assert len(pol_residual) == 1
    assert "토큰 등장은 근거 아님" in pol_residual[0]["hint"]
