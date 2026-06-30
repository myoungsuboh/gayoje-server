# blog — Product Overview

blog 는 사내 또는 커뮤니티용 블로그 플랫폼이다. 사용자는 게시글을 작성·조회·
좋아요·댓글하며, 운영자(admin)는 부적절한 댓글을 숨길 수 있다.

대상 사용자: 작성자(author), 일반 사용자(viewer), 관리자(admin), 비로그인 방문자(guest).

# Epic & Story Map

## Epic 01: 게시글 CRUD

[Story 1.1] author 는 새 게시글을 등록할 수 있다.
- 입력: title (1~200자, 필수), body (1~50000자, 필수), tags (선택, 최대 10개)
- 출력: 생성된 게시글 ID + createdAt
- 권한: 인증된 author 자동 owner

[Story 1.2] 누구나 게시글 목록을 조회할 수 있다.
- 출력: 활성(status=PUBLISHED) 게시글 목록
- 권한: guest 가능. 단 DELETED/HIDDEN 게시글은 owner+admin 만 조회 가능.

[Story 1.3] author 는 자신의 게시글을 수정할 수 있다.
- 입력: title, body, tags (모두 선택, 부분 갱신)
- 권한: 본인 게시글만. admin 도 가능 (모더레이션).
- 응답: 200 + updatedAt

[Story 1.4] author 또는 admin 은 게시글을 소프트 삭제할 수 있다.
- 동작: status=DELETED 로 전이. 댓글은 보존 (참조 무결성).
- 권한: 본인 게시글 또는 admin.

## Epic 02: 댓글

[Story 2.1] 인증된 viewer 는 게시글에 댓글을 달 수 있다.
- 입력: body (1~2000자, 필수)
- 권한: 인증된 모든 viewer. guest 차단.
- 검증: 게시글 status=PUBLISHED 가 아니면 422.

[Story 2.2] admin 은 부적절한 댓글을 숨길 수 있다 (모더레이션).
- 동작: comment.status=HIDDEN 으로 전이. 본문은 보존하되 UI 에 미표시.
- 권한: admin 만.

## Epic 03: 좋아요 / 조회수

[Story 3.1] 인증된 viewer 는 게시글에 좋아요를 누를 수 있다.
- 동작: 멱등 (이미 좋아요면 토글로 해제). PostLike 테이블에 (postId, userId) UNIQUE.
- 권한: 인증된 viewer. guest 차단.

## Epic 04: 사용자 / 권한

[Story 4.1] 시스템은 사용자 계정과 권한을 관리한다.
- 사용자 속성: id (uuid), email (필수, unique), passwordHash (Argon2),
  role (author|viewer|admin, 기본 viewer), createdAt
- 인증: OAuth 2.0 기반 JWT
- guest: 비로그인 상태. 일부 GET API 만 접근 가능.

# Non-Functional Requirements

- NFR-01 (Availability): 가동률 99.9% 이상
- NFR-02 (Compatibility): Chrome/Firefox/Safari/Edge 최신 2버전
- NFR-03 (Performance): API 응답 95th percentile 500ms 이내
- NFR-04 (Performance): 게시글 조회 캐시 60초 (좋아요/댓글 카운트 stale 허용)
- NFR-05 (Scalability): 게시글 수 100만 건까지 페이지네이션
- NFR-06 (Security): 사용자 데이터 암호화 저장 + RBAC
- NFR-07 (Security): OAuth 2.0 + HTTPS

# Error Handling 공통 규칙

- 401 AUTH_REQUIRED
- 403 FORBIDDEN_OWNER / FORBIDDEN_ROLE
- 404 *_NOT_FOUND
- 409 STATE_CONFLICT: 이미 삭제된 게시글에 댓글 등
- 422 VALIDATION_ERROR
- 500 INTERNAL
