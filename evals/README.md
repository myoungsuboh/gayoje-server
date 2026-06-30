# Eval Harness

SPACK/DDD/Architecture 그래프를 4-tier 점수로 채점해 변환 LLM / 프롬프트 /
schema 변경의 효과를 정량 측정.

## 왜

정성 분석 ("이 PRD 가 코드 생성에 충분한가?") 에서 정량 측정으로 전환.
매 PR 마다 점수 회귀 추적 → 변환 단계의 진보가 가시화됨.

plant 시나리오 기준:
- legacy (Phase A 이전) — **26%**
- phase_a (Phase A 충실 채움) — **98%**

이 72%p 격차가 사용자가 보던 "디테일 부족" 의 정량 표현.

## 4-tier 채점

| Tier | 가중치 | 무엇 |
|------|--------|------|
| 1 구조  | 10% | apis/entities/policies 가 비어있지 않은지 |
| 2 디테일 | 40% | A-1/A-2/A-3 contract 가 채워졌는지 (attribute 타입, request/response body, error_cases, auth) |
| 3 추적성 | 25% | PRD ↔ 도출 항목 lineage (related_story_id, confidence) |
| 4 정합성 | 25% | design_validator violations (error 0건, warning 감점) |

overall = weighted sum. 0.0 ~ 1.0.

## 사용법

```bash
# 모든 시나리오 채점 (상세 표)
python -m evals.run_eval

# 특정 시나리오만
python -m evals.run_eval plant

# 한 줄 요약만
python -m evals.run_eval --quiet

# 결과를 snapshots/<label>.json 에 저장 (git commit 가능)
python -m evals.run_eval --quiet --snapshot baseline

# baseline 대비 비교 (회귀 가시화)
python -m evals.run_eval --quiet --compare baseline
```

## 새 시나리오 추가

```
evals/scenarios/<name>/
  graph_*.json    # 채점 대상 그래프 (최소 1개, 패턴 graph_*.json)
  README.md       # 시나리오 설명 (선택)
```

`graph_*.json` 형식:
```json
{
  "spack": { "apis": [...], "entities": [...], "policies": [...] },
  "ddd":   { "contexts": [...], "aggregates": [...] },
  "arch":  { "services": [...], "databases": [...] },
  "validation_report": {
    "total_errors": 0, "total_warnings": 5, "total_infos": 12
  }
}
```

`spack` 필수, 나머지는 선택. validation_report 부재 시 Tier 4 만점 처리.

## 실 Gemini 호출 채점

`evals/run_real_llm.py` 가 PRD 텍스트 → 실 Gemini → normalize → 채점 흐름을 한 번에.

```bash
# 자격증명 필요: GEMINI_API_KEY (또는 GOOGLE_API_KEY) 또는
#               LITELLM_PROXY_URL + LITELLM_MASTER_KEY
export GEMINI_API_KEY='<your-key>'

# plant 시나리오의 prd_input.md 를 실 Gemini 에 통과시켜 채점
python -m evals.run_real_llm plant --verbose

# 결과 그래프를 graph_real_llm.json 으로 저장 (이후 run_eval 로 재채점 가능)
python -m evals.run_real_llm plant --save
```

자격증명 미설정 시 명확한 안내 후 rc=3 으로 종료. Neo4j 미사용 (LLM 단계만).

## 다음 단계 (별 PR 후보)

1. **시나리오 확대** — todo, ecommerce, blog 등 도메인 다양화.
2. **CI gate** — PR 마다 baseline 대비 회귀 시 fail.
3. **FE 노출** — Lineage Health 점수를 디자인 패널에 막대 차트로.
4. **위반 항목 추적** — Tier 4 점수 옆에 violation code list 첨부.
