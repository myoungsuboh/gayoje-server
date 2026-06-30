"""build_plan 합성 — 회의록(meeting_content) → 구조화 빌드 플랜.

체크포인트 2: 인터뷰 턴 루프와 독립된 별도 기능. 회의록 텍스트만 입력받아
(인터뷰 산출물이든 이미 등록된 미팅 로그든) AI-buildable 플랜 JSON 을 합성한다.
파싱 실패/빈 출력/예외 시 회의록을 보존한 폴백을 반환해 흐름을 깨지 않는다.
"""
from __future__ import annotations

import json

import pytest

from app.pipelines.base import PipelineContext
from app.pipelines.interview import (
    BuildPlan,
    build_graph_summary,
    build_plan_input_hash,
    get_build_plan,
    is_substantive_plan,
    save_build_plan,
    synthesize_build_plan,
)
from app.pipelines.interview.interview import (
    _build_plan_critique,
    _fallback_build_plan,
    _format_graph_relations,
    _format_graph_summary,
    _loads_list,
    _parse_build_plan,
    _refine_build_plan,
    build_plan_quality_score,
)
from tests.conftest import FakeGemini, FakeNeo4j

pytestmark = pytest.mark.asyncio


def _ctx(gemini) -> PipelineContext:
    return PipelineContext(
        gemini=gemini, neo4j=FakeNeo4j(responses=[]), idempotency_key="bp-test"
    )


_VALID = json.dumps(
    {
        "recommended_stack": "Next.js + Supabase — 흔하고 AI가 안정적으로 만듦",
        "scope_now": ["할 일 추가/완료"],
        "scope_later": ["팀 공유"],
        "milestones": ["데이터 모델", "목록 화면", "추가/완료"],
        "acceptance_criteria": ["할 일을 추가하면 목록에 보인다"],
        "risks": ["로그인은 이메일 하나로 단순화"],
        "start_prompt": "할 일 앱을 Next.js+Supabase 로 만들어줘 ...",
    },
    ensure_ascii=False,
)


def test_parse_valid():
    p = _parse_build_plan(_VALID)
    assert p is not None
    assert "Supabase" in p.recommended_stack
    assert p.scope_now == ["할 일 추가/완료"]
    assert len(p.milestones) == 3
    assert p.acceptance_criteria == ["할 일을 추가하면 목록에 보인다"]
    assert p.start_prompt.startswith("할 일 앱")


def test_parse_strips_code_fence():
    p = _parse_build_plan("```json\n" + _VALID + "\n```")
    assert p is not None and p.scope_later == ["팀 공유"]


def test_parse_garbage_returns_none():
    assert _parse_build_plan("그냥 텍스트, JSON 아님") is None
    assert _parse_build_plan("") is None
    assert _parse_build_plan("{잘못된 json,,,") is None


def test_parse_coerces_non_list_fields():
    p = _parse_build_plan(json.dumps({"scope_now": "문자열아님", "milestones": None}))
    assert p is not None
    assert p.scope_now == []  # 비-리스트 → 빈 리스트로 안전 강등
    assert p.milestones == []


async def test_synthesize_returns_parsed_plan_and_injects_meeting():
    gemini = FakeGemini(responses=[_VALID])
    plan = await synthesize_build_plan(_ctx(gemini), "# 프로젝트 개요\n할 일 관리 앱")
    assert plan.recommended_stack.startswith("Next.js")
    assert len(gemini.calls) == 1
    assert "할 일 관리 앱" in gemini.calls[0]["prompt"]  # 회의록이 프롬프트에 주입됨


async def test_synthesize_falls_back_on_bad_output():
    gemini = FakeGemini(responses=["이건 JSON 이 아니에요"])
    plan = await synthesize_build_plan(_ctx(gemini), "# 개요\n동네 책방 앱")
    assert "동네 책방 앱" in plan.start_prompt  # 폴백 — 회의록 보존
    assert plan.milestones == []


def test_fallback_preserves_meeting():
    p = _fallback_build_plan("# 개요\n중고거래 앱")
    assert isinstance(p, BuildPlan)
    assert "중고거래 앱" in p.start_prompt


# ─── 그래프 컨텍스트 (CP5) ──────────────────────────────────────────────

def test_format_graph_summary_full():
    specs = {
        "ddd": {"contexts": [{"name": "주문"}], "aggregates": [{"name": "Order"}], "domain_entities": []},
        "spack": {"entities": [{"name": "Order"}], "apis": [{"method": "post", "endpoint": "/orders"}]},
        "architecture": {"services": [{"name": "order-svc"}]},
    }
    s = _format_graph_summary(specs)
    assert "주문" in s
    assert "Order" in s
    assert "POST /orders" in s
    assert "order-svc" in s


def test_format_graph_summary_is_order_independent():
    # Neo4j collect() 순서가 달라도 같은 그래프면 같은 요약 → build_plan 캐시 해시 안정.
    a = {
        "ddd": {"aggregates": [{"name": "Order"}, {"name": "Cart"}, {"name": "User"}]},
        "spack": {"apis": [{"method": "get", "endpoint": "/a"}, {"method": "post", "endpoint": "/b"}]},
        "architecture": {"services": [{"name": "svc-b"}, {"name": "svc-a"}]},
    }
    b = {  # 동일 그래프, 리스트 순서만 뒤섞음
        "ddd": {"aggregates": [{"name": "User"}, {"name": "Order"}, {"name": "Cart"}]},
        "spack": {"apis": [{"method": "post", "endpoint": "/b"}, {"method": "get", "endpoint": "/a"}]},
        "architecture": {"services": [{"name": "svc-a"}, {"name": "svc-b"}]},
    }
    sa, sb = _format_graph_summary(a), _format_graph_summary(b)
    assert sa == sb  # 순서 무관 동일 요약
    assert build_plan_input_hash("m", sa) == build_plan_input_hash("m", sb)  # → 해시도 동일


def test_format_graph_summary_empty():
    assert _format_graph_summary({}) == ""
    assert _format_graph_summary({"ddd": {}, "spack": {}, "architecture": {}}) == ""


async def test_synthesize_injects_graph_summary():
    gemini = FakeGemini(responses=[_VALID])
    await synthesize_build_plan(_ctx(gemini), "# 개요\n주문앱", graph_summary="- Aggregate: Order")
    prompt = gemini.calls[0]["prompt"]
    assert "Aggregate: Order" in prompt  # 설계 그래프 요약이 프롬프트에 주입됨


async def test_build_graph_summary_no_project_returns_empty():
    # 프로젝트명이 없으면 그래프 조회 없이 즉시 빈 문자열 (neo4j/ lint import 불필요).
    s = await build_graph_summary(_ctx(FakeGemini(responses=["x"])), "")
    assert s == ""


def test_format_graph_summary_non_string_name_safe():
    # name/method 가 비-문자열이어도 .strip()/.upper() 에서 터지지 않아야 (방어적).
    specs = {
        "ddd": {"aggregates": [{"name": 123}, {"name": "Order"}]},
        "spack": {"apis": [{"method": 5, "endpoint": None, "name": "/x"}]},
        "architecture": {},
    }
    s = _format_graph_summary(specs)  # 예외 없이 문자열 반환해야
    assert isinstance(s, str)
    assert "Order" in s


# ─── 그래프 관계 엣지 → build_plan (의존성 위상 주입) ─────────────────────

def _graph(nodes, edges):
    """duck-typed ProjectGraph 스탠드인 (query_repository 무거운 import 회피)."""
    from types import SimpleNamespace
    return SimpleNamespace(
        nodes=[SimpleNamespace(id=i, properties={"name": nm}) for i, nm in nodes],
        edges=[SimpleNamespace(source_id=s, target_id=t, type=ty) for s, t, ty in edges],
    )


def test_format_graph_relations_maps_names_and_filters():
    g = _graph(
        nodes=[("api1", "주문생성API"), ("story1", "주문하기"), ("agg1", "Order"), ("evt1", "OrderPlaced")],
        edges=[("api1", "story1", "IMPLEMENTS"), ("agg1", "evt1", "PUBLISHES"), ("u", "p", "OWNS")],
    )
    s = _format_graph_relations(g)
    assert "주문생성API -[구현]-> 주문하기" in s   # id→이름 매핑 + 한글 라벨
    assert "Order -[발행]-> OrderPlaced" in s
    assert "OWNS" not in s and "u -" not in s      # 화이트리스트 외 엣지 제외
    assert "의존성 관계" in s


def test_format_graph_relations_order_independent():
    # 엣지 순서가 달라도 동일 출력 → 캐시 해시 안정 (B 수정과 동일 원칙).
    n = [("a", "A"), ("b", "B"), ("c", "C")]
    s1 = _format_graph_relations(_graph(n, [("a", "b", "BELONGS_TO"), ("b", "c", "PART_OF")]))
    s2 = _format_graph_relations(_graph(n, [("b", "c", "PART_OF"), ("a", "b", "BELONGS_TO")]))
    assert s1 == s2


async def test_build_graph_summary_combines_nodes_and_relations(monkeypatch):
    # _fetch_specs(노드) + get_project_graph(관계) 결과가 한 요약으로 결합되어야.
    import types as _t
    import app.pipelines.lint_pipeline as _lint
    import app.service.query_repository as _qr
    specs = {
        "ddd": {"aggregates": [{"name": "Order"}]},
        "spack": {"apis": [{"method": "post", "endpoint": "/orders"}]}, "architecture": {},
    }
    g = _t.SimpleNamespace(
        nodes=[_t.SimpleNamespace(id="api1", properties={"name": "주문API"}),
               _t.SimpleNamespace(id="s1", properties={"name": "주문하기"})],
        edges=[_t.SimpleNamespace(source_id="api1", target_id="s1", type="IMPLEMENTS")],
    )

    async def fake_fetch(ctx, name):
        return specs

    async def fake_graph(name, team_id=""):
        return g

    monkeypatch.setattr(_lint, "_fetch_specs", fake_fetch)
    monkeypatch.setattr(_qr, "get_project_graph", fake_graph)
    out = await build_graph_summary(_ctx(FakeGemini(responses=["x"])), "proj")
    assert "Aggregate: Order" in out            # 노드 요약 섹션
    assert "POST /orders" in out
    assert "주문API -[구현]-> 주문하기" in out     # 관계 요약 섹션 결합


def test_format_graph_relations_empty_and_dangling():
    from types import SimpleNamespace
    assert _format_graph_relations(SimpleNamespace(nodes=[], edges=[])) == ""
    # 노드가 없어 양끝 매핑 안 되는 엣지(dangling)는 제외 → 빈 문자열
    dangling = SimpleNamespace(
        nodes=[], edges=[SimpleNamespace(source_id="a", target_id="b", type="IMPLEMENTS")]
    )
    assert _format_graph_relations(dangling) == ""


# ─── build_plan 영속/캐시 (WS-D) ─────────────────────────────────────────

def test_input_hash_stable_and_sensitive():
    h1 = build_plan_input_hash("회의록", "- Aggregate: Order")
    h2 = build_plan_input_hash("회의록", "- Aggregate: Order")
    h3 = build_plan_input_hash("회의록", "- Aggregate: Cart")
    h4 = build_plan_input_hash("다른 회의록", "- Aggregate: Order")
    assert h1 == h2          # 같은 입력 → 같은 해시
    assert h1 != h3          # 그래프 바뀌면 다름
    assert h1 != h4          # 회의록 바뀌면 다름


def test_loads_list_absorbs_shapes():
    assert _loads_list('["a","b"]') == ["a", "b"]   # JSON string
    assert _loads_list(["a", "b"]) == ["a", "b"]    # list
    assert _loads_list(None) == []
    assert _loads_list("") == []
    assert _loads_list("not json") == []


async def test_save_build_plan_writes_merge_and_json():
    neo = FakeNeo4j(responses=[])
    ctx = PipelineContext(gemini=FakeGemini(responses=["x"]), neo4j=neo, idempotency_key="t")
    plan = BuildPlan(recommended_stack="Next.js", scope_now=["a"], milestones=["m1", "m2"], start_prompt="sp")
    await save_build_plan(ctx, "proj", plan, "hash123")
    assert len(neo.executed) == 1
    call = neo.executed[0]
    assert "MERGE (bp:BuildPlan" in call["cypher"]
    p = call["params"]
    assert p["project"] == "proj"
    assert p["input_hash"] == "hash123"
    assert p["scope_now"] == '["a"]'          # list → JSON string 직렬화
    assert p["milestones"] == '["m1", "m2"]'
    assert p["recommended_stack"] == "Next.js"


async def test_get_build_plan_decodes_row():
    row = {
        "recommended_stack": "Next.js", "scope_now": '["a","b"]', "scope_later": "[]",
        "milestones": '["m1"]', "acceptance_criteria": '["ac1"]', "risks": "[]",
        "start_prompt": "sp", "input_hash": "h1",
    }
    neo = FakeNeo4j(responses=[[row]])
    ctx = PipelineContext(gemini=FakeGemini(responses=["x"]), neo4j=neo, idempotency_key="t")
    plan, h = await get_build_plan(ctx, "proj")
    assert h == "h1"
    assert plan is not None
    assert plan.recommended_stack == "Next.js"
    assert plan.scope_now == ["a", "b"]       # JSON string → list 복원
    assert plan.milestones == ["m1"]
    assert plan.acceptance_criteria == ["ac1"]


def test_is_substantive_plan_rejects_fallback():
    # 폴백(_fallback_build_plan): start_prompt 만 채움 → 저장 대상 아님.
    assert is_substantive_plan(BuildPlan(start_prompt="아래 기획대로 만들어줘 ...")) is False
    assert is_substantive_plan(BuildPlan()) is False
    # 실질 필드가 하나라도 있으면 저장 대상.
    assert is_substantive_plan(BuildPlan(recommended_stack="Next.js")) is True
    assert is_substantive_plan(BuildPlan(milestones=["m1"])) is True
    assert is_substantive_plan(BuildPlan(scope_now=["a"])) is True
    assert is_substantive_plan(BuildPlan(acceptance_criteria=["ac1"])) is True


async def test_get_build_plan_missing_returns_none():
    neo = FakeNeo4j(responses=[[]])           # 저장된 노드 없음
    ctx = PipelineContext(gemini=FakeGemini(responses=["x"]), neo4j=neo, idempotency_key="t")
    plan, h = await get_build_plan(ctx, "proj")
    assert plan is None and h == ""


# ─── [P1] build_plan 자기정제(우로보로스) — 점수<임계 시 약점만 1회 재합성 ──────

# 약점 있는 초안(점수 ~0.07): 스택·범위·리스크 비고, 마일스톤 1개, AC 모호.
_WEAK = json.dumps(
    {
        "recommended_stack": "",
        "scope_now": [],
        "scope_later": [],
        "milestones": ["만들기"],
        "acceptance_criteria": ["됨"],
        "risks": [],
        "start_prompt": "짧음",
    },
    ensure_ascii=False,
)

# 현실적 회의록 — _VALID 의 AC/리스크 어휘(추가/목록/보인다/로그인/이메일)를 담아
# grounding 가드를 통과한다(실제 회의록이라면 이 정도 어휘는 들어 있다).
_MEETING = (
    "할 일 관리 앱입니다. 사용자가 할 일을 추가하면 목록에 보이고 완료 체크를 합니다. "
    "로그인은 이메일 하나로 하고 팀 공유는 나중에."
)


async def test_synthesize_no_refine_when_score_high():
    # _VALID 는 점수 0.9 → 추가 LLM 호출 없이 그대로 통과(불필요 재합성 안 함).
    gemini = FakeGemini(responses=[_VALID, _VALID])  # 두 번째 응답은 쓰이면 안 됨
    plan = await synthesize_build_plan(_ctx(gemini), "# 개요\n할 일 앱")
    assert len(gemini.calls) == 1                     # 재합성 호출 없음
    assert plan.recommended_stack.startswith("Next.js")


async def test_synthesize_refines_low_quality_plan():
    # 약한 초안(점수<0.7) → 1회 재합성, 점수 오른 결과 채택.
    gemini = FakeGemini(responses=[_WEAK, _VALID])
    plan = await synthesize_build_plan(_ctx(gemini), _MEETING)
    assert len(gemini.calls) == 2                     # 초안 + 재합성
    assert plan.recommended_stack.startswith("Next.js")   # 개선본 채택
    assert len(plan.milestones) == 3
    # 재합성 프롬프트에 초안 JSON 과 개선 지시가 실렸는지
    refine_prompt = gemini.calls[1]["prompt"]
    assert "현재 초안" in refine_prompt and "개선 지시" in refine_prompt


async def test_synthesize_keeps_draft_when_refine_not_better():
    # 재합성 결과 점수가 더 낮으면 초안 유지(퇴화 차단).
    draft = json.dumps({
        "recommended_stack": "Flask", "scope_now": ["A"], "milestones": ["m1"],
        "acceptance_criteria": [], "risks": [], "start_prompt": "x",
    }, ensure_ascii=False)                              # 점수 ~0.37
    worse = json.dumps({                                # 점수 0 (전부 빈약)
        "recommended_stack": "", "scope_now": [], "milestones": [],
        "acceptance_criteria": [], "risks": [], "start_prompt": "x",
    }, ensure_ascii=False)
    gemini = FakeGemini(responses=[draft, worse])
    plan = await synthesize_build_plan(_ctx(gemini), "# 개요\n앱")
    assert len(gemini.calls) == 2
    assert plan.recommended_stack == "Flask"            # 초안 유지(개선본 폐기)


async def test_synthesize_keeps_draft_when_refine_unparsable():
    # 재합성 출력이 JSON 아님 → 파싱 실패 → 초안 보존(흐름 안전).
    draft = json.dumps({
        "recommended_stack": "Flask", "scope_now": ["A"], "milestones": ["m1"],
        "acceptance_criteria": [], "risks": [], "start_prompt": "x",
    }, ensure_ascii=False)
    gemini = FakeGemini(responses=[draft, "이건 JSON 이 아니에요"])
    plan = await synthesize_build_plan(_ctx(gemini), "# 개요\n앱")
    assert plan.recommended_stack == "Flask"


async def test_synthesize_refines_when_acceptance_empty_despite_threshold():
    # 점수=0.70(임계)이어도 완료기준(AC)이 텅 비면 정제 대상 — 0.70 사각 보정.
    draft = json.dumps({
        "recommended_stack": "Next.js", "scope_now": ["로그인"],
        "milestones": ["m1", "m2", "m3"], "acceptance_criteria": [],
        "risks": ["인증은 단순화"],
        "start_prompt": "이 프로젝트를 흔한 웹 스택으로 작은 단위부터 순서대로 만들어줘 충분히 긴 지시문",
    }, ensure_ascii=False)                              # 점수 정확히 0.70, acceptance=0
    gemini = FakeGemini(responses=[draft, _VALID])      # 개선본은 AC 포함
    plan = await synthesize_build_plan(_ctx(gemini), _MEETING)
    assert len(gemini.calls) == 2                       # AC 비어 정제 발동(임계여도)
    assert len(plan.acceptance_criteria) >= 1           # 완료기준이 채워짐


async def test_synthesize_rejects_ungrounded_refinement():
    # [환각 가드] 재합성이 회의록에 전혀 없는 내용(결제/환불)만 지어내면 폐기·draft 유지.
    draft = json.dumps({
        "recommended_stack": "Next.js", "scope_now": ["할 일 추가"],
        "milestones": ["데이터 모델", "목록 화면"], "acceptance_criteria": [],
        "risks": [],
        "start_prompt": "할 일 앱을 흔한 웹 스택으로 작은 단위부터 순서대로 만들어줘 충분히 길게",
    }, ensure_ascii=False)                              # 점수<0.7 + AC 비어 정제 발동
    fabricated = json.dumps({                           # 점수는 높지만 회의록과 무관
        "recommended_stack": "Next.js", "scope_now": ["할 일 추가"],
        "milestones": ["데이터 모델", "목록 화면"],
        "acceptance_criteria": ["결제하면 영수증이 발급된다", "환불 신청하면 정산된다"],
        "risks": ["PG 연동 실패 대비"],
        "start_prompt": "할 일 앱을 흔한 웹 스택으로 작은 단위부터 순서대로 만들어줘 충분히 길게",
    }, ensure_ascii=False)
    gemini = FakeGemini(responses=[draft, fabricated])
    plan = await synthesize_build_plan(_ctx(gemini), _MEETING)
    assert len(gemini.calls) == 2                       # 정제 시도는 함
    assert plan.acceptance_criteria == []               # 날조 거부 → draft 유지(AC 빈 채)
    assert "결제하면 영수증이 발급된다" not in plan.acceptance_criteria


_GRAPH = (
    "- Aggregate: Order, Cart\n- 핵심 엔티티: 주문, 장바구니\n"
    "- API: POST /orders, GET /cart\n- 서비스: order-svc"
)


async def test_synthesize_refines_when_graph_ignored():
    # [Q2] brownfield: 초안이 설계 그래프를 무시하면 base 점수가 높아도(정합 트리거)
    # 정제 발동, 그래프 이름을 쓰는 정렬본을 채택한다.
    meeting = "주문/장바구니 도메인을 보강하려고 해요. 주문 흐름과 장바구니를 다듬고 싶어요."
    ignore = json.dumps({                               # 그래프 무시(할일앱) → align 0
        "recommended_stack": "Next.js", "scope_now": ["할 일 추가"],
        "milestones": ["데이터 모델", "목록 화면", "완료 체크"],
        "acceptance_criteria": ["할 일을 추가하면 목록에 보인다"],
        "risks": ["로그인 단순화"],
        "start_prompt": "할 일 앱을 흔한 웹 스택으로 작은 단위부터 순서대로 만들어줘 충분히 긴 지시문",
    }, ensure_ascii=False)
    aligned = json.dumps({                              # 그래프 이름 사용 → align↑
        "recommended_stack": "Next.js", "scope_now": ["주문 생성"],
        "milestones": ["Order 데이터 모델", "장바구니 Cart 화면", "주문 POST API"],
        "acceptance_criteria": ["주문하면 Order가 저장된다", "장바구니에 담으면 Cart에 추가된다"],
        "risks": ["결제 단순화"],
        "start_prompt": "Order/Cart 도메인을 order-svc 위에 작은 단위부터 만들어줘 충분히 긴 지시문",
    }, ensure_ascii=False)
    gemini = FakeGemini(responses=[ignore, aligned])
    plan = await synthesize_build_plan(_ctx(gemini), meeting, graph_summary=_GRAPH)
    assert len(gemini.calls) == 2                       # 정합 낮아 정제 발동(점수 0.8여도)
    assert "Order" in " ".join(plan.milestones)         # 그래프 정렬본 채택


async def test_synthesize_rejects_fabricated_milestones():
    # [QA] 재합성이 회의록과 무관한 마일스톤만 날조해 점수를 올려도 폐기(grounding 확장).
    meeting = "할 일 관리 앱. 할 일을 추가하면 목록에 보이고 완료 체크를 합니다."
    draft = json.dumps({                                # 점수 0.667 (<0.7) → 정제 트리거
        "recommended_stack": "Next.js", "scope_now": ["할 일 추가"],
        "milestones": ["데이터 모델"],
        "acceptance_criteria": ["할 일을 추가하면 목록에 보인다"],
        "risks": [], "start_prompt": "짧은 지시문",
    }, ensure_ascii=False)
    fabricated = json.dumps({                           # 마일스톤만 회의록 무관 날조로 점수↑
        "recommended_stack": "Next.js", "scope_now": ["할 일 추가"],
        "milestones": ["외계인 통신 모듈", "블록체인 채굴기", "양자 컴퓨터 연동"],
        "acceptance_criteria": ["할 일을 추가하면 목록에 보인다"],
        "risks": [], "start_prompt": "짧은 지시문",
    }, ensure_ascii=False)
    gemini = FakeGemini(responses=[draft, fabricated])
    plan = await synthesize_build_plan(_ctx(gemini), meeting)
    assert len(gemini.calls) == 2                       # 정제 시도는 함
    assert "외계인 통신 모듈" not in plan.milestones      # 날조 마일스톤 거부 → draft 유지
    assert plan.milestones == ["데이터 모델"]


async def test_synthesize_evolve_converges_early():
    # [다세대 evolve] 재합성이 직전 세대와 구조적으로 거의 같으면(유사도 수렴) 점수가
    # 임계 미만이어도 남은 세대를 낭비하지 않고 조기 종료한다.
    draft = json.dumps({
        "recommended_stack": "", "scope_now": ["할 일 추가"],
        "milestones": ["데이터 모델"], "acceptance_criteria": ["할 일을 추가하면 목록에 보인다"],
        "risks": [], "start_prompt": "짧음",
    }, ensure_ascii=False)                                  # score 0.517 <0.7 → 정제 트리거
    gen1 = json.dumps({                                     # 구조 동일 + stack만 → score 0.667, sim=1.0
        "recommended_stack": "Next.js", "scope_now": ["할 일 추가"],
        "milestones": ["데이터 모델"], "acceptance_criteria": ["할 일을 추가하면 목록에 보인다"],
        "risks": [], "start_prompt": "짧음",
    }, ensure_ascii=False)
    gen2 = json.dumps({                                     # 더 개선되지만 — 수렴으로 소비 안 됨
        "recommended_stack": "Next.js", "scope_now": ["할 일 추가"],
        "milestones": ["데이터 모델"], "acceptance_criteria": ["할 일을 추가하면 목록에 보인다"],
        "risks": ["로그인은 이메일 하나로 단순화"],
        "start_prompt": "이건 마흔 자가 넘는 충분히 긴 시작 프롬프트입니다 그래서 통과해요",
    }, ensure_ascii=False)
    gemini = FakeGemini(responses=[draft, gen1, gen2])
    plan = await synthesize_build_plan(_ctx(gemini), _MEETING)
    assert len(gemini.calls) == 2                           # draft + gen1, 수렴으로 gen2 미소비
    assert plan.recommended_stack == "Next.js"             # gen1 채택
    assert plan.risks == []                                # gen2(개선본) 적용 안 됨


def test_build_plan_critique_targets_weak_only():
    # 강한 플랜 → 지적 없음("")
    strong = BuildPlan(
        recommended_stack="Next.js", scope_now=["a"],
        milestones=["m1", "m2", "m3"],
        acceptance_criteria=["추가하면 목록에 보인다"], risks=["r"],
        start_prompt="x" * 50,
    )
    _, sb = build_plan_quality_score(strong)
    assert _build_plan_critique(sb) == ""
    # 약한 플랜 → 빈 항목을 콕 집어 지적
    weak = BuildPlan(milestones=["하나"], acceptance_criteria=["됨"])
    _, wb = build_plan_quality_score(weak)
    crit = _build_plan_critique(wb)
    assert "recommended_stack" in crit and "risks" in crit and "마일스톤" in crit
