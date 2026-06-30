"""
Notion 블록 트리 → Markdown 변환.

[입력]
NotionClient.get_page_blocks() 결과 — 각 블록은 Notion 원본 + 우리 확장 `_children`.

[출력]
순수 markdown 문자열. 미팅 로그 import 용이므로 코드 블록의 언어 보존, 리스트 중첩 들여쓰기
보존이 중요. 이미지는 외부 URL 만 보존 (Notion 호스팅 file URL 은 만료될 수 있어
fetch & re-upload 는 후속 단계 — 일단 외부 링크 그대로).

[지원 블록]
heading_1/2/3, paragraph, bulleted_list_item, numbered_list_item, to_do, toggle,
quote, callout, code (언어 보존), divider, table + table_row, image, bookmark,
child_page (제목만), child_database (제목만).

미지원 블록은 silent skip + logger.debug. 사용자에게 변환 손실을 알릴 책임은
호출자(라우트) 가 진다.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 들여쓰기 1단계 = 공백 2칸 (markdown 리스트 컨벤션).
_INDENT = "  "


def blocks_to_markdown(blocks: List[Dict[str, Any]]) -> str:
    """블록 트리 → markdown. 외부 진입점."""
    if not blocks:
        return ""
    lines: List[str] = []
    _render_blocks(blocks, lines, depth=0, numbered_counters=[])
    # 연속된 빈 줄 압축 — Notion 의 빈 paragraph 가 누적되면 보기 안 좋음.
    return _collapse_blank_lines("\n".join(lines)).strip() + "\n"


# ===== 내부 =====


def _render_blocks(
    blocks: List[Dict[str, Any]],
    lines: List[str],
    *,
    depth: int,
    numbered_counters: List[int],
) -> None:
    """
    한 children 리스트를 순회. numbered_counters 는 깊이별 카운터 스택 —
    같은 깊이의 연속된 numbered_list_item 만 카운트, 다른 블록이 끼면 리셋.
    """
    # 현재 깊이 카운터를 보장 (없으면 0 으로 push).
    while len(numbered_counters) <= depth:
        numbered_counters.append(0)
    # 이 depth 카운터는 이번 sibling 그룹에서 새로 시작.
    numbered_counters[depth] = 0

    prev_kind: Optional[str] = None
    for block in blocks:
        kind = block.get("type") or ""
        # numbered 가 연속 끊기면 카운터 리셋.
        if prev_kind == "numbered_list_item" and kind != "numbered_list_item":
            numbered_counters[depth] = 0

        rendered = _render_block(
            block, depth=depth, numbered_counters=numbered_counters, lines=lines
        )
        if rendered is not None:
            lines.append(rendered)
        # 자식 재귀 — toggle / list 의 nested children.
        children = block.get("_children") or []
        if children:
            _render_blocks(
                children, lines, depth=depth + 1, numbered_counters=numbered_counters
            )

        prev_kind = kind


def _render_block(
    block: Dict[str, Any],
    *,
    depth: int,
    numbered_counters: List[int],
    lines: List[str],
) -> Optional[str]:
    """단일 블록 → markdown 1줄 (혹은 여러 줄 string). 미지원이면 None."""
    kind = block.get("type") or ""
    indent = _INDENT * depth

    if kind == "paragraph":
        text = _rich_text_to_md(block.get("paragraph", {}).get("rich_text") or [])
        return f"{indent}{text}" if text else ""

    if kind == "heading_1":
        text = _rich_text_to_md(block.get("heading_1", {}).get("rich_text") or [])
        # heading 은 indent 무시 — markdown 에서 #은 줄 첫 글자여야 함.
        return f"# {text}" if text else None
    if kind == "heading_2":
        text = _rich_text_to_md(block.get("heading_2", {}).get("rich_text") or [])
        return f"## {text}" if text else None
    if kind == "heading_3":
        text = _rich_text_to_md(block.get("heading_3", {}).get("rich_text") or [])
        return f"### {text}" if text else None

    if kind == "bulleted_list_item":
        text = _rich_text_to_md(
            block.get("bulleted_list_item", {}).get("rich_text") or []
        )
        return f"{indent}- {text}"

    if kind == "numbered_list_item":
        numbered_counters[depth] += 1
        n = numbered_counters[depth]
        text = _rich_text_to_md(
            block.get("numbered_list_item", {}).get("rich_text") or []
        )
        return f"{indent}{n}. {text}"

    if kind == "to_do":
        td = block.get("to_do", {})
        text = _rich_text_to_md(td.get("rich_text") or [])
        mark = "x" if td.get("checked") else " "
        return f"{indent}- [{mark}] {text}"

    if kind == "toggle":
        # 토글은 markdown 표준이 없음. <details> 변환 — 미팅 로그용으로는 본문이 더 중요.
        text = _rich_text_to_md(block.get("toggle", {}).get("rich_text") or [])
        # children 은 외부 재귀가 indent+1 로 처리하므로 본문만 출력.
        return f"{indent}- {text}" if text else f"{indent}-"

    if kind == "quote":
        text = _rich_text_to_md(block.get("quote", {}).get("rich_text") or [])
        # 여러 줄 quote 면 각 줄에 prefix — 일단 한 줄 가정.
        return f"{indent}> {text}" if text else None

    if kind == "callout":
        co = block.get("callout", {})
        text = _rich_text_to_md(co.get("rich_text") or [])
        icon = co.get("icon") or {}
        emoji = icon.get("emoji") if isinstance(icon, dict) else None
        prefix = f"{emoji} " if emoji else "💡 "
        return f"{indent}> {prefix}{text}" if text else None

    if kind == "code":
        cb = block.get("code", {})
        text = _rich_text_to_md(cb.get("rich_text") or [], inline_code_ok=False)
        lang = cb.get("language") or ""
        # code 블록은 indent 적용하면 markdown 파서가 들여쓰기 코드로 오해할 수 있음 →
        # depth>0 이면 fenced 만 사용.
        fence = "```"
        return f"{fence}{lang}\n{text}\n{fence}"

    if kind == "divider":
        return "---"

    if kind == "image":
        return _render_image(block.get("image") or {}, indent)

    if kind == "bookmark":
        url = block.get("bookmark", {}).get("url") or ""
        caption = _rich_text_to_md(
            block.get("bookmark", {}).get("caption") or []
        )
        if not url:
            return None
        label = caption or url
        return f"{indent}[{label}]({url})"

    if kind == "table":
        # table 자체는 marker. row 들이 _children 에 와서 외부 재귀가 처리.
        # 하지만 markdown 테이블은 header 와 separator 가 필요해 특별 처리.
        return _render_table(block, indent)

    if kind == "table_row":
        # 단독 호출 — table 의 _render_table 이 처리하지만, 비정상 케이스 대비.
        return None

    if kind == "child_page":
        title = block.get("child_page", {}).get("title") or "(제목 없음)"
        return f"{indent}- 📄 **{title}** (하위 페이지)"

    if kind == "child_database":
        title = block.get("child_database", {}).get("title") or "(이름 없음)"
        return f"{indent}- 🗄 **{title}** (하위 데이터베이스)"

    if kind in ("unsupported", "equation", "synced_block", "column_list", "column"):
        # 일단 무시. column_list/column 은 자식 블록은 외부 재귀가 잡아줘서 손실 적음.
        logger.debug("notion block kind silently skipped: %s", kind)
        return None

    logger.debug("notion block kind not handled: %s", kind)
    return None


def _render_image(image: Dict[str, Any], indent: str) -> Optional[str]:
    kind = image.get("type")
    url = ""
    if kind == "external":
        url = (image.get("external") or {}).get("url") or ""
    elif kind == "file":
        # Notion 호스팅 파일 — 시간 제한 URL 이지만 일단 그대로.
        url = (image.get("file") or {}).get("url") or ""
    if not url:
        return None
    caption = _rich_text_to_md(image.get("caption") or [])
    alt = caption or "image"
    return f"{indent}![{alt}]({url})"


def _render_table(table: Dict[str, Any], indent: str) -> Optional[str]:
    """
    table 블록 → markdown 테이블. row 는 _children 에 있음.
    markdown 테이블은 indent 가 어려우니 depth>0 도 indent 무시.
    """
    rows = table.get("_children") or []
    if not rows:
        return None
    has_header = bool(table.get("table", {}).get("has_column_header"))

    lines: List[str] = []
    for i, row in enumerate(rows):
        if row.get("type") != "table_row":
            continue
        cells = row.get("table_row", {}).get("cells") or []
        rendered_cells = [
            _rich_text_to_md(cell or []).replace("|", "\\|").replace("\n", " ")
            for cell in cells
        ]
        lines.append("| " + " | ".join(rendered_cells) + " |")
        if i == 0:
            sep = "| " + " | ".join(["---"] * len(rendered_cells)) + " |"
            if has_header:
                lines.append(sep)
            else:
                # markdown 은 헤더가 필수 — 첫 행을 헤더로 쓰고 separator 추가.
                lines.append(sep)
    # table 의 _children 은 외부 재귀가 다시 돌리지 않도록 비우는 책임은 호출자가...
    # 하지만 우리는 외부 재귀가 그냥 호출되는 구조라, table_row 가 _render_block 에서
    # None 을 반환하게 해서 중복 출력 방지함.
    return "\n".join(lines)


# ===== rich_text =====


def _rich_text_to_md(parts: List[Dict[str, Any]], *, inline_code_ok: bool = True) -> str:
    """
    Notion rich_text 배열 → markdown 인라인. annotations + href 처리.

    inline_code_ok=False: 코드 블록 내부에서는 어노테이션/링크 무시하고 plain_text 만.
    """
    if not parts:
        return ""
    out: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("plain_text")
        if text is None:
            # equation 같은 type 은 plain_text 없을 수 있음.
            t = part.get("type")
            if t == "equation":
                expr = (part.get("equation") or {}).get("expression") or ""
                text = f"${expr}$"
            else:
                continue
        if not inline_code_ok:
            out.append(text)
            continue
        ann = part.get("annotations") or {}
        href = part.get("href")
        rendered = _apply_annotations(text, ann)
        if href:
            # URL 의 ) 는 escape — markdown link 깨짐 방지.
            safe_url = href.replace(")", "%29")
            rendered = f"[{rendered}]({safe_url})"
        out.append(rendered)
    return "".join(out)


def _apply_annotations(text: str, ann: Dict[str, Any]) -> str:
    """
    bold/italic/strikethrough/code 적용. 빈 텍스트는 그대로.

    markdown 의 인라인 wrapping 은 텍스트 양 끝이 공백/구두점이면 깨질 수 있어서,
    edge whitespace 를 wrap 밖으로 빼낸다.
    """
    if not text:
        return text
    if ann.get("code"):
        # 코드 안의 백틱은 더 긴 fence 로 감싸야 하지만 흔치 않은 케이스라 escape 만.
        return f"`{text.replace('`', 'ˋ')}`"

    # 앞/뒤 공백 분리
    leading_len = len(text) - len(text.lstrip())
    trailing_len = len(text) - len(text.rstrip())
    lead = text[:leading_len]
    trail = text[len(text) - trailing_len:] if trailing_len else ""
    core = text[leading_len: len(text) - trailing_len] if trailing_len else text[leading_len:]

    if not core:
        return text  # all-whitespace — annotation 무의미

    if ann.get("bold"):
        core = f"**{core}**"
    if ann.get("italic"):
        core = f"*{core}*"
    if ann.get("strikethrough"):
        core = f"~~{core}~~"
    if ann.get("underline"):
        # markdown 표준 없음 — HTML 태그로.
        core = f"<u>{core}</u>"
    return f"{lead}{core}{trail}"


def _collapse_blank_lines(text: str) -> str:
    """연속된 빈 줄 3개 이상 → 2개로 압축."""
    out_lines: List[str] = []
    blank_run = 0
    for line in text.split("\n"):
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 2:
                out_lines.append("")
        else:
            blank_run = 0
            out_lines.append(line)
    return "\n".join(out_lines)
