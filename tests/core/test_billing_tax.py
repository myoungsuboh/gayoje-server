"""
부가세(VAT) 분리 — vat_breakdown + 영수증 이메일 표기.
"""
from __future__ import annotations

import pytest

from app.core.billing_tax import vat_breakdown
from app.core import email as email_lib


@pytest.mark.parametrize("total, supply, vat", [
    (9_900, 9_000, 900),       # Pro
    (17_900, 16_273, 1_627),   # Pro+
    (29_900, 27_182, 2_718),   # Pro Max
    (12_500, 11_364, 1_136),   # 업그레이드 차액 예시
    (0, 0, 0),                 # 쿠폰 무료
])
def test_vat_breakdown_sums_to_total(total, supply, vat):
    s, v = vat_breakdown(total)
    assert (s, v) == (supply, vat)
    # 공급가액 + 부가세 = 합계 (항상 보장)
    assert s + v == total


def test_vat_breakdown_negative_is_zero():
    assert vat_breakdown(-100) == (0, 0)


def test_payment_success_email_shows_vat_split():
    """영수증에 공급가액/부가세/합계가 분리 표기되고 합이 맞는다."""
    subject, html, text = email_lib.render_payment_success_email(
        recipient_name="홍길동",
        plan_label="Pro",
        amount_krw=9_900,
        purpose_label="정기결제",
        paid_at="2026-05-31 10:00",
        next_billing_at="2026-06-30",
        receipt_url=None,
        pricing_url="https://x/pricing",
    )
    # text + html 양쪽에 공급가액/부가세 표기
    for body in (text, html):
        assert "9,000" in body      # 공급가액
        assert "900" in body        # 부가세
        assert "9,900" in body      # 합계
    assert "부가세" in text and "공급가액" in text
    assert "VAT 포함" in text
