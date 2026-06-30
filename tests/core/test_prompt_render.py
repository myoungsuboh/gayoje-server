"""prompt_render — single-pass 렌더 + untrusted 하드닝 테스트."""
from app.core.prompt_render import harden_untrusted, render_template


def test_basic_substitution():
    out = render_template("hello <<name>>", name="world")
    assert out == "hello world"


def test_unknown_token_left_intact():
    # 정의되지 않은 토큰은 원문 보존 (실수로 비우지 않음).
    out = render_template("a <<known>> b <<unknown>>", known="X")
    assert out == "a X b <<unknown>>"


def test_placeholder_injection_blocked():
    # 핵심 보안 테스트: 먼저 치환되는 값이 <<secret>> 을 품고 있어도
    # single-pass 이므로 2차 치환이 일어나지 않는다.
    out = render_template(
        "user=<<user>> | secret=<<secret>>",
        user="evil <<secret>>",
        secret="TOPSECRET",
    )
    # user 자리에 들어간 '<<secret>>' 은 그대로 남아야 한다 (확장 금지).
    assert out == "user=evil <<secret>> | secret=TOPSECRET"
    assert out.count("TOPSECRET") == 1


def test_order_independent_no_reexpansion():
    # 변수 순서가 어떻든 재확장이 없어야 한다.
    out = render_template(
        "<<a>>::<<b>>",
        a="<<b>>",
        b="<<a>>",
    )
    assert out == "<<b>>::<<a>>"


def test_non_str_value_coerced():
    out = render_template("count=<<n>>", n=42)
    assert out == "count=42"


def test_harden_untrusted_neutralizes_delimiters():
    raw = "name with <<inject>> and >>close"
    hardened = harden_untrusted(raw)
    assert "<<" not in hardened
    assert ">>" not in hardened
    # 사람이 읽는 의미(텍스트 본문)는 보존.
    assert "inject" in hardened and "close" in hardened


def test_harden_untrusted_caps_length():
    assert harden_untrusted("x" * 100, max_len=10) == "x" * 10


def test_harden_untrusted_none():
    assert harden_untrusted(None) == ""


def test_hardened_value_cannot_introduce_placeholder():
    # 외부 입력을 harden 후 렌더에 넣으면 새 토큰을 만들 수 없다.
    evil = harden_untrusted("<<context_json>>")
    out = render_template(
        "ctx=<<context_json>> | user=<<user>>",
        context_json="REAL_CONTEXT",
        user=evil,
    )
    assert "REAL_CONTEXT" in out
    # user 자리의 가짜 토큰은 REAL_CONTEXT 로 치환되면 안 됨.
    assert out.count("REAL_CONTEXT") == 1
