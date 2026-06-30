# 실 LLM 호출로 plant 시나리오 채점하기

이 컨테이너에서는 `GEMINI_API_KEY` 가 없어 실 호출 불가. 사용자 환경에서
1번 실행 후 결과를 push 하면 후속 분석 가능.

## 사전 점검 (선택)

```bash
# LLM 에 들어갈 prompt 미리 보기
python -m evals.dry_run plant --stats     # 통계만
python -m evals.dry_run plant             # 전체 prompt 출력
```

## 실 호출 단계

### 1. 자격증명 설정

```bash
# 옵션 A: 직접 Gemini API key
export GEMINI_API_KEY='<your-google-ai-studio-key>'

# 옵션 B: LITELLM 프록시 (운영 환경)
export LITELLM_PROXY_URL='https://your-proxy'
export LITELLM_MASTER_KEY='<your-master-key>'
```

### 2. 실행

```bash
python -m evals.run_real_llm plant --verbose --save
```

출력 예 (기대):
```
📄 PRD 입력: evals/scenarios/plant/prd_input.md (2,757 bytes)
🤖 Gemini 호출 시작 (model=gemini-2.5-flash)…
✅ LLM 응답 수신: apis=9, entities=5, policies=9
🔍 normalize: errors=0, warnings=N, infos=M

Violations (severity 별):
  [WARNING] API_PATH_PARAM_UNDECLARED — ...
  ...

============================================================
Overall:   XX.X%
------------------------------------------------------------
Tier 1 (구조)          XXX.X%  (가중치 10%)
...

✅ 그래프 저장: evals/scenarios/plant/graph_real_llm.json
```

### 3. 결과 push

```bash
git add evals/scenarios/plant/graph_real_llm.json
git commit -m "eval: plant 실 LLM 결과 (overall: XX%)"
git push
```

## 후속 분석 — 점수 해석 가이드

### 기대 범위
- **목표**: phase_a fixture (98.2%) 와 비교했을 때 ±15%p 이내
- **이상적**: 80%+ (변환 LLM 이 새 schema 잘 채움)
- **개선 필요**: 50% 미만 — 변환 프롬프트 추가 보강 필요
- **legacy 수준 (26%)** : Phase A 변경이 LLM 동작에 영향 미친 게 없음 — 우리 작업 효과 없음

### 어떤 Tier 가 낮으면 무엇을 의미?
| Tier 가 낮음 | 의미 | 대응 |
|------------|------|------|
| Tier 2 (디테일) 낮음 | LLM 이 attributes/request_body/response_body 안 채움 | design_spack.md 의 "절대 규칙" 강화 |
| Tier 3 (추적성) 낮음 | LLM 이 related_story_id 누락 | LINEAGE 섹션 강화 |
| Tier 4 (정합성) 낮음 | normalize_spack 위반 다수 | 위반 코드 확인 후 prompt 가이드 추가 |

### 점수 추이 비교

```bash
# 실 LLM 결과를 evals 채점기에 통과
python -m evals.run_eval plant

# baseline 과 변동 표시
python -m evals.run_eval plant --compare baseline
```
