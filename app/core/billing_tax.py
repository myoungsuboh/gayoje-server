"""
부가세(VAT) 계산 — 표시가(소비자가)는 VAT 포함이라는 전제로 공급가액/부가세 분리.

[정책]
- 국내 부가가치세율 10%. 등급 가격(9,900 / 17,900 / 29,900원)은 모두 VAT 포함 표시가.
- 공급가액 = round(합계 / 1.1), 부가세 = 합계 - 공급가액 (합이 항상 표시가와 일치).
- 0원(쿠폰 등)은 (0, 0).

[범위]
영수증/내역에 "공급가액 + 부가세" 분리 표기까지가 이 모듈 책임.
세금계산서 자동발행(홈택스/팝빌 등 외부연동)은 범위 밖 — 추후 사업자 고객 대응 시.
"""
from __future__ import annotations

VAT_RATE = 0.10  # 부가가치세 10%


def vat_breakdown(total_krw: int) -> tuple[int, int]:
    """VAT 포함 합계(total_krw) → (공급가액, 부가세). 합 = total_krw 보장.

    >>> vat_breakdown(9900)
    (9000, 900)
    >>> vat_breakdown(17900)
    (16273, 1627)
    >>> vat_breakdown(0)
    (0, 0)
    """
    total = int(total_krw or 0)
    if total <= 0:
        return 0, 0
    supply = round(total / (1 + VAT_RATE))
    vat = total - supply
    return int(supply), int(vat)
