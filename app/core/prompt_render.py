"""안전한 프롬프트 템플릿 렌더 — 단일 진실원(single source).

[배경]
이전에는 13개 파이프라인이 각자 동일한 naive `_render` 를 중복 정의했다:

    def _render(template, **vars):
        out = template
        for k, v in vars.items():
            out = out.replace(f"<<{k}>>", v)   # ← 취약
        return out

이 방식은 두 가지 문제가 있다.

1) **placeholder 주입** — `str.replace` 를 변수마다 순차로 돌리면, 먼저 치환된
   값 안에 `<<other_key>>` 같은 토큰이 들어있을 경우 다음 루프에서 *그것까지*
   치환된다. 사용자/외부에서 들어온 데이터(회의록, GitHub 레포 본문)가
   템플릿 구조를 조작할 수 있다.

2) **delimiter 주입** — 외부 본문에 `<<` `>>` 가 섞여 있으면 새 placeholder 를
   만들어내거나 기존 placeholder 경계를 흐릴 수 있다.

[해결]
- `render_template` 은 템플릿을 **정확히 한 번** 스캔(single-pass)한다. 치환된
  값은 다시 스캔되지 않으므로 placeholder 주입이 원천 차단된다.
- `harden_untrusted` 는 신뢰할 수 없는 스칼라 입력의 `<<`/`>>` 마커를 무력화하고
  길이를 제한한다 (프롬프트 비용/크기 방어 겸용).

특히 Lint 파이프라인은 **임의의 외부 GitHub 레포 본문**을 프롬프트에 넣으므로
이 모듈을 통한 렌더가 필수다.
"""
from __future__ import annotations

import re
from typing import Optional

# `<<key>>` — key 는 영문/숫자/언더스코어만. 그 외 문자가 끼면 placeholder 로
# 보지 않는다 (외부 본문에 우연히 들어간 `<< ... >>` 를 토큰으로 오인 방지).
_TOKEN_RE = re.compile(r"<<([A-Za-z0-9_]+)>>")

# untrusted 본문 안의 delimiter 를 시각적으로 동일하지만 ASCII 가 아닌 글자로
# 치환 — 사람이 읽기엔 그대로지만 `_TOKEN_RE` 에는 안 걸린다.
_LT = "‹‹"  # ‹‹
_GT = "››"  # ››


def render_template(template: str, /, **values: object) -> str:
    """`<<key>>` 토큰을 single-pass 로 치환한다.

    - 알려진 key 만 치환하고, 정의되지 않은 토큰은 원문 그대로 남긴다.
    - 치환된 값은 재스캔하지 않으므로, 값 안의 `<<other>>` 는 절대 확장되지 않는다
      (placeholder 주입 차단).
    - 값이 str 이 아니면 str() 로 강제 변환한다 (기존 호출부는 모두 str 을 넘기지만
      안전을 위해).
    """
    def _sub(m: "re.Match[str]") -> str:
        key = m.group(1)
        if key in values:
            v = values[key]
            return v if isinstance(v, str) else str(v)
        return m.group(0)  # 미정의 토큰은 그대로 둔다

    return _TOKEN_RE.sub(_sub, template)


def harden_untrusted(value: object, *, max_len: Optional[int] = None) -> str:
    """신뢰할 수 없는(사용자/외부) 스칼라 입력을 프롬프트 삽입 전에 무력화.

    - `<<` / `>>` delimiter 를 ASCII 가 아닌 유사 글자로 치환 → 새 placeholder
      생성 / 경계 조작 차단. 의미(사람이 읽는 내용)는 보존된다.
    - max_len 지정 시 길이를 잘라 프롬프트 폭주/비용을 방어한다.

    JSON 으로 직렬화된 본문(이미 escape 됨)에는 굳이 쓸 필요는 없으나,
    project_name / github_url 같은 raw 스칼라에는 반드시 적용한다.
    """
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    if max_len is not None and len(s) > max_len:
        s = s[:max_len]
    return s.replace("<<", _LT).replace(">>", _GT)
