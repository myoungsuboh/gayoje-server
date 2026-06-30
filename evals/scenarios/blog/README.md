# Scenario: blog 게시글·댓글 시스템

게시글·댓글의 **N:M 관계 + 권한 분기** 도메인을 plant·todo·ecommerce 와 다른
관점에서 검증.

## 두 가지 케이스
- `graph_legacy.json`  : 빈약한 변환 (Phase A 이전)
- `graph_phase_a.json` : 충실한 변환 (Phase A 이후)

## 왜 새 시나리오인가
- **권한 분기 다양성**: owner / admin / guest 3계층 — 같은 API 가 사용자 종류별로
  다른 정책 적용 (예: 게시글 수정은 owner+admin, 좋아요는 인증된 모든 사용자).
- **삭제 → soft delete**: status=DELETED 상태로 두고 댓글은 보존 (참조 무결성).
- **모더레이션 흐름**: 댓글 신고 → status=HIDDEN 전이.
- 다른 시나리오는 본인 owner 단일 권한 위주 — blog 는 다대다·다중 역할 검증.
