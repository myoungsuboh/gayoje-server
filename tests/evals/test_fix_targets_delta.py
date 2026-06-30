"""fix_targets delta_pct_for — '이 항목 채우면 약 +X%' 효과 산출 단위테스트."""
from evals.fix_targets import delta_pct_for


def test_full_missing_tier2():
    # T2(0.40), 10개 metric, now=0 → (1-0)/10*0.40*100 = 4
    assert delta_pct_for(0.0, 10, 0.40) == 4


def test_partial_rounds_to_min_1():
    # T2, 12개, now=0.75 → 0.25/12*0.4*100 ≈ 0.83 → 최소 1
    assert delta_pct_for(0.75, 12, 0.40) == 1


def test_tier2_weight_beats_tier3():
    # 같은 now/N 이면 가중치 큰 T2(0.40) 가 T3(0.25) 보다 효과 큼
    assert delta_pct_for(0.0, 5, 0.40) > delta_pct_for(0.0, 5, 0.25)


def test_zero_metrics_safe():
    assert delta_pct_for(0.5, 0, 0.40) == 1


def test_already_full_clamps_to_1():
    assert delta_pct_for(1.0, 10, 0.40) == 1
