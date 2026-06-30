"""markdown → Notion blocks 변환기 테스트."""
from app.core.markdown_to_notion_blocks import (
    markdown_to_blocks,
    chunk_blocks_by_weight,
    _block_weight,
    _MAX_TABLE_ROWS,
    _rich_text,
)


def _type(b):
    return b["type"]


def test_headings_and_paragraph():
    bs = markdown_to_blocks("# Title\n\nhello world")
    assert _type(bs[0]) == "heading_1"
    assert bs[0]["heading_1"]["rich_text"][0]["text"]["content"] == "Title"
    assert _type(bs[1]) == "paragraph"
    assert bs[1]["paragraph"]["rich_text"][0]["text"]["content"] == "hello world"


def test_heading_levels_capped_at_3():
    bs = markdown_to_blocks("## H2\n### H3\n#### H4")
    assert _type(bs[0]) == "heading_2"
    assert _type(bs[1]) == "heading_3"
    assert _type(bs[2]) == "heading_3"  # h4+ → h3


def test_bullets_numbered_todo():
    bs = markdown_to_blocks("- a\n* b\n1. one\n- [ ] todo\n- [x] done")
    assert _type(bs[0]) == "bulleted_list_item"
    assert _type(bs[1]) == "bulleted_list_item"
    assert _type(bs[2]) == "numbered_list_item"
    assert _type(bs[3]) == "to_do" and bs[3]["to_do"]["checked"] is False
    assert _type(bs[4]) == "to_do" and bs[4]["to_do"]["checked"] is True


def test_code_fence_keeps_language():
    bs = markdown_to_blocks("```python\nx = 1\nprint(x)\n```")
    assert _type(bs[0]) == "code"
    assert bs[0]["code"]["language"] == "python"
    content = bs[0]["code"]["rich_text"][0]["text"]["content"]
    assert "x = 1" in content and "print(x)" in content


def test_code_fence_unknown_language_falls_back():
    bs = markdown_to_blocks("```weirdlang\nabc\n```")
    assert bs[0]["code"]["language"] == "plain text"


def test_mermaid_language_preserved():
    bs = markdown_to_blocks("```mermaid\ngraph LR; A-->B\n```")
    assert bs[0]["code"]["language"] == "mermaid"


def test_divider_and_quote():
    bs = markdown_to_blocks("> a note\n\n---")
    assert _type(bs[0]) == "quote"
    assert bs[0]["quote"]["rich_text"][0]["text"]["content"] == "a note"
    assert _type(bs[1]) == "divider"


def test_table():
    bs = markdown_to_blocks("| A | B |\n| --- | --- |\n| 1 | 2 |")
    assert _type(bs[0]) == "table"
    assert bs[0]["table"]["table_width"] == 2
    rows = bs[0]["table"]["children"]
    assert len(rows) == 2  # header + 1 data row, separator dropped
    assert rows[0]["table_row"]["cells"][0][0]["text"]["content"] == "A"
    assert rows[1]["table_row"]["cells"][1][0]["text"]["content"] == "2"


def test_rich_text_splits_at_2000():
    seg = _rich_text("x" * 4500)
    assert [len(s["text"]["content"]) for s in seg] == [2000, 2000, 500]


def test_large_table_splits_into_multiple_with_repeated_header():
    # [노션 100블록/요청 제한] 데이터 200행 → 여러 table 로 분할, 각 table 은
    # 헤더(1) + 데이터(≤_MAX_TABLE_ROWS-1) 이고 자식 수가 _MAX_TABLE_ROWS 이하.
    rows = ["| A | B |", "| --- | --- |"] + [f"| r{i} | v{i} |" for i in range(200)]
    bs = markdown_to_blocks("\n".join(rows))
    tables = [b for b in bs if b["type"] == "table"]
    assert len(tables) >= 3  # 200 / 89 ≈ 3
    for t in tables:
        children = t["table"]["children"]
        assert len(children) <= _MAX_TABLE_ROWS
        # 각 분할 table 첫 행은 헤더(A) 반복
        assert children[0]["table_row"]["cells"][0][0]["text"]["content"] == "A"
    # 데이터 손실 없음: 헤더 중복분 제외한 데이터 행 총합 = 200
    data_rows = sum(len(t["table"]["children"]) - 1 for t in tables)
    assert data_rows == 200


def test_block_weight_counts_table_children():
    [table] = [b for b in markdown_to_blocks("| A |\n| --- |\n| 1 |\n| 2 |") if b["type"] == "table"]
    assert _block_weight(table) == 1 + 3  # table + (header + 2 data rows)
    assert _block_weight({"type": "paragraph"}) == 1


def test_chunk_blocks_by_weight_respects_table_children():
    # 표(자식 많음) + 단락들이 섞여도 각 chunk 합계(블록+table_row) ≤ limit.
    big_table_md = "\n".join(["| A | B |", "| --- | --- |"] + [f"| r{i} | v{i} |" for i in range(80)])
    blocks = markdown_to_blocks(big_table_md) + [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}} for _ in range(40)
    ]
    chunks = chunk_blocks_by_weight(blocks, limit=95)
    for ch in chunks:
        assert sum(_block_weight(b) for b in ch) <= 95


def test_inline_bold_and_link():
    bs = markdown_to_blocks("a **b** [c](http://x)")
    rts = bs[0]["paragraph"]["rich_text"]
    assert any(
        r["text"]["content"] == "b" and r["annotations"]["bold"] for r in rts
    )
    assert any(
        r["text"].get("link") and r["text"]["link"]["url"] == "http://x" for r in rts
    )


def test_inline_code_and_italic():
    bs = markdown_to_blocks("use `cmd` and _em_")
    rts = bs[0]["paragraph"]["rich_text"]
    assert any(r["text"]["content"] == "cmd" and r["annotations"]["code"] for r in rts)
    assert any(r["text"]["content"] == "em" and r["annotations"]["italic"] for r in rts)


def test_snake_case_not_italicized():
    # get_notion_info 같은 snake_case 가 *notion* 으로 기울임 처리되면 안 됨
    rts = _rich_text("call get_notion_info often")
    assert all(not r["annotations"]["italic"] for r in rts)
    assert "".join(r["text"]["content"] for r in rts) == "call get_notion_info often"


def test_blank_and_empty_input():
    assert markdown_to_blocks("") == []
    assert markdown_to_blocks("\n\n\n") == []


def test_block_cap_truncates():
    md = "\n".join(f"- item {i}" for i in range(2100))
    bs = markdown_to_blocks(md)
    assert len(bs) <= 1801  # 1800 cap + 1 truncation note
    assert bs[-1]["type"] == "paragraph"
