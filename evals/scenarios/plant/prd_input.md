# plant — Product Overview

plant 는 가정 또는 소규모 농장에서 식물을 모니터링하고 자동으로 환경을 제어하는
IoT 식물 관리 시스템이다. 사용자는 웹/모바일 앱으로 식물의 현재 상태 (온도/습도/조도)
와 생장 기록을 확인하고, 자동 환경 제어 설정으로 적정 환경을 유지한다.

대상 사용자: 가정 원예 사용자, 소규모 농장주, 식물 관리자(admin).

# Epic & Story Map

## Epic 01: 식물 정보 관리

[Story 1.1] 사용자는 자신의 식물을 시스템에 등록할 수 있다.
- 입력: name (식물 이름, 1~100자, 필수), species (종, 선택, 100자 이내),
  plantedAt (심은 날짜, 선택)
- 출력: 생성된 식물 ID
- 권한: 인증된 사용자만. 등록자가 자동으로 owner 가 된다.

[Story 1.2] 사용자는 자신의 식물 목록을 조회할 수 있다.
- 출력: 본인이 owner 인 식물 목록 (id, name, species, plantedAt, createdAt)
- 권한: 인증된 사용자만 본인 식물 조회. admin 은 모두 조회 가능.

## Epic 02: 환경 데이터 모니터링

[Story 2.1] 사용자는 식물의 현재 환경 데이터를 실시간으로 조회할 수 있다.
- 데이터: temperature (℃, double), humidity (0~100, integer 백분율),
  lightLevel (lux, integer), measuredAt (UTC ISO-8601)
- 응답 시간: 2초 이내 (POL-06)
- 권한: 본인 소유 식물 또는 admin

## Epic 03: 식물 생장 기록

[Story 3.1] 사용자는 자신의 식물의 생장 기록을 조회할 수 있다.
- 데이터: height (cm, double), leafCount (정수), healthStatus
  (HEALTHY|WARNING|DEAD), recordedAt (UTC)
- 쿼리: from / to 기간 필터 (UTC datetime, 선택)
- 권한: 본인 소유 식물 또는 admin (403 차단)
- 식물 미존재 시 404

[Story 3.2] 사용자는 자신의 식물에 생장 기록을 등록할 수 있다.
- 입력 (POST body):
  · height: double, 필수, > 0 (cm 단위, 양수만)
  · leafCount: integer, 필수, >= 0
  · healthStatus: enum (HEALTHY|WARNING|DEAD), 선택
- 검증: 음수 값 또는 enum 외 값은 422 거부
- 권한: 본인 식물에만 등록 가능 (403 차단)
- 응답: 201 + 생성된 기록 id + recordedAt

## Epic 04: 자동 환경 제어 설정

[Story 4.1] 사용자는 식물의 자동 환경 제어 설정을 조회할 수 있다.
- 데이터: temperatureMin/Max (℃), humidityMin/Max (0~100)
- 설정 미존재 시 404

[Story 4.2] 사용자는 식물의 자동 환경 제어 설정을 생성할 수 있다.
- 입력: temperatureMin/Max (필수), humidityMin/Max (0~100, 필수)
- 검증: min > max 면 422 거부 (범위 역전)
- 권한: 본인 식물만

[Story 4.3] 사용자는 식물의 자동 환경 제어 설정을 변경할 수 있다.
- 입력: 모든 필드 선택 (부분 갱신)
- 검증: 범위 역전 시 422
- 응답: 200 + updatedAt

## Epic 05: 사용자 계정 및 권한

[Story 5.1] 시스템은 사용자 계정과 권한을 관리한다.
- 사용자 속성: id (uuid), email (필수, 유효한 이메일), passwordHash
  (Argon2 해시, DB 저장), role (owner|admin), createdAt
- 인증: OAuth 2.0 기반 JWT (NFR-09)
- 비밀번호: Argon2 해시 저장 — 평문 저장 절대 금지
- 사용자 데이터는 암호화 저장 + RBAC (NFR-08)

# Non-Functional Requirements

- NFR-01 (Availability): 시스템 가동률 99.9% 이상
- NFR-02 (Compatibility): 브라우저 Chrome/Firefox/Safari/Edge 최신 2버전 +
  모바일 앱 iOS 15+/Android 12+
- NFR-03 (Performance): 모든 API 응답 시간 95th percentile 500ms 이내
- NFR-04 (Performance): 데이터 조회/기록 작업 2초 이내
- NFR-05 (Performance): 동시 사용자 100명 처리
- NFR-06 (Performance): 실시간 데이터 조회 2초 이내
- NFR-07 (Scalability): 향후 센서/제어 장치 추가 시 유연한 확장
- NFR-08 (Security): 사용자 데이터 암호화 저장 + RBAC (역할 기반 접근 제어)
- NFR-09 (Security): 인증 OAuth 2.0 + 전송 HTTPS

# Error Handling 공통 규칙

- 401 AUTH_REQUIRED: JWT 누락 또는 만료
- 403 FORBIDDEN_OWNER: 본인 소유 아닌 리소스 접근 시
- 404 *_NOT_FOUND: 경로 파라미터 (plantId 등) 가 존재하지 않을 때
- 422 VALIDATION_ERROR: 입력값 검증 실패 (음수, enum 외, 범위 역전 등)
- 500 INTERNAL: 시스템 오류
