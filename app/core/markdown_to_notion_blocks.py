"""
Markdown → Notion 블록 변환기 (export 용, 순수 함수).

`notion_to_markdown.py` 의 역방향. CPS/PRD 마크다운과 설계 렌더 마크다운을
Notion 블록 dict 리스트로 변환한다.

[Notion 제약 처리]
- rich-text content 는 세그먼트당 2000자 → 분할.
- append 는 호출자(NotionClient.append_block_children)가 100개씩 분할.
- 전체 블록 수 상한(_MAX_BLOCKS) 초과 시 잘라내고 안내 문단 추가.

지원: heading 1-3, paragraph, bulleted/numbered list, to-do, quote, code(lang),
divider, table. 인라인: **bold**, *italic*/_italic_, `code`, [text](url).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_MAX_TEXT = 2000
_MAX_BLOCKS = 1800
# 노션 한 요청 100 블록 제한 — 테이블은 자식(table_row)도 합산되므로, 한 테이블이
# 단독으로 100 을 넘지 않도록 데이터 행을 분할(헤더 반복)한다. 여유 두고 90.
_MAX_TABLE_ROWS = 90

# Notion code block 이 인식하는 언어 (일부) — 그 외엔 'plain text'.
_NOTION_LANGS = {
    "bash", "c", "c++", "cpp", "css", "diff", "docker", "go", "graphql", "html",
    "java", "javascript", "json", "kotlin", "markdown", "mermaid", "plain text",
    "python", "ruby", "rust", "shell", "sql", "swift", "typescript", "yaml",
}
_LANG_ALIASES = {
    "js": "javascript", "ts": "typescript", "sh": "bash", "py": "python",
    "yml": "yaml", "plaintext": "plain text", "text": "plain text", "": "plain text",
}

# 인라인 토큰: code → bold → link → italic 순 (code/bold 가 italic 보다 우선).
_INLINE_RE = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<bold>\*\*[^*]+\*\*)"
    r"|(?P<link>\[[^\]]+\]\([^)]+\))"
    r"|(?P<italic>(?<!\*)\*[^*]+\*(?!\*)|(?<!\w)_[^_]+_(?!\w))"
)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _rt(
    content: str,
    *,
    bold: bool = False,
    italic: bool = False,
    code: bool = False,
    link: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """문자열 → rich_text 객체 리스트 (2000자 분할 + annotations)."""
    if not content:
        return []
    out: List[Dict[str, Any]] = []
    for i in range(0, len(content), _MAX_TEXT):
        chunk = content[i : i + _MAX_TEXT]
        text_obj: Dict[str, Any] = {"content": chunk}
        if link:
            text_obj["link"] = {"url": link}
        out.append(
            {
                "type": "text",
                "text": text_obj,
                "annotations": {
                    "bold": bold,
                    "italic": italic,
                    "strikethrough": False,
                    "underline": False,
                    "code": code,
                    "color": "default",
                },
            }
        )
    return out


def _rich_text(s: str) -> List[Dict[str, Any]]:
    """인라인 마크다운(굵게/기울임/코드/링크) 파싱 → rich_text 리스트."""
    if not s:
        return []
    out: List[Dict[str, Any]] = []
    last = 0
    for m in _INLINE_RE.finditer(s):
        if m.start() > last:
            out.extend(_rt(s[last : m.start()]))
        if m.group("code"):
            out.extend(_rt(m.group("code")[1:-1], code=True))
        elif m.group("bold"):
            out.extend(_rt(m.group("bold")[2:-2], bold=True))
        elif m.group("link"):
            lm = _LINK_RE.match(m.group("link"))
            if lm:
                out.extend(_rt(lm.group(1), link=lm.group(2)))
        elif m.group("italic"):
            out.extend(_rt(m.group("italic")[1:-1], italic=True))
        last = m.end()
    if last < len(s):
        out.extend(_rt(s[last:]))
    return out


def _notion_lang(lang: str) -> str:
    l = (lang or "").strip().lower()
    l = _LANG_ALIASES.get(l, l)
    return l if l in _NOTION_LANGS else "plain text"


def _block(btype: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"object": "block", "type": btype, btype: payload}


def _heading(level: int, text: str) -> Dict[str, Any]:
    key = f"heading_{level}"
    return _block(key, {"rich_text": _rich_text(text)})


def _table_row_block(cells: List[str], width: int) -> Dict[str, Any]:
    padded = cells + [""] * (width - len(cells))
    return _block("table_row", {"cells": [_rich_text(c) for c in padded[:width]]})


def _table_blocks(rows: List[str]) -> List[Dict[str, Any]]:
    """마크다운 표 → 1개 이상의 Notion table 블록.

    행이 많으면(_MAX_TABLE_ROWS 초과) 헤더를 반복하며 여러 table 로 분할한다 —
    한 table(자신 1 + table_row N)이 노션 요청당 100 블록 제한을 넘지 않도록.
    """
    parsed: List[List[str]] = []
    for r in rows:
        cells = [c.strip() for c in r.strip().strip("|").split("|")]
        parsed.append(cells)

    # 구분행(---|:---) 제거
    def _is_sep(cells: List[str]) -> bool:
        return all(bool(c) and set(c) <= set("-: ") for c in cells)

    parsed = [c for c in parsed if not _is_sep(c)]
    if not parsed:
        return [_block("paragraph", {"rich_text": []})]
    width = max(len(c) for c in parsed)

    header, data = parsed[0], parsed[1:]
    # 데이터가 없으면 헤더만. 있으면 (헤더 1행 + 데이터) 가 _MAX_TABLE_ROWS 이하가 되도록 분할.
    chunk = _MAX_TABLE_ROWS - 1
    groups = [data[k : k + chunk] for k in range(0, len(data), chunk)] or [[]]

    out: List[Dict[str, Any]] = []
    for g in groups:
        children = [_table_row_block(header, width)] + [_table_row_block(c, width) for c in g]
        out.append(
            _block(
                "table",
                {
                    "table_width": width,
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": children,
                },
            )
        )
    return out


def _block_weight(b: Dict[str, Any]) -> int:
    """노션 요청당 100 블록 한도 계산용 — table 은 자신 + table_row 자식 수."""
    if b.get("type") == "table":
        return 1 + len(b.get("table", {}).get("children", []))
    return 1


def chunk_blocks_by_weight(blocks: List[Dict[str, Any]], limit: int = 100) -> List[List[Dict[str, Any]]]:
    """블록 리스트를 노션 100 블록/요청 제한 안에서 묶는다(table 자식 포함 합산).

    각 chunk 의 (블록 + 모든 table_row 자식) 합계가 limit(노션 한도=100) 이하.
    일반 블록(weight 1)만 있으면 기존처럼 100개씩. 단일 table 은 _table_blocks 가
    _MAX_TABLE_ROWS(90 → weight ≤91)로 분할해 두므로 항상 한 chunk 에 들어감.
    """
    out: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    w = 0
    for b in blocks:
        bw = _block_weight(b)
        if cur and w + bw > limit:
            out.append(cur)
            cur, w = [], 0
        cur.append(b)
        w += bw
    if cur:
        out.append(cur)
    return out


def markdown_to_blocks(md: str) -> List[Dict[str, Any]]:
    """마크다운 문자열 → Notion 블록 dict 리스트."""
    lines = (md or "").split("\n")
    blocks: List[Dict[str, Any]] = []
    i, n = 0, len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        # 코드 펜스
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            code_lines: List[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 닫는 펜스 skip
            blocks.append(
                _block(
                    "code",
                    {
                        "rich_text": _rt("\n".join(code_lines)),
                        "language": _notion_lang(lang),
                    },
                )
            )
            continue

        if stripped == "":
            i += 1
            continue

        if stripped in ("---", "***", "___"):
            blocks.append(_block("divider", {}))
            i += 1
            continue

        # 표 — 연속된 | 행 수집
        if stripped.startswith("|") and stripped.endswith("|"):
            table_rows = []
            while i < n and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                table_rows.append(lines[i].strip())
                i += 1
            blocks.extend(_table_blocks(table_rows))
            continue

        h = re.match(r"(#{1,6})\s+(.*)", stripped)
        if h:
            blocks.append(_heading(min(len(h.group(1)), 3), h.group(2)))
            i += 1
            continue

        td = re.match(r"[-*]\s+\[([ xX])\]\s+(.*)", stripped)
        if td:
            blocks.append(
                _block(
                    "to_do",
                    {
                        "rich_text": _rich_text(td.group(2)),
                        "checked": td.group(1).lower() == "x",
                    },
                )
            )
            i += 1
            continue

        b = re.match(r"[-*]\s+(.*)", stripped)
        if b:
            blocks.append(_block("bulleted_list_item", {"rich_text": _rich_text(b.group(1))}))
            i += 1
            continue

        num = re.match(r"\d+\.\s+(.*)", stripped)
        if num:
            blocks.append(_block("numbered_list_item", {"rich_text": _rich_text(num.group(1))}))
            i += 1
            continue

        if stripped.startswith(">"):
            blocks.append(_block("quote", {"rich_text": _rich_text(stripped.lstrip(">").strip())}))
            i += 1
            continue

        blocks.append(_block("paragraph", {"rich_text": _rich_text(stripped)}))
        i += 1

    if len(blocks) > _MAX_BLOCKS:
        blocks = blocks[:_MAX_BLOCKS]
        blocks.append(
            _block(
                "paragraph",
                {"rich_text": _rt("… (내용이 길어 일부 생략되었습니다 — Harness 에서 전체 확인)")},
            )
        )
    return blocks
