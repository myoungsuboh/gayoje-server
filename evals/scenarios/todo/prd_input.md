# todo — Product Overview

todo 는 개인 할 일 관리 SaaS 서비스다. 사용자는 웹/모바일 앱으로 할 일을 등록·
분류·완료 처리하고, 마감일/우선순위/태그로 정리한다.

대상 사용자: 개인 (owner), 관리자 (admin).

# Epic & Story Map

## Epic 01: 할 일 CRUD

[Story 1.1] 사용자는 새 할 일을 등록할 수 있다.
- 입력: title (1~200자, 필수), description (선택, 5000자 이내),
  dueDate (UTC ISO-8601, 선택), priority (LOW|MEDIUM|HIGH, 기본 MEDIUM)
- 출력: 생성된 할 일 ID + createdAt
- 권한: 인증된 사용자만. 등록자가 자동 owner.

[Story 1.2] 사용자는 자신의 할 일 목록을 조회할 수 있다.
- 출력: 본인이 owner 인 할 일 목록 (id, title, status, dueDate, priority, createdAt)
- 쿼리: status 필터 (선택), priority 필터 (선택), sort (createdAt|dueDate, 기본 createdAt 내림차순)
- 권한: 본인 할 일만. admin 은 모두 조회 가능.

[Story 1.3] 사용자는 자신의 할 일을 수정할 수 있다.
- 입력: 모든 필드 선택 (부분 갱신). status 갱신 시 enum 검증 (PENDING|DONE|ARCHIVED)
- 권한: 본인 할 일만 (403 차단)
- 응답: 200 + updatedAt

[Story 1.4] 사용자는 자신의 할 일을 삭제할 수 있다.
- 권한: 본인 할 일만
- 응답: 204 No Content. 할 일 미존재 시 404.

## Epic 02: 완료 처리

[Story 2.1] 사용자는 할 일을 완료 처리할 수 있다.
- POST /api/v1/todos/{todoId}/complete
- 동작: status=DONE, completedAt=now 로 설정
- 이미 DONE 인 경우 409 (멱등 X — 사용자가 의도적 중복 호출 방지)
- 권한: 본인 할 일

## Epic 03: 태그 관리

[Story 3.1] 사용자는 할 일에 태그를 부여하고 태그별로 필터링할 수 있다.
- 태그 속성: name (1~30자, 영문/숫자/한글), color (HEX, 선택)
- 할 일 ↔ 태그: N:M 관계
- 권한: 본인 태그만 부여 가능. 같은 사용자 내 태그명 unique.

## Epic 04: 사용자 계정 및 권한

[Story 4.1] 시스템은 사용자 계정과 권한을 관리한다.
- 사용자 속성: id (uuid), email (필수, 유효 이메일), passwordHash (Argon2),
  role (owner|admin), createdAt
- 인증: OAuth 2.0 기반 JWT
- 비밀번호: Argon2 해시 — 평문 저장 절대 금지

# Non-Functional Requirements

- NFR-01 (Availability): 가동률 99.9% 이상
- NFR-02 (Compatibility): Chrome/Firefox/Safari/Edge 최신 2버전 + iOS 15+/Android 12+
- NFR-03 (Performance): API 응답 시간 95th percentile 500ms 이내
- NFR-04 (Performance): 동시 사용자 1000명 처리 (todo CRUD 가 hot path)
- NFR-05 (Scalability): 단일 사용자 할 일 10만 건까지 페이지네이션 지원
- NFR-06 (Security): 사용자 데이터 암호화 저장 + RBAC
- NFR-07 (Security): 인증 OAuth 2.0 + 전송 HTTPS

# Error Handling 공통 규칙

- 401 AUTH_REQUIRED: JWT 누락 또는 만료
- 403 FORBIDDEN_OWNER: 본인 소유 아닌 리소스 접근 시
- 404 *_NOT_FOUND: 경로 파라미터 (todoId 등) 미존재
- 409 ALREADY_DONE: 이미 완료된 할 일 재완료 시도
- 422 VALIDATION_ERROR: 입력값 검증 실패 (enum 외, 길이 초과, 마감일 과거 등)
- 500 INTERNAL: 시스템 오류
