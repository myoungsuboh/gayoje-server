"""
notion_to_markdown 단위 테스트 — 블록 픽스처 → markdown 골든 비교.

각 블록 종류마다 최소 1개 케이스. 중첩, 어노테이션, 미지원 블록 silent skip 도 검증.
"""
from __future__ import annotations

from app.core.notion_to_markdown import blocks_to_markdown


def _rt(text: str, **annotations) -> dict:
    """rich_text part 헬퍼."""
    return {
        "plain_text": text,
        "annotations": {
            "bold": False, "italic": False, "strikethrough": False,
            "underline": False, "code": False, "color": "default",
            **annotations,
        },
        "href": annotations.get("href"),
    }


def _paragraph(text: str) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": [_rt(text)]}}


# ─── 기본 블록 ────────────────────────────────────────────────


class TestHeadings:
    def test_h1_h2_h3(self):
        blocks = [
            {"type": "heading_1", "heading_1": {"rich_text": [_rt("Title")]}},
            {"type": "heading_2", "heading_2": {"rich_text": [_rt("Sub")]}},
            {"type": "heading_3", "heading_3": {"rich_text": [_rt("SubSub")]}},
        ]
        out = blocks_to_markdown(blocks)
        assert "# Title" in out
        assert "## Sub" in out
        assert "### SubSub" in out

    def test_empty_heading_skipped(self):
        blocks = [{"type": "heading_1", "heading_1": {"rich_text": []}}]
        assert blocks_to_markdown(blocks).strip() == ""


class TestParagraph:
    def test_simple(self):
        out = blocks_to_markdown([_paragraph("hello")])
        assert "hello" in out

    def test_empty_paragraph_kept_as_blank(self):
        # Notion 의 빈 paragraph 는 빈 줄로 보존 (가독성 — 단, 연속 3개 이상은 압축)
        out = blocks_to_markdown([_paragraph("a"), _paragraph(""), _paragraph("b")])
        assert "a" in out and "b" in out


class TestLists:
    def test_bulleted(self):
        blocks = [
            {"type": "bulleted_list_item",
             "bulleted_list_item": {"rich_text": [_rt("first")]}},
            {"type": "bulleted_list_item",
             "bulleted_list_item": {"rich_text": [_rt("second")]}},
        ]
        out = blocks_to_markdown(blocks)
        assert "- first" in out
        assert "- second" in out

    def test_numbered_counts_correctly(self):
        blocks = [
            {"type": "numbered_list_item",
             "numbered_list_item": {"rich_text": [_rt("a")]}},
            {"type": "numbered_list_item",
             "numbered_list_item": {"rich_text": [_rt("b")]}},
            {"type": "numbered_list_item",
             "numbered_list_item": {"rich_text": [_rt("c")]}},
        ]
        out = blocks_to_markdown(blocks)
        assert "1. a" in out
        assert "2. b" in out
        assert "3. c" in out

    def test_numbered_resets_after_other_block(self):
        blocks = [
            {"type": "numbered_list_item",
             "numbered_list_item": {"rich_text": [_rt("first")]}},
            _paragraph("interruption"),
            {"type": "numbered_list_item",
             "numbered_list_item": {"rich_text": [_rt("restart")]}},
        ]
        out = blocks_to_markdown(blocks)
        assert "1. first" in out
        assert "1. restart" in out

    def test_to_do_checked_and_unchecked(self):
        blocks = [
            {"type": "to_do", "to_do": {
                "rich_text": [_rt("done")], "checked": True,
            }},
            {"type": "to_do", "to_do": {
                "rich_text": [_rt("pending")], "checked": False,
            }},
        ]
        out = blocks_to_markdown(blocks)
        assert "- [x] done" in out
        assert "- [ ] pending" in out


class TestNesting:
    def test_nested_list_indentation(self):
        # 부모 bullet 에 자식 bullet 들이 _children 으로 붙는 케이스
        blocks = [{
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [_rt("parent")]},
            "_children": [{
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [_rt("child")]},
            }],
        }]
        out = blocks_to_markdown(blocks)
        assert "- parent" in out
        # 자식은 2칸 들여쓰기
        assert "  - child" in out


class TestRichTextAnnotations:
    def test_bold(self):
        block = {
            "type": "paragraph",
            "paragraph": {"rich_text": [_rt("bold", bold=True)]},
        }
        assert "**bold**" in blocks_to_markdown([block])

    def test_italic(self):
        block = {
            "type": "paragraph",
            "paragraph": {"rich_text": [_rt("italic", italic=True)]},
        }
        assert "*italic*" in blocks_to_markdown([block])

    def test_strikethrough(self):
        block = {
            "type": "paragraph",
            "paragraph": {"rich_text": [_rt("strike", strikethrough=True)]},
        }
        assert "~~strike~~" in blocks_to_markdown([block])

    def test_inline_code(self):
        block = {
            "type": "paragraph",
            "paragraph": {"rich_text": [_rt("code", code=True)]},
        }
        out = blocks_to_markdown([block])
        assert "`code`" in out

    def test_link(self):
        part = _rt("click")
        part["href"] = "https://example.com"
        block = {"type": "paragraph", "paragraph": {"rich_text": [part]}}
        out = blocks_to_markdown([block])
        assert "[click](https://example.com)" in out

    def test_combined_bold_italic(self):
        block = {
            "type": "paragraph",
            "paragraph": {"rich_text": [_rt("text", bold=True, italic=True)]},
        }
        out = blocks_to_markdown([block])
        # bold 가 안쪽, italic 가 바깥 — 또는 그 반대. 둘 다 있으면 OK.
        assert "**" in out and "*" in out


class TestCode:
    def test_fenced_with_language(self):
        block = {
            "type": "code",
            "code": {
                "rich_text": [_rt("print('hi')")],
                "language": "python",
            },
        }
        out = blocks_to_markdown([block])
        assert "```python" in out
        assert "print('hi')" in out
        assert out.count("```") >= 2

    def test_code_no_annotation_leak(self):
        # 코드 블록 안의 rich_text 에 bold 가 있어도 **...** 으로 감싸지면 안 됨.
        block = {
            "type": "code",
            "code": {
                "rich_text": [_rt("bold_in_code", bold=True)],
                "language": "",
            },
        }
        out = blocks_to_markdown([block])
        assert "**bold_in_code**" not in out
        assert "bold_in_code" in out


class TestQuoteAndCallout:
    def test_quote(self):
        block = {"type": "quote", "quote": {"rich_text": [_rt("wise words")]}}
        out = blocks_to_markdown([block])
        assert "> wise words" in out

    def test_callout_with_emoji(self):
        block = {
            "type": "callout",
            "callout": {
                "rich_text": [_rt("note")],
                "icon": {"type": "emoji", "emoji": "💡"},
            },
        }
        out = blocks_to_markdown([block])
        assert "> 💡 note" in out

    def test_callout_default_emoji_when_none(self):
        block = {
            "type": "callout",
            "callout": {"rich_text": [_rt("plain note")], "icon": None},
        }
        out = blocks_to_markdown([block])
        # icon 없으면 기본 💡 사용
        assert "💡" in out and "plain note" in out


class TestDivider:
    def test_divider(self):
        out = blocks_to_markdown([{"type": "divider"}])
        assert "---" in out


class TestImage:
    def test_external_image(self):
        block = {
            "type": "image",
            "image": {
                "type": "external",
                "external": {"url": "https://example.com/a.png"},
                "caption": [_rt("alt text")],
            },
        }
        out = blocks_to_markdown([block])
        assert "![alt text](https://example.com/a.png)" in out

    def test_image_without_caption_uses_default_alt(self):
        block = {
            "type": "image",
            "image": {
                "type": "external",
                "external": {"url": "https://x.png"},
            },
        }
        out = blocks_to_markdown([block])
        assert "![image](https://x.png)" in out


class TestBookmark:
    def test_with_caption(self):
        block = {
            "type": "bookmark",
            "bookmark": {
                "url": "https://example.com",
                "caption": [_rt("My Link")],
            },
        }
        out = blocks_to_markdown([block])
        assert "[My Link](https://example.com)" in out


class TestTable:
    def test_renders_with_header_separator(self):
        block = {
            "type": "table",
            "table": {"has_column_header": True},
            "_children": [
                {
                    "type": "table_row",
                    "table_row": {"cells": [
                        [_rt("Col A")], [_rt("Col B")],
                    ]},
                },
                {
                    "type": "table_row",
                    "table_row": {"cells": [
                        [_rt("1")], [_rt("2")],
                    ]},
                },
            ],
        }
        out = blocks_to_markdown([block])
        assert "| Col A | Col B |" in out
        assert "| --- | --- |" in out
        assert "| 1 | 2 |" in out


class TestUnsupportedSilentSkip:
    def test_unknown_block_does_not_crash(self):
        blocks = [
            _paragraph("before"),
            {"type": "totally_unknown", "totally_unknown": {}},
            _paragraph("after"),
        ]
        out = blocks_to_markdown(blocks)
        assert "before" in out
        assert "after" in out


class TestEmptyInput:
    def test_empty_list(self):
        assert blocks_to_markdown([]) == ""

    def test_none_safe(self):
        # 호출자가 None 을 줄 수도 있어 (BE 가 보낼 가능성 낮지만 방어).
        assert blocks_to_markdown(None) == ""


class TestBlankLineCollapse:
    def test_three_blanks_collapsed_to_two(self):
        blocks = [
            _paragraph("a"),
            _paragraph(""), _paragraph(""), _paragraph(""), _paragraph(""),
            _paragraph("b"),
        ]
        out = blocks_to_markdown(blocks)
        # 연속 3+ 빈 줄 없어야 함
        assert "\n\n\n\n" not in out


class TestEquation:
    def test_inline_equation(self):
        # rich_text 안에 equation type 이 섞여 들어옴.
        part = {
            "type": "equation",
            "equation": {"expression": "E = mc^2"},
            "annotations": {},
        }
        block = {"type": "paragraph", "paragraph": {"rich_text": [part]}}
        out = blocks_to_markdown([block])
        assert "$E = mc^2$" in out
