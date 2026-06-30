# Scenario: plant 식물 모니터링 시스템

이 시나리오는 사용자가 제공한 plant 패키지 (식물 자동 환경 제어 시스템) 의
변환 결과를 채점하기 위한 fixture 다.

## 두 가지 케이스
- `graph_legacy.json`  : 빈약한 변환 결과 (Phase A 이전 수준)
  - Entity attributes 가 string list 만 또는 미정의
  - API 에 request/response/error_cases/auth 누락
  - Tier 2/3 점수 낮음
- `graph_phase_a.json` : 충실한 변환 결과 (Phase A 이후 기대치)
  - 위 6개 필드 모두 채워짐
  - Tier 2/3 점수 높음

`run_eval.py` 가 두 케이스를 동시 채점해 변환 LLM 의 진보를 정량 표시.

## 다음 단계
- 실 LLM 호출 시나리오: `prd.md` 를 실제 design pipeline 에 통과시켜 받은
  그래프와 `graph_phase_a.json` 비교 (별도 PR).
