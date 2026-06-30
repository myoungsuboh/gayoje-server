"""
시나리오 fixture 채점 CLI.

사용법:
    python -m evals.run_eval                    # 모든 시나리오의 모든 graph 채점
    python -m evals.run_eval plant              # 특정 시나리오만
    python -m evals.run_eval --snapshot         # 결과를 evals/snapshots/ 에 저장
    python -m evals.run_eval --compare BASELINE # baseline snapshot 과 비교

시나리오 구조:
    evals/scenarios/<name>/
      graph_*.json    # 채점 대상 그래프 (1개 이상)
      README.md       # 시나리오 설명 (선택)

graph JSON 형식:
    {
      "spack": {...},
      "ddd": {...},                          # 선택
      "arch": {...},                          # 선택
      "validation_report": {                  # 선택
        "total_errors": N, "total_warnings": M, "total_infos": K
      }
    }
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from evals.scorer import EvalReport, render_report_text, score_spack

logger = logging.getLogger(__name__)


_SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
_SNAPSHOTS_DIR = Path(__file__).resolve().parent / "snapshots"


def _load_graph(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _list_scenarios(filter_name: Optional[str] = None) -> List[Path]:
    if not _SCENARIOS_DIR.exists():
        return []
    out: List[Path] = []
    for entry in sorted(_SCENARIOS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if filter_name and entry.name != filter_name:
            continue
        out.append(entry)
    return out


def _list_graphs(scenario_dir: Path) -> List[Path]:
    return sorted(scenario_dir.glob("graph_*.json"))


def _score_graph_file(path: Path) -> EvalReport:
    data = _load_graph(path)
    return score_spack(
        data.get("spack") or {},
        ddd=data.get("ddd"),
        arch=data.get("arch"),
        validation_report=data.get("validation_report"),
    )


def _report_to_dict(report: EvalReport) -> Dict[str, Any]:
    """snapshot 저장용 JSON 직렬화. dataclass 라 asdict 가능."""
    return {
        "overall": round(report.overall, 4),
        "tier1": {"score": round(report.tier1.score, 4),
                  "sub_metrics": {k: round(v, 4) for k, v in report.tier1.sub_metrics.items()}},
        "tier2": {"score": round(report.tier2.score, 4),
                  "sub_metrics": {k: round(v, 4) for k, v in report.tier2.sub_metrics.items()},
                  "notes": list(report.tier2.notes)},
        "tier3": {"score": round(report.tier3.score, 4),
                  "sub_metrics": {k: round(v, 4) for k, v in report.tier3.sub_metrics.items()}},
        "tier4": {"score": round(report.tier4.score, 4),
                  "sub_metrics": {k: round(v, 4) for k, v in report.tier4.sub_metrics.items()},
                  "notes": list(report.tier4.notes)},
        "summary": report.summary,
    }


def _save_snapshot(results: Dict[str, Dict[str, Any]], label: str) -> Path:
    _SNAPSHOTS_DIR.mkdir(exist_ok=True)
    path = _SNAPSHOTS_DIR / f"{label}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, sort_keys=True)
    return path


def _compare_against_baseline(
    current: Dict[str, Dict[str, Any]], baseline_path: Path
) -> List[str]:
    """baseline 과 비교, 회귀가 있으면 경고. 변화 없음/향상은 정보."""
    if not baseline_path.exists():
        return [f"⚠️  baseline 파일 없음: {baseline_path}"]
    with baseline_path.open(encoding="utf-8") as f:
        baseline = json.load(f)
    lines: List[str] = []
    for key, cur in current.items():
        base = baseline.get(key)
        if base is None:
            lines.append(f"  + {key}: 신규 (baseline 에 없음)")
            continue
        delta = cur["overall"] - base["overall"]
        marker = "🟢" if delta > 0.005 else ("🔴" if delta < -0.005 else "⚪")
        lines.append(
            f"  {marker} {key}: {base['overall']*100:5.1f}% → "
            f"{cur['overall']*100:5.1f}%  ({'+' if delta>=0 else ''}{delta*100:+.1f}%p)"
        )
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SPACK eval harness")
    parser.add_argument("scenario", nargs="?", default=None,
                        help="특정 시나리오 이름 (생략 시 전체)")
    parser.add_argument("--snapshot", metavar="LABEL", default=None,
                        help="결과를 snapshots/<LABEL>.json 에 저장")
    parser.add_argument("--compare", metavar="LABEL", default=None,
                        help="baseline snapshot 과 비교 표시")
    parser.add_argument("--quiet", action="store_true",
                        help="상세 표 출력 생략, summary line 만")
    args = parser.parse_args(argv)

    scenarios = _list_scenarios(args.scenario)
    if not scenarios:
        print(f"❌ 시나리오 없음 (scenarios dir: {_SCENARIOS_DIR})")
        return 2

    results: Dict[str, Dict[str, Any]] = {}
    for sc in scenarios:
        for gp in _list_graphs(sc):
            key = f"{sc.name}/{gp.stem}"
            report = _score_graph_file(gp)
            results[key] = _report_to_dict(report)
            print()
            print(f"### {key}")
            if args.quiet:
                print(f"  Overall: {report.overall*100:5.1f}%")
            else:
                print(render_report_text(report))

    print()
    print("=" * 60)
    print("Summary:")
    for key, r in results.items():
        print(f"  {key:40s} {r['overall']*100:5.1f}%")

    if args.compare:
        baseline_path = _SNAPSHOTS_DIR / f"{args.compare}.json"
        print()
        print(f"vs baseline `{args.compare}`:")
        for line in _compare_against_baseline(results, baseline_path):
            print(line)

    if args.snapshot:
        path = _save_snapshot(results, args.snapshot)
        print()
        print(f"✅ snapshot 저장: {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
