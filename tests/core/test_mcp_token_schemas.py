"""McpToken Pydantic 스키마 검증."""
import pytest
from pydantic import ValidationError

from app.schemas import McpTokenIssueRequest


def test_label_strips_whitespace():
    req = McpTokenIssueRequest(label="  노트북-Cursor  ")
    assert req.label == "노트북-Cursor"


def test_label_whitespace_only_is_rejected():
    with pytest.raises(ValidationError):
        McpTokenIssueRequest(label="   ")


def test_label_empty_string_is_rejected():
    with pytest.raises(ValidationError):
        McpTokenIssueRequest(label="")


def test_label_over_80_chars_is_rejected():
    with pytest.raises(ValidationError):
        McpTokenIssueRequest(label="x" * 81)
