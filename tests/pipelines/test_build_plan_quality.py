"""build_plan 품질 점수 eval (약점 #2 — 객관 측정).

LLM judge 없이 build_plan 산출물의 구체성을 정량화하고, 품질이 다른 플랜들이
점수로 '올바른 순위'를 갖는지 검증한다 — 품질 회귀 잠금 + 전후 비교의 객관 proxy.

[한계] 결정적 휴리스틱 — '에이전트가 바로 쓸 구체성'의 proxy이지 LLM judge 의
의미 평가가 아니다. 라이브 LLM judge 는 키 확보 시 이 위에 얹는다.
"""
from __future__ import annotations

from app.pipelines.interview import BuildPlan, build_plan_quality_score
from app.pipelines.interview.interview import _fallback_build_plan, _ac_is_specific


_GOOD = BuildPlan(
    recommended_stack="Next.js + Supabase (흔하고 안정적, AI가 끝까지 만들기 쉬움)",
    scope_now=["이메일 회원가입/로그인", "글 작성·목록", "본인 글만 수정/삭제"],
    scope_later=["댓글", "이미지 업로드"],
    milestones=["데이터 모델·인증", "글 CRUD", "권한·배포"],
    acceptance_criteria=[
        "이메일·비번으로 가입·로그인하면 본인 글만 보인다",
        "남의 글 수정 시도하면 403으로 막힌다",
        "글 작성 후 목록 맨 위에 표시된다",
    ],
    risks=["인증은 Supabase Auth로 단순화", "이미지 업로드는 1차 제외"],
    start_prompt="아래 기획대로 Next.js+Supabase로, 데이터 모델→글 CRUD→권한 순으로 작은 단위로 만들어줘. 각 단계 동작 확인하며 진행.",
)

_MEDIOCRE = BuildPlan(
    recommended_stack="React",
    scope_now=["로그인", "글"],
    milestones=["만들기"],
    acceptance_criteria=["로그인 된다", "잘 동작한다"],  # 모호 — 관찰 불가
    start_prompt="만들어줘",
)


def test_quality_ranks_good_above_mediocre_above_fallback():
    good, _ = build_plan_quality_score(_GOOD)
    mediocre, _ = build_plan_quality_score(_MEDIOCRE)
    fallback, _ = build_plan_quality_score(_fallback_build_plan("# 개요\n할 일 앱"))
    assert good > mediocre > fallback     # 핵심: 품질 차이를 순위로 구분
    assert good >= 0.85                    # 충실한 플랜은 높은 점수
    assert fallback <= 0.15                # 폴백(start_prompt만)은 낮은 점수


def test_quality_breakdown_components():
    score, b = build_plan_quality_score(_GOOD)
    assert b["stack"] == 1.0
    assert b["scope"] == 1.0               # 3개 (1~7 집중)
    assert b["milestones"] == 1.0          # 3개 → 만점
    assert b["acceptance"] == 1.0          # 전부 관찰 가능
    assert b["risks"] == 1.0
    assert b["start_prompt"] == 1.0


def test_acceptance_specificity_distinguishes_observable():
    assert _ac_is_specific("이메일로 가입하면 본인 글만 보인다") is True
    assert _ac_is_specific("결제는 3회까지 재시도한다") is True   # 숫자
    assert _ac_is_specific("로그인 된다") is False                # 너무 짧음·모호
    assert _ac_is_specific("") is False


def test_acceptance_vacuous_phrases_rejected():
    # 굿하트 차단: 동사 어미('한다')만 맞춘 공허한 문장은 길이가 충분해도 비관찰.
    assert _ac_is_specific("모든 기능이 정상 작동한다") is False      # '정상 작동'
    assert _ac_is_specific("사용자가 알아서 잘 쓰게 한다") is False    # '알아서'/'잘 쓰'
    assert _ac_is_specific("서비스가 멋지게 동작하게 한다") is False   # '멋지게'
    assert _ac_is_specific("모든 화면이 원활하게 표시된다") is False   # '원활'(표시 마커여도)
    # 대조: 구체적 관찰 문장은 여전히 통과
    assert _ac_is_specific("결제하면 영수증 화면으로 이동한다") is True


def test_scope_sprawl_penalized():
    sprawl = BuildPlan(recommended_stack="X", scope_now=[f"f{i}" for i in range(12)])
    _, b = build_plan_quality_score(sprawl)
    assert b["scope"] == 0.5               # 8개 이상 → 집중 못 함, 감점


def test_empty_plan_scores_zero():
    score, b = build_plan_quality_score(BuildPlan())
    assert score == 0.0
    assert all(v == 0.0 for v in b.values())


# ─── [Q2] 진행중 프로젝트(brownfield) 그래프 정합 차원 ──────────────────────

_GRAPH_SUMMARY = (
    "- Aggregate: Order, Cart\n- 핵심 엔티티: 주문, 장바구니\n"
    "- API: POST /orders, GET /cart\n- 서비스: order-svc"
)


def test_extract_graph_names_parses_name_lines_only():
    from app.pipelines.interview.interview import _extract_graph_names
    names = _extract_graph_names(_GRAPH_SUMMARY)
    assert "Order" in names and "주문" in names and "order-svc" in names
    # 관계·완성도 라인은 이름으로 취급 안 함
    assert _extract_graph_names("- 의존성 관계:\n  · A -[구현]-> B\n- 설계 완성도: 50%") == []
    assert _extract_graph_names("") == []


def test_graph_alignment_penalizes_ignoring_design():
    # 그래프 이름을 쓰는 플랜은 정합↑, 무시하는 플랜은 정합 0 + 점수 하락(이전엔 만점 가능).
    aligned = BuildPlan(
        recommended_stack="Next.js", scope_now=["주문 생성"],
        milestones=["Order 모델", "장바구니 화면", "주문 API"],
        acceptance_criteria=["주문하면 Order가 저장된다"], risks=["결제 단순화"],
        start_prompt="Order Cart 도메인을 order-svc 위에 작은 단위부터 만들어줘 충분히 길게",
    )
    ignore = BuildPlan(
        recommended_stack="Next.js", scope_now=["할 일 추가"],
        milestones=["데이터 모델", "목록 화면", "완료 체크"],
        acceptance_criteria=["할 일을 추가하면 목록에 보인다"], risks=["로그인 단순화"],
        start_prompt="할 일 앱을 흔한 웹 스택으로 작은 단위부터 순서대로 만들어줘 충분히 길게",
    )
    sa, ba = build_plan_quality_score(aligned, _GRAPH_SUMMARY)
    si, bi = build_plan_quality_score(ignore, _GRAPH_SUMMARY)
    assert ba["graph_align"] > 0.5
    assert bi["graph_align"] == 0.0
    assert sa > si                  # 정렬 플랜이 더 높은 점수
    assert si < 1.0                 # 그래프 무시 → 만점 불가


def test_graph_alignment_greenfield_backward_compatible():
    # graph_summary 없거나 빈 문자열이면 graph_align 차원 없음 + 점수 동일(후방호환).
    s_default, b_default = build_plan_quality_score(_GOOD)
    s_empty, b_empty = build_plan_quality_score(_GOOD, "")
    assert "graph_align" not in b_default and "graph_align" not in b_empty
    assert s_default == s_empty


def test_extract_graph_names_strips_http_method():
    # [QA Fix B] API 라인은 HTTP 메서드 떼고 path 만 → get/post 동사 거짓양성 차단.
    from app.pipelines.interview.interview import _extract_graph_names, _graph_alignment
    names = _extract_graph_names("- API: GET /orders, POST /payments")
    assert names == ["/orders", "/payments"]
    ignore = BuildPlan(
        start_prompt="users can get and post and put things in this generic todo app freely",
    )
    assert _graph_alignment(ignore, names) == 0.0  # 동사 평문 써도 정합 0


def test_acceptance_short_quantitative_passes():
    # [QA Fix C] 길이 바닥보다 신호 검사 먼저 — 짧아도 숫자(정량)면 관찰가능.
    assert _ac_is_specific("3초 이내에 응답한다") is True   # 11자지만 숫자
    assert _ac_is_specific("로그인 된다") is False           # 짧고 신호 없음


def test_graph_alignment_empty_names_no_crash():
    # [QA Fix D] names 비면 0.0 자기방어 (ZeroDivision 트랩 제거).
    from app.pipelines.interview.interview import _graph_alignment
    assert _graph_alignment(BuildPlan(milestones=["x"]), []) == 0.0


def test_build_plan_similarity_metric():
    # [다세대 evolve] 세대 간 구조 유사도 — 동일=1.0(수렴), 무관≈0, 둘다빔=1.0.
    from app.pipelines.interview.interview import _build_plan_similarity
    a = BuildPlan(scope_now=["주문 생성", "결제 처리"], milestones=["Order 모델", "결제 연동"],
                  acceptance_criteria=["주문하면 저장된다"])
    same = BuildPlan(scope_now=["주문 생성", "결제 처리"], milestones=["Order 모델", "결제 연동"],
                     acceptance_criteria=["주문하면 저장된다"])
    diff = BuildPlan(scope_now=["할 일 추가"], milestones=["목록 화면"],
                     acceptance_criteria=["추가하면 목록에 보인다"])
    assert _build_plan_similarity(a, same) == 1.0     # 구조 동일 → 수렴
    assert _build_plan_similarity(a, diff) < 0.3      # 무관 → 낮음
    assert _build_plan_similarity(BuildPlan(), BuildPlan()) == 1.0  # 둘 다 비면 1.0
