"""
evals.run_eval CLI 회귀 보호.

핵심:
- fixture (plant/graph_legacy, graph_phase_a) 가 변경되면 점수 변동 감지
- snapshot ↔ compare round-trip 동작
- legacy ≪ phase_a 의 점수 격차 유지 (~70%p 이상)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.run_eval import (
    _SNAPSHOTS_DIR,
    _compare_against_baseline,
    _list_graphs,
    _list_scenarios,
    _score_graph_file,
    main,
)


def test_plant_scenario_exists():
    """plant 시나리오 fixture 가 존재 + 2개 그래프 (legacy / phase_a)."""
    scenarios = _list_scenarios()
    names = [s.name for s in scenarios]
    assert "plant" in names

    plant_dir = next(s for s in scenarios if s.name == "plant")
    graphs = _list_graphs(plant_dir)
    stems = [g.stem for g in graphs]
    assert "graph_legacy" in stems
    assert "graph_phase_a" in stems


def test_plant_legacy_score_low():
    """Phase A 이전 (디테일 부재) 그래프는 30% 미만."""
    plant_dir = next(s for s in _list_scenarios() if s.name == "plant")
    legacy_path = next(g for g in _list_graphs(plant_dir) if g.stem == "graph_legacy")
    report = _score_graph_file(legacy_path)
    assert report.overall < 0.30


def test_plant_phase_a_score_high():
    """Phase A 충실 채움 그래프는 90% 이상."""
    plant_dir = next(s for s in _list_scenarios() if s.name == "plant")
    full_path = next(g for g in _list_graphs(plant_dir) if g.stem == "graph_phase_a")
    report = _score_graph_file(full_path)
    assert report.overall > 0.90


def test_legacy_vs_phase_a_gap_at_least_60pp():
    """legacy ↔ phase_a 격차가 60%p 이상 — Phase A 의 누적 효과 가시화."""
    plant_dir = next(s for s in _list_scenarios() if s.name == "plant")
    legacy = _score_graph_file(
        next(g for g in _list_graphs(plant_dir) if g.stem == "graph_legacy")
    )
    full = _score_graph_file(
        next(g for g in _list_graphs(plant_dir) if g.stem == "graph_phase_a")
    )
    assert (full.overall - legacy.overall) > 0.60


def test_phase_a_tier2_at_least_90pct():
    """Phase A 충실 그래프의 Tier 2 (디테일) 가 90% 이상."""
    plant_dir = next(s for s in _list_scenarios() if s.name == "plant")
    full = _score_graph_file(
        next(g for g in _list_graphs(plant_dir) if g.stem == "graph_phase_a")
    )
    assert full.tier2.score >= 0.90


def test_legacy_tier2_below_10pct():
    """legacy 그래프의 Tier 2 가 10% 미만 (모든 디테일 0)."""
    plant_dir = next(s for s in _list_scenarios() if s.name == "plant")
    legacy = _score_graph_file(
        next(g for g in _list_graphs(plant_dir) if g.stem == "graph_legacy")
    )
    assert legacy.tier2.score < 0.10


def test_cli_main_runs_without_args(capsys):
    """python -m evals.run_eval (인자 없음) — 모든 시나리오 채점."""
    rc = main(["--quiet"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "plant/graph_legacy" in out
    assert "plant/graph_phase_a" in out


def test_cli_main_filters_scenario(capsys):
    rc = main(["plant", "--quiet"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "plant/graph_legacy" in out


def test_cli_main_unknown_scenario_returns_nonzero(capsys):
    rc = main(["nonexistent_scenario_xyz"])
    assert rc != 0
    out = capsys.readouterr().out
    assert "❌" in out or "없음" in out


def test_cli_main_snapshot_then_compare_round_trip(tmp_path, monkeypatch, capsys):
    """snapshot 저장 → compare 시 변동 0%p 확인."""
    # 임시 snapshots 디렉토리로 redirect
    from evals import run_eval as re_mod
    monkeypatch.setattr(re_mod, "_SNAPSHOTS_DIR", tmp_path)

    rc1 = main(["--quiet", "--snapshot", "test_baseline"])
    assert rc1 == 0
    snapshot_file = tmp_path / "test_baseline.json"
    assert snapshot_file.exists()

    capsys.readouterr()  # clear
    rc2 = main(["--quiet", "--compare", "test_baseline"])
    assert rc2 == 0
    out = capsys.readouterr().out
    # 변동 없는 round-trip → ⚪
    assert "⚪" in out


def test_baseline_snapshot_committed_is_consistent():
    """저장된 baseline snapshot 이 현재 채점 결과와 일치 (회귀 보호)."""
    baseline_path = _SNAPSHOTS_DIR / "baseline.json"
    if not baseline_path.exists():
        pytest.skip("baseline snapshot 미존재")
    with baseline_path.open(encoding="utf-8") as f:
        baseline = json.load(f)
    for key, base_data in baseline.items():
        # 현재 점수와 baseline 의 overall 차이 0.01 이상이면 회귀.
        scenario_name, graph_stem = key.split("/")
        plant_dir = next(s for s in _list_scenarios() if s.name == scenario_name)
        graph_path = next(g for g in _list_graphs(plant_dir) if g.stem == graph_stem)
        current = _score_graph_file(graph_path)
        diff = abs(current.overall - base_data["overall"])
        assert diff < 0.01, (
            f"{key} overall 변동: baseline={base_data['overall']:.4f} → "
            f"current={current.overall:.4f}. fixture 변경했다면 "
            f"`python -m evals.run_eval --snapshot baseline` 로 재생성."
        )


def test_compare_against_missing_baseline_reports_warning(tmp_path):
    """없는 baseline 으로 compare → 경고 라인 반환 (raise 없음)."""
    missing = tmp_path / "ghost.json"
    lines = _compare_against_baseline({"x/y": {"overall": 0.5}}, missing)
    assert any("baseline 파일 없음" in l for l in lines)
