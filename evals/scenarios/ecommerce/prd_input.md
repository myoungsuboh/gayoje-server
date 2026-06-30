# ecommerce — Product Overview

ecommerce 는 소규모 셀러를 위한 전자상거래 플랫폼이다. 사용자는 상품을 등록·
판매하고, 구매자는 주문·결제·환불을 수행한다.

대상 사용자: 셀러 (seller), 구매자 (buyer), 관리자 (admin).

# Epic & Story Map

## Epic 01: 상품 관리

[Story 1.1] 셀러는 상품을 등록할 수 있다.
- 입력: name (1~200자), price (0 초과, 정수 원화), stock (0 이상 정수),
  description (선택, 10000자 이내)
- 출력: 상품 ID + createdAt
- 권한: seller 또는 admin

[Story 1.2] 누구나 상품을 조회할 수 있다.
- 출력: 활성 상품 목록 (status=ACTIVE). 페이지네이션.
- 권한: 비로그인 가능

## Epic 02: 주문

[Story 2.1] 구매자는 상품을 주문할 수 있다.
- 입력: items (productId + quantity 배열, 최소 1개)
- 검증: 모든 상품 stock >= quantity (부족 시 422)
- 동작: Order(status=CREATED) + OrderItem N건 atomic 생성. totalAmount 자동 계산.
- 권한: 인증된 buyer

[Story 2.2] 구매자는 본인 주문 목록을 조회할 수 있다.
- 출력: 본인 주문 (id, status, totalAmount, createdAt)
- 권한: 본인 buyer 또는 admin

[Story 2.3] 구매자는 미결제 주문을 취소할 수 있다.
- 동작: status=CREATED → CANCELED 전이만 허용. PAID 이상은 환불 흐름 필요.
- 권한: 본인 buyer

## Epic 03: 결제 / 환불

[Story 3.1] 구매자는 주문에 대해 결제할 수 있다.
- 동작: Payment 생성 + Order.status: CREATED → PAID. 이미 PAID 이면 409.
- 입력: orderId, paymentMethod (CARD|BANK|VIRTUAL_ACCOUNT)
- 권한: 본인 주문에만

[Story 3.2] 구매자는 결제 후 환불을 요청할 수 있다.
- 동작: status=PAID → REFUNDED. 부분 환불 불가 (전액만).
- 검증: 결제 후 7일 이내 (POL-03)
- 권한: 본인 주문

## Epic 04: 사용자 / 권한

[Story 4.1] 시스템은 사용자 계정과 권한을 관리한다.
- 사용자 속성: id (uuid), email (필수, unique), passwordHash (Argon2),
  role (seller|buyer|admin), createdAt
- 인증: OAuth 2.0 기반 JWT

# Non-Functional Requirements

- NFR-01 (Availability): 가동률 99.9% 이상
- NFR-02 (Compatibility): Chrome/Firefox/Safari/Edge 최신 2버전
- NFR-03 (Performance): API 응답 95th percentile 500ms 이내
- NFR-04 (Performance): 동시 결제 500건/초 처리
- NFR-05 (Consistency): 주문 생성은 atomic — 일부 OrderItem 만 저장 금지
- NFR-06 (Security): 결제 정보 PCI-DSS 준수, DB 평문 저장 금지
- NFR-07 (Security): OAuth 2.0 + HTTPS

# Error Handling 공통 규칙

- 401 AUTH_REQUIRED
- 403 FORBIDDEN_OWNER
- 404 *_NOT_FOUND
- 409 STATE_CONFLICT: 잘못된 상태 전이 (이미 PAID, 이미 CANCELED 등)
- 422 VALIDATION_ERROR: stock 부족, 환불 기한 초과 등
- 500 INTERNAL
