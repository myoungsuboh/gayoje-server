"""
Pipeline 공통 기반.

설계 원칙:
  - 한 파이프라인 = 하나의 async function (`run`). 하나의 webhook 엔드포인트에 매핑.
  - 모든 stage 는 dataclass 입력 → dataclass 출력. 중간 상태는 모두 직렬화 가능해야 함
    → PR2 에서 큐(arq)로 옮길 때 그대로 enqueue 가능.
  - 외부 효과(Gemini/Neo4j) 는 생성자 주입 → 테스트에서 fake 로 교체 가능.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


class _GeminiLike(Protocol):
    async def generate(self, prompt: str, *, temperature: float = ...) -> Any: ...


class _Neo4jLike(Protocol):
    async def run_cypher(self, cypher: str, params: Dict[str, Any] | None = ...) -> Any: ...

    # [2026-05] 인접 write 들을 atomic 트랜잭션으로 묶기 — 부분 commit 회피.
    # 미구현 ctx(테스트 fake 등)는 None 반환해도 무방 — 파이프라인 측에서 fallback.
    async def run_in_transaction(
        self,
        operations: "list[tuple[str, Dict[str, Any]]]",
    ) -> Any: ...


class Neo4jClientProxy:
    """
    `neo4j_client` 모듈 함수를 PipelineContext.neo4j Protocol 에 맞추는 공통 어댑터.

    [2026-05] 9개 라우트 파일에 산재했던 _Neo4jProxy 중복을 흡수.
    run_cypher 외에 run_in_transaction (인접 write atomic) 도 노출.
    """

    async def run_cypher(self, cypher: str, params: Dict[str, Any] | None = None):
        # 지연 import — base.py 가 neo4j_client 를 모듈 import 시점에 끌어오면
        # 테스트 환경에서 NEO4J_URI 강제 evaluation 발생.
        from app.clients import neo4j_client
        return await neo4j_client.run_cypher(cypher, params)

    async def run_in_transaction(
        self,
        operations: "list[tuple[str, Dict[str, Any]]]",
    ):
        from app.clients import neo4j_client
        return await neo4j_client.run_in_transaction(operations)


_CYPHER_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_safe_cypher_identifier(name: Any) -> bool:
    """
    Cypher label / relationship type / property key 로 안전하게 인터폴레이션 가능한지 검증.

    `$param` 바인딩이 불가능한 자리(라벨/관계타입/식별자)에 LLM 출력을 그대로
    꽂으면 그래프 파괴 인젝션 가능. 화이트리스트:
      - 영문자/언더스코어로 시작
      - 이후 영숫자/언더스코어만
      - 길이 1~64

    이 함수가 False 를 반환하면 호출자는 해당 노드/관계/속성을 **드롭** 해야 함
    (예외를 던지는 대신 silently drop — LLM 한 항목 오류가 전체를 망치지 않게).
    """
    if not isinstance(name, str):
        return False
    if not (1 <= len(name) <= 64):
        return False
    return bool(_CYPHER_IDENT_RE.match(name))


@dataclass(frozen=True)
class PipelineContext:
    """
    Pipeline 호출당 1회 생성. 외부 효과 주입 + 멱등성 키.

    idempotency_key 는 같은 입력에 대해 같은 결과를 보장해야 하는 단계가 있을 때 쓴다.
    현재는 로깅 식별자로만 사용 — PR2 에서 큐 dedup 키로 재활용 예정.

    [stage_callback — 2026-05-26 perf C]
    파이프라인이 주요 LLM 단계 시작 직전에 호출하는 hook (옵션). arq worker 가
    Redis 에 stage 마커를 기록하도록 wire 하면 FE 폴링이 sub-stage 표시 가능.
    None 이면 no-op — pipeline 호출자(tests, dry-run 등)가 stage 추적 안 함.
    """

    gemini: _GeminiLike
    neo4j: _Neo4jLike
    idempotency_key: str
    stage_callback: Optional[Callable[[str], Awaitable[None]]] = None
    user_email: str = ""
    # [멀티테넌시] 팀 컨텍스트 (빈 문자열=개인). design/skill 등 ctx 기반 read 가
    # 도메인 노드 project property 를 스코프 키로 매칭하는 데 사용.
    team_id: str = ""

    async def emit_stage(self, stage: str) -> None:
        """stage_callback 이 있으면 호출. 실패해도 swallow (마커는 best-effort)."""
        cb = self.stage_callback
        if cb is None:
            return
        try:
            await cb(stage)
        except Exception:  # noqa: BLE001 — stage 마커는 best-effort
            pass


# ─── 공통 유틸 ─────────────────────────────────────────────


def escape_cypher_string(s: Any) -> str:
    """
    Save Meeting Log / Save CPS 단계의 escapeCypher() 헬퍼.

    Cypher 문자열 리터럴 안에 들어갈 텍스트를 이스케이프한다.
    backslash → \\\\, single quote → \\', newline/CR 동일.
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def strip_code_blocks(s: str) -> str:
    """
    CPS Section Filter1 / CPS Reassembler1 단계의 stripCodeBlocks() 헬퍼.
    LLM 출력 앞뒤의 ```json|```markdown 등 fence 를 제거.

    [2026-06-12 워커 동결 장애 fix]
    후행 fence 를 `\\n?\\s*```\\s*$` 정규식으로 지우던 코드는 fence 가 없는 입력에서
    모든 시작 위치마다 `\\s*` 그리디 백트래킹을 수행해 O(n²). flash-lite 가 구조화
    출력 모드에서 간헐적으로 뱉는 퇴행성 출력(수십만 자 공백/줄바꿈 블럭)에서는
    수십 분짜리 CPU 루프가 되어 워커 이벤트 루프 전체를 동결시켰다 (py-spy 확인:
    base.py:146 sub 에서 CPU 100% 고정, arq job_timeout 타이머조차 발화 불가).
    → C-속도 선형인 str.rstrip 기반으로 교체. 의미 동일: 끝쪽 공백을 무시하고
    ``` 로 끝나면 그 fence 만 제거. (선두 정규식은 `^` 앵커라 위치당 O(1) — 안전.)
    """
    if not s or not isinstance(s, str):
        return ""
    out = re.sub(r"^\s*```(?:json|markdown)?\s*\n?", "", s, flags=re.IGNORECASE)
    tail = out.rstrip()
    if tail.endswith("```"):
        out = tail[: -len("```")]
    return out.strip()


# [2026-05-19] PRD/CPS merge·rebuild agent 가 prompt 의 placeholder 를 그대로
# emit 한 케이스 사후 정리. HTML 주석 fix (commit 3154e1b) 와 같은 패턴 — prompt
# 규칙으로 막되 빠져나온 것은 server-side 에서 일관되게 '미정' 으로 대체.
#
# 관측된 leak 형태:
#   1) `[기능 영역\n  예: 핵심 데이터 관리]` — `[NAME\n  예: SAMPLE]` 멀티라인
#      template placeholder. 사용자가 본 케이스 (PRD Screen Architecture 의
#      `(from {에픽명})` 자리에서 노출).
#   2) `[도메인명 - 예: 사용자 계정 관리]`, `[예: prb_01]` — 단일라인 [...] 안에
#      "예:" 가 들어있는 placeholder. prd_extract.md OUTPUT SCHEMA 의 흔적.
#   3) `{에픽명}`, `{스토리 내용}`, `{화면명}` — curly placeholder 가 채워지지
#      않고 그대로 노출 (한글-only).
#
# 정책: 의심 영역만 좁게 잡고 안전한 fallback ("미정") 으로 치환. 정상 markdown
# 의 italic `*...*`, code span `` `...` ``, 정상 한글 텍스트는 건드리지 않음.
# bracket 안에 "예:" 가 들어있는 경우는 거의 100% template artifact 라 단일라인
# 도 포함.
_PLACEHOLDER_LEAK_PATTERNS: List[re.Pattern[str]] = [
    # 1) [...예:...] — 중첩 bracket 없는 [...] 안에 "예:" 가 들어있는 placeholder.
    #    멀티라인 / 단일라인 둘 다 매치. `[^\[\]]` 는 줄바꿈 포함 모든 비-bracket 글자.
    #    예시 매치:
    #      - `[기능 영역\n  예: 핵심 데이터 관리]`  (멀티라인)
    #      - `[도메인명 - 예: 사용자 계정 관리]`    (단일라인)
    #      - `[예: prb_01]`                          (단일라인, 짧음)
    re.compile(r"\[[^\[\]]*예\s*:[^\[\]]*\]"),
    # 2) {한글-only} — 한글 + 공백만 들어있는 curly placeholder. 한글 외 다른
    #    글자 (영문/숫자/구두점) 가 섞이면 정상 콘텐츠일 가능성 있어 미매치.
    re.compile(r"\{[가-힣\s]{1,40}\}"),
]


def strip_template_placeholders(s: str) -> str:
    """
    LLM 이 prompt template 의 placeholder 를 그대로 emit 한 흔적을 '미정' 으로 치환.

    prompt 규칙 (`NO PLACEHOLDERS`) 위반을 최종 단계에서 받아주는 safety net.
    빈 입력 / 비문자열 입력은 그대로 통과.
    """
    if not s or not isinstance(s, str):
        return s
    out = s
    for pat in _PLACEHOLDER_LEAK_PATTERNS:
        out = pat.sub("미정", out)
    return out


# ─── Determinism helpers (입력/출력 정규화) ────────────────────────
#
# [2026-05] LLM 비결정성 완화 정책의 첫 번째 layer — "결정성을 LLM 밖에서 확보".
# 같은 의미의 입력이 다르게 hash 되거나 같은 출력이 다른 순서로 표현되어도
# 다음 단계에서 byte-level 동일하도록 정규화.
#
# 정책:
#   - 의미 변화 0: NFC unicode, line endings, trailing whitespace, 연속 빈줄만 처리
#   - 토큰 추가 0: 오히려 약간 감소 (whitespace 제거)
#   - 위험 0: rollback 시 그냥 호출 안 하면 됨


def canonicalize_meeting_content(text: str) -> str:
    """
    회의록 입력 정규화 — 같은 의미의 텍스트를 결정적으로 동일 byte 로 변환.

    [무엇을 정규화하는가]
      - NFC unicode normalize: invisible composing character (e.g. 한글 NFD 입력) 통일
      - line endings: \\r\\n / \\r → \\n
      - trailing whitespace: 각 줄 끝 공백 제거
      - 연속 빈줄 3개 이상 → 2개로 축약
      - 전체 strip

    [무엇을 정규화하지 않는가]
      - 의미가 변할 수 있는 변경 (예: 양옆 공백 strip, 줄바꿈 제거, lowercase) 은 하지 않음
      - 의도적 들여쓰기는 보존

    [용도]
      - LLM 호출 직전에 1회 적용 → 클립보드/에디터 차이 흡수
      - cache 키 계산 시에도 동일 함수 사용 → 정규화 일관성 보장

    [위험성]
      거의 0. 사용자가 직접 작성한 회의록 중 trailing whitespace 나 \\r\\n 차이만 정규화.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    # 1. NFC unicode — 한글 NFD (조합형) 를 NFC (완성형) 로 통일.
    #    macOS 파일명에서 복사 시 NFD 인 경우가 있어 같은 글자가 다른 byte 가 됨.
    out = unicodedata.normalize("NFC", text)
    # 2. line endings — Windows 줄바꿈 (\r\n) 과 Mac 옛 (\r) 을 unix (\n) 로.
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    # 3. trailing whitespace — 각 줄 끝 공백/탭 제거. leading 은 보존 (들여쓰기 의도 가능).
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    # 4. 연속 빈줄 3개 이상 → 2개. 사용자가 spacing 위해 3~4 줄 비워도 의미 동일.
    out = re.sub(r"\n{3,}", "\n\n", out)
    # 5. 앞뒤 빈줄만 제거 — `.strip()` 은 첫 줄 leading 공백까지 지워서 의도된
    #    들여쓰기를 망친다. 줄바꿈만 trim 하고 줄 내부 공백은 보존.
    out = re.sub(r"^\n+", "", out)
    out = re.sub(r"\n+$", "", out)
    return out


def canonicalize_graph(graph: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    LLM 이 생성한 그래프 JSON 을 결정적 형태로 정규화.

    [무엇을 정규화하는가]
      - nodes 정렬: (label, id) 기준
      - relationships 정렬: (type, source, target) 기준
      - 각 node 의 properties dict 키 정렬
      - properties 의 string 값 strip (앞뒤 공백)
      - id 중복 노드 제거 (먼저 등장한 것 유지)

    [무엇을 정규화하지 않는가]
      - properties 값의 의미 변경 (lowercase, NFC 등) 은 하지 않음 — 사용자에게 보이는 표현 보존
      - _harness_metadata 같은 top-level 보조 키는 그대로 통과

    [용도]
      - CPS / PRD agent 가 만든 graph JSON 을 그대로 반환하기 전에 마지막 단계로 호출
      - 같은 LLM 출력의 노드/관계 순서 차이가 다음 단계 (cache 비교, diff view 등) 에 노이즈
        가 되지 않도록 안정화

    [위험성]
      낮음. 다만 frontend / downstream 코드가 nodes 순서에 의존하면 안 됨 (현재는 의존 안 함).
    """
    if not graph or not isinstance(graph, dict):
        return graph or {}

    nodes_in = graph.get("nodes") or []
    rels_in = graph.get("relationships") or []

    # ─ 노드 정규화 ─
    seen_ids: set = set()
    nodes_out: List[Dict[str, Any]] = []
    for n in nodes_in:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        if not nid or nid in seen_ids:
            continue
        seen_ids.add(nid)
        # properties 키 정렬 + string 값 strip
        props_in = n.get("properties") or {}
        if isinstance(props_in, dict):
            props_out: Dict[str, Any] = {}
            for k in sorted(props_in.keys()):
                v = props_in[k]
                if isinstance(v, str):
                    props_out[k] = v.strip()
                else:
                    props_out[k] = v
        else:
            props_out = props_in
        nodes_out.append({
            "id": nid,
            "label": n.get("label", ""),
            "properties": props_out,
        })
    # 정렬 — (label, id)
    nodes_out.sort(key=lambda n: (str(n.get("label") or ""), str(n.get("id") or "")))

    # ─ 관계 정규화 ─
    rels_out: List[Dict[str, Any]] = []
    for r in rels_in:
        if not isinstance(r, dict):
            continue
        src = r.get("source")
        tgt = r.get("target")
        rtype = r.get("type")
        if not (src and tgt and rtype):
            continue
        props_in = r.get("properties") or {}
        if isinstance(props_in, dict):
            props_out = {k: (v.strip() if isinstance(v, str) else v)
                         for k, v in sorted(props_in.items())}
        else:
            props_out = props_in
        rels_out.append({
            "source": src,
            "target": tgt,
            "type": rtype,
            "properties": props_out,
        })
    rels_out.sort(key=lambda r: (
        str(r.get("type") or ""),
        str(r.get("source") or ""),
        str(r.get("target") or ""),
    ))

    # top-level metadata 등 보조 키는 그대로 전달 — 단 nodes / relationships 만 정규화 결과로 교체.
    out = dict(graph)
    out["nodes"] = nodes_out
    out["relationships"] = rels_out
    return out


_RETRY_STRICT_PREFIX = (
    "[SYSTEM: 직전 응답이 JSON 으로 파싱되지 않았습니다. "
    "이번엔 **반드시** 단일 JSON 객체 `{...}` 만 출력하세요. "
    "마크다운 fence 금지, 머릿말/꼬릿말 금지, 객체 1개만.]\n\n"
)


async def _gemini_call(
    gemini: _GeminiLike,
    prompt: str,
    *,
    temperature: float,
    response_schema: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> Any:
    """
    GeminiLike.generate 호출 wrapper — response_schema/model 인자를 지원하지 않는
    legacy fake (e.g. tests/conftest.py 의 FakeGemini) 와의 호환성 유지 +
    backend 가 schema 를 거부할 때 schema 없이 자동 fallback (운영 안전망).

    실제 GeminiClient / TrackedGemini 는 response_schema/model 인자 지원.

    [운영 안전망]
    LiteLLM proxy 가 response_format=json_schema 미지원이거나 Gemini 가 schema
    문법 거부 시 GeminiError 가 발생할 수 있다. 그 경우 schema 없이 한 번 더
    시도 → 기존 동작 (자유 텍스트 + extract_json_object) 으로 graceful degradation.
    schema 가 운영 환경에서 호환 문제로 fail 해도 사용자 경험은 변동 없음.

    [model — 2026-05-26 perf A]
    stage-level model override (e.g. impact analyzer → flash-lite). FakeGemini 가
    인자를 모르면 단계적으로 떨궈서 호출.
    """
    kwargs: Dict[str, Any] = {"temperature": temperature}
    if response_schema is not None:
        kwargs["response_schema"] = response_schema
    if model is not None:
        kwargs["model"] = model
    # [2026-06-01] timeout/max_retries — fast-fail override (autofill 등). 실제
    # GeminiClient/TrackedGemini 는 지원, 옛 fake 는 모를 수 있어 아래 점진 degradation.
    if timeout is not None:
        kwargs["timeout"] = timeout
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    try:
        return await gemini.generate(prompt, **kwargs)
    except TypeError:
        # 옛 fake — 일부 kwarg 미지원. 최신 인자부터 점진적으로 떨궈서 retry:
        # timeout/max_retries → model → response_schema 순 (지원 폭 넓은 쪽을 마지막까지 유지).
        if "timeout" in kwargs or "max_retries" in kwargs:
            kwargs.pop("timeout", None)
            kwargs.pop("max_retries", None)
            try:
                return await gemini.generate(prompt, **kwargs)
            except TypeError:
                pass
        if "model" in kwargs:
            kwargs.pop("model", None)
            try:
                return await gemini.generate(prompt, **kwargs)
            except TypeError:
                pass
        if "response_schema" in kwargs:
            kwargs.pop("response_schema", None)
        return await gemini.generate(prompt, **kwargs)
    except Exception as e:  # noqa: BLE001
        # [운영 안전망] schema 호출 자체 실패 시 schema 없이 재시도.
        # GeminiError (quota/auth/transient) 는 schema 무관 — 다시 시도해도 같은 실패
        # 가능성 높지만, 'invalid_response' / 'unknown' 4xx 류는 schema 거부일 수도
        # 있어 fallback 시도. fallback 도 실패하면 그대로 raise.
        from app.clients.gemini_client import GeminiError
        if isinstance(e, GeminiError) and e.kind in ("quota", "auth", "transient"):
            # quota/auth/transient(network·timeout) 은 schema 거부와 무관 — schema 없이
            # bare 재시도해도 같은 실패다. 특히 transient=timeout 일 때 bare 재시도는
            # 호출부의 fast-fail(timeout/max_retries override)을 잃고 기본값(90s×3)으로
            # 길게 매달리므로(autofill 폴백이 늦어짐) 즉시 전파한다.
            # (schema 문법 거부는 보통 4xx → kind 'unknown'/'invalid_response' 로 분류돼
            #  아래 bare fallback 으로 떨어진다.)
            raise
        if response_schema is None and model is None:
            raise
        logger.warning(
            "generate with schema/model failed (%s) — falling back to bare call",
            type(e).__name__,
        )
        return await gemini.generate(prompt, temperature=temperature)


async def generate_json_with_retry(
    gemini: _GeminiLike,
    prompt: str,
    *,
    temperature: float = 0.2,
    strict_prefix: str = _RETRY_STRICT_PREFIX,
    response_schema: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> tuple[Dict[str, Any], Any]:
    """
    LLM 을 호출해 JSON 객체를 추출. 첫 시도가 `{}` 면 더 엄격한 시스템 메시지 +
    낮은 temperature 로 1회 재시도.

    [반환]
        (parsed_dict, last_result)
        - parsed_dict: 성공 시 추출된 JSON object, 실패 시 `{}` (호출자 ValueError 분기).
        - last_result: 마지막 GeminiResult — 호출자가 token usage / model 노출 가능.

    [정책]
    - 첫 시도 실패만 재시도 (1회). 두 번째도 실패면 빈 dict 반환 → 호출자가 결정.
    - 재시도는 temperature 를 0.5 배로 낮춰 결정성 ↑ + strict prefix 부착.
    - 네트워크/quota 오류 등 GeminiError 는 재시도 안 하고 그대로 전파.

    [response_schema — 2026-05]
    Gemini structured output schema 가 주어지면 backend (LiteLLM / Google) 에 그대로
    전달. LLM 출력이 schema 안에 머무르도록 강제 → fence/머릿말 섞임 0 → 재시도
    필요성 거의 사라짐. None 이면 기존 동작 (자유 텍스트 + extract_json_object).

    Legacy fake (response_schema 인자 미지원) 는 _gemini_call 이 자동 흡수.
    """
    result = await _gemini_call(
        gemini, prompt, temperature=temperature,
        response_schema=response_schema, model=model,
        timeout=timeout, max_retries=max_retries,
    )
    parsed = extract_json_object(result.text)
    if parsed:
        return parsed, result

    # [2026-06-12 관측성] 실패한 응답의 정체를 로그로 — flash-lite 가 schema 강제
    # 모드에서 깡통(수십 토큰, 브레이스 없음)을 반환하던 운영 장애에서 원문을 알 수
    # 없어 디버깅이 길어졌다. head 200자면 거부문구/빈 fence/퇴행 출력 구분에 충분.
    _t = result.text or ""
    logger.warning(
        "generate_json_with_retry: 첫 시도 JSON 파싱 실패 — strict retry 진입 "
        "(len=%d finish=%s head=%r)",
        len(_t), getattr(result, "finish_reason", None), _t[:200],
    )
    retry_prompt = strict_prefix + prompt
    # [2026-06-12 schema 미사용 재시도] 첫 시도가 schema 강제 모드로 깡통을 냈다면
    # 같은 schema 로 또 시도해도 같은 깡통일 확률이 높다 (운영 실측: flash-lite 가
    # 두 번 연속 ~41토큰 무브레이스 출력). 재시도는 schema 없이 자유 텍스트 +
    # extract_json_object 로 — responseSchema 제약에 막힌 모델 구제 경로.
    result2 = await _gemini_call(
        gemini, retry_prompt, temperature=temperature * 0.5,
        response_schema=None, model=model,
        timeout=timeout, max_retries=max_retries,
    )
    parsed2 = extract_json_object(result2.text)
    if parsed2:
        return parsed2, result2
    _t2 = result2.text or ""
    logger.warning(
        "generate_json_with_retry: 재시도도 실패 — 빈 dict 반환 "
        "(len=%d finish=%s head=%r)",
        len(_t2), getattr(result2, "finish_reason", None), _t2[:200],
    )
    return {}, result2


def extract_json_object(s: str) -> Dict[str, Any]:
    """
    LLM 출력에서 첫 `{` ~ 마지막 `}` 블록을 찾아 JSON 파싱.
    실패 시 `{}` 반환 — CPS Section Filter1 의 방어적 파싱과 동일.

    [2026-06-12] 기존 `\\{[\\s\\S]*\\}` 그리디 정규식과 동일 의미(첫 `{` ~ 마지막
    `}`)를 find/rfind 로 구현 — 브레이스가 많은 퇴행성 출력에서도 항상 O(n).
    strip_code_blocks 의 O(n²) 동결 장애와 같은 계열의 예방 교체.
    """
    if not s:
        return {}
    body = strip_code_blocks(s)
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(body[start : end + 1])
    except json.JSONDecodeError as e:
        logger.warning("extract_json_object: parse failed (%s)", e)
        return {}


def format_props(props: Dict[str, Any] | None) -> str:
    """
    Cypher property-map 직렬화.

    key 는 식별자 위치이므로 화이트리스트 검증 — 부적합 key 는 silently drop
    (LLM 한 항목이 그래프 전체를 깨뜨리지 않도록).
    """
    if not props:
        return "{}"
    parts = []
    for k, v in props.items():
        if not is_safe_cypher_identifier(k):
            logger.warning("format_props: dropping unsafe property key: %r", k)
            continue
        if v is None:
            parts.append(f"{k}: ''")
        elif isinstance(v, bool):
            parts.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}: {v}")
        elif isinstance(v, str):
            parts.append(f"{k}: '{escape_cypher_string(v)}'")
        elif isinstance(v, list):
            inner = []
            for it in v:
                if isinstance(it, str):
                    inner.append(f"'{escape_cypher_string(it)}'")
                elif isinstance(it, (int, float, bool)):
                    inner.append(
                        "true" if it is True else "false" if it is False else str(it)
                    )
                else:
                    inner.append(f"'{escape_cypher_string(json.dumps(it, ensure_ascii=False))}'")
            parts.append(f"{k}: [{', '.join(inner)}]")
        else:
            parts.append(
                f"{k}: '{escape_cypher_string(json.dumps(v, ensure_ascii=False))}'"
            )
    return "{ " + ", ".join(parts) + " }"
