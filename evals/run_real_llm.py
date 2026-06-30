"""
실 Gemini 호출 시나리오 채점.

사용법:
    python -m evals.run_real_llm plant

  요구 환경변수: GEMINI_API_KEY (또는 LITELLM_PROXY_URL + LITELLM_MASTER_KEY)
  없으면 명확한 에러로 종료.

흐름:
  1. evals/scenarios/<name>/prd_input.md 읽기
  2. design_pipeline.agents.call_spack_agent (실 Gemini) 호출
  3. design_validator.normalize_spack 으로 정규화 + 검증
  4. evals.scorer.score_spack 으로 채점
  5. 결과 출력 + (옵션) evals/scenarios/<name>/graph_real_llm.json 저장

비용:
  Gemini 호출 1회 (SPACK only). DDD / Architecture 는 별 PR (의존성 더 큼).

Neo4j 미사용:
  call_spack_agent 는 LLM 호출만. 그래프 저장 단계 (cypher build/실행) 는
  스킵 — 이 실행기는 schema/디테일 채워짐만 확인이 목적.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


def _check_credentials() -> Optional[str]:
    """LLM 호출에 필요한 자격증명 점검. 없으면 사람이 읽을 사유 반환."""
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return None
    if os.getenv("LITELLM_PROXY_URL") and os.getenv("LITELLM_MASTER_KEY"):
        return None
    return (
        "GEMINI_API_KEY (또는 GOOGLE_API_KEY) 미설정. "
        "LITELLM_PROXY_URL + LITELLM_MASTER_KEY 조합도 가능.\n"
        "운영 환경에서 다음 중 하나를 export 후 다시 실행하세요:\n"
        "  export GEMINI_API_KEY='<your-key>'\n"
        "  export LITELLM_PROXY_URL='https://...'; export LITELLM_MASTER_KEY='...'"
    )


async def _run(scenario_name: str, *, save: bool, verbose: bool) -> int:
    scenario_dir = _SCENARIOS_DIR / scenario_name
    prd_path = scenario_dir / "prd_input.md"
    if not prd_path.exists():
        print(f"❌ PRD 입력 부재: {prd_path}", file=sys.stderr)
        return 2

    spack_input = prd_path.read_text(encoding="utf-8")
    print(f"📄 PRD 입력: {prd_path} ({len(spack_input):,} bytes)")

    # imports — 호출 시점에 (LLM 자격증명 점검 후)
    from app.clients.gemini_client import GeminiClient
    from app.pipelines.base import PipelineContext
    from app.pipelines.design_pipeline.agents import call_spack_agent
    from app.pipelines.design_validator import normalize_spack, summarize_reports
    from evals.scorer import render_report_text, score_spack

    # call_spack_agent 는 ctx.neo4j 미사용 (LLM 호출만). 그러나 PipelineContext
    # 가 neo4j 필수 필드라 stub 으로 우회 — 사용하면 즉시 raise.
    class _NeoStub:
        async def run_cypher(self, *args, **kwargs):
            raise NotImplementedError("eval real-llm 실행기는 Neo4j 사용 안 함")

    gemini = GeminiClient()
    ctx = PipelineContext(gemini=gemini, neo4j=_NeoStub(), idempotency_key="eval-real-llm")

    print(f"🤖 Gemini 호출 시작 (model={getattr(gemini, 'model', '?')})…")
    raw = await call_spack_agent(ctx, spack_input)
    print(f"✅ LLM 응답 수신: apis={len(raw.get('apis') or [])}, "
          f"entities={len(raw.get('entities') or [])}, "
          f"policies={len(raw.get('policies') or [])}")

    normalized, report = normalize_spack(raw)
    summary = summarize_reports(report, None, None)
    print(f"🔍 normalize: errors={summary['total_errors']}, "
          f"warnings={summary['total_warnings']}, infos={summary.get('total_infos', 0)}")

    if verbose:
        print()
        print("Violations (severity 별):")
        for v in report.violations[:20]:
            print(f"  [{v.severity}] {v.code} — {v.message}")
        if len(report.violations) > 20:
            print(f"  ... (외 {len(report.violations) - 20}건)")

    # 채점
    eval_report = score_spack(
        normalized,
        validation_report={
            "total_errors": summary["total_errors"],
            "total_warnings": summary["total_warnings"],
            "total_infos": summary.get("total_infos", 0),
        },
    )

    print()
    print(render_report_text(eval_report))

    if save:
        graph_data = {
            "spack": normalized,
            "validation_report": {
                "total_errors": summary["total_errors"],
                "total_warnings": summary["total_warnings"],
                "total_infos": summary.get("total_infos", 0),
            },
        }
        out_path = scenario_dir / "graph_real_llm.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(graph_data, f, ensure_ascii=False, indent=2)
        print()
        print(f"✅ 그래프 저장: {out_path}")
        print(f"   다음 채점: python -m evals.run_eval {scenario_name}")

    return 0


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="실 Gemini 호출로 시나리오 채점")
    parser.add_argument("scenario", help="evals/scenarios/<name> 하위 디렉토리")
    parser.add_argument("--save", action="store_true",
                        help="결과 그래프를 graph_real_llm.json 으로 저장")
    parser.add_argument("--verbose", action="store_true", help="violation 상세 출력")
    args = parser.parse_args(argv)

    reason = _check_credentials()
    if reason:
        print(f"❌ 자격증명 부족:\n{reason}", file=sys.stderr)
        return 3

    try:
        return asyncio.run(_run(args.scenario, save=args.save, verbose=args.verbose))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
