# Scenario: ecommerce 전자상거래 시스템

상품/주문/결제/환불의 **트랜잭션 정합성** 도메인을 plant·todo 와 다른 관점에서
회귀 감지하기 위한 fixture.

## 두 가지 케이스
- `graph_legacy.json`  : 빈약한 변환 결과 (Phase A 이전)
- `graph_phase_a.json` : 충실한 변환 결과 (Phase A 이후)

## 왜 새 시나리오인가
- **트랜잭션 정합성**: Order ↔ OrderItem ↔ Payment 의 상태 머신
- **상태 전이 invariants**: Order.status = CREATED → PAID → SHIPPED → DELIVERED → CANCELED
- **환불 흐름**: Payment 의 환불 가능 여부 + 부분 환불 거부
- plant/todo 의 단순 CRUD 와 달리 다단계 트랜잭션 + 도메인 이벤트 검증.
