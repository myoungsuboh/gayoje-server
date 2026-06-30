"""AI 인터뷰 턴 처리 — 대화 history → 다음 질문(ask) 또는 회의록 합성(synthesize).

[모델 선택 — gemini-2.5-flash 고정 (Pro 아님, lite 아님)]
인터뷰의 두 작업(질문하기 / 회의록 합성)은 "기존 회의록을 읽고 뭘 만들려는지
이해해 빠진 곳을 묻고, 대화를 구조화 요약"하는 일이다. 이는 프론티어급 추론이
아니라 구조화된 대화 분석 → gemini-2.5-flash 로 충분히 똑똑하다.
  - Pro: 과하고 토큰당 단가가 높아(대략 5~8배) 다회 턴 인터뷰엔 비용 부담.
  - flash-lite: 분석이 부실해 질문이 산으로 감 → 제외.
  - flash(2.5): 능력 충분 + 저렴 + 빠름 → 인터뷰는 구독 등급과 무관하게 이걸로 고정.
추가로 질문 프롬프트(phase_interview.md)에서 회의록 템플릿을 떼어내 슬림화하고
합성을 별도 phase_synthesize.md 로 분리해 매 턴 토큰을 더 줄였다.

Neo4j write 없음. 라우트의 tracked_pipeline_context 가 토큰 누적을 담당한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, List, Literal, Optional, Tuple

from app.pipelines.base import PipelineContext
from app.pipelines.guide import GuidePhase, compose_prompt, compose_with_safety

logger = logging.getLogger(__name__)

# 인터뷰는 짧고 빈번 → 비용/지연 최소화 위해 가벼운 호출.
_TEMPERATURE = 0.4
# 폭주 방지: 한 인터뷰가 무한히 길어지지 않도록 서버 측에서도 상한.
_MAX_TURNS = 12
# [T2] 정량 done 게이트: 준비도(가중합)가 이 값 이상이고 최소 턴을 채웠을 때만 done 허용.
# OMC 모호성 임계값(ambiguity 0.2 ≈ readiness 0.8) 과 동급. 운영에서 이 상수만 조정.
_READINESS_DONE_THRESHOLD = 0.8
# 조기 종료 방지 — 사용자가 최소 이만큼 답한 뒤에야 done 가능 (프롬프트와 일치).
_MIN_USER_TURNS = 3
# [T9] 정체 완화 — 이 턴 수를 넘기면 done 임계를 낮춰(0.8→0.6) "있는 걸로" 마무리.
# 모호한 대화가 하드캡(12)까지 끌리지 않게. 사다리: min 3 → soft 7(완화) → hard 12(강제).
_SOFT_CAP_TURNS = 7
_READINESS_SOFT_THRESHOLD = 0.6
# [객관 보정] 자가보고 readiness 를 '설계 그래프 완성도'로 최대 이만큼 감쇠한다.
# greenfield(그래프 없음)는 완성도 1.0 → 감쇠 0(기존 동작 유지), brownfield 미완성만 보수적.
_GRAPH_GROUNDING_WEIGHT = 0.4
# [2026-06-12 보강(supplement) 모드 — 진행 중 프로젝트의 부족분 채우기]
# 핵심 5개(정의·문제·사용자·기능·데이터)는 프로젝트 현황(브리프)으로 이미 충족이라
# greenfield 의 최소 3턴을 강제하면 "이미 아는 걸 또 묻는" 동문서답이 된다(실사고:
# '부족한 부분 채워줘' → '무슨 앱 만드세요?' 반복). 의제 1~2개면 1턴에도 끝나야 자연.
_MIN_USER_TURNS_SUPPLEMENT = 1
# 브리프(프로젝트 현황) 길이 캡 — 매 턴 프롬프트에 실리므로 토큰 폭주 방지.
# [Phase 1 — 2026-06-12] 1,000 → 2,400: 회의록 발췌 + 설계 평가 라인이 추가됨.
# (2.4k자 ≈ 1k 토큰 — flash 단가에서 턴당 무시 가능, 동문서답 비용이 훨씬 큼.)
_MAX_BRIEF_CHARS = 2_400
# 회의록 발췌 캡 — get_all_meeting_content 는 시간순 join 이므로 꼬리(최신)를 취한다.
_MAX_MEETING_EXCERPT_CHARS = 600
# 의제(브리프 모드 질문 거리) 총량 캡 — agenda(FE) + PRD lint + 그래프 갭 병합 후.
_MAX_AGENDA_ITEMS = 10
# 인터뷰 전용 모델 — 구독 등급(Pro 등)과 무관하게 flash 로 고정. lite 는 분석 부실로 제외.
# (LiteLLM 등록된 기본 모델이라 안전. 운영에서 더 싸/비싼 모델로 바꾸려면 이 상수만 변경.)
_INTERVIEW_MODEL = "gemini-2.5-flash"
# [2026-06] 빌드플랜(JSON) 출력 상한 — 회의록을 요약·분해한 JSON 이라 4096 토큰이면
# 충분하고, 혹 넘쳐 잘려도 파싱 실패 시 폴백(회의록 보존)이 받아준다.
# [주의] 회의록 '합성'에는 상한을 두지 않는다 — 보완 인터뷰는 기존 초안(최대 20k자)을
#   그대로 보존·병합하므로 출력이 길어, 상한을 걸면 초안이 잘려 유실된다. 질문 턴도 미설정.
_BUILD_PLAN_MAX_OUTPUT_TOKENS = 4096
# [P1] build_plan 자기정제(우로보로스) — 합성물을 객관 적합도 점수(build_plan_quality_score)로
# 비판해 약한 항목만 재합성한다. 점수가 이 값 이상이면 추가 LLM 호출 없이 그대로 통과(이미
# 충분). 재합성 결과는 점수가 '오를 때만' 채택하고, 정체/하락하면 즉시 중단 → 자기루프 퇴화
# (모델붕괴) 차단. 캐시 미스(신규 합성) 경로에서만 도므로 input_hash 캐시 이점을 해치지 않는다.
_BUILD_PLAN_REFINE_THRESHOLD = 0.7
# [다세대 evolve] ouroboros 의 빠진 축(세대 간 구조 유사도 수렴)을 복원. 단발(1)이 아니라
# 최대 3세대까지 굴리되, 세대 간 유사도가 이 값 이상이면 '수렴'(더 굴려도 안 변함)으로 보고
# 조기 종료해 평균 비용을 단발 수준으로 묶는다. 점수 충족/정체/진동/grounding 실패도 즉시 종료.
# (유사도는 LLM 0회 순수함수. 임계 0.85 는 휴리스틱 — ouroboros 그래프용 0.95 직접 차용 금지.)
_BUILD_PLAN_REFINE_MAX_PASSES = 3
_BUILD_PLAN_CONVERGE_SIM = 0.85

_ROLE_USER = "user"
_ROLE_ASSISTANT = "assistant"

_SYNTHESIZE_PROMPT_FILE = "phase_synthesize.md"
# 회의록 → AI-buildable 빌드 플랜(JSON) 합성. 인터뷰 턴 루프와 독립 — 회의록만
# 입력받으므로 인터뷰 산출물이든 이미 등록된 미팅 로그든 동일하게 적용 가능.
_BUILD_PLAN_PROMPT_FILE = "phase_build_plan.md"
# [P1] 자기정제 재합성 프롬프트 — 초안(JSON) + 객관 점수 기반 critique 를 받아 약점만 보강.
_BUILD_PLAN_REFINE_PROMPT_FILE = "phase_build_plan_refine.md"


@dataclass
class BuildPlan:
    """회의록에서 뽑은, 에이전트가 바로 쓸 수 있는 구체적 빌드 플랜."""

    recommended_stack: str = ""
    scope_now: List[str] = field(default_factory=list)
    scope_later: List[str] = field(default_factory=list)
    milestones: List[str] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    start_prompt: str = ""


def _build_plan_prompt(meeting_content: str, graph_summary: str = "") -> str:
    """안전 프리앰블 + phase_build_plan.md (회의록 + 설계 그래프 치환)."""
    return compose_with_safety(
        _BUILD_PLAN_PROMPT_FILE,
        variables={
            "{{MEETING}}": (meeting_content or "").strip() or "(내용 없음)",
            "{{GRAPH}}": (graph_summary or "").strip() or "(없음 — 아직 설계 그래프가 없습니다)",
        },
    )


def _parse_build_plan(text: str) -> Optional[BuildPlan]:
    """LLM JSON 출력을 BuildPlan 으로 파싱. 실패 시 None (호출부가 폴백 처리).

    코드펜스를 벗기고 가장 바깥 {..} 만 취해 json.loads — 모델이 앞뒤로 군말을
    붙여도 견딘다. 비-리스트 필드는 빈 리스트로 안전 강등.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    def _strlist(v: Any) -> List[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()]

    return BuildPlan(
        recommended_stack=str(data.get("recommended_stack", "")).strip(),
        scope_now=_strlist(data.get("scope_now")),
        scope_later=_strlist(data.get("scope_later")),
        milestones=_strlist(data.get("milestones")),
        acceptance_criteria=_strlist(data.get("acceptance_criteria")),
        risks=_strlist(data.get("risks")),
        start_prompt=str(data.get("start_prompt", "")).strip(),
    )


def _fallback_build_plan(meeting_content: str) -> BuildPlan:
    """합성 실패 시 — 회의록을 보존한 최소 start_prompt 로 흐름을 지킨다."""
    text = (meeting_content or "").strip()
    if text:
        sp = (
            "아래 기획대로 만들어줘. 흔하고 안정적인 평범한 웹 스택으로, "
            "작은 단위부터 순서대로 구현하고 각 단계가 동작하는지 확인하면서 진행해.\n\n"
            f"{text}"
        )
    else:
        sp = "만들고 싶은 기획 내용을 알려줘."
    return BuildPlan(start_prompt=sp)


async def synthesize_build_plan(
    ctx: PipelineContext, meeting_content: str, *, graph_summary: str = ""
) -> BuildPlan:
    """회의록 → BuildPlan 합성 (flash 1회). 실패·빈 출력·예외 시 폴백 반환.

    인터뷰 턴 루프와 분리 — done 흐름을 건드리지 않고, 사용자가 빌드/번들로
    넘어갈 때(또는 이미 등록된 미팅 로그에 대해) 호출한다.

    graph_summary: 프로젝트의 기존 설계 그래프 요약(build_graph_summary). 주면
    플랜이 그 Aggregate·엔티티·API 에 정렬되어 품질이 올라간다.
    """
    try:
        result = await ctx.gemini.generate(
            _build_plan_prompt(meeting_content, graph_summary),
            temperature=_TEMPERATURE,
            model=_INTERVIEW_MODEL,
            max_output_tokens=_BUILD_PLAN_MAX_OUTPUT_TOKENS,
        )
        plan = _parse_build_plan(result.text)
        if plan is not None:
            # [P1] 객관 점수가 임계 미만이면 약점만 1회 재합성(점수 오를 때만 채택).
            # _refine_build_plan 은 어떤 실패에도 draft 를 보존해 반환 — 흐름 안전.
            return await _refine_build_plan(ctx, plan, meeting_content, graph_summary)
        logger.warning("interview: build_plan 파싱 실패 — 폴백 사용")
    except Exception:  # noqa: BLE001 — 합성 실패가 사용자 흐름을 깨지 않게
        logger.exception("interview: build_plan 합성 실패 — 폴백 사용")
    return _fallback_build_plan(meeting_content)


def _format_graph_summary(specs: dict) -> str:
    """_fetch_specs 결과 → 빌드 플랜 프롬프트용 간결 이름 요약. 비면 ''.

    (순수 함수 — neo4j 불필요, 단위 테스트 용이)
    """
    ddd = (specs or {}).get("ddd") or {}
    spack = (specs or {}).get("spack") or {}
    arch = (specs or {}).get("architecture") or {}

    def _names(items, cap=15):
        out = []
        for it in items or []:
            if isinstance(it, dict):
                nm = str(it.get("name") or "").strip()
                if nm:
                    out.append(nm)
        # [결정성] Neo4j collect() 는 순서 미보장 → 정렬 후 cap. 같은 그래프가 항상
        # 같은 요약을 내야 build_plan 캐시 해시가 안정적으로 일치(불필요 재합성 방지).
        return sorted(out)[:cap]

    contexts = _names(ddd.get("contexts"))
    aggregates = _names(ddd.get("aggregates"))
    entities = _names(spack.get("entities")) or _names(ddd.get("domain_entities"))
    services = _names(arch.get("services"))
    apis = []
    for a in (spack.get("apis") or []):
        if isinstance(a, dict):
            method = str(a.get("method") or "").upper()
            path = str(a.get("endpoint") or a.get("name") or "")
            ep = f"{method} {path}".strip()
            if ep:
                apis.append(ep)
    apis = sorted(apis)[:20]  # [결정성] 정렬 후 cap — collect 순서 무관하게 안정

    lines = []
    if contexts:
        lines.append(f"- 도메인(Bounded Context): {', '.join(contexts)}")
    if aggregates:
        lines.append(f"- Aggregate: {', '.join(aggregates)}")
    if entities:
        lines.append(f"- 핵심 엔티티: {', '.join(entities)}")
    if apis:
        lines.append(f"- API: {', '.join(apis)}")
    if services:
        lines.append(f"- 서비스: {', '.join(services)}")
    return "\n".join(lines)


# 빌드 순서에 의미 있는 관계만 화이트리스트 (OWNS/CONTAINS 등 노이즈 제외).
# value = 프롬프트에 노출할 한글 라벨.
_BUILD_RELEVANT_EDGES: dict = {
    "IMPLEMENTS": "구현",    # API → Story
    "BELONGS_TO": "소속",    # Entity → Aggregate 등
    "PART_OF": "구성",
    "PUBLISHES": "발행",     # Aggregate → DomainEvent
    "TRIGGERS": "트리거",    # Story → DomainEvent
    "CONNECTS_TO": "연동",   # Service → Service (통합 지점)
    "HANDLED_BY": "처리",    # API → Service
    "MAPPED_TO": "매핑",
}


def _format_graph_relations(graph: Any, cap: int = 30) -> str:
    """ProjectGraph(nodes, edges) → 빌드 순서에 의미 있는 '의존성 관계' 요약.

    노드 id→이름 매핑 후 화이트리스트 엣지만 'src -[관계]-> tgt' 로 풀어, milestone/
    scope 가 그래프 위상(기초 Aggregate → API → 연동)을 따르게 LLM 에 힌트를 준다.
    collect 순서와 무관하게 정렬·캡 → 같은 그래프면 같은 출력(캐시 해시 안정).
    순수 함수(neo4j 불필요) — duck-typing 으로 GraphNode/GraphEdge 또는 동형 객체 수용.
    """
    nodes = getattr(graph, "nodes", None) or []
    edges = getattr(graph, "edges", None) or []

    id_to_name: dict = {}
    for n in nodes:
        nid = str(getattr(n, "id", "") or "")
        if not nid:
            continue
        props = getattr(n, "properties", None) or {}
        nm = str(props.get("name") or "").strip()
        id_to_name[nid] = nm or nid  # 이름 없으면 id 로 폴백

    lines = set()
    for e in edges:
        label = _BUILD_RELEVANT_EDGES.get(str(getattr(e, "type", "") or ""))
        if not label:
            continue
        src = id_to_name.get(str(getattr(e, "source_id", "") or ""))
        tgt = id_to_name.get(str(getattr(e, "target_id", "") or ""))
        if not src or not tgt:  # cap 으로 잘려 한쪽 노드가 없으면 dangling — 제외
            continue
        lines.add(f"{src} -[{label}]-> {tgt}")

    if not lines:
        return ""
    body = "\n".join(f"  · {ln}" for ln in sorted(lines)[:cap])
    return f"- 의존성 관계:\n{body}"


async def build_graph_summary(ctx: PipelineContext, project_name: str) -> str:
    """프로젝트 설계 그래프(DDD/SPACK/Architecture) → 간결 요약. 없거나 실패 시 ''.

    노드 이름 요약(_fetch_specs, lint 와 동일 뷰)에 더해, get_project_graph 의
    의존성 관계 엣지(IMPLEMENTS/PUBLISHES/TRIGGERS/CONNECTS_TO 등)를 덧붙여
    빌드 플랜의 마일스톤·범위가 그래프 위상을 따르게 한다. 두 조회는 독립적이라
    한쪽이 실패해도 나머지로 진행한다.
    """
    name = (project_name or "").strip()
    if not name:
        return ""

    node_summary = ""
    try:
        # 지연 import — interview 모듈 로드가 lint 체인을 끌어오지 않게.
        from app.pipelines.lint_pipeline import _fetch_specs
        specs = await _fetch_specs(ctx, name)
        node_summary = _format_graph_summary(specs)
    except Exception:  # noqa: BLE001 — 그래프 조회/포맷 실패는 빌드 플랜을 막지 않는다
        logger.exception("interview: 설계 그래프(노드) 조회 실패 — 노드 요약 생략")

    rel_summary = ""
    try:
        # 지연 import — query_repository 의 무거운 의존성을 모듈 로드 시 끌어오지 않게.
        # name 은 이미 스코프된 키 → scoped_project 멱등성으로 team_id="" 안전.
        from app.service.query_repository import get_project_graph
        pg = await get_project_graph(name, team_id="")
        rel_summary = _format_graph_relations(pg)
        # [T6] 설계 완성도 — 갭(미연결 기능·빈 규칙/항목)이 있으면 플랜이 보완 단계를
        # 포함하도록 신호. 같은 pg 재사용(추가 fetch 0). 비율이라 캐시 해시 결정적.
        readiness = graph_readiness(pg)
        if readiness < 1.0:
            line = (
                f"- 설계 완성도: {int(round(readiness * 100))}% — 일부 기능이 사용자 "
                f"시나리오에 미연결이거나 규칙·항목이 비어 있음. 플랜에 '미완성 항목 보완' "
                f"단계를 포함하라."
            )
            rel_summary = f"{rel_summary}\n{line}" if rel_summary else line
    except Exception:  # noqa: BLE001 — 관계 조회 실패는 노드 요약만으로 진행
        logger.exception("interview: 설계 그래프(관계) 조회 실패 — 관계 요약 생략")

    # [T7] 이전 빌드 검증(lint) 환류 — 코드에 없던 설계 항목을 플랜이 반영하게.
    lint_summary = ""
    try:
        from app.service.lint_repository import get_last_lint_for_project
        lr = await get_last_lint_for_project(name)
        lint_summary = _format_lint_feedback(lr)
    except Exception:  # noqa: BLE001 — lint 환류 실패는 플랜을 막지 않는다
        logger.exception("interview: 이전 lint 조회 실패 — 검증 환류 생략")

    return "\n".join(s for s in (node_summary, rel_summary, lint_summary) if s)


# ─── build_plan 영속 (캐시) ──────────────────────────────────────────────
# 입력(회의록 + 그래프 요약)의 해시로 캐시한다: 입력이 같으면 저장된 플랜을
# 재사용해 LLM 재호출을 없애고(지연·비용↓), 입력이 바뀌면 재생성·재저장해
# stale 을 막는다. 저장된 AC 는 추후 Lint 검증의 기준으로도 재사용 가능.

def build_plan_input_hash(meeting_content: str, graph_summary: str = "") -> str:
    """build_plan 캐시 키 — 회의록 + 그래프 요약의 sha256."""
    h = hashlib.sha256()
    h.update((meeting_content or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((graph_summary or "").encode("utf-8"))
    return h.hexdigest()


def _loads_list(v: Any) -> List[str]:
    """Neo4j 에 JSON string 으로 저장한 list 복원 (list / JSON string / None 흡수)."""
    if isinstance(v, list):
        return [str(x) for x in v]
    if not isinstance(v, str) or not v.strip():
        return []
    try:
        parsed = json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


_SAVE_BUILD_PLAN_CYPHER = """\
MERGE (bp:BuildPlan {project: $project})
SET bp.recommended_stack = $recommended_stack,
    bp.scope_now = $scope_now,
    bp.scope_later = $scope_later,
    bp.milestones = $milestones,
    bp.acceptance_criteria = $acceptance_criteria,
    bp.risks = $risks,
    bp.start_prompt = $start_prompt,
    bp.input_hash = $input_hash,
    bp.updated_at = timestamp()
"""

_GET_BUILD_PLAN_CYPHER = """\
MATCH (bp:BuildPlan {project: $project})
RETURN bp.recommended_stack AS recommended_stack,
       bp.scope_now AS scope_now, bp.scope_later AS scope_later,
       bp.milestones AS milestones, bp.acceptance_criteria AS acceptance_criteria,
       bp.risks AS risks, bp.start_prompt AS start_prompt,
       bp.input_hash AS input_hash
"""


def is_substantive_plan(plan: BuildPlan) -> bool:
    """실질 합성 결과인지 판별 — 폴백(빈 껍데기)은 캐시에 저장하지 않기 위함.

    합성 실패 폴백(_fallback_build_plan)은 start_prompt 만 채우고 나머지는 비운다.
    그런 폴백을 저장하면 LLM 일시 장애가 캐시에 '박제'되어, 입력이 그대로인 한
    LLM 이 복구돼도 폴백을 계속 돌려준다. → 실질 필드가 하나라도 있을 때만 저장.
    (희박하게 실제 플랜이 비어도 저장만 건너뛸 뿐 — 다음 호출에 재합성되어 무해.)
    """
    return bool(
        plan.recommended_stack
        or plan.scope_now
        or plan.scope_later
        or plan.milestones
        or plan.acceptance_criteria
    )


# ─── build_plan 품질 점수 (객관 측정, eval) ───────────────────────────────
# LLM judge 없이 build_plan 산출물의 '에이전트가 바로 쓸 수 있는 구체성'을 정량화.
# 좋은 플랜 > 모호한 플랜 > 폴백을 점수로 구분 → 품질 회귀 잠금·전후 비교의 객관 proxy.
# (라이브 LLM judge 는 이 위에 얹는 별개 작업 — 키 필요.)
_QUALITY_WEIGHTS = {
    "stack": 0.15, "scope": 0.15, "milestones": 0.2,
    "acceptance": 0.3, "risks": 0.1, "start_prompt": 0.1,
}
# AC '관찰 가능성' 휴리스틱 — 조건/결과 신호가 있으면 검증 가능한 기준으로 본다.
_AC_SPECIFIC_MARKERS = ("면", "하면", "때", "이면", "한다", "보인다", "표시", "when", "if", "after")
# 공허(비관찰) 표현 — 동사 어미('한다' 등)만 맞춰 '검증 가능'으로 오탐되는 대표 문구.
# 이 표현이 있으면 마커가 있어도 비관찰로 본다 ('모든 기능이 정상 작동한다'류 굿하트 차단).
_AC_VACUOUS_PATTERNS = (
    "정상 작동", "정상작동", "정상적으로", "잘 작동", "잘작동", "잘 동작", "잘 된다", "잘된다",
    "잘 쓰", "문제 없", "문제없", "좋은 경험", "알아서", "원활", "적절히", "성공적으로",
    "멋지게", "훌륭하게", "완벽하게", "제대로 동작", "제대로 작동",
)
# [Q2 brownfield 정합] 설계 그래프가 있으면 플랜이 그 이름(Aggregate·엔티티·API·
# 서비스·도메인)을 실제로 참조하는지 점수화한다. 그래프를 무시한 엉뚱한 플랜
# ('주문앱에 할일앱 플랜')이 만점을 못 받게 하고, 정제를 정합 쪽으로 끈다.
# greenfield(그래프 없음)는 이 차원 자체가 없어 기존 점수와 동일(후방호환).
_GRAPH_ALIGN_WEIGHT = 0.2   # 그래프 있을 때 정합 차원 가중(나머지 6차원을 0.8로 축소)
_GRAPH_ALIGN_MIN = 0.3      # 정합이 이 값 미만이면 정제 트리거(설계 무시 의심)
# graph_summary(_format_graph_summary 출력)에서 '이름'이 담긴 라인 접두어.
_GRAPH_NAME_PREFIXES = ("- 도메인", "- Aggregate", "- 핵심 엔티티", "- API", "- 서비스")


def _ac_is_specific(ac: str) -> bool:
    """관찰 가능한(검증 가능한) AC 인지 — 길이 + 조건/결과 신호 또는 숫자.

    공허한 품질·부사 표현(_AC_VACUOUS_PATTERNS)이 들어가면 어미 마커가 있어도
    비관찰로 본다 — '정상 작동한다'류가 '한다' 마커로 통과하던 굿하트 오탐 차단.
    """
    s = (ac or "").strip()
    if any(v in s for v in _AC_VACUOUS_PATTERNS):
        return False
    has_digit = any(c.isdigit() for c in s)
    if not (has_digit or any(m in s for m in _AC_SPECIFIC_MARKERS)):
        return False
    # 길이 바닥은 신호 검사 뒤에. 단 숫자(정량)는 짧아도 관찰가능으로 인정
    # ('3초 이내에 응답한다'(11자)가 길이만으로 탈락하던 경계 오탐 보정, QA 발견).
    if len(s) < 12 and not has_digit:
        return False
    return True


# API 이름의 HTTP 메서드 접두어 — 'GET /orders' 의 GET 은 영어 평문(get)에 흔히
# 매칭돼 정합을 부풀린다(QA 발견). 제거하고 path(도메인 명사)만 정합 신호로 쓴다.
_HTTP_METHOD_PREFIX = re.compile(r"^(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+", re.IGNORECASE)


def _extract_graph_names(graph_summary: str) -> List[str]:
    """graph_summary 이름 라인(Aggregate/엔티티/API/서비스/도메인)에서 이름 추출.

    관계·완성도 라인은 제외. 형식은 _format_graph_summary 가 생성('- 라벨: A, B').
    API 이름은 HTTP 메서드를 떼어 path 만 남긴다(동사 거짓양성 방지).
    """
    names: List[str] = []
    for raw in (graph_summary or "").splitlines():
        line = raw.strip()
        if not any(line.startswith(p) for p in _GRAPH_NAME_PREFIXES):
            continue
        if ":" not in line:
            continue
        for nm in line.split(":", 1)[1].split(","):
            nm = _HTTP_METHOD_PREFIX.sub("", nm.strip()).strip()
            if nm:
                names.append(nm)
    return names


def _name_referenced(name: str, plan_text: str, plan_words: set) -> bool:
    """설계 이름이 플랜에 참조됐는지 — 한글 이름은 부분일치, ASCII 는 단어일치."""
    for run in _HANGUL_RUN.findall(name or ""):
        if len(run) >= 2 and run in plan_text:
            return True
    for w in _ASCII_WORD.findall(name or ""):
        if w.lower() in plan_words:
            return True
    return False


def _graph_alignment(plan: BuildPlan, names: List[str]) -> float:
    """플랜이 설계 이름들을 참조하는 비율 0~1. names 비면 0.0(자기방어).

    risks 는 제외 — '무엇을 뺀다/주의한다'는 caveat 이라 설계 정합 신호가 아니고,
    엔티티명을 우연히 언급하면 정합을 부풀려 정당한 정제를 억제한다(QA 발견).
    """
    if not names:
        return 0.0
    plan_text = " ".join(
        [plan.recommended_stack or "", plan.start_prompt or ""]
        + plan.scope_now + plan.scope_later + plan.milestones
        + plan.acceptance_criteria
    )
    plan_words = _ascii_words(plan_text)
    hit = sum(1 for nm in names if _name_referenced(nm, plan_text, plan_words))
    return round(hit / len(names), 3)


def build_plan_quality_score(plan: BuildPlan, graph_summary: str = "") -> Tuple[float, dict]:
    """build_plan 품질 0~1 + 항목별 분해. 순수 함수(LLM 불필요).

    기준: 스택 명시·범위 집중(1~7)·마일스톤(3+)·AC 관찰가능성·리스크·start_prompt 충실.
    graph_summary 에 설계 이름이 있으면(brownfield) '그래프 정합' 차원을 더해 기존
    설계를 무시한 플랜의 점수를 낮춘다. 없으면(greenfield) 기존 6차원 그대로(후방호환).
    """
    b: dict = {}
    b["stack"] = 1.0 if (plan.recommended_stack or "").strip() else 0.0
    n_now = len(plan.scope_now)
    b["scope"] = 1.0 if 1 <= n_now <= 7 else (0.5 if n_now > 7 else 0.0)
    b["milestones"] = round(min(len(plan.milestones) / 3.0, 1.0), 3)
    acs = [a for a in plan.acceptance_criteria if (a or "").strip()]
    b["acceptance"] = round(sum(1 for a in acs if _ac_is_specific(a)) / len(acs), 3) if acs else 0.0
    b["risks"] = 1.0 if plan.risks else 0.0
    b["start_prompt"] = 1.0 if len((plan.start_prompt or "").strip()) >= 40 else 0.0
    base = sum(_QUALITY_WEIGHTS[k] * b[k] for k in _QUALITY_WEIGHTS)

    names = _extract_graph_names(graph_summary)
    if names:  # brownfield — 설계 정합 차원 추가(가중 재정규화)
        align = _graph_alignment(plan, names)
        b["graph_align"] = align
        score = round(base * (1 - _GRAPH_ALIGN_WEIGHT) + align * _GRAPH_ALIGN_WEIGHT, 3)
    else:      # greenfield — 기존 동작 그대로
        score = round(base, 3)
    return score, b


def _build_plan_to_json(plan: BuildPlan) -> str:
    """BuildPlan → 재합성 프롬프트에 넣을 JSON 문자열(초안 제시용)."""
    return json.dumps(
        {
            "recommended_stack": plan.recommended_stack,
            "scope_now": plan.scope_now,
            "scope_later": plan.scope_later,
            "milestones": plan.milestones,
            "acceptance_criteria": plan.acceptance_criteria,
            "risks": plan.risks,
            "start_prompt": plan.start_prompt,
        },
        ensure_ascii=False,
        indent=2,
    )


def _build_plan_critique(breakdown: dict) -> str:
    """build_plan_quality_score 분해 → 약한 항목만 가리키는 보강 지시. 약점 없으면 ''.

    결정적(점수→지시) 매핑 — 모델 자기 인상이 아니라 객관 점수가 critique 를 만든다(grounding).
    """
    lines: List[str] = []
    if breakdown.get("graph_align", 1.0) < _GRAPH_ALIGN_MIN:
        lines.append(
            "- 기존 설계(그래프)를 반영하라: 이미 정의된 Aggregate·엔티티·API·서비스 "
            "이름을 마일스톤·완료기준·범위에 그대로 사용해 정렬하라(새 구조를 지어내지 말 것)."
        )
    if breakdown.get("acceptance", 0.0) < 0.7:
        lines.append(
            "- 완료 기준(acceptance_criteria)이 모호하다: 각 핵심 기능마다 "
            "'무엇을 하면/언제 무엇이 보인다'처럼 관찰·검증 가능한 문장으로 다시 쓰고, "
            "기준이 없는 기능엔 추가하라."
        )
    if breakdown.get("milestones", 0.0) < 1.0:
        lines.append(
            "- 마일스톤이 부족하다: 데이터 → 기능 → 연동 순서로, 그 자체로 동작·확인 "
            "가능한 단계를 최소 3개 이상으로 쪼개라."
        )
    if breakdown.get("stack", 0.0) < 1.0:
        lines.append(
            "- 추천 스택(recommended_stack)이 비어 있다: 흔하고 검증된 평범한 조합을 "
            "한 줄 + 한 줄 이유로 명시하라."
        )
    if breakdown.get("scope", 0.0) < 1.0:
        lines.append(
            "- 1차 범위(scope_now)를 다듬어라: 1차에 끝낼 수 있는 핵심 기능만 1~7개로 "
            "추리고, 나머지는 scope_later 로 옮겨라."
        )
    if breakdown.get("risks", 0.0) < 1.0:
        lines.append(
            "- risks 가 비어 있다: 결제·인증·실시간·외부연동 등 실패하기 쉬운 지점과 "
            "대응(빼기/단순화/주의)을 적어라."
        )
    if breakdown.get("start_prompt", 0.0) < 1.0:
        lines.append(
            "- start_prompt 가 빈약하다: 무엇을·어떤 스택으로·어떤 순서로·무엇을 충족해야 "
            "하는지 한 문단으로 충실히 요약하라."
        )
    return "\n".join(lines)


def _build_plan_refine_prompt(
    meeting_content: str, graph_summary: str, draft: BuildPlan, critique: str
) -> str:
    """안전 프리앰블 + phase_build_plan_refine.md (회의록·그래프·초안·critique 치환)."""
    return compose_with_safety(
        _BUILD_PLAN_REFINE_PROMPT_FILE,
        variables={
            "{{MEETING}}": (meeting_content or "").strip() or "(내용 없음)",
            "{{GRAPH}}": (graph_summary or "").strip() or "(없음 — 아직 설계 그래프가 없습니다)",
            "{{DRAFT}}": _build_plan_to_json(draft),
            "{{CRITIQUE}}": critique,
        },
    )


# ─── 재합성 grounding 가드 (환각 차단) ─────────────────────────────────────
# 재합성이 회의록·그래프·초안에 전혀 없는 내용(AC/리스크)을 지어내 점수만 올리는 것을
# 막는다. 보수적: 추가된 AC/리스크가 '하나도' 앵커되지 않을 때만 그 재합성을 폐기
# (draft 유지) — 정당한 재서술·구체화(기존 어휘 재사용)는 막지 않는다.
_HANGUL_RUN = re.compile(r"[가-힣]+")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]{3,}")


def _korean_bigrams(text: str) -> set:
    """텍스트의 한글 2-gram 집합 — 조사·어미에 견디는 어간 겹침 판정용."""
    grams: set = set()
    for run in _HANGUL_RUN.findall(text or ""):
        for i in range(len(run) - 1):
            grams.add(run[i : i + 2])
    return grams


def _ascii_words(text: str) -> set:
    return {w.lower() for w in _ASCII_WORD.findall(text or "")}


def _item_anchored(item: str, corpus_bigrams: set, corpus_words: set) -> bool:
    """항목이 corpus 에 어휘적으로 앵커되는지.

    한글은 2-gram 이 2개 이상 겹쳐야 앵커로 본다 — '하면/된다' 같은 흔한 어미 1개로
    날조가 우연히 앵커되는 오탐을 막는다. ASCII 단어(스택명·API명 등)는 1개로 충분.
    """
    if len(_korean_bigrams(item) & corpus_bigrams) >= 2:
        return True
    return bool(_ascii_words(item) & corpus_words)


def _refinement_is_grounded(
    draft: BuildPlan, revised: BuildPlan, meeting_content: str, graph_summary: str
) -> bool:
    """재합성이 추가한 AC/리스크/마일스톤/범위가 회의록·그래프·초안에 앵커되는지(날조 차단).

    추가분이 없으면(재서술·타 항목 개선만) True. 추가분이 있는데 '하나도' 앵커되지
    않으면 날조 의심 → False. 일부라도 앵커되면 통과(보수적 — 정당한 작업 안 막음).
    점수에 기여하는 모든 리스트 차원을 검사한다 — 마일스톤만 날조해 점수를 올리는
    경로(QA 발견)를 막기 위해 AC/리스크뿐 아니라 milestones/scope 추가분도 포함.
    """
    added: List[str] = []
    added += [x for x in revised.acceptance_criteria if x not in set(draft.acceptance_criteria)]
    added += [x for x in revised.risks if x not in set(draft.risks)]
    added += [x for x in revised.milestones if x not in set(draft.milestones)]
    added += [x for x in revised.scope_now if x not in set(draft.scope_now)]
    added += [x for x in revised.scope_later if x not in set(draft.scope_later)]
    added = [x for x in added if (x or "").strip()]
    if not added:
        return True
    corpus = "\n".join([
        meeting_content or "", graph_summary or "",
        draft.recommended_stack or "", draft.start_prompt or "",
        " ".join(
            draft.scope_now + draft.scope_later + draft.milestones
            + draft.acceptance_criteria + draft.risks
        ),
    ])
    cb, cw = _korean_bigrams(corpus), _ascii_words(corpus)
    return any(_item_anchored(x, cb, cw) for x in added)


# ─── 다세대 evolve — 세대 간 구조 유사도 (수렴/진동 판정, LLM 0회) ──────────
def _plan_struct_tokens(items: List[str]) -> set:
    """리스트 항목들의 구조 토큰(한글 2-gram + ASCII 단어) 집합."""
    text = " ".join(items or [])
    return _korean_bigrams(text) | _ascii_words(text)


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _build_plan_similarity(prev: BuildPlan, cur: BuildPlan) -> float:
    """두 build_plan 세대의 구조 유사도 0~1 (LLM 0회, 순수 함수).

    ouroboros 의 세대 간 온톨로지 유사도를 harness 현실로 단순화 —
    name_overlap(scope_now+acceptance_criteria 토큰 Jaccard) 0.6 +
    milestone_overlap(milestones 토큰 Jaccard) 0.4. 기존 토큰화 재사용.
    1.0=구조 동일(수렴), 0=무관. (둘 다 비면 1.0, 한쪽만 비면 0.0)
    """
    name_prev = _plan_struct_tokens(prev.scope_now + prev.acceptance_criteria)
    name_cur = _plan_struct_tokens(cur.scope_now + cur.acceptance_criteria)
    ms_prev = _plan_struct_tokens(prev.milestones)
    ms_cur = _plan_struct_tokens(cur.milestones)
    return round(0.6 * _jaccard(name_prev, name_cur) + 0.4 * _jaccard(ms_prev, ms_cur), 3)


async def _refine_build_plan(
    ctx: PipelineContext, draft: BuildPlan, meeting_content: str, graph_summary: str
) -> BuildPlan:
    """[P1 우로보로스] draft → 객관 점수 critique → 1회 재합성. 점수 오를 때만 채택.

    - 트리거: build_plan_quality_score(draft) < _BUILD_PLAN_REFINE_THRESHOLD 이고 약점 존재.
    - 앵커: 점수는 LLM 없는 순수 함수 → 자기 인상이 아닌 객관 적합도가 재합성을 결정·게이팅.
    - 안전: 재합성 결과는 점수가 '오를 때만' 채택, 정체/하락 시 즉시 중단(퇴화 차단). 어떤
      실패(파싱·예외)에도 항상 입력 draft 를 보존해 반환 → 합성 흐름을 깨지 않는다.
    """
    try:
        score, breakdown = build_plan_quality_score(draft, graph_summary)
        # AC(완료기준)는 에이전트 완주의 급소이자 최대 가중(0.3). 텅 비면(또는 전부
        # 비관찰이면) 다른 항목 합이 임계를 채워도 정제 대상으로 본다 — 점수 0.70 사각
        # ('완료기준 빈 플랜'이 정제 없이 통과)을 보정.
        acceptance_ok = breakdown.get("acceptance", 0.0) > 0.0
        # [Q2] brownfield 정합 — 설계 그래프가 있는데 플랜이 그 이름을 거의 안 쓰면
        # (설계 무시 의심) 점수가 높아도 정제 대상. greenfield(graph_align 없음)는 통과.
        align_ok = breakdown.get("graph_align", 1.0) >= _GRAPH_ALIGN_MIN
        if score >= _BUILD_PLAN_REFINE_THRESHOLD and acceptance_ok and align_ok:
            logger.info("interview: build_plan score=%.3f refined=False (충분)", score)
            return draft
        critique = _build_plan_critique(breakdown)
        if not critique:
            logger.info("interview: build_plan score=%.3f refined=False (약점없음)", score)
            return draft

        best, best_score = draft, score
        gens = [draft]            # 세대 이력 — 진동(GenN≈GenN-2) 판정용
        stop = "max_pass"
        for _ in range(_BUILD_PLAN_REFINE_MAX_PASSES):
            result = await ctx.gemini.generate(
                _build_plan_refine_prompt(meeting_content, graph_summary, best, critique),
                temperature=_TEMPERATURE,
                model=_INTERVIEW_MODEL,
                max_output_tokens=_BUILD_PLAN_MAX_OUTPUT_TOKENS,
            )
            revised = _parse_build_plan(result.text)
            if revised is None:
                stop = "parse_fail"
                break
            new_score, new_breakdown = build_plan_quality_score(revised, graph_summary)
            if new_score <= best_score:  # 점수 정체/하락 → 기존 채택, 중단
                stop = "no_gain"
                break
            # [환각 가드] 점수가 올라도, 재합성이 회의록·그래프에 없는 내용을
            # 지어내 올린 거면 폐기(draft 유지). 일부라도 앵커되면 통과(보수적).
            if not _refinement_is_grounded(best, revised, meeting_content, graph_summary):
                logger.info("interview: build_plan 재합성 grounding 실패(날조 의심) — 폐기")
                stop = "grounding_fail"
                break
            # 채택. 직전 세대와의 구조 유사도(수렴 판정)는 채택 전에 계산.
            sim_prev = _build_plan_similarity(best, revised)
            best, best_score, breakdown = revised, new_score, new_breakdown
            gens.append(revised)
            # [수렴] 직전 세대와 구조가 거의 같으면 더 굴려도 안 변함 → 중단(비용 절감).
            if sim_prev >= _BUILD_PLAN_CONVERGE_SIM:
                stop = "converged"
                break
            # [진동] GenN 이 GenN-1 보다 GenN-2 와 더 닮았으면 두 설계 사이 왕복 → 중단.
            if len(gens) >= 3 and (
                _build_plan_similarity(gens[-1], gens[-3])
                > _build_plan_similarity(gens[-1], gens[-2])
            ):
                stop = "oscillate"
                break
            critique = _build_plan_critique(breakdown)
            if best_score >= _BUILD_PLAN_REFINE_THRESHOLD or not critique:
                stop = "threshold" if best_score >= _BUILD_PLAN_REFINE_THRESHOLD else "no_critique"
                break

        # [관측성] 매 신규 합성마다 점수·세대수·종료사유 로깅 → 운영 A/B(evolve 가 점수를
        # 올렸나 vs 단발 대비 비용)와 회귀 감시의 기준선. score=pre, best_score=post.
        logger.info(
            "interview: build_plan score=%.3f→%.3f gens=%d stop=%s refined=%s",
            score, best_score, len(gens) - 1, stop, best_score > score,
        )
        return best
    except Exception:  # noqa: BLE001 — 정제 실패가 합성 흐름을 깨지 않게 draft 보존
        logger.exception("interview: build_plan 자기정제 실패 — draft 유지")
        return draft


async def save_build_plan(
    ctx: PipelineContext, project: str, plan: BuildPlan, input_hash: str
) -> None:
    """build_plan 을 프로젝트별 1개 노드로 저장(MERGE). list 는 JSON string 으로."""
    await ctx.neo4j.run_cypher(
        _SAVE_BUILD_PLAN_CYPHER,
        {
            "project": project,
            "recommended_stack": plan.recommended_stack,
            "scope_now": json.dumps(plan.scope_now, ensure_ascii=False),
            "scope_later": json.dumps(plan.scope_later, ensure_ascii=False),
            "milestones": json.dumps(plan.milestones, ensure_ascii=False),
            "acceptance_criteria": json.dumps(plan.acceptance_criteria, ensure_ascii=False),
            "risks": json.dumps(plan.risks, ensure_ascii=False),
            "start_prompt": plan.start_prompt,
            "input_hash": input_hash,
        },
    )


async def get_build_plan(
    ctx: PipelineContext, project: str
) -> Tuple[Optional[BuildPlan], str]:
    """저장된 (BuildPlan, input_hash) 반환. 없으면 (None, "")."""
    rows = await ctx.neo4j.run_cypher(_GET_BUILD_PLAN_CYPHER, {"project": project})
    if not rows:
        return None, ""
    r = rows[0] or {}
    plan = BuildPlan(
        recommended_stack=str(r.get("recommended_stack") or ""),
        scope_now=_loads_list(r.get("scope_now")),
        scope_later=_loads_list(r.get("scope_later")),
        milestones=_loads_list(r.get("milestones")),
        acceptance_criteria=_loads_list(r.get("acceptance_criteria")),
        risks=_loads_list(r.get("risks")),
        start_prompt=str(r.get("start_prompt") or ""),
    )
    return plan, str(r.get("input_hash") or "")



@dataclass
class InterviewMessage:
    """대화 한 줄. role 은 'user' 또는 'assistant'."""

    role: str
    content: str


@dataclass
class InterviewTurn:
    """LLM 한 턴 결과."""

    phase: Literal["ask", "done"]
    assistant_message: str
    suggestions: List[str] = field(default_factory=list)
    coverage: List[str] = field(default_factory=list)
    meeting_content: str = ""
    # [T1] 정량 준비도 — 차원별 0~1 점수와 가중합. done 게이트(T2)·진행바의 토대.
    scores: dict = field(default_factory=dict)
    readiness: float = 0.0
    # [T3] ask 턴에 다음으로 집중할 가장 약한 차원(없으면 None). FE 힌트·프롬프트 타기팅.
    next_focus: Optional[str] = None


# ─── 준비도 점수 (정량 done 게이트의 토대, T1) ────────────────────────────
# 비전공자 인터뷰의 '핵심 주제'를 차원으로 삼아 0~1 점수의 가중합 = readiness.
# OMC 모호성 점수와 발상은 같되, 차원이 다운스트림(CPS/PRD/설계) 파이프라인이
# 필요로 하는 슬롯과 일치해 '빌드 가능 완성도'로 해석된다. 가중치 합 = 1.0.
_READINESS_WEIGHTS: dict = {
    "goal": 0.25,        # 무엇을·왜·누구를 위해
    "features": 0.25,    # 핵심 기능 3~5개
    "data": 0.20,        # 다루는 데이터/대상
    "users": 0.15,       # 핵심 사용자/역할
    "constraints": 0.10,  # 로그인·결제·연동·규모
    "usage": 0.05,       # 용도(취미/포트폴리오/사업/사내)
}


def _parse_scores(raw: str) -> dict:
    """'goal=0.8|features=0.4|...' → {dim: float}. 알려진 차원만, 0~1 클램프."""
    out: dict = {}
    for part in (raw or "").split("|"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        if k not in _READINESS_WEIGHTS:
            continue
        try:
            f = float(v.strip())
        except (TypeError, ValueError):
            continue
        out[k] = max(0.0, min(1.0, f))
    return out


def compute_readiness(scores: dict) -> float:
    """차원별 0~1 점수의 가중합(누락 차원=0) → 0~1, 소수 3자리. 순수 함수."""
    total = sum(
        _READINESS_WEIGHTS.get(k, 0.0) * float(v) for k, v in (scores or {}).items()
    )
    return round(max(0.0, min(1.0, total)), 3)


def weakest_dimension(scores: dict) -> Optional[str]:
    """다음 질문을 집중할 가장 약한 차원(T3 용). 점수 없으면 가중 최상위 차원."""
    if not scores:
        return max(_READINESS_WEIGHTS, key=_READINESS_WEIGHTS.get)
    # 누락 차원은 0 으로 간주 → 최저점. 동점이면 가중치 큰 차원 우선.
    return min(
        _READINESS_WEIGHTS,
        key=lambda d: (scores.get(d, 0.0), -_READINESS_WEIGHTS[d]),
    )


# 최약 차원 templated 질문 — done→ask 로 되돌릴 때 안전망(모델 done 메시지가 부적절하므로).
_DIMENSION_NUDGE: dict = {
    "goal": "조금만 더 여쭤볼게요 — 이걸 한 줄로 하면 '누구를 위한 무슨 서비스'일까요?",
    "features": "사용자가 이 서비스로 할 수 있어야 하는 핵심 기능 2~3개만 콕 집어 알려주실래요?",
    "data": "이 서비스가 저장·관리하는 정보는 무엇인가요? (예: 글, 주문, 회원 정보)",
    "users": "주로 누가 사용하나요? (예: 일반 사용자·관리자·로그인 없이 누구나)",
    "constraints": "로그인·결제·알림·외부 서비스 연동 중 필요한 게 있을까요?",
    "usage": "이걸 어디에 쓰실 거예요? (취미·포트폴리오·실제 사업·사내용)",
}


def _targeted_question(scores: dict) -> str:
    """가장 약한 차원에 대한 templated 질문."""
    return _DIMENSION_NUDGE.get(weakest_dimension(scores) or "goal", _DIMENSION_NUDGE["goal"])


# ─── [T4] 설계 그래프 갭 → 보강 질문 (Phase 2) ────────────────────────────
# 기존 검증(design_validator)이 내는 Violation code 중 '사용자 의도가 빠진' 갭만
# 선별해 비전공자 질문으로 매핑한다. 내부/자동교정 코드(ID 재배정·dangling·pascal
# 등)는 제외 — 그건 시스템이 고치거나 개발 관심사이지 기획자에게 물을 게 아니다.
# dict 는 우선순위순(앞이 더 중요) — 그대로 순회해 캡한다.
_GAP_QUESTIONS: dict = {
    "API_MISSING_STORY_REF": "설계에 어떤 기능이 '왜 필요한지(어떤 사용자 시나리오)'가 빠져 있어요. 그 기능으로 사용자가 무엇을 하려는 건가요?",
    "DDD_MISSING_SPACK_ENTITY": "저장 대상에서 빠진 정보가 있는 것 같아요. 더 저장·관리해야 할 정보가 있을까요?",
    "AGGREGATE_INVARIANTS_MISSING": "데이터가 꼭 지켜야 할 규칙이 안 정해졌어요 (예: 재고는 0 미만 불가, 이메일 중복 가입 불가). 이런 규칙이 있나요?",
    "ENTITY_ATTRIBUTES_MISSING": "일부 정보에 어떤 항목이 담기는지가 비어 있어요. 예를 들어 '사용자'에는 이름·이메일처럼 무엇이 들어가나요?",
    "DOMAIN_ENTITY_ATTRIBUTES_MISSING": "일부 정보에 어떤 항목이 담기는지가 비어 있어요. 무엇이 들어가야 하나요?",
    "API_VALIDATION_ERROR_CASE_MISSING": "잘못된 입력이 들어오면 어떻게 반응해야 하나요? (예: 빈칸이면 저장 막기)",
    "API_NOT_FOUND_CASE_MISSING": "찾는 항목이 없을 때 사용자에게 어떻게 보여줄까요? (예: '없습니다' 안내)",
    "API_AUTH_ERROR_CASE_MISSING": "권한이 없는 사람이 접근하면 어떻게 해야 하나요?",
    "DDD_EVENT_MISSING_STORY_REF": "어떤 후속 동작(알림 등)이 '어떤 사용자 행동에서' 일어나는지가 빠졌어요. 언제 발생하나요?",
    "POLICY_CATEGORY_MISSING": "정해둔 규칙(정책)의 종류가 불분명해요. 어떤 성격의 규칙인가요?",
}


def graph_gaps_to_questions(violations, cap: int = 5) -> List[str]:
    """검증 위반(갭) → 비전공자용 보강 질문 (코드별 1개, 우선순위순·중복제거·캡).

    violations: [{'code': ...}] dict 또는 Violation(.code) 객체 모두 수용(duck-typing).
    순수 함수 — 위반을 어떻게 얻는지(eval 파이프라인 등)와 분리해 테스트 용이.
    """
    codes = set()
    for v in violations or []:
        code = v.get("code") if isinstance(v, dict) else getattr(v, "code", None)
        if code:
            codes.add(str(code))
    questions = [q for code, q in _GAP_QUESTIONS.items() if code in codes]
    return questions[:cap]


def _is_empty_jsonish(v) -> bool:
    """속성이 '명시적으로 비어있음'인지 — 누락(None/키 없음)은 False(보수적).

    false-positive 회피: 값을 모르면(누락) 갭으로 단정하지 않고, 빈 list/JSON
    빈 배열 문자열 등 '있는데 비었다'만 갭으로 본다. legacy 노드 오탐 방지.
    """
    if v is None:
        return False
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return str(v).strip() in ("", "[]", "{}", "null")


def extract_graph_gap_codes(graph) -> List[dict]:
    """get_project_graph(nodes, edges) → 사용자 의도 갭 코드 목록 (경량·보수적).

    엣지 기반(IMPLEMENTS/TRIGGERS 부재)은 확실한 갭, 속성 기반은 '명시적 빈 값'만
    플래그(누락은 제외) → eval 백필 없이도 false-positive 최소화. 순수 함수.
    """
    nodes = getattr(graph, "nodes", None) or []
    edges = getattr(graph, "edges", None) or []

    def _etype(e):
        return str(getattr(e, "type", "") or "")

    impl_sources = {str(getattr(e, "source_id", "") or "") for e in edges if _etype(e) == "IMPLEMENTS"}
    trig_targets = {str(getattr(e, "target_id", "") or "") for e in edges if _etype(e) == "TRIGGERS"}

    gaps: List[dict] = []
    for n in nodes:
        label = str(getattr(n, "label", "") or "")
        nid = str(getattr(n, "id", "") or "")
        props = getattr(n, "properties", None) or {}
        if label == "API" and nid not in impl_sources:
            gaps.append({"code": "API_MISSING_STORY_REF", "item_id": nid})
        elif label == "DomainEvent" and nid not in trig_targets:
            gaps.append({"code": "DDD_EVENT_MISSING_STORY_REF", "item_id": nid})
        elif label == "Aggregate" and _is_empty_jsonish(props.get("invariants")):
            gaps.append({"code": "AGGREGATE_INVARIANTS_MISSING", "item_id": nid})
        elif label == "Entity" and _is_empty_jsonish(props.get("attributes")):
            gaps.append({"code": "ENTITY_ATTRIBUTES_MISSING", "item_id": nid})
        elif label == "DomainEntity" and _is_empty_jsonish(props.get("attributes")):
            gaps.append({"code": "DOMAIN_ENTITY_ATTRIBUTES_MISSING", "item_id": nid})
    return gaps


_SCOREABLE_LABELS = ("API", "DomainEvent", "Aggregate", "Entity", "DomainEntity")


def graph_readiness(graph) -> float:
    """[T6] 설계 그래프 완성도 0~1 — 점수 대상 노드 중 갭(미연결·미정의) 없는 비율.

    extract_graph_gap_codes 재사용(노드당 최대 1갭). 점수 대상이 없으면 1.0(흠 없음).
    개수 비율이라 노드 순서와 무관 → build_plan 캐시 해시 결정성 유지. 순수 함수.
    """
    nodes = getattr(graph, "nodes", None) or []
    scoreable = sum(
        1 for n in nodes if str(getattr(n, "label", "") or "") in _SCOREABLE_LABELS
    )
    if scoreable == 0:
        return 1.0
    gap_nodes = len({g["item_id"] for g in extract_graph_gap_codes(graph)})
    return round(max(0.0, 1.0 - gap_nodes / scoreable), 3)


async def graph_interview_context(project_scoped: str) -> Tuple[List[str], float]:
    """[T5+T8] 설계 그래프 1회 조회 → (보강 질문, 그래프 완성도 0~1).

    완성도 1.0 = 갭 없음 또는 그래프 없음(greenfield) → done 게이트 grounding 감쇠 0.
    project_scoped 는 이미 스코프된 키(scoped_project 멱등). 없음/실패 → ([], 1.0).
    """
    name = (project_scoped or "").strip()
    if not name:
        return [], 1.0
    try:
        from app.service.query_repository import get_project_graph
        pg = await get_project_graph(name, team_id="")
        return graph_gaps_to_questions(extract_graph_gap_codes(pg)), graph_readiness(pg)
    except Exception:  # noqa: BLE001 — 조회 실패는 인터뷰를 막지 않는다(보강·grounding 생략)
        logger.exception("interview: 그래프 컨텍스트 조회 실패 — 보강/grounding 생략")
        return [], 1.0


async def graph_gap_questions(project_scoped: str) -> List[str]:
    """[T5] 보강 질문만 필요할 때 — graph_interview_context 의 첫 요소."""
    qs, _ = await graph_interview_context(project_scoped)
    return qs


# ─── [2026-06-12] 보강(supplement) 모드 — 프로젝트 현황 + 의제 ────────────────
#
# [실사고] 진행 중 프로젝트('ai agent', PRD 98%)에서 "부족한 부분 채워줘" 를 입력해도
# 인터뷰가 "혹시 만들고 계신 앱이 있으신가요?" 를 반복 — 모델 입장에선 빈 대화 +
# 빈 초안 + (설계가 멀쩡해) 빈 그래프 갭뿐이라 그게 유일한 합리적 질문이었다.
# 정작 '부족한 부분' 목록(PRD lint 이슈·autofix needs_input)은 시스템이 이미 갖고
# 있는데 인터뷰에 연결돼 있지 않았다. 여기서 프로젝트 브리프 + 의제로 연결한다.


@dataclass
class InterviewProjectContext:
    """인터뷰 턴에 주입되는 프로젝트 컨텍스트 묶음.

    brief 가 비면 greenfield(기존 동작과 동일) — supplement=False.
    """

    brief: str = ""
    gap_questions: List[str] = field(default_factory=list)
    graph_readiness: float = 1.0
    supplement: bool = False
    # [B-1] 소유 검증 + 스코프 완료된 프로젝트 키 — ACTION 도구 조회 전용.
    # 라우트의 assert_access 이후에만 set (IDOR 차단은 그 단계가 담당).
    project_scoped: str = ""


def _format_project_brief(
    display_name: str,
    prd_content: str,
    lint_report: Any,
    *,
    eval_pct: Optional[int] = None,
    meeting_excerpt: str = "",
) -> str:
    """프로젝트 현황 요약. 매 턴 프롬프트에 실리므로 _MAX_BRIEF_CHARS 캡.

    개요 발췌는 PRD 앞부분(Overview 가 항상 1번 섹션) — 섹션 파싱보다 견고.
    [Phase 1 — 2026-06-12] 설계 평가 점수(기획서 완성도)와 최근 회의록 발췌 추가 —
    "점수가 왜 낮아?" / "회의록 봤어?" 에 데이터로 답할 수 있는 근거.
    """
    lines = [f"프로젝트명: {display_name}"]
    if lint_report is not None:
        score_pct = round((getattr(lint_report, "score", 0.0) or 0.0) * 100)
        stories = (getattr(lint_report, "summary", None) or {}).get("stories_found", 0)
        issues = len(getattr(lint_report, "issues", []) or [])
        lines.append(f"PRD 충실도: {score_pct}% · Story {stories}개 · 남은 이슈 {issues}건")
    if eval_pct is not None:
        lines.append(
            f"기획서 완성도(설계 평가): {eval_pct}% — 낮은 이유는 [빠진 것들] 목록이 그 원인"
        )
    overview = (prd_content or "").strip()[:700]
    if overview:
        lines.append(f"PRD 개요(발췌):\n{overview}")
    if meeting_excerpt:
        lines.append(f"최근 회의록(발췌):\n{meeting_excerpt}")
    return "\n".join(lines)[:_MAX_BRIEF_CHARS]


def _lint_issues_to_agenda(lint_report: Any, cap: int = 5) -> List[str]:
    """PRD lint 이슈 → 의제 문구. error > warning > info 순으로 상위 cap 개."""
    issues = list(getattr(lint_report, "issues", []) or [])
    rank = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: rank.get(getattr(i, "severity", "info"), 3))
    return [getattr(i, "message", "") for i in issues[:cap] if getattr(i, "message", "")]


def fix_targets_to_agenda(fix_targets, cap: int = 5) -> List[str]:
    """[Phase 1 — 2026-06-12] eval fix_targets → 항목 이름까지 담은 의제 문구.

    설계 페이지 '기획서 완성도' 모달과 같은 데이터(collect_fix_targets) — 기존
    _GAP_QUESTIONS(코드당 일반 질문 1개, 이름 없음)와 달리 "어느 항목이 몇 건
    비었는지"를 인터뷰가 콕 집어 말할 수 있다. dict/객체 duck-typing. 순수 함수.
    """
    out: List[str] = []
    for ft in fix_targets or []:
        if len(out) >= cap:
            break
        get = ft.get if isinstance(ft, dict) else lambda k, _ft=ft: getattr(_ft, k, None)
        label = str(get("label") or "").strip()
        missing = list(get("missing") or [])
        total_missing = int(get("missing_total") or len(missing) or 0)
        if not label or total_missing <= 0:
            continue
        names = []
        for m in missing[:3]:
            mget = m.get if isinstance(m, dict) else lambda k, _m=m: getattr(_m, k, None)
            nm = str(mget("name") or mget("id") or "").strip()
            if nm:
                names.append(nm)
        suffix = " 외" if total_missing > len(names) else ""
        ex = f" (예: {', '.join(names)}{suffix})" if names else ""
        fix = str(get("fix") or "").strip()
        line = f"{label} — {total_missing}건 미비{ex}." + (f" {fix}" if fix else "")
        out.append(line[:200])
    return out


async def _fetch_eval_context(project_scoped: str) -> Tuple[Optional[int], List[dict]]:
    """[Phase 1] 설계 평가 — 기획서 완성도 %와 이름 포함 보강 대상(fix_targets).

    eval-score 라우트(get_eval_score)와 같은 계산(LLM 없음, ~100ms) — FE 가 보는
    그 점수·그 목록을 인터뷰도 본다. 설계 그래프가 아직 없으면 (None, []) 로
    완성도 언급 자체를 생략하고, 조회 실패도 (None, []) (인터뷰를 막지 않음).
    """
    name = (project_scoped or "").strip()
    if not name:
        return None, []
    try:
        # 지연 import — interview 모듈 로드가 평가 체인을 끌어오지 않게.
        from app.pipelines.design_validator import (
            normalize_architecture,
            normalize_ddd,
            normalize_spack,
            summarize_reports,
        )
        from app.pipelines.design_validator.eval_backfill import backfill_graph_dicts
        from app.service import query_repository
        from evals.fix_targets import collect_fix_targets
        from evals.scorer import score_spack

        spack = await query_repository.get_spack_graph(name, team_id="")
        ddd = await query_repository.get_ddd_graph(name, team_id="")
        arch = await query_repository.get_architecture_graph(name, team_id="")
        spack_dict = spack.model_dump()
        ddd_dict = ddd.model_dump()
        arch_dict = arch.model_dump()
        # 설계가 아직 없으면(greenfield) 점수도 보강 대상도 없음 — 0% 오인 방지.
        if not (spack_dict.get("apis") or spack_dict.get("entities") or ddd_dict.get("contexts")):
            return None, []

        backfill_graph_dicts(spack_dict, ddd_dict, arch_dict)
        norm_spack, spack_report = normalize_spack(spack_dict)
        norm_ddd, ddd_report = normalize_ddd(ddd_dict, norm_spack)
        _, arch_report = normalize_architecture(arch_dict, norm_spack, norm_ddd)
        summary = summarize_reports(spack_report, ddd_report, arch_report)
        report = score_spack(
            spack_dict,
            ddd=ddd_dict,
            arch=arch_dict,
            validation_report={
                "total_errors": summary.get("total_errors", 0),
                "total_warnings": summary.get("total_warnings", 0),
                "total_infos": 0,
            },
        )
        fix_targets = collect_fix_targets(norm_spack, ddd=norm_ddd, arch=arch_dict)
        return round(report.overall * 100), fix_targets
    except Exception:  # noqa: BLE001 — 평가 실패는 인터뷰를 막지 않는다(완성도 생략)
        logger.exception("interview: 설계 평가 조회 실패 — 완성도/보강 대상 생략")
        return None, []


async def build_interview_project_context(
    project_scoped: str,
    *,
    display_name: str = "",
    agenda: Optional[List[str]] = None,
) -> InterviewProjectContext:
    """프로젝트가 있으면 브리프 + 통합 의제를 만든다.

    의제 우선순위: FE agenda > 설계 평가 fix_targets(이름 포함) > PRD lint > 그래프 갭.
    PRD·회의록이 모두 없으면(신규 프로젝트) greenfield 컨텍스트 — 기존 인터뷰와 동일.
    각 소스의 조회 실패는 인터뷰를 막지 않는다(해당 소스만 생략).
    agenda: FE 가 들고 온 우선 의제 (예: PRD autofix 의 needs_input 질문들).
    """
    name = (project_scoped or "").strip()
    fe_agenda = [str(a).strip()[:200] for a in (agenda or []) if str(a).strip()]
    fe_agenda = fe_agenda[:_MAX_AGENDA_ITEMS]
    if not name:
        return InterviewProjectContext(gap_questions=fe_agenda, supplement=bool(fe_agenda))

    gap_qs, readiness = await graph_interview_context(name)

    prd_content = ""
    lint_report = None
    lint_agenda: List[str] = []
    try:
        from app.pipelines.prd_lint import lint_prd
        from app.service.query_repository import get_master_prd

        prd = await get_master_prd(name)  # scoped 멱등 — 이미 스코프된 키 그대로 사용
        if prd is not None and (prd.prd_content or "").strip():
            prd_content = prd.prd_content
            lint_report = lint_prd(prd_content)
            lint_agenda = _lint_issues_to_agenda(lint_report)
    except Exception:  # noqa: BLE001 — PRD 조회/lint 실패는 해당 소스만 생략
        logger.exception("interview: PRD 컨텍스트 생략 (조회/lint 실패)")

    # [Phase 1] 회의록 — "미팅 로그도 이해 못한다" 보완. 시간순 join 의 꼬리 = 최신.
    meeting_excerpt = ""
    try:
        from app.service.query_repository import get_all_meeting_content

        meetings = await get_all_meeting_content(name)
        meeting_excerpt = (meetings or "").strip()[-_MAX_MEETING_EXCERPT_CHARS:]
    except Exception:  # noqa: BLE001 — 회의록 조회 실패는 발췌만 생략
        logger.exception("interview: 회의록 발췌 생략 (조회 실패)")

    # [Phase 1] 설계 평가 — FE 완성도 모달과 같은 점수·이름 포함 미비 목록.
    eval_pct, fix_targets = await _fetch_eval_context(name)
    fix_agenda = fix_targets_to_agenda(fix_targets)

    brief = ""
    if prd_content or meeting_excerpt:
        brief = _format_project_brief(
            display_name or name,
            prd_content,
            lint_report,
            eval_pct=eval_pct,
            meeting_excerpt=meeting_excerpt,
        )

    # 의제 병합 — FE(사용자 의도와 가장 가까움) > 평가 fix_targets > lint > 그래프 갭.
    merged: List[str] = []
    for q in [*fe_agenda, *fix_agenda, *lint_agenda, *gap_qs]:
        if q and q not in merged:
            merged.append(q)
        if len(merged) >= _MAX_AGENDA_ITEMS:
            break

    return InterviewProjectContext(
        brief=brief,
        gap_questions=merged,
        graph_readiness=readiness,
        supplement=bool(brief),
        project_scoped=name,
    )


# ─── [T7] 빌드 검증(lint) 환류 — 코드↔설계 검증 결과를 다음 플랜에 되먹임 ────
def lint_failures_to_feedback(lint_result, cap: int = 6) -> List[str]:
    """LintResult → '이전 빌드에서 코드에 없던 설계 항목' 피드백 (applied==False 룰).

    빌드된 코드 vs 설계 그래프 검증(run_lint_pipeline) 결과 중 미구현(applied=False)
    룰의 설명을 모아 다음 build_plan 이 반영하도록 환류. dict/LintResult(.cases[].rules
    [].applied/description) 모두 수용(duck-typed). 순수 함수.
    """
    cases = getattr(lint_result, "cases", None)
    if cases is None and isinstance(lint_result, dict):
        cases = lint_result.get("cases")
    out: List[str] = []
    seen = set()
    for c in cases or []:
        rules = getattr(c, "rules", None)
        if rules is None and isinstance(c, dict):
            rules = c.get("rules")
        for r in rules or []:
            applied = r.get("applied") if isinstance(r, dict) else getattr(r, "applied", None)
            if applied is not False:  # True/None(미상) → 미구현 아님, 건너뜀(보수적)
                continue
            desc = r.get("description") if isinstance(r, dict) else getattr(r, "description", None)
            rule = r.get("rule") if isinstance(r, dict) else getattr(r, "rule", None)
            label = str(desc or rule or "").strip()
            if label and label not in seen:
                seen.add(label)
                out.append(label)
    return out[:cap]


def _format_lint_feedback(lint_result) -> str:
    """LintResult → build_plan 컨텍스트용 '이전 빌드 검증' 블록. 미구현 없으면 ''."""
    if lint_result is None:
        return ""
    fails = lint_failures_to_feedback(lint_result)
    if not fails:
        return ""
    body = "\n".join(f"  · {f}" for f in fails)
    return (
        "- 이전 빌드 검증(코드↔설계): 아래 설계 항목이 코드에 없었음 — "
        f"이번 플랜에서 반드시 구현/확인하라:\n{body}"
    )


# ─── [B-1 — 2026-06-12] 읽기 코파일럿 도구 (ACTION 텍스트 프로토콜) ──────────
#
# 보강 모드에서 모델이 브리프(요약)만으로 부족할 때 프로젝트 원자료를 직접
# 조회한다. gemini 클라이언트가 native function calling 미지원(텍스트 in/out)
# 이라, 기존 PHASE:/MESSAGE: 와 같은 결의 한 줄 텍스트 프로토콜을 쓴다:
#   모델이 MESSAGE 대신 `ACTION: <도구>` 한 줄 출력 → 서버가 실행 →
#   [조회 결과]로 재프롬프트 → 모델이 데이터 근거로 답변. 턴당 최대 2회.
# 읽기 전용(쓰기 액션 없음) — B-2 에서 확인 버튼 흐름과 함께 별도 도입.

_MAX_TOOL_CALLS = 2
# 도구 출력 캡 — flash 컨텍스트는 넉넉하나 토큰 비용·프롬프트 집중도 관리.
_TOOL_OUTPUT_CAPS = {"prd": 6_000, "meetings": 4_000, "eval": 2_000, "design": 2_000}

_TOOL_DESCRIPTIONS = {
    "prd": "PRD(기획서) 전문",
    "meetings": "회의록 원문 (시간순 — 끝이 최신)",
    "eval": "기획서 완성도 점수 + 미비 항목 전체 목록(이름 포함)",
    "design": "설계 그래프 요약 (도메인·엔티티·API·의존 관계)",
}


def parse_action(text: str) -> Optional[str]:
    """모델 출력에서 ACTION 도구명 추출 — _has_message() 가 False 일 때만 쓸 것.

    첫 `ACTION:` 줄의 첫 단어를 whitelist 검증. 없거나 미등록 도구면 None.
    """
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("ACTION:"):
            rest = s[7:].strip()
            tool = rest.split()[0].lower() if rest else ""
            return tool if tool in _TOOL_DESCRIPTIONS else None
    return None


def _has_message(text: str) -> bool:
    """MESSAGE 필드가 실제 내용과 함께 있는지 — 있으면 ACTION 무시(답변 우선).

    스트리밍과의 정합: _MsgExtractor 는 MESSAGE 가 있어야만 토큰을 내보내므로,
    '토큰이 이미 사용자에게 보였는데 도구를 도는' 어긋남이 구조적으로 없다.
    """
    return any(
        line.startswith("MESSAGE:") and line[8:].strip()
        for line in (text or "").splitlines()
    )


async def execute_interview_tool(ctx: PipelineContext, tool: str, project_scoped: str) -> str:
    """도구 1회 실행 → 텍스트 결과 (캡 적용). 실패는 안내 문구로 강등(인터뷰 보호)."""
    name = (project_scoped or "").strip()
    if not name or tool not in _TOOL_DESCRIPTIONS:
        return "(조회 불가)"
    cap = _TOOL_OUTPUT_CAPS.get(tool, 2_000)
    try:
        if tool == "prd":
            from app.service.query_repository import get_master_prd

            prd = await get_master_prd(name)
            content = (getattr(prd, "prd_content", "") or "").strip() if prd else ""
            return content[:cap] or "(PRD 없음)"
        if tool == "meetings":
            from app.service.query_repository import get_all_meeting_content

            meetings = ((await get_all_meeting_content(name)) or "").strip()
            return meetings[-cap:] or "(회의록 없음)"
        if tool == "eval":
            pct, fts = await _fetch_eval_context(name)
            if pct is None:
                return "(아직 설계가 없어 완성도 평가가 없습니다)"
            lines = [f"기획서 완성도: {pct}%", *fix_targets_to_agenda(fts, cap=10)]
            return "\n".join(lines)[:cap]
        if tool == "design":
            summary = await build_graph_summary(ctx, name)
            return (summary or "(설계 그래프 없음)")[:cap]
    except Exception:  # noqa: BLE001 — 도구 실패가 턴을 깨지 않게
        logger.exception("interview: 도구 실행 실패 — %s", tool)
    return "(조회 실패 — 이미 가진 자료로 답하세요)"


def _render_tools(enabled: bool) -> str:
    if not enabled:
        return "(사용 불가 — ACTION 을 출력하지 마세요.)"
    return "\n".join(f"- `{k}`: {v}" for k, v in _TOOL_DESCRIPTIONS.items())


def _render_tool_results(results: Optional[List[Tuple[str, str]]]) -> str:
    if not results:
        return "(없음)"
    return "\n\n".join(f"[{tool}]\n{body}" for tool, body in results)


def _apply_readiness_gate(
    turn: InterviewTurn,
    user_turns: int,
    force_finalize: bool,
    graph_readiness: float = 1.0,
    supplement: bool = False,
) -> None:
    """[T2+T8] done 판정을 모델 감 → 정량 준비도로 게이팅 (turn 을 in-place 수정).

    - 하드캡(force_finalize): 모델이 ask 여도 강제 done (기존 폭주 방지 유지).
    - 모델 done: 준비도≥임계 + 최소턴 충족 시만 수락. 아니면 ask 로 되돌리고
      최약 차원 질문으로 교체 → 조기 종료(추측 회의록) 방지.
    - 모델 ask 지만 준비도 충분 + 최소턴: auto-stop done (OMC 식).

    [객관 보정] 자가보고 readiness 를 설계 그래프 완성도(graph_readiness)로 감쇠해
    모델 과대평가를 막는다. greenfield(graph_readiness=1.0) → 감쇠 0(기존과 동일).
    감쇠된 값을 turn.readiness 에 반영(응답에 정직한 수치 노출)하고 게이트에 사용한다.
    합성(meeting_content)은 호출부가 phase==done 일 때만 하므로 여기선 phase 만 정한다.
    """
    if supplement:
        # [2026-06-12 보강 모드] 핵심 5개는 프로젝트 현황으로 충족 — 의제 소진이
        # 기준이므로 최소 턴을 1로 완화. 그래프 감쇠도 비활성: '미완성이라서 보강하러
        # 온' 사람에게 미완성을 이유로 인터뷰를 더 끄는 역설 방지.
        min_met = user_turns >= _MIN_USER_TURNS_SUPPLEMENT
        effective = round(turn.readiness, 3)
    else:
        min_met = user_turns >= _MIN_USER_TURNS
        # 자가점수 × (1 - W + W·완성도) — 완성도 1.0 이면 그대로, 낮을수록 보수적.
        effective = round(
            turn.readiness
            * (1.0 - _GRAPH_GROUNDING_WEIGHT + _GRAPH_GROUNDING_WEIGHT * graph_readiness),
            3,
        )
    turn.readiness = effective
    # [T9] soft-cap: 충분히 길어지면 임계를 낮춰 정체 시 그만 끈다(하드캡 전 우아한 마무리).
    threshold = (
        _READINESS_SOFT_THRESHOLD if user_turns >= _SOFT_CAP_TURNS else _READINESS_DONE_THRESHOLD
    )
    ready = effective >= threshold

    if force_finalize:
        if turn.phase != "done":
            logger.warning("interview: 턴 상한 도달 — 강제 done 전환")
            turn.phase = "done"
            turn.assistant_message = "말씀해 주신 내용을 바탕으로 정리해 시작할게요!"
            turn.suggestions = []
        return

    if turn.phase == "done":
        if ready and min_met:
            return  # 준비도 충족 — 모델 done 수락
        logger.info(
            "interview: 모델 done 이나 준비도 부족(readiness=%.2f, turns=%d) — 계속 질문",
            turn.readiness, user_turns,
        )
        turn.phase = "ask"
        turn.assistant_message = _targeted_question(turn.scores)
        turn.suggestions = []
        return

    # 모델 ask 지만 준비도 충분 → 더 끌지 않고 마무리.
    if ready and min_met:
        logger.info("interview: 준비도 충족(readiness=%.2f) — auto-stop done", turn.readiness)
        turn.phase = "done"
        turn.assistant_message = "핵심은 다 모인 것 같아요. 바로 정리해 드릴게요!"
        turn.suggestions = []


def _render_project(project_brief: str) -> str:
    """프로젝트 현황(브리프) 렌더 — 비면 빈 문자열 (greenfield 프롬프트는 기존과 동일).

    [2026-06-12] 보강 모드의 핵심 입력: 이게 있어야 모델이 '무슨 앱 만드세요?' 대신
    '진행 중인 {프로젝트} 봤어요 — 이 부분부터 채울게요' 로 시작할 수 있다.
    """
    b = (project_brief or "").strip()
    if not b:
        return "(없음 — 새 기획입니다. (A) 빈 상태로 시작하세요.)"
    return b[:_MAX_BRIEF_CHARS]


def _interview_prompt(
    history: List[InterviewMessage],
    existing_content: str = "",
    gap_questions: Optional[List[str]] = None,
    project_brief: str = "",
    tools_enabled: bool = False,
    tool_results: Optional[List[Tuple[str, str]]] = None,
) -> str:
    """안전 프리앰블 + 인터뷰 단계 프롬프트를 합성 (history/기존 초안 치환).

    오케스트레이터의 compose_prompt 를 거쳐 거대 프롬프트 대신 "공통 안전 +
    이 단계 하나"만 LLM 에 전달 — 과부하/지시 경쟁 방지.

    existing_content: 사용자가 이미 작성한 회의록 초안 (보완 인터뷰). 비면 빈 상태.
    project_brief: [2026-06-12] 진행 중 프로젝트의 현황 요약 (보강 모드). 비면 신규.
    tools_enabled/tool_results: [B-1] ACTION 읽기 도구 — 보강 모드 전용.
    """
    return compose_prompt(
        GuidePhase.INTERVIEW,
        variables={
            "{{HISTORY}}": _render_history(history),
            "{{EXISTING}}": _render_existing(existing_content),
            "{{GAPS}}": _render_gaps(gap_questions),
            "{{PROJECT}}": _render_project(project_brief),
            "{{TOOLS}}": _render_tools(tools_enabled),
            "{{TOOL_RESULTS}}": _render_tool_results(tool_results),
        },
    )


def _render_gaps(gap_questions: Optional[List[str]]) -> str:
    qs = [q for q in (gap_questions or []) if q]
    if not qs:
        return "(없음 — 새 기획이거나 기존 설계에 큰 갭이 없습니다.)"
    return "\n".join(f"- {q}" for q in qs)


def _render_history(history: List[InterviewMessage]) -> str:
    if not history:
        return "(아직 대화 없음 — 첫 턴입니다. 위 '두 가지 진입 상황'에 따라 첫 질문을 결정하세요.)"
    lines = []
    for m in history:
        who = "사용자" if m.role == _ROLE_USER else "인터뷰어"
        lines.append(f"{who}: {m.content}")
    return "\n".join(lines)


def _render_existing(existing_content: str) -> str:
    text = (existing_content or "").strip()
    if not text:
        return "(없음 — 빈 상태에서 시작합니다.)"
    return text


def _parse_turn(text: str) -> InterviewTurn:
    """plain-text 출력(PHASE:/MESSAGE:/SUGGESTIONS:/COVERAGE:/MEETING_CONTENT:)을 파싱.

    필드 누락이나 형식 오류 시 안전 폴백 반환.
    """
    lines = (text or "").strip().splitlines()
    phase = "ask"
    msg = ""
    suggestions: List[str] = []
    coverage: List[str] = []
    scores: dict = {}
    meeting_lines: List[str] = []
    in_meeting = False

    for line in lines:
        if in_meeting:
            meeting_lines.append(line)
            continue
        if line.startswith("PHASE:"):
            val = line[6:].strip()
            if val in ("ask", "done"):
                phase = val
        elif line.startswith("MESSAGE:"):
            msg = line[8:].strip()
        elif line.startswith("SUGGESTIONS:"):
            raw = line[12:].strip()
            if raw:
                suggestions = [s.strip() for s in raw.split("|") if s.strip()][:4]
        elif line.startswith("COVERAGE:"):
            raw = line[9:].strip()
            if raw:
                coverage = [c.strip() for c in raw.split("|") if c.strip()]
        elif line.startswith("SCORES:"):
            raw = line[7:].strip()
            if raw:
                scores = _parse_scores(raw)
        elif line.startswith("MEETING_CONTENT:"):
            in_meeting = True
            rest = line[16:].strip()
            if rest:
                meeting_lines.append(rest)

    if not msg:
        logger.warning("interview: MESSAGE 필드 누락 — 폴백 질문 반환")
        return InterviewTurn(
            phase="ask",
            assistant_message="조금 더 알려주시겠어요? 만들고 싶은 서비스가 무엇이고, 누가 주로 사용하게 될까요?",
        )
    # 질문 프롬프트는 더 이상 MEETING_CONTENT 를 내지 않는다(합성은 별도 단계).
    # 혹시 모델이 넣었으면 보존하되, done 판단은 PHASE 만으로 — 빈 meeting 으로 강등하지 않음.
    meeting = "\n".join(meeting_lines).strip()
    return InterviewTurn(
        phase=phase,
        assistant_message=msg,
        suggestions=suggestions,
        coverage=coverage,
        meeting_content=meeting,
        scores=scores,
        readiness=compute_readiness(scores),
    )


class _MsgExtractor:
    """스트리밍 청크에서 MESSAGE: 필드 내용을 실시간 추출.

    'MESSAGE: ' 패턴을 감지한 뒤 개행 전까지의 문자를 하나씩 yield 한다.
    """

    _NEEDLE = "MESSAGE: "

    def __init__(self) -> None:
        self._buf = ""
        self._in_msg = False
        self._exhausted = False

    def feed(self, chunk: str) -> List[str]:
        if self._exhausted:
            return []
        tokens: List[str] = []
        for ch in chunk:
            if self._in_msg:
                if ch == "\n":
                    self._in_msg = False
                    self._exhausted = True
                    break
                tokens.append(ch)
            else:
                self._buf += ch
                if self._buf.endswith(self._NEEDLE):
                    self._in_msg = True
                    self._buf = ""
                elif len(self._buf) > len(self._NEEDLE):
                    self._buf = self._buf[-len(self._NEEDLE):]
        return tokens


def _synthesize_prompt(history: List[InterviewMessage], existing_content: str = "") -> str:
    """합성 단계 프롬프트 — 안전 프리앰블 + phase_synthesize.md (history/초안 치환)."""
    return compose_with_safety(
        _SYNTHESIZE_PROMPT_FILE,
        variables={
            "{{HISTORY}}": _render_history(history),
            "{{EXISTING}}": _render_existing(existing_content),
        },
    )


def _clean_markdown(text: str) -> str:
    """합성 출력 정리 — 양끝 공백 + 실수로 감싼 코드펜스 제거."""
    t = (text or "").strip()
    if t.startswith("```"):
        # 첫 줄(``` 또는 ```markdown)과 마지막 ``` 제거
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


async def _synthesize_meeting_content(
    ctx: PipelineContext,
    history: List[InterviewMessage],
    existing_content: str,
) -> str:
    """구독 등급 모델(Pro 등 = 클라이언트 기본, model=None)로 회의록 본문 합성.

    실패하거나 빈 결과면 대화 원문 기반 폴백으로 최소 회의록을 만든다.
    """
    try:
        result = await ctx.gemini.generate(
            _synthesize_prompt(history, existing_content),
            temperature=_TEMPERATURE,
            model=_INTERVIEW_MODEL,
        )
        meeting = _clean_markdown(result.text)
        if meeting:
            return meeting
        logger.warning("interview: 합성 결과 비어 폴백 사용")
    except Exception:  # noqa: BLE001 — 합성 실패해도 폴백으로 진행 (사용자 흐름 보호)
        logger.exception("interview: 합성 호출 실패 — 폴백 사용")
    return _fallback_meeting_content(history, existing_content)


async def run_interview_turn(
    ctx: PipelineContext,
    history: List[InterviewMessage],
    existing_content: str = "",
    gap_questions: Optional[List[str]] = None,
    graph_readiness: float = 1.0,
    project_brief: str = "",
    supplement: bool = False,
    project_scoped: str = "",
) -> InterviewTurn:
    """대화 history 를 받아 다음 턴을 반환. done 이면 회의록까지 합성해 채운다.

    질문·합성 모두 gemini-2.5-flash. 질문 프롬프트는 슬림(템플릿 분리),
    합성만 별도 phase_synthesize.md 호출.
    [B-1] 보강 모드 + project_scoped 가 있으면 모델이 ACTION 으로 원자료를
    조회(최대 _MAX_TOOL_CALLS회)한 뒤 답한다.

    Args:
        ctx: tracked gemini 를 담은 컨텍스트 (토큰 누적은 ctx 가 담당).
        history: 지금까지의 대화. 마지막은 보통 사용자 발화.
        existing_content: 사용자가 이미 작성한 회의록 초안 (보완 인터뷰). 비면 빈 상태.
        project_scoped: 소유 검증 + 스코프 완료된 프로젝트 키 (도구 조회 전용).
    """
    user_turns = sum(1 for m in history if m.role == _ROLE_USER)
    force_finalize = user_turns >= _MAX_TURNS
    tools_enabled = supplement and bool((project_scoped or "").strip())

    # 질문 턴 — flash 로 다음 질문/마무리 판단. ACTION 이면 도구 실행 후 재호출.
    tool_results: List[Tuple[str, str]] = []
    text = ""
    for _ in range(_MAX_TOOL_CALLS + 1):
        result = await ctx.gemini.generate(
            _interview_prompt(
                history, existing_content, gap_questions, project_brief,
                tools_enabled=tools_enabled, tool_results=tool_results,
            ),
            temperature=_TEMPERATURE,
            model=_INTERVIEW_MODEL,
        )
        text = result.text
        if tools_enabled and len(tool_results) < _MAX_TOOL_CALLS and not _has_message(text):
            tool = parse_action(text)
            if tool:
                tool_results.append((tool, await execute_interview_tool(ctx, tool, project_scoped)))
                continue
        break
    turn = _parse_turn(text)

    # [T2+T8] 정량 준비도 게이트 — done 을 점수(그래프 완성도로 보정)로 결정.
    _apply_readiness_gate(turn, user_turns, force_finalize, graph_readiness, supplement)
    # [T3] ask 턴이면 다음 집중 차원을 표시 (FE 힌트).
    turn.next_focus = weakest_dimension(turn.scores) if turn.phase == "ask" else None

    # done 이면 구독 등급 모델로 회의록 본문 합성. ask 면 meeting_content 는 항상 빈 값
    # (모델이 done+MEETING_CONTENT 흘렸다가 게이트가 ask 로 되돌린 경우의 누출 방지).
    if turn.phase == "done":
        turn.meeting_content = await _synthesize_meeting_content(ctx, history, existing_content)
    else:
        turn.meeting_content = ""
    return turn


async def run_interview_turn_stream(
    ctx: PipelineContext,
    history: List[InterviewMessage],
    existing_content: str = "",
    gap_questions: Optional[List[str]] = None,
    graph_readiness: float = 1.0,
    project_brief: str = "",
    supplement: bool = False,
    project_scoped: str = "",
) -> AsyncGenerator[Tuple[str, Any], None]:
    """스트리밍 버전 — 이벤트 시퀀스:
        ("tool", str)           [B-1] ACTION 도구 실행 시작 (FE: "자료 확인 중" 표시)
        ("token", str)          질문 메시지 청크 (빠른 모델, 도착 즉시)
        ("finalizing", None)    done 판정 후 회의록 합성 시작 (FE: "정리 중" 표시)
        ("done", InterviewTurn) 최종 — ask 면 다음 질문, done 이면 합성된 meeting_content 포함

    질문·합성 모두 gemini-2.5-flash. 질문은 슬림 프롬프트로 스트리밍, done 합성만 별도 호출.
    existing_content: 사용자가 이미 작성한 회의록 초안 (보완 인터뷰). 비면 빈 상태.

    [B-1 스트리밍 정합] _MsgExtractor 는 MESSAGE 가 있어야만 토큰을 내보내고,
    ACTION 턴은 MESSAGE 가 없으므로 토큰 누출 없이 도구만 돈다 — "이미 보여준
    글자와 다른 답" 어긋남이 구조적으로 차단된다.
    """
    user_turns = sum(1 for m in history if m.role == _ROLE_USER)
    force_finalize = user_turns >= _MAX_TURNS
    tools_enabled = supplement and bool((project_scoped or "").strip())

    tool_results: List[Tuple[str, str]] = []
    full_text = ""
    for _ in range(_MAX_TOOL_CALLS + 1):
        extractor = _MsgExtractor()
        full_chunks: List[str] = []
        async for chunk in ctx.gemini.generate_stream(
            _interview_prompt(
                history, existing_content, gap_questions, project_brief,
                tools_enabled=tools_enabled, tool_results=tool_results,
            ),
            temperature=_TEMPERATURE,
            model=_INTERVIEW_MODEL,
        ):
            full_chunks.append(chunk)
            for token in extractor.feed(chunk):
                yield ("token", token)
        full_text = "".join(full_chunks)
        if tools_enabled and len(tool_results) < _MAX_TOOL_CALLS and not _has_message(full_text):
            tool = parse_action(full_text)
            if tool:
                yield ("tool", tool)
                tool_results.append((tool, await execute_interview_tool(ctx, tool, project_scoped)))
                continue
        break

    turn = _parse_turn(full_text)

    # [T2] 정량 준비도 게이트 — 비스트리밍과 동일 규칙. 메시지를 교체할 수 있으나,
    # 스트림된 토큰 ≠ 최종 메시지는 force_finalize 에서 이미 쓰던 패턴(최종 turn 이 권위).
    _apply_readiness_gate(turn, user_turns, force_finalize, graph_readiness, supplement)
    # [T3] ask 턴이면 다음 집중 차원을 표시 (FE 힌트).
    turn.next_focus = weakest_dimension(turn.scores) if turn.phase == "ask" else None

    if turn.phase == "done":
        # 합성은 시간이 걸리므로 FE 가 "정리 중" 을 띄우도록 알린 뒤 Pro 로 합성.
        yield ("finalizing", None)
        turn.meeting_content = await _synthesize_meeting_content(ctx, history, existing_content)
    else:
        turn.meeting_content = ""  # ask 턴은 회의록 없음 (down-convert 시 누출 방지)

    yield ("done", turn)


def _fallback_meeting_content(history: List[InterviewMessage], existing_content: str = "") -> str:
    """LLM 합성 실패 시 — 기존 초안을 보존하고 대화 원문을 덧붙여 최소 회의록 생성."""
    user_says = [m.content.strip() for m in history if m.role == _ROLE_USER and m.content.strip()]
    body = "\n".join(f"- {s}" for s in user_says) or "- (내용 없음)"
    existing = (existing_content or "").strip()
    if existing:
        # 보완 상황 — 기존 초안을 통째로 보존하고 대화 보강분을 아래에 덧붙임 (절대 덮어쓰지 않음).
        return f"{existing}\n\n# 인터뷰 보강 내용\n{body}\n"
    return f"# 프로젝트 개요\n사용자 인터뷰로 수집된 요구사항입니다.\n\n# 주요 내용\n{body}\n"
