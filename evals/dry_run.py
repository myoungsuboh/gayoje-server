"""
dry-run: LLM 에 보낼 prompt 를 미리 보기 (실 호출 X).

사용 동기:
실 호출 전에 어떤 prompt 가 Gemini 에 들어가는지 확인 → 토큰 추정, 비용
예측, 입력 검증. 자격증명 없이도 실행 가능.

사용법:
    python -m evals.dry_run plant                       # full prompt 표시
    python -m evals.dry_run plant --stats               # size/token 만
    python -m evals.dry_run plant --save                # rendered_prompt.txt 저장

토큰 추정:
4 chars ≈ 1 token (rule of thumb, 한국어는 보통 더 많음). 정확치는 실
LLM 응답의 prompt_tokens 참조.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "app" / "prompts"
_SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


def render(scenario_name: str) -> str:
    """design_spack.md 의 <<spack_input>> 자리에 prd_input.md 를 치환."""
    tmpl_path = _PROMPT_DIR / "design_spack.md"
    prd_path = _SCENARIOS_DIR / scenario_name / "prd_input.md"
    if not tmpl_path.exists():
        raise FileNotFoundError(tmpl_path)
    if not prd_path.exists():
        raise FileNotFoundError(prd_path)
    tmpl = tmpl_path.read_text(encoding="utf-8")
    prd = prd_path.read_text(encoding="utf-8")
    if "<<spack_input>>" not in tmpl:
        raise ValueError("design_spack.md 에 <<spack_input>> placeholder 부재")
    return tmpl.replace("<<spack_input>>", prd)


def _print_stats(rendered: str, scenario_name: str, file=None) -> None:
    out = file or sys.stdout
    print(f"📊 {scenario_name} dry-run", file=out)
    print(f"  Final rendered prompt:   {len(rendered):>7,} bytes", file=out)
    # 4 chars/token 근사 (한국어는 보통 1.5~2 배 더 많음)
    est_low = len(rendered) // 4
    est_high = len(rendered) // 2
    print(f"  Estimated input tokens:  ~{est_low:,} ~ {est_high:,}", file=out)
    print(f"  Gemini 2.5 Flash 한계:   1,048,576 (1M)", file=out)
    print(f"  ✅ 충분히 여유" if est_high < 1_048_576 else "  ⚠️  한계 초과 위험", file=out)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="LLM prompt dry-run (실 호출 X)")
    parser.add_argument("scenario", help="evals/scenarios/<name>")
    parser.add_argument("--stats", action="store_true",
                        help="size/token 통계만, prompt 본문 생략")
    parser.add_argument("--save", action="store_true",
                        help="rendered_prompt.txt 로 저장 (디버그용)")
    args = parser.parse_args(argv)

    try:
        rendered = render(args.scenario)
    except FileNotFoundError as e:
        print(f"❌ 파일 부재: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    if args.stats:
        # 통계만 — stdout 에 출력 (사용자가 직접 본다)
        _print_stats(rendered, args.scenario)
    else:
        # 전체 prompt 는 stdout 에, 통계는 stderr 에 (pipe 시 prompt 만 통과)
        print(rendered)
        print("=" * 60, file=sys.stderr)
        _print_stats(rendered, args.scenario, file=sys.stderr)

    if args.save:
        out = _SCENARIOS_DIR / args.scenario / "rendered_prompt.txt"
        out.write_text(rendered, encoding="utf-8")
        print(file=sys.stderr)
        print(f"✅ 저장: {out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
