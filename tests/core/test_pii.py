"""BE-E01-T06 회귀 — PII 마스킹 유틸 + 로깅 필터."""
from __future__ import annotations

import logging

from app.core.observability import PiiMaskingFilter
from app.core.pii import mask_email_partial, mask_pii


def test_mask_pii_email():
    assert mask_pii("문의는 user@example.com 으로") == "문의는 ***@*** 으로"


def test_mask_pii_phone():
    out = mask_pii("연락처 010-1234-5678 입니다")
    assert "***-****-****" in out
    assert "1234-5678" not in out
    assert "01012345678" not in mask_pii("번호 01012345678")


def test_mask_email_partial():
    assert mask_email_partial("alice@gayoje.kr") == "a***@gayoje.kr"
    assert mask_email_partial("-") == "-"
    assert mask_email_partial("nodomain") == "nodomain"


def test_mask_pii_noop_on_plain_text():
    assert mask_pii("그냥 일반 텍스트") == "그냥 일반 텍스트"
    assert mask_pii("") == ""


def test_pii_masking_filter_masks_message_and_user_email():
    flt = PiiMaskingFilter()
    record = logging.LogRecord(
        "t", logging.INFO, "", 0, "user bob@x.com logged in", (), None
    )
    record.user_email = "bob@x.com"
    assert flt.filter(record) is True
    assert "bob@x.com" not in record.getMessage()
    assert record.user_email == "b***@x.com"
