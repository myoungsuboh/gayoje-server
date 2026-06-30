"""
인젝션 회귀 가드.

[위협 모델]
  1. LLM 출력 → Cypher 보간 — LLM 이 라벨/관계타입/속성키에 악성 토큰 주입
  2. LLM 출력 → Cypher 문자열 리터럴 — quote/backslash/절텅 escape 가 필수

[방어 계층]
  1. is_safe_cypher_identifier — 명시적 화이트리스트 (label/type/property key)
  2. escape_cypher_string — 문자열 리터럴 escape
  3. format_props — 안전 key + escaped value 명시 구축
  4. build_save_cps_query — _BLOCKED_LABELS + safety 검증 통과 못 한 노드 drop
  5. canonicalize_graph — 입력 정규화 (payload 보존, 순서/공백만 흔)
  6. parameter binding — LLM 출력 string value 는 cypher 안으로 직접 보간되지 않음

이 테스트들이 깨지면 인젝션 회귀 가능성 — 즉시 조사필요.
"""
from __future__ import annotations

from app.pipelines.base import (
    canonicalize_graph,
    escape_cypher_string,
    format_props,
    is_safe_cypher_identifier,
)
from app.pipelines.cps_pipeline.cypher import build_save_cps_query


# ─── is_safe_cypher_identifier ───────────────────────────────────


def test_is_safe_accepts_valid_identifiers():
    """영문자/언더스코어 시작 + 영숫자/언더스코어 모두 광지."""
    for ok in ["Problem", "EXTRACTED_FROM", "_priv", "a", "Foo123", "x" * 64]:
        assert is_safe_cypher_identifier(ok) is True, ok


def test_is_safe_rejects_special_chars():
    """hyphen / space / quote / semicolon / digit prefix / empty 차단."""
    for bad in [
        "",                       # empty
        "x" * 65,                 # too long
        "Bad-Label",              # hyphen
        "Bad Label",              # space
        "123Numeric",             # digit prefix
        "Bad'Label",              # single quote
        'Bad"Label',              # double quote
        "Bad;Label",              # semicolon
        "Bad)Label",              # paren
        "Bad}Label",              # brace
        "Bad`Label",              # backtick
        "Bad.Label",              # dot
        "라벨",                   # non-ASCII (Cypher label 은 ASCII 권장)
        "DETACH DELETE",          # cypher keyword + space
    ]:
        assert is_safe_cypher_identifier(bad) is False, bad


def test_is_safe_rejects_non_string():
    assert is_safe_cypher_identifier(None) is False
    assert is_safe_cypher_identifier(123) is False
    assert is_safe_cypher_identifier(["x"]) is False


# ─── escape_cypher_string ───────────────────────────────────


def test_escape_backslash_first_then_quote():
    """backslash escape 가 quote 보다 먼저 일어나 double-escape 방지."""
    # input: \' (backslash + quote) — raw escape
    # output: \\\\\\' (escaped backslash + escaped quote)
    assert escape_cypher_string("\\'") == "\\\\\\'"


def test_escape_quote_neutralizes_injection():
    """LLM 이 string 에 ' 붙여 cypher escape 시도 → ' 이 \\' 로으로 이스케이프."""
    out = escape_cypher_string("x'); DETACH DELETE n; //")
    assert "\\'" in out
    # ); 자체는 남지만 literal 경계를 탈출 못 함 (quote 가 escape 됨)
    assert out.startswith("x\\')")


def test_escape_newline_and_cr():
    assert escape_cypher_string("a\nb\rc") == "a\\nb\\rc"


def test_escape_none_returns_empty_string():
    assert escape_cypher_string(None) == ""


def test_escape_coerces_non_string():
    assert escape_cypher_string(123) == "123"


# ─── format_props ───────────────────────────────────────────────


def test_format_props_drops_unsafe_key():
    """key 이 화이트리스트 통과 못 하면 silently drop — 개별 오류로 전체 그래프를 극릭 못게."""
    out = format_props({"valid": "ok", "bad-key": "x", "bad space": "y"})
    assert "valid: 'ok'" in out
    assert "bad-key" not in out
    assert "bad space" not in out


def test_format_props_escapes_string_value():
    out = format_props({"summary": "x' DETACH DELETE //"})
    # raw injection 문자열이 아닌 escape 된 형태로 들어감
    assert "DETACH DELETE" in out  # 제거 아님 — 그러나 quote 가 escape
    assert "x\\'" in out  # quote 는 escape 됨 (literal 경계 탈출 불가)


def test_format_props_handles_types():
    out = format_props({"i": 42, "f": 1.5, "b": True, "n": None})
    assert "i: 42" in out
    assert "f: 1.5" in out
    assert "b: true" in out
    assert "n: ''" in out  # None → 빈 문자열


def test_format_props_empty_returns_empty_dict():
    assert format_props(None) == "{}"
    assert format_props({}) == "{}"


# ─── build_save_cps_query 인젝션 회귀 ─────────────────────────────────


def test_build_save_cps_query_drops_label_injection_attempt():
    """LLM 가 라벨 필드에 cypher 문법 주입 시도 → unsafe 로 drop."""
    malicious = {
        "nodes": [
            {"id": "ok", "label": "Problem", "properties": {}},
            {"id": "evil1", "label": "Problem) DETACH DELETE n //", "properties": {}},
            {"id": "evil2", "label": "Problem`); DROP", "properties": {}},
            {"id": "evil3", "label": "P; MATCH (m) DELETE m", "properties": {}},
        ],
        "relationships": [],
    }
    cypher, _ = build_save_cps_query(malicious)
    # 동일 라벨 그룹도 안전하면 1개 분리 보존 — evil 3개 드롭
    # cypher 에는 cypher injection 문자열 이 없어야 함
    assert "DETACH DELETE n //" not in cypher
    assert "DROP" not in cypher
    assert "MATCH (m) DELETE m" not in cypher


def test_build_save_cps_query_drops_relationship_type_injection():
    """관계 타입에 cypher 문법 주입 → drop.

    [2026-05-26] spec 노드 빈 필드 가드 추가 후 properties 에 summary 부여
    (의미 있는 spec 노드만 유지되도록 — 별개 테스트가 spec 가드 검증).
    """
    g = {
        "nodes": [
            {"id": "p1", "label": "Problem", "properties": {"summary": "f1"}},
            {"id": "p2", "label": "Problem", "properties": {"summary": "f2"}},
        ],
        "relationships": [
            {"source": "p1", "target": "p2", "type": "SAFE_REL"},
            {"source": "p1", "target": "p2", "type": "BAD] DELETE x ["},
        ],
    }
    cypher, _ = build_save_cps_query(g)
    assert "SAFE_REL" in cypher
    assert "DELETE x" not in cypher


def test_build_save_cps_query_string_values_go_through_parameter_binding():
    """LLM string property value 는 cypher 안에 들어가지 않고 params 로 전달됨.
    Neo4j driver 가 server-side parameterize — 따로 escape 필요 없이 안전.

    주의: build_save_cps_query 는 params 에 {"id": ..., "props": ...} 형식으로
    저장하므로 (cypher.py::build_save_cps_query 참조) 검증 시에도 "props" 키를 사용.
    """
    inj = "x'); DETACH DELETE n; //"
    g = {
        "nodes": [{"id": "prb_x", "label": "Problem", "properties": {"summary": inj}}],
        "relationships": [],
    }
    cypher, params = build_save_cps_query(g)
    # cypher 에는 raw injection 없음 — $param 해석 자리만 있음
    assert "DETACH DELETE n" not in cypher
    # params 에는 그대로 보존 (driver 가 parameterize)
    found_inj = any(
        isinstance(v, list)
        and any((item.get("props") or {}).get("summary") == inj for item in v)
        for v in params.values()
    )
    assert found_inj, "injection string 은 params 안에 그대로 보존되어야 함"


# ─── canonicalize_graph — payload 보존 ───────────────────────────────


def test_canonicalize_graph_preserves_payload_only_strips_whitespace():
    """strip 은 공백만 — LLM 이 적은 악성 문자열이 계속 판채 보존되어 검사 가능."""
    g = {
        "nodes": [{"id": "x", "label": "Problem", "properties": {"summary": "  inj';DROP--  "}}],
        "relationships": [],
    }
    out = canonicalize_graph(g)
    # 앞뒤 공백만 strip — 악성 본문은 그대로 (downstream 이 parameterize 하면 안전)
    assert out["nodes"][0]["properties"]["summary"] == "inj';DROP--"


def test_canonicalize_graph_does_not_lowercase_or_modify_values():
    """속성값 의미 변경 없음 (lowercase / unicode normalize 없음)."""
    g = {
        "nodes": [{"id": "x", "label": "P", "properties": {"summary": "ABC 가나다 123"}}],
        "relationships": [],
    }
    out = canonicalize_graph(g)
    assert out["nodes"][0]["properties"]["summary"] == "ABC 가나다 123"


# ─── prompt template 이욕 주입 경고 ───────────────────────────────
#
# _render 는 simple substring replace — 이터레이티브 그래프로
# user-controlled var 이 다른 placeholder 를 트리거 가능 (downstream injection 위험).
# 현재 동작을 문서화 하는 테스트 — 향후 강화 시점의 기준점.


def test_render_template_substitution_is_single_pass_no_propagation():
    """[2026-05 강화] _render 가 single-pass 로 통일되어 placeholder 주입이 차단됨.

    이전엔 순차 str.replace 라서 먼저 치환된 var 의 값에 들어있는 <<other_var>> 가
    다음 iteration 에서 *또* 치환됐다(주입 벡터). 이제 app.core.prompt_render 의
    single-pass 렌더를 쓰므로 치환된 값은 재스캔되지 않는다.
    """
    from app.pipelines.cps_pipeline.agents import _render

    template = "Project: <<project_name>>, Version: <<version>>"
    out = _render(template, project_name="<<version>>BAD", version="v1")
    # 강화 후: project_name 값 안의 <<version>> 은 그대로 보존 (재확장 금지).
    assert out == "Project: <<version>>BAD, Version: v1"
