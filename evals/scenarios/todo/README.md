# Scenario: todo 할 일 관리 시스템

plant 시나리오와 동일한 4-tier 채점 패턴을 다른 도메인(개인 할 일 관리 SaaS)
에서 검증하기 위한 fixture.

## 두 가지 케이스
- `graph_legacy.json`  : 빈약한 변환 결과 (Phase A 이전 수준)
  - Entity attributes 비어있음
  - API 의 request/response/error_cases/auth 누락
  - Tier 2/3 점수 낮음 (≈ plant legacy 와 비슷한 26%대)
- `graph_phase_a.json` : 충실한 변환 결과 (Phase A 이후 기대치)
  - 6개 필드 모두 채워짐 + lineage 매핑
  - Tier 2/3 점수 높음 (≈ plant phase_a 와 비슷한 95%+)

`run_eval.py` 가 두 케이스를 동시 채점해 변환 LLM 의 진보를 정량 표시.

## 왜 새 시나리오인가
- plant 단일 도메인으로는 prompt regression 의 도메인 편향 회귀를 못 잡음.
- todo 는 흔한 SaaS CRUD 패턴 — Entity 간 N:M 관계 (Todo ↔ Tag), enum 상태값
  (PENDING|DONE|ARCHIVED), 마감일 검증 같이 다른 종류의 contract 디테일 검증.

## 다음 도메인 후보
- `ecommerce` — 결제/주문 (트랜잭션 정합성, 환불 흐름)
- `blog`      — 댓글/조회수 (다대다 관계, 권한 분기 owner/admin/guest)
