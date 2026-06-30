# NORI 백엔드 작업 리스트 — PHASE 1 (무료 통합 디렉토리)

> 코드명 NORI · 폴더 `singa-server` · 레포 `gayoje-server`
> Phase 1 목표: **통합 검색/리스트/지도/캘린더/상세 API, 회원/찜/제보, 알림(웹푸시·이메일)+날씨, 신청 가이드·LLM 작성 도움, 작년 정보·영상·선곡분석, 관리자 검수 콘솔.**
> 공유 에픽(AUTH/NOTI/APP/LIST/DETAIL/ARCH)은 **서버·데이터·API·잡 부분만** 포함하고, 대응 FE 화면 ID를 dependencies에 명시한다.

## Phase 1 에픽 인덱스

| Epic | 영역 | 제목 | Tasks |
|---|---|---|---:|
| BE-EP-E02 | BE | 인증·인가(백엔드) | 4 |
| BE-EP-E03 | BE | 목록·상세·검색·필터·지도·캘린더 API | 6 |
| BE-EP-E04 | BE | 찜·제보·마이데이터 API | 3 |
| BE-EP-E05 | BE | 알림 시스템(백엔드) | 4 |
| BE-EP-E07 | BE | 미디어·파일 서비스 | 4 |
| BE-EP-E10 | BE | 관리자(Admin) 콘솔 백엔드 | 6 |
| DATA-EPIC-DEXTRACT | DATA | LLM 추출·정규화 파이프라인 | 7 |
| DATA-EPIC-DMEDIA | DATA | 포스터/이미지·phash·썸네일·지오코딩 | 3 |
| DATA-EPIC-DEDUP | DATA | 중복제거·병합·자동분류·검증 | 4 |
| AUTH-E1 | AUTH | 인증 백엔드 기반 | 4 |
| AUTH-E2 | AUTH | 이메일 회원가입·로그인(백엔드) | 3 |
| AUTH-E3 | AUTH | 소셜 로그인(카카오·네이버)+연동 | 3 |
| AUTH-E5 | AUTH | 프로필 API | 1 |
| AUTH-E6 | AUTH | 마이페이지 백엔드(찜/알림설정/탈퇴) | 4 |
| AUTH-E7 | AUTH | 2FA 백엔드 | 1 |
| AUTH-E8 | AUTH | 보안·동의/약관 버전·법무(백엔드) | 1 |
| LIST-E5 | LIST | 검색/필터/목록 백엔드 API | 3 |
| DETAIL-EP01 | DETAIL | 상세 데이터 계약 & BFF 엔드포인트 | 5 |
| NAV-0312 | NAV | 홈 집계 백엔드 API | 1 |
| NAV-0402 | NAV | 자동완성 백엔드 | 1 |
| APP-EP01 | APP | 신청 가이드 데이터/API | 2 |
| APP-EP02 | APP | LLM 신청서 작성 도움(백엔드) | 3 |
| APP-EP03 | APP | 음원/악보 준비 가이드·검증(백엔드) | 2 |
| ARCH-E1 | ARCH | 아카이브 데이터모델·수집 연계 | 4 |
| ARCH-VID-E2 | ARCH | 영상 자동 매칭·검증·임베드 파이프라인 | 4 |
| ARCH-SONG-E3 | ARCH | 선정곡/수상곡 DB·메타 정규화 | 2 |
| ARCH-STAT-E4 | ARCH | 선곡 분석·통계 집계 엔진 | 3 |
| ARCH-CUR-E6 | ARCH | 큐레이션 검수 콘솔(admin 백엔드) | 3 |
| ARCH-QA-E7 | ARCH | evals·관측·캐시·성능 | 2 |
| NOTI-01~06 | NOTI | 알림 코어·웹푸시·이메일·설정·날씨·스케줄러 | 19 |
| COM-BE | COM | 법무 enforcement·보안·QA·관측(백엔드측) | 9 |
| GRNZ | GROUNZ | 벤치마크 반영 신규 Task(백엔드) | 10 |
| **합계** | | **33 Epic 영역** | **~146 Task** |

---

# A. BE 코어 API (인증·목록·상세·찜·알림·미디어·Admin)

## [BE] EP-E02 · 인증·인가 (백엔드)

### [BE-E02-T01] 소셜 로그인 OAuth2 연동
- **설명:** 카카오/네이버 OAuth2 인가코드 흐름 가입·로그인. (AUTH-E3와 정합)
- **Subtasks:** authorize-url(state Redis)·callback, social_accounts 매핑, 이메일 미제공 처리, 토큰 암호화, unlink
- **Acceptance Criteria:**
  - 카카오/네이버 가입·로그인이 동작한다.
  - 기존 회원은 즉시, 신규는 동의 후 가입된다.
  - 이메일 미제공 시 가입+보완 안내, state 위조는 거부, 탈퇴 시 연결 해제된다.
- **Edge cases:** 동일 이메일 충돌, 토큰 만료.
- **Endpoints:** `GET /auth/{provider}/authorize-url` · `GET /auth/{provider}/callback`
- **dependencies:** BE-E01-T04 · FE: AUTH-E4-T1(로그인 화면)
- **effort:** L · **priority:** P0

### [BE-E02-T02] JWT 발급/갱신/회전+세션/디바이스
- **설명:** access+refresh 회전·재사용 탐지·세션/디바이스 관리. (AUTH-E1-T2와 정합)
- **Subtasks:** access(짧음)+refresh(긴/jti Redis 회전), 재사용 탐지, 단일/전체 로그아웃, HttpOnly+SameSite, CSRF, 키 회전, clock skew
- **Acceptance Criteria:**
  - access 만료 시 refresh로 자동 재발급된다.
  - refresh 재사용 탐지 시 전체 세션 무효화+알림.
  - 로그아웃 즉시 무효화되고 suspended/deleted는 거부된다.
- **Edge cases:** 다중탭 동시 refresh, 시계 오차.
- **dependencies:** BE-E02-T01
- **effort:** L · **priority:** P0

### [BE-E02-T03] RBAC 역할/권한+인가 가드
- **설명:** 역할(user/organizer/instructor/admin/superadmin)·스코프·소유권 검사.
- **Subtasks:** 역할 정의, 스코프, 소유권 검사, 승인 전 제한
- **Acceptance Criteria:**
  - 권한 없음 시 403, 타인 리소스 접근 차단.
  - 역할 변경이 감사로 남는다.
- **Edge cases:** IDOR, 역할 상승 경합.
- **dependencies:** BE-E02-T02
- **effort:** M · **priority:** P0

### [BE-E02-T04] 관리자 인증 강화(2FA·IP·세션)
- **설명:** 관리자 2FA 필수·IP 허용목록·민감작업 재인증.
- **Acceptance Criteria:** 2FA 필수, 허용목록 외 차단, 민감작업 재인증.
- **dependencies:** BE-E02-T03
- **effort:** M · **priority:** P1

## [BE] EP-E03 · 가요제 목록·상세·검색·필터·지도·캘린더 API
> LIST-E5와 정합. 동일 계약을 BE 영역에서 소유하고 LIST는 FE 소비.

### [BE-E03-T01] 목록 API(페이지네이션·정렬·필터)
- **설명:** 다중 필터 AND·안정 커서·동률 tie-break·비로그인 isFavorited=false.
- **Acceptance Criteria:**
  - 다중 필터가 AND로 결합되고 커서가 안정적이다.
  - 동률 정렬에 tie-break 2차키가 적용된다.
  - 비로그인 시 `isFavorited=false`.
- **Edge cases:** 동시 데이터 변경 중 커서, 빈 결과.
- **Endpoints:** `GET /contests`
- **dependencies:** BE-E01-T04, BE-E02-T03 · FE: LIST-E1, LIST-E2
- **effort:** L · **priority:** P0

### [BE-E03-T02] 상세 API(통합 페이로드)
- **설명:** 일정/주최/장소/장르/상태/D-day + 포스터/첨부/출처 + 신청가이드 요약 + 영상 임베드+선곡 + 좌표 + 관련추천.
- **Acceptance Criteria:**
  - 출처 4종(source_url·기관·수집시각·방식)이 항상 포함된다.
  - 재호스팅 금지(썸네일/임베드만), 비공개는 404.
- **Edge cases:** 부분 섹션 실패, 좌표 없음.
- **Endpoints:** `GET /festivals/{id}` (DETAIL-T-01-01과 정합)
- **dependencies:** BE-E03-T01, BE-EP-E07 · FE: DETAIL-EP02
- **effort:** L · **priority:** P0

### [BE-E03-T03] 검색 API(전문검색·자동완성·동의어)
- **설명:** 한글 부분/초성 검색·자동완성<200ms·동의어 동일 결과.
- **Acceptance Criteria:** 한글 부분/초성, 자동완성 200ms 미만, 동의어 동일 결과.
- **dependencies:** BE-E03-T01
- **effort:** L · **priority:** P1

### [BE-E03-T04] 지도 API(bbox·클러스터링)
- **설명:** bbox 내만+줌별 클러스터·페이로드 제한·필터 동일.
- **Acceptance Criteria:** bbox 내만 반환, 줌별 클러스터, 페이로드 제한, 목록과 동일 필터.
- **Endpoints:** `GET /contests/map?bbox=...&zoom=...`
- **dependencies:** BE-E03-T01 · FE: LIST-E6
- **effort:** M · **priority:** P1

### [BE-E03-T05] 캘린더 API(월/주 집계)
- **설명:** 월 호출 일자 집계·다일 전개·ICS 구독.
- **Acceptance Criteria:** 월 호출 시 일자별 집계, 멀티데이 전개, ICS 구독 제공.
- **Endpoints:** `GET /contests/calendar?month=...`
- **dependencies:** BE-E03-T01 · FE: LIST-E7
- **effort:** M · **priority:** P1

### [BE-E03-T06] 읽기 캐싱·ETag·레이트리밋·OpenAPI
- **설명:** 캐시 히트 시 DB 미조회·검수 승인 후 무효화·OpenAPI 예시.
- **Acceptance Criteria:** 캐시 히트 시 DB 미조회, 검수 승인 후 무효화, OpenAPI 예시 포함.
- **Edge cases:** 캐시 스탬피드, 무효화 누락.
- **dependencies:** BE-E03-T01, T02, T04, T05
- **effort:** M · **priority:** P0

## [BE] EP-E04 · 찜·제보·마이데이터 API

### [BE-E04-T01] 찜 API
- **설명:** 멱등 토글·상태 필터·D-day 알림 자동 구독.
- **Acceptance Criteria:** 멱등 토글, 상태 필터, 찜 시 D-day 알림 자동 구독.
- **Endpoints:** `POST/DELETE /favorites/{festivalId}` · `GET /me/favorites`
- **dependencies:** BE-E03-T01, BE-E02-T03 · FE: LIST-E8, DETAIL-T-08-05
- **effort:** M · **priority:** P0

### [BE-E04-T02] 사용자 제보 API
- **설명:** 검수 큐 승인 시만 공개·중복 식별/병합·스팸 차단.
- **Acceptance Criteria:** 검수 큐 승인 후만 공개, 중복 식별/병합, 스팸 차단.
- **Endpoints:** `POST /reports`
- **dependencies:** BE-E02-T03, BE-EP-E10 · FE: DETAIL-T-01-04
- **effort:** M · **priority:** P1

### [BE-E04-T03] 프로필·계정·탈퇴(PIPA)
- **설명:** 동의 이력 저장·탈퇴 진행 거래 가드·삭제 요청 기간 내+로그.
- **Acceptance Criteria:** 동의 이력 저장, 진행 거래 가드, 삭제 요청 기간 내 처리+로그.
- **dependencies:** BE-E02-T03
- **effort:** M · **priority:** P1

## [BE] EP-E05 · 알림 시스템 (백엔드, NOTI와 정합)

### [BE-E05-T01] 알림 설정·구독 채널 관리
- **설명:** 채널/이벤트 저장·웹푸시 등록/해제·야간 보류.
- **Acceptance Criteria:** 채널/이벤트 저장, 웹푸시 등록/해제, 야간 보류.
- **dependencies:** BE-E04-T01 · 정합: NOTI-T-04-01
- **effort:** M · **priority:** P0

### [BE-E05-T02] 발송 엔진(채널 추상화·템플릿·멱등·재시도)
- **설명:** 3채널+이력·멱등 차단·알림톡 실패 SMS/이메일 폴백.
- **Acceptance Criteria:** 3채널 발송+이력, 멱등 차단, 알림톡 실패 시 SMS/이메일 폴백.
- **Edge cases:** 동시 워커 중복, 폴백 무한루프.
- **dependencies:** BE-E05-T01 · 정합: NOTI-T-01-02/03
- **effort:** XL · **priority:** P0

### [BE-E05-T03] D-day 스케줄러·트리거
- **설명:** 설정 D-day 1회·일정 변경 재계산·KST 일관.
- **Acceptance Criteria:** 설정 D-day 1회 발송, 일정 변경 재계산, KST 일관.
- **dependencies:** BE-E05-T02, BE-E04-T01 · 정합: NOTI-T-06-02
- **effort:** L · **priority:** P0

### [BE-E05-T04] 기상청 날씨 연동
- **설명:** 2주 이내 갱신/제공·악천후 알림·좌표 미상 graceful.
- **Acceptance Criteria:** D-14 이내 갱신/제공, 악천후 알림, 좌표 미상 graceful 처리.
- **dependencies:** BE-E03-T02, BE-E05-T02 · 정합: NOTI-EPIC-05/06
- **effort:** L · **priority:** P1

## [BE] EP-E07 · 미디어·파일 서비스

### [BE-E07-T01] 오브젝트스토리지·CDN·서명URL 추상화
- **설명:** NCP/S3 호환+CDN·공개/비공개 버킷·멀웨어 스캔·라이프사이클.
- **Acceptance Criteria:** presigned 업/다운+만료, 비공개는 인가만, MIME 거부.
- **dependencies:** BE-E01-T03
- **effort:** L · **priority:** P0

### [BE-E07-T02] 이미지 파이프라인(포스터 썸네일·OG)
- **설명:** AVIF/WebP·OG 이미지·EXIF 제거·재호스팅 금지 범위 썸네일/캐시.
- **Acceptance Criteria:** 반응형 썸네일 CDN, OG 생성, 출처 메타 보존.
- **dependencies:** BE-E07-T01
- **effort:** M · **priority:** P1

### [BE-E07-T03] 음원/악보 업로드(제출대행 입력)
- **설명:** 허용 형식+메타·비공개 안전 저장·주문 연결.
- **Acceptance Criteria:** 허용 형식+메타, 비공개 안전 저장, 주문 연결.
- **dependencies:** BE-E07-T01, BE-EP-E08(Phase2)
- **effort:** L · **priority:** P1

### [BE-E07-T04] 영상 임베드 메타·선곡분석 API
- **설명:** 공식 임베드만·선곡/수상 분석·임베드 비허용 제외.
- **Acceptance Criteria:** 공식 임베드만, 선곡/수상 분석 제공, 임베드 비허용 제외.
- **dependencies:** BE-E03-T02
- **effort:** M · **priority:** P2

## [BE] EP-E10 · 관리자(Admin) 콘솔 백엔드

### [BE-E10-T01] 가요제 CRUD·검수·승인 워크플로
- **설명:** 상태 필터·인라인 편집·검수 큐·승인/반려·merge/언두·버전 이력·출처 강제.
- **Acceptance Criteria:**
  - 승인 시 공개+캐시 무효화, 반려 사유 비공개.
  - 중복 병합 시 출처 보존, 동시편집 잠금.
- **Edge cases:** 동시편집 충돌, 병합 언두.
- **dependencies:** BE-E03-T06, BE-E02-T04
- **effort:** XL · **priority:** P0

### [BE-E10-T02] 크롤/수집 결과 검토·재처리
- **설명:** 결과+품질 점수·재추출 트리거·소스 커버리지 집계.
- **Acceptance Criteria:** 결과+품질 점수 제공, 재추출 트리거, 커버리지/실패율 집계.
- **Endpoints:** `GET /admin/ingest/runs` · `POST /admin/ingest/{id}/reextract`
- **dependencies:** BE-E10-T01
- **effort:** L · **priority:** P1

### [BE-E10-T03] 제보·강사/파트너 승인 관리
- **설명:** 제보 반영/반려·파트너 승인/정지+알림·스팸 차단.
- **Acceptance Criteria:** 제보 반영/반려, 파트너 승인/정지+알림, 스팸 차단.
- **dependencies:** BE-E04-T02, BE-EP-E09(Phase3)
- **effort:** M · **priority:** P1

### [BE-E10-T04] 사용자·구독·결제·대행 운영 관리
- **설명:** 조회/조정·환불/역할변경 감사·즉시 반영.
- **Acceptance Criteria:** 조회/조정, 환불/역할변경 감사, 즉시 반영.
- **dependencies:** AUTH-E6-T4, PAY(Phase2), BE-E02-T04
- **effort:** L · **priority:** P1

### [BE-E10-T05] 통계·운영 대시보드 데이터
- **설명:** 기간/필터 집계·사전집계 빠른 반환·원천 정합.
- **Acceptance Criteria:** 기간/필터 집계, 사전집계 빠른 반환, 원천 정합.
- **dependencies:** BE-E10-T04
- **effort:** L · **priority:** P1

### [BE-E10-T06] 감사 로그·시스템 설정·공지/배너
- **설명:** 민감 행위 감사·배너/점검모드·takedown 즉시 비공개.
- **Acceptance Criteria:** 민감 행위 감사, 배너/점검모드, takedown 즉시 비공개.
- **dependencies:** BE-E10-T01
- **effort:** M · **priority:** P1

---

# B. DATA 가공/정규화/품질 파이프라인

## [DATA] EPIC-DEXTRACT · LLM 추출·정규화 파이프라인 (LiteLLM)

### [DATA-T-EXT-01] 추출 스키마(Pydantic/JSON Schema)+필드 사전
- **설명:** 전 화면 요구 필드·타입/포맷/허용값/예시·null+raw_text·근거 span 동반.
- **Subtasks:** festival_name/edition/접수/행사일/장소/주최/장르/부문/참가비/상금/자격/서류/submission_targets 필드, controlled vocabulary, few-shot
- **Acceptance Criteria:**
  - 전 화면 요구 필드를 담는다.
  - 타입/포맷/허용값/예시가 정의되고 null+raw_text+근거 span을 동반한다.
- **dependencies:** DATA-T-SCHEMA-02
- **effort:** M · **priority:** P0

### [DATA-T-EXT-02] 문서 전처리(HWP/HWPX/PDF→텍스트·표)
- **설명:** 포맷 분기+실패 명시·표 구조 보존·스캔 OCR 라우팅·해시 멱등.
- **Subtasks:** 포맷 감지, HWP(pyhwp/hwp5)·HWPX(XML)·PDF(텍스트/표/스캔), 섹션 분할, 추출물 캐싱, 품질 메트릭
- **Acceptance Criteria:**
  - 포맷별 분기+실패가 명시된다.
  - 표 구조 보존, 스캔은 OCR 라우팅, 해시 멱등.
- **Edge cases:** HWP 변환 성공률(Open Q12), 깨진 폰트.
- **dependencies:** DATA-T-SCHEMA-03
- **effort:** XL · **priority:** P0

### [DATA-T-EXT-03] OCR 파이프라인+한국어 후처리
- **설명:** 핵심 필드 복원·OCR 신뢰도 반영·LLM 후보정·텍스트 레이어 스킵.
- **Acceptance Criteria:** 핵심 필드 복원, OCR 신뢰도 반영, LLM 후보정 감소, 텍스트 레이어 스킵.
- **dependencies:** DATA-T-EXT-02
- **effort:** L · **priority:** P1

### [DATA-T-EXT-04] LLM 구조화 추출 코어(스키마 강제·근거·신뢰도)
- **설명:** 항상 스키마 통과/명시 실패·필드 신뢰도+검증 span·캐시 재현·비용/지연 기록.
- **Subtasks:** 프롬프트+청크 병합, JSON mode 강제, 필드 신뢰도, span 환각 검증, temperature 0+캐시, 비용 로깅
- **Acceptance Criteria:**
  - 항상 스키마 통과 또는 명시적 실패.
  - 필드 신뢰도+검증 span, 캐시 재현, 비용/지연 기록.
- **Edge cases:** 장문 청크 병합, span 환각.
- **dependencies:** DATA-T-EXT-01, DATA-T-EXT-02
- **effort:** XL · **priority:** P0

### [DATA-T-EXT-05] 정규화 룰 엔진(날짜·금액·전화·주소·기관·부문)
- **설명:** 한국 표현 표준화/명시 미정·주소 분해 지오코딩 입력·controlled vocabulary 매핑·상태 결정적 산출.
- **Acceptance Criteria:**
  - 한국 표현이 표준화되고 미정은 명시된다.
  - 주소 분해(지오코딩 입력), vocabulary 매핑, 상태 결정적 산출.
- **dependencies:** DATA-T-EXT-04
- **effort:** L · **priority:** P0

### [DATA-T-EXT-06] Evals 품질게이트(골든셋·정확도·회귀)
- **설명:** 필드별 정확도/환각 측정·임계 미만 차단→검수·회귀 CI 차단.
- **Acceptance Criteria:**
  - 필드별 정확도/환각이 측정된다.
  - 임계 미만은 검수 라우팅, 회귀는 CI 차단.
- **Edge cases:** human-review confidence 임계(Open Q16).
- **dependencies:** DATA-T-EXT-04, DATA-T-EXT-05
- **effort:** L · **priority:** P0

### [DATA-T-EXT-07] 신뢰도·검수 라우팅+피드백 루프
- **설명:** 자동게시/검수/차단 결정적·수정 감사+버전 증가·골든셋 적립·임박/인기 우선.
- **Acceptance Criteria:** 자동게시/검수/차단 결정적, 수정 감사+버전 증가, 골든셋 적립, 임박/인기 우선.
- **dependencies:** DATA-T-EXT-06, DATA-T-SCHEMA-05
- **effort:** M · **priority:** P1

## [DATA] EPIC-DMEDIA · 포스터/이미지·phash·썸네일·지오코딩

### [DATA-T-MED-01] 포스터 수집·메타추출·perceptual hash
- **설명:** phash+메타 저장+유사 묶음·원본 URL 보존+재호스팅 금지·손상/초대용량 거부·EXIF 제거.
- **Acceptance Criteria:** phash+메타 저장+유사 묶음, 원본 URL 보존+재호스팅 금지, 손상/초대용량 거부, EXIF 제거.
- **dependencies:** DATA-T-SCHEMA-03
- **effort:** M · **priority:** P1

### [DATA-T-MED-02] 썸네일·반응형·CDN·alt-text(a11y/다국어)
- **설명:** 반응형+LQIP CDN·alt 다국어·법무 미승인 미노출·원본 변경 무효화.
- **Acceptance Criteria:** 반응형+LQIP CDN, alt 다국어, 법무 미승인 미노출, 원본 변경 무효화.
- **Edge cases:** 자체 썸네일이 재호스팅 금지와 충돌 여부(Open Q17 — 법무 확정).
- **dependencies:** DATA-T-MED-01
- **effort:** M · **priority:** P2

### [DATA-T-MED-03] 지오코딩(주소→좌표·다공급자 폴백·정확도)
- **설명:** 카카오/네이버/VWorld 추상화·정확도 등급·캐시·역지오코딩·온라인 좌표 없음.
- **Acceptance Criteria:** 좌표+정확도 PostGIS 저장, 폴백/캐시 한도 준수, region 보정, 해결 불가 시 실패+수동큐.
- **dependencies:** DATA-T-SCHEMA-04, DATA-T-EXT-05
- **effort:** M · **priority:** P1

## [DATA] EPIC-DEDUP · 중복제거·병합·자동분류·검증

### [DATA-T-DEDUP-01] 엔티티 해상(가요제/회차 동일성·블로킹·매칭)
- **설명:** 다출처 클러스터·블로킹 선형 축소·임계 라우팅·증분 매칭.
- **Subtasks:** 매칭 키(이름 trigram/지역/연도/주최/phash/외부키), 블로킹, 유사도 가중합, 임계 정책, dedup_cluster, 증분
- **Acceptance Criteria:** 다출처 클러스터, 블로킹 선형 축소, 임계 라우팅, 증분 매칭.
- **dependencies:** DATA-T-SCHEMA-02, DATA-T-MED-01, DATA-T-EXT-05
- **effort:** XL · **priority:** P0

### [DATA-T-DEDUP-02] 병합·survivorship·provenance·언머지
- **설명:** 필드별 최적값+출처 추적·원천 보존·멱등+언머지·감사.
- **Subtasks:** survivorship(공식>주최>크롤>제보), field-level lineage, 게시 출처표기 데이터
- **Acceptance Criteria:** 필드별 최적값+출처 추적, 원천 보존, 멱등+언머지, 감사.
- **dependencies:** DATA-T-DEDUP-01
- **effort:** L · **priority:** P0

### [DATA-T-DEDUP-03] 자동분류(장르·상태·지역·부문)+신뢰도
- **설명:** 자동 분류+신뢰도·상태 자동 전이·미확신 후보큐·필터/지도/캘린더 정합.
- **Acceptance Criteria:** 자동 분류+신뢰도, 상태 자동 전이, 미확신 후보큐, 필터/지도/캘린더 정합.
- **dependencies:** DATA-T-EXT-05, DATA-T-SCHEMA-04
- **effort:** M · **priority:** P1

### [DATA-T-DEDUP-04] 검증·이상치·결측·신선도+품질 게이트
- **설명:** 게시 전 검증·상태/신선도 자동·품질 메트릭 집계·규칙 위반 flag.
- **Subtasks:** 정합성(날짜 순서/좌표/금액), 이상치, 결측 정책(핵심 보류), 신선도/만료, data_quality_flag, 품질 메트릭(커버리지/신선도/중복률/결측률/신뢰도)
- **Acceptance Criteria:** 게시 전 검증 통과, 상태/신선도 자동, 품질 메트릭 집계, 규칙 위반 flag.
- **dependencies:** DATA-T-DEDUP-02, DATA-T-DEDUP-03
- **effort:** L · **priority:** P0

---

# C. AUTH 백엔드 (JWT·소셜·세션·프로필·마이·탈퇴)

## [AUTH] AUTH-E1 · 인증 백엔드 기반

### [AUTH-E1-T1] 사용자·인증 데이터 스키마(PG+Neo4j)
- **설명:** 이메일/소셜/연동·동의 채널별 분리+버전·동의 이력 append-only·soft delete/hard purge 구분.
- **Subtasks:** users·user_credentials·social_accounts·user_consents·consent_history·verification/reset tokens·user_sessions·user_devices·two_factor·deletion_requests·login_audit, PII 암호화, Neo4j 연계(아웃박스)
- **Acceptance Criteria:**
  - 이메일/소셜/연동 표현, 동의 채널별 분리+버전, 동의 이력 append-only.
  - soft delete/hard purge 구분, user_id 정합.
- **dependencies:** 없음
- **effort:** L · **priority:** P0

### [AUTH-E1-T2] JWT 발급/검증/회전+Redis 세션/블랙리스트
- **설명:** access+refresh 회전·재사용 탐지·HttpOnly·CSRF·키 회전.
- **Acceptance Criteria:**
  - access 만료 시 refresh 자동갱신, 재사용 탐지 시 전체 무효화+알림.
  - 로그아웃 즉시 무효, suspended/deleted 거부, refresh HttpOnly.
- **dependencies:** AUTH-E1-T1
- **effort:** L · **priority:** P0

### [AUTH-E1-T3] 비밀번호 보안+레이트리밋+잠금+감사
- **설명:** argon2id 자동 재해싱·캡차/잠금·이메일 열거 불가·이상 로그인 탐지.
- **Acceptance Criteria:** argon2id 자동 재해싱, 캡차/잠금, 이메일 열거 불가, 새 기기/지역 알림, 변경 시 세션 무효화.
- **dependencies:** AUTH-E1-T1, AUTH-E1-T2
- **effort:** M · **priority:** P0

### [AUTH-E1-T4] 인증 이메일/알림 발송(arq)
- **설명:** 비동기+재시도·1회용 만료 토큰·재전송 쿨다운·locale 렌더·SPF/DKIM/DMARC.
- **Acceptance Criteria:** 비동기+재시도, 1회용 만료 토큰, 재전송 쿨다운, locale별 렌더, SPF/DKIM/DMARC.
- **dependencies:** AUTH-E1-T1
- **effort:** M · **priority:** P0

## [AUTH] AUTH-E2 · 이메일 회원가입·로그인 (백엔드)

### [AUTH-E2-T1] 회원가입/이메일 인증 엔드포인트
- **설명:** 필수 동의 없이 거부·마케팅 채널 분리·토큰/코드 인증·중복 가입 불가·동의 이력+버전+IP.
- **Acceptance Criteria:** 필수 동의 없이 거부, 마케팅 채널 분리 저장, 토큰/코드만 인증, 중복 가입 불가, 동의 이력+버전+IP.
- **Endpoints:** `POST /auth/signup` · `POST /auth/verify-email` · `POST /auth/resend` · `GET /auth/check-email`
- **dependencies:** AUTH-E1-T1~T4 · FE: AUTH-E4-T2
- **effort:** M · **priority:** P0

### [AUTH-E2-T2] 로그인/로그아웃/갱신/세션 엔드포인트
- **설명:** remember_me 만료 차등·미인증 재인증·원격 세션 종료·전체 로그아웃·신규 기기 알림.
- **Acceptance Criteria:** remember_me 만료 차등, 미인증 재인증 안내, 원격 세션 종료, 전체 로그아웃 후 갱신 실패, 신규 기기 알림.
- **Endpoints:** `POST /auth/login` · `/auth/refresh` · `/auth/logout` · `/auth/logout-all` · `GET /auth/me` · `GET /auth/sessions` · `DELETE /auth/sessions/{id}`
- **dependencies:** AUTH-E1-T2, AUTH-E1-T3 · FE: AUTH-E4-T1
- **effort:** M · **priority:** P0

### [AUTH-E2-T3] 비밀번호 재설정+변경 엔드포인트
- **설명:** 열거 불가 동일 응답·재설정 시 전체 세션 무효화·변경 시 현재 비번 확인·소셜 전용 비번 설정.
- **Acceptance Criteria:** 열거 불가 동일 응답, 재설정 시 전체 세션 무효화, 변경 시 현재 비번 확인, 소셜 전용 비번 설정.
- **dependencies:** AUTH-E1-T3, AUTH-E1-T4 · FE: AUTH-E4-T3
- **effort:** S · **priority:** P0

## [AUTH] AUTH-E3 · 소셜 로그인 (카카오·네이버) + 계정 연동

### [AUTH-E3-T1] 카카오 로그인 연동
- **설명:** 인가 코드 흐름·이메일 미제공 처리·state Redis·토큰 암호화·unlink.
- **Acceptance Criteria:** 인가 코드 흐름 로그인/가입, 기존 즉시/신규 약관 후, 이메일 미제공 가입+보완, state 위조 거부, 탈퇴 연결 해제.
- **dependencies:** AUTH-E1-T1, AUTH-E1-T2, AUTH-E2-T1 · FE: AUTH-E4-T1
- **effort:** M · **priority:** P0

### [AUTH-E3-T2] 네이버 로그인 연동
- **설명:** 인가 흐름·이름/이메일 프로필 초기값·state+토큰 암호화.
- **Acceptance Criteria:** 인가 흐름 로그인/가입, 이름/이메일 프로필 초기값, state+토큰 암호화.
- **dependencies:** AUTH-E1-T1, AUTH-E1-T2, AUTH-E2-T1
- **effort:** M · **priority:** P0

### [AUTH-E3-T3] 계정 연동/해제+동일 이메일 통합
- **설명:** 동시 연동/해제·마지막 수단 해제 불가·충돌 본인확인·연동/해제 알림.
- **Acceptance Criteria:** 이메일·카카오·네이버 동시 연동/해제, 마지막 수단 해제 불가, 충돌 본인확인 후 통합, 연동/해제 알림.
- **dependencies:** AUTH-E3-T1, AUTH-E3-T2, AUTH-E2-T3
- **effort:** M · **priority:** P1

## [AUTH] AUTH-E5 · 프로필 (음역대·장르·지역·경력) API

### [AUTH-E5-T1] 프로필 데이터 모델+조회/수정 API
- **설명:** 음역대/장르/지역/경력/소개·이미지 썸네일 CDN·공개/비공개·표준 코드 정규화.
- **Subtasks:** user_profiles(vocal_range·preferred_genres·activity_regions·experience·bio·visibility), 표준 코드 참조, 아바타 업로드, Neo4j PREFERS/ACTIVE_IN, bio 모더레이션
- **Acceptance Criteria:** 음역대/장르/지역/경력/소개 저장, 이미지 썸네일 CDN, 공개/비공개 필터, 표준 코드 정규화.
- **dependencies:** AUTH-E1-T1 · FE: AUTH-E5-T2
- **effort:** M · **priority:** P1

## [AUTH] AUTH-E6 · 마이페이지 허브 (백엔드)

### [AUTH-E6-T1] 마이페이지 대시보드+찜 목록 API
- **설명:** 찜 D-day+상태·즉시 해제·정렬/필터·요약.
- **Acceptance Criteria:** 찜 D-day+상태, 즉시 해제, 정렬/필터, 요약 제공.
- **Endpoints:** `GET /me/favorites` (Neo4j FAVORITES)
- **dependencies:** AUTH-E2-T2, AUTH-E1-T1 · FE: AUTH-E6-T1
- **effort:** M · **priority:** P0

### [AUTH-E6-T2] 알림 설정 백엔드(채널·D-day·마케팅)
- **설명:** 채널×유형 개별 on/off·웹푸시 구독·마케팅 동의 이력·D-day 다중 시점.
- **Acceptance Criteria:** 채널×유형 개별 on/off, 웹푸시 권한+구독+테스트, 마케팅 동의 이력, D-day 다중 시점, 차단 안내.
- **dependencies:** AUTH-E1-T1 · 정합: NOTI-T-04-01 · FE: AUTH-E6-T2
- **effort:** M · **priority:** P0

### [AUTH-E6-T4] 계정 설정(이메일·비번·소셜·기기·2FA·언어) API
- **설명:** 이메일 변경 인증·비번 변경/소셜 연동·원격 세션·언어·동의 현황+이력.
- **Acceptance Criteria:** 이메일 변경 인증 후 반영, 비번 변경/소셜 연동, 원격 세션 종료, 언어 반영, 동의 현황+이력.
- **dependencies:** AUTH-E2-T2, AUTH-E2-T3, AUTH-E3-T3, AUTH-E5-T1
- **effort:** L · **priority:** P1

### [AUTH-E6-T5] 탈퇴+데이터 삭제/내보내기(PIPA)
- **설명:** 본인확인 후 유예 purge·유예 내 복구·법적 보관 익명화/식별 삭제·데이터 내보내기.
- **Subtasks:** soft delete+scheduled_purge_at, restore, export(JSON), purge 워커(PII/아바타/소셜토큰), Neo4j/PG/스토리지/푸시 일괄
- **Acceptance Criteria:** 본인확인 후 유예 purge, 유예 내 복구, 법적 보관 익명화/식별 삭제, 데이터 내보내기, 진행 거래 안내.
- **dependencies:** AUTH-E1-T1, AUTH-E6-T4
- **effort:** M · **priority:** P0

> 참고: AUTH-E6-T3(신청이력 화면)은 FE 골격으로 분류 — BE는 Phase2 BIZ/APP 신청이력 연계로 다룸.

## [AUTH] AUTH-E7 · 선택적 2단계 인증

### [AUTH-E7-T1] 2FA 백엔드(TOTP/이메일 OTP+복구코드)
- **설명:** TOTP 등록/활성+로그인 챌린지·복구코드 소진·신뢰기기 스킵·오입력 잠금.
- **Acceptance Criteria:** TOTP 등록/활성+로그인 챌린지, 복구코드 소진, 신뢰기기 스킵, 오입력 잠금, 활성/비활성 알림.
- **dependencies:** AUTH-E1-T2, AUTH-E1-T3, AUTH-E2-T2 · FE: AUTH-E7-T2
- **effort:** M · **priority:** P2

## [AUTH] AUTH-E8 · 공통 인프라(보안·동의/약관·법무 — 백엔드)

### [AUTH-E8-T2] 보안 강화+동의/약관 버전+법무 준수
- **설명:** CSP/HSTS·개정 시 재동의·만14세·마케팅 철회 즉시·탈퇴 시 보관/삭제 분리.
- **Acceptance Criteria:** CSP/HSTS, 개정 시 재동의, 만14세 처리, 마케팅 철회 즉시, 탈퇴 시 보관/삭제 분리.
- **dependencies:** AUTH-E1-T1, AUTH-E2-T1
- **effort:** M · **priority:** P0

> 참고: AUTH-E8-T1(Pinia store), AUTH-E8-T3(i18n/a11y/SEO FE), AUTH-E4(화면)는 FE 스코프 — dependencies 교차참조만.

---

# D. LIST / DETAIL / NAV / APP 백엔드 (FE 화면 대응 서버측)

## [LIST] E5 · 검색/필터/목록 백엔드 API

### [LIST-E5-T1] GET /contests 목록·필터·정렬·페이징
- **설명:** 전 필터 정확+안정 커서·5정렬 결정적 2차키·상태/D-day 서버 KST·facets 일치.
- **Subtasks:** 쿼리 파라미터 전수, 정렬(deadline_soon/latest/prize/popular/distance), 커서 페이징, 응답 스키마(source 메타·dday·wishlisted), 동적 상태 계산, 복합 인덱스, Redis 캐시, facets, dedup
- **Acceptance Criteria:**
  - 전 필터 정확+안정 커서, 5정렬 결정적 2차키.
  - 상태/D-day 서버 KST 계산, facets 일치, p95<300ms.
- **Endpoints:** `GET /contests`
- **dependencies:** 없음(BE-E03-T01과 동일 계약 소유) · FE: LIST-E1~E3, E9
- **effort:** XL · **priority:** P0

### [LIST-E5-T2] 검색·자동완성·오타보정·인기검색
- **설명:** 엔티티 가중 정렬·초성/자모/오타 후보·인기 기간 윈도우·하이라이트.
- **Subtasks:** `GET /search/suggest`·`/search`·`/search/popular`, PG FTS(nori)/OpenSearch 추상화, 편집거리/자모 유사도, ZSET 랭킹, NFC 정규화, 어뷰징/민감어 필터
- **Acceptance Criteria:** 엔티티 가중 정렬, 초성/자모/오타 후보, 인기 기간 윈도우, 하이라이트, 캐시 p95<150ms.
- **Edge cases:** 검색 백엔드 선택(PG FTS vs OpenSearch — Open Q5).
- **dependencies:** LIST-E5-T1 · FE: LIST-E4, NAV-EP04
- **effort:** XL · **priority:** P0

### [LIST-E5-T3] 지도/캘린더 경량 응답·클러스터링
- **설명:** bbox 줌별 클러스터·캘린더 일자 집계+멀티데이·좌표없음 별도.
- **Subtasks:** `GET /contests/map?bbox`(서버 클러스터·geohash), `GET /contests/calendar?month`, 멀티데이 펼침, 좌표없음 카운트, bbox/줌 캐싱
- **Acceptance Criteria:** bbox 줌별 클러스터 마커 제한, 캘린더 일자 집계+멀티데이, 좌표없음 별도.
- **dependencies:** LIST-E5-T1 · FE: LIST-E6, E7
- **effort:** L · **priority:** P1

## [NAV] 홈/자동완성 백엔드

### [NAV-T-NAV-0312] 홈 집계 백엔드 API(섹션 통합)
- **설명:** 단일 요청 above-the-fold·게스트/로그인 분기·출처 메타·캐시 적중 P95<200ms.
- **Subtasks:** `GET /home` 또는 분할(/closing-soon·/recommendations·/by-region·/new·/popular·/banners), 개인화 파라미터, 출처 필드, Redis 캐시(시드 정규화/버킷팅), 커서, 재호스팅 금지 반영, 스탬피드 방지
- **Acceptance Criteria:** 단일 요청 above-the-fold, 게스트/로그인 분기, 출처 메타, 캐시 적중 P95<200ms.
- **Edge cases:** 단일 /home vs 섹션 분할(Open Q7), 캐시 스탬피드.
- **dependencies:** 데이터 수집/정규화 · FE: NAV-EP03
- **effort:** XL · **priority:** P0

### [NAV-T-NAV-0402] 자동완성 백엔드(가요제/지역/장르/주최)
- **설명:** 초성/오타 제안·카테고리 그룹+하이라이트·P95<150ms.
- **Endpoints:** `GET /search/suggest`
- **Acceptance Criteria:** 초성/오타 제안, 카테고리 그룹+하이라이트, P95<150ms, 인젝션 방지.
- **dependencies:** 데이터 정규화(LIST-E5-T2와 정합) · FE: NAV-EP04
- **effort:** L · **priority:** P0

## [DETAIL] EP-DETAIL-01 · 상세 화면 데이터 계약 & BFF 엔드포인트

### [DETAIL-T-01-01] GET /festivals/{id} 통합(aggregate) 응답 스키마
- **설명:** 단일 호출 above-the-fold·null 의미 문서화·section_status 부분실패·source 1+·OpenAPI CI 검증.
- **Subtasks:** 식별/핵심/일정(timezone KST·is_date_tbd·date_confidence)/포스터(blurhash)/첨부(parsed_summary_available·virus_scan)/상금/자격/신청/★submission/위치(geocode_confidence)/sources/메타(last_verified_at·has_changes_pending)/관계 ref/section_status, OpenAPI+픽스처
- **Acceptance Criteria:**
  - 단일 호출로 above-the-fold 충족, null 의미 문서화.
  - section_status 부분실패 표현, source 1개 이상, OpenAPI CI 검증.
- **Endpoints:** `GET /festivals/{id}`
- **dependencies:** 수집/정규화·지오코딩 · FE: DETAIL-EP02
- **effort:** L · **priority:** P0

### [DETAIL-T-01-02] 섹션 지연로드 보조 엔드포인트(weather/videos/similar/instructors/summary)
- **설명:** weather D-14 이전 available=false·독립 실패 격리·ETag 304·instructors Phase 플래그.
- **Subtasks:** 보조 GET 5종, Cache-Control/ETag/SWR, Redis TTL(weather 1h/videos 24h/similar 6h)
- **Acceptance Criteria:** weather D-14 이전 available=false, 독립 실패 격리, ETag 304, instructors Phase 플래그.
- **Endpoints:** `GET /festivals/{id}/weather|videos|similar|instructors|summary`
- **dependencies:** DETAIL-T-01-01
- **effort:** M · **priority:** P0

### [DETAIL-T-01-03] 조회수 카운팅·어뷰즈 방지
- **설명:** 30분 디듀프 1회·LCP 비차단·봇 미반영.
- **Subtasks:** ip+ua 해시/user_id 디듀프, Redis INCR 버퍼→arq, 봇 필터, sendBeacon 수신
- **Acceptance Criteria:** 30분 디듀프 1회, LCP 비차단, 봇 미반영.
- **dependencies:** DETAIL-T-01-01
- **effort:** S · **priority:** P1

### [DETAIL-T-01-04] 오류·변경 신고 접수·모더레이션 큐
- **설명:** 비로그인 캡차·접수번호·저작권 takedown 우선 라우팅.
- **Endpoints:** `POST /reports`
- **Acceptance Criteria:** 비로그인 캡차, 접수번호, 저작권 takedown 우선 라우팅.
- **dependencies:** DETAIL-T-01-01
- **effort:** M · **priority:** P1

### [DETAIL-T-01-05] 첨부 다운로드 프록시·바이러스 스캔·미리보기 변환
- **설명:** 프록시 안정·스캔 미통과 차단·HWP 미리보기 불가 다운로드 폴백.
- **Subtasks:** 서명 URL 리다이렉트, 미러링→스토리지+clamav, HWP/HWPX→PDF 변환, PDF→썸네일, MIME 검증, 라이선스 모호 시 원문 링크만(법무)
- **Acceptance Criteria:** 프록시 안정, 스캔 미통과 차단, HWP 미리보기 불가 시 다운로드 폴백.
- **Edge cases:** HWP 변환 실패율(Open Q12), 라이선스 모호.
- **dependencies:** DETAIL-T-01-01 · FE: DETAIL-EP06
- **effort:** L · **priority:** P0

## [APP] EP-APP-01 · 대회별 신청 가이드 (데이터/API)

### [APP-T-01-01] 신청 가이드 데이터 모델·정규화 스키마
- **설명:** 신청방법/서류/제출물/마감/문의 정규화·마감 timestamptz+소인/도착/온라인·전 필드 출처+원문 발췌.
- **Subtasks:** application_guide 테이블, steps 스키마, extraction_confidence/needs_review, source 보존, Neo4j 관계, dday 파생, alembic
- **Acceptance Criteria:** 누락없이 정규화, 마감 timestamptz+유형, 전 필드 출처+원문 발췌, 신뢰도/검수 조회, PG↔Neo4j 정합.
- **dependencies:** 수집 contest·LLM 추출 · FE: APP-T-01-03
- **effort:** L · **priority:** P0

### [APP-T-01-02] 신청 가이드 API
- **설명:** steps 의존순서·진행상태 저장/복원·비로그인 본문/저장 401·만료 서명 URL·중복 제보 rate-limit.
- **Endpoints:** `GET /contests/{id}/application-guide` · `PUT /contests/{id}/checklist` · `POST .../report-issue` · `GET .../source-documents`
- **Acceptance Criteria:** steps 의존순서, 진행상태 저장/복원, 비로그인 본문 가능/저장 401, 만료 서명 URL, 중복 제보 rate-limit, ETag/304.
- **dependencies:** APP-T-01-01 · FE: APP-T-01-03, T-01-04
- **effort:** M · **priority:** P0

## [APP] EP-APP-02 · LLM 신청서 작성 도움 (백엔드)

### [APP-T-02-01] 요강→신청서 항목 자동 식별·동적 폼 스키마
- **설명:** 라벨/유형/필수/제약 JSON·정량 제약 검증·PII sensitive 태깅·evals F1≥0.85·실패 기본폼 폴백.
- **Subtasks:** form_fields 추출, 유형 추론, 제약 추출, PII 태깅(마스킹/암호화), 섹션 그룹화, arq 비동기, evals 골든셋, needs_human_review, 기본폼 폴백
- **Acceptance Criteria:** 라벨/유형/필수/제약 JSON, 정량 제약 검증가능, PII sensitive 태깅, evals F1≥0.85 회귀 차단, 실패 시 기본폼 폴백.
- **dependencies:** APP-T-01-01, 요강 원문
- **effort:** XL · **priority:** P0

### [APP-T-02-02] LLM 작성 도움 백엔드(자동완성·예시·맞춤법·분량·점검)
- **설명:** 맥락 예시·다듬기 diff·한글 글자수+제약 비교·민감정보 마스킹 후 전송·쿼터 429·evals.
- **Subtasks:** example/improve/autocomplete(SSE)/check/polish-all, PII 마스킹, 토큰 쿼터(등급별), 프롬프트 인젝션 방어, evals, 감사로그
- **Acceptance Criteria:** 맥락 반영 예시, 다듬기 diff, 한글 글자수(공백 포함/제외)+제약 비교, 민감정보 마스킹 후 전송, 쿼터 429, evals 회귀 차단.
- **Endpoints:** `POST /assist/example|improve|autocomplete|check|polish-all`
- **dependencies:** APP-T-02-01 · FE: APP-T-02-03
- **effort:** XL · **priority:** P0

### [APP-T-02-04] 초안 버전관리·복원·내보내기 백엔드
- **설명:** 버전 스냅샷+복원·HWP/PDF 서명 다운로드·민감 암호화 본인만·탈퇴 시 파기.
- **Subtasks:** application_drafts(jsonb 암호화), draft CRUD·versions·restore·export(arq), HWP 템플릿 매핑 엔진, PII at-rest 암호화, 낙관적 잠금
- **Acceptance Criteria:** 버전 스냅샷+복원, HWP/PDF 서명 다운로드, 민감 암호화 본인만, 탈퇴 시 파기.
- **dependencies:** APP-T-02-01
- **effort:** L · **priority:** P1

## [APP] EP-APP-03 · 음원/악보 준비 가이드 & 검증 (백엔드)

### [APP-T-03-01] 음원/악보/MR 준비 가이드·대회별 요구사항 매핑
- **설명:** 대회별 길이/포맷/수 추출 반영·저작권 체크리스트·다국어.
- **Subtasks:** 표준 가이드(포맷/비트레이트/길이/네이밍), MR/반주 안내, 악보 가이드, 저작권 체크리스트(KOMCA/표절/AI), contest_media_requirements, 맞춤 차이 하이라이트, 면책
- **Acceptance Criteria:** 대회별 요구사항 반영, 저작권 체크리스트(공통+대회별), 다국어.
- **dependencies:** APP-T-01-01 · FE: APP-T-03-03
- **effort:** L · **priority:** P0

### [APP-T-03-02] 음원/악보 업로드·형식검증·미리듣기 백엔드
- **설명:** 실제 포맷/길이/용량 서버 검증+요구사항 대조·미리듣기/파형·악성/위조 차단·pass/warn/fail.
- **Subtasks:** presigned 멀티파트, ffprobe(길이/LUFS/채널), 요구사항 대조, 미리듣기+파형 peaks, 악보 썸네일, ClamAV, 쿼터, 보존/파기
- **Acceptance Criteria:** 실제 포맷/길이/용량 검증+요구사항 대조, 미리듣기/파형, 악성/위조 차단, pass/warn/fail 판정.
- **dependencies:** APP-T-03-01 · FE: APP-T-03-03
- **effort:** XL · **priority:** P0

---

# E. ARCH 백엔드 (아카이브·영상 매칭·선정곡·통계·큐레이션)

## [ARCH] ARCH-E1 · 연도별 아카이브 데이터 모델 & 수집 연계

### [ARCH-E1-T1] 아카이브 그래프/정형 스키마·마이그레이션
- **설명:** 시드3 단일 Cypher·Video embed_url+platform(바이너리 없음)·핵심 출처/trust_tier NOT NULL·work_id 동명이곡 분리.
- **Subtasks:** Festival/Edition/Performance/Song/Artist/Award/Video 노드+관계, 출처 메타 표준화, song_master, PG 미러, unique/복합 인덱스, 시드3
- **Acceptance Criteria:** 시드3 전 경로 단일 Cypher, Video는 embed_url+platform(바이너리 필드 없음), 핵심 출처/trust_tier NOT NULL, work_id 동명이곡 분리, up/down 멱등.
- **dependencies:** 없음
- **effort:** L · **priority:** P0

### [ARCH-E1-T2] 동일 시리즈 canonical 매핑·연도 묶기
- **설명:** 명칭변경 동일 canonical+NEXT_EDITION·별칭 검색 라우팅·잘못 병합 unmerge.
- **Acceptance Criteria:** 명칭변경 동일 canonical+NEXT_EDITION, 별칭 검색 라우팅, 잘못 병합 unmerge.
- **dependencies:** ARCH-E1-T1
- **effort:** M · **priority:** P0

### [ARCH-E1-T3] 수상 발표/요강 HWP·PDF→수상자·선정곡 LLM 추출
- **설명:** 골든셋50 등급/참가자/곡 정확도·임계 미만 검수큐·재처리 무중복·source/method 보존.
- **Subtasks:** HWP/PDF/OCR 핸들러, 추출 스키마, few-shot, song_master 매칭, 신뢰도, evals 골든셋50, 멱등 upsert, arq retry
- **Acceptance Criteria:** 골든셋50 등급/참가자/곡 정확도 목표 달성, 임계 미만 검수큐, 재처리 무중복, source/method 보존.
- **dependencies:** ARCH-E1-T1
- **effort:** XL · **priority:** P0

### [ARCH-E1-T4] 사용자 제보 수집·검수 게이트
- **설명:** 검수 전 비공개·중복 신뢰도 가중 합산·승인/거절 알림.
- **Acceptance Criteria:** 검수 전 비공개, 중복 신뢰도 가중 합산, 승인/거절 알림.
- **dependencies:** ARCH-E1-T1
- **effort:** M · **priority:** P1

## [ARCH] VID-E2 · 공식 영상 자동 매칭·검증·임베드 파이프라인

### [ARCH-VID-E2-T1] YouTube/Naver 검색·매칭 배치(LLM 보조)
- **설명:** 수상 공연 목표비율 candidate·confidence/근거/source·바이너리 미저장·쿼터 초과 이월.
- **Subtasks:** YouTube Data API v3+Naver, 쿼리 생성기, 매칭 피처(텍스트/채널 공식성/날짜/길이), LLM 판정, metadata만, 우선순위 큐, 캐시
- **Acceptance Criteria:** 수상 공연 목표비율 candidate, confidence/근거/source, 바이너리 미저장, 쿼터 초과 이월.
- **dependencies:** ARCH-E1-T1, ARCH-E1-T3
- **effort:** XL · **priority:** P0

### [ARCH-VID-E2-T2] 임베드 가능성·공식성·생존성 검증
- **설명:** 임베드 불가 링크만·삭제 24h내 dead·공식 official_flag.
- **Subtasks:** embeddable/oEmbed, 공식 채널 화이트리스트+휴리스틱, 생존성 모니터링, candidate→verified→confirmed/dead/blocked, 링크 폴백
- **Acceptance Criteria:** 임베드 불가는 링크만, 삭제 24h내 dead, 공식 official_flag.
- **dependencies:** ARCH-VID-E2-T1
- **effort:** L · **priority:** P0

### [ARCH-VID-E2-T3] 라이브 풀영상 타임스탬프/세그먼트 매핑
- **설명:** 특정 공연 시작시각 임베드·없으면 전체+안내.
- **Acceptance Criteria:** 특정 공연 시작시각 임베드, 없으면 전체+안내.
- **dependencies:** ARCH-VID-E2-T1
- **effort:** M · **priority:** P1

### [ARCH-VID-E2-T4] 저작권/임베드 정책 가드·takedown
- **설명:** 다운로드 경로 부재·접수 즉시 숨김·차단 재매칭 제외·출처 누락 published 불가.
- **Subtasks:** embed_only 가드, 출처표기 강제, takedown 폼/워크플로(SLA 48h), 블랙리스트, 다국어 고지
- **Acceptance Criteria:** 다운로드 경로 부재(테스트), 접수 즉시 숨김, 차단 재매칭 제외, 출처 누락 시 published 불가.
- **dependencies:** ARCH-VID-E2-T1
- **effort:** M · **priority:** P0

## [ARCH] SONG-E3 · 선정곡/수상곡 DB & 음악 메타데이터 정규화

### [ARCH-SONG-E3-T1] 곡 마스터·정규화/디듀프
- **설명:** 표기 변형 합침·동명이곡 분리·원곡-커버 그래프.
- **Subtasks:** song_master, 퍼지+LLM disambiguation, COVER_OF/ADAPTED_FROM, 아티스트 정규화, 수동 병합/분리
- **Acceptance Criteria:** 표기 변형 합침, 동명이곡 분리, 원곡-커버 그래프.
- **Edge cases:** 곡/수상곡 메타 저작권 범위(Open Q13, KOMCA).
- **dependencies:** ARCH-E1-T1
- **effort:** L · **priority:** P0

### [ARCH-SONG-E3-T2] 난이도 스코어링(규칙+LLM)·설명가능성
- **설명:** 점수+등급+근거 저장·근거 없는 점수 없음·공개 시 추정 면책.
- **Subtasks:** 음역대/지속고음/템포/조옮김 피처, 규칙+LLM, 등급+수치+근거 json, evals 보정
- **Acceptance Criteria:** 점수+등급+근거 저장, 근거 없는 점수 없음, 공개 시 추정 면책.
- **Edge cases:** 난이도 공개 노출 여부·최소 표본(Open Q15).
- **dependencies:** ARCH-SONG-E3-T1
- **effort:** L · **priority:** P1

## [ARCH] STAT-E4 · 선곡 분석·통계 집계 엔진

### [ARCH-STAT-E4-T1] 통계 집계 잡·머티리얼라이즈드 뷰
- **설명:** 차원별 사전집계·표본 미달 데이터 부족·갱신 시 무효화/재집계.
- **Subtasks:** MV(장르분포/인기선곡/수상경향/난이도/아티스트빈도), 최소표본 임계, 증분 재집계, Redis 캐시, 신뢰도 메타
- **Acceptance Criteria:** 차원별 사전집계 빠른 반환, 표본 미달 시 데이터 부족 표시, 갱신 시 무효화/재집계.
- **dependencies:** ARCH-E1-T1, ARCH-SONG-E3-T1
- **effort:** L · **priority:** P0

### [ARCH-STAT-E4-T2] 연도별 비교·트렌드
- **설명:** N개 연도 추세·결손 구분·LLM 인사이트 수치 근거.
- **Acceptance Criteria:** N개 연도 추세, 결손 구분, LLM 인사이트 수치 근거.
- **dependencies:** ARCH-STAT-E4-T1, ARCH-E1-T2
- **effort:** M · **priority:** P1

### [ARCH-STAT-E4-T3] 선곡 분석 공개 API
- **설명:** 전 응답 source/provenance·미검수 제외·필터 422·캐시 헤더.
- **Endpoints:** `/archive/festivals|editions|performances|songs|videos` · `/stats/genres|popular-songs|awards|difficulty|compare`
- **Acceptance Criteria:** 전 응답 source/provenance, 미검수 제외, 필터 422, 캐시 헤더(ETag/trust_tier 필터).
- **dependencies:** ARCH-STAT-E4-T1, ARCH-VID-E2-T2 · FE: ARCH-UI-E5
- **effort:** M · **priority:** P0

## [ARCH] CUR-E6 · 영상 매칭 큐레이션·검수 도구 (어드민 백엔드)

### [ARCH-CUR-E6-T1] 영상 매칭 검수 큐·컨펌 백엔드
- **설명:** confirm 시만 공개·전 액션 감사·임베드 미리보기·권한 통제.
- **Acceptance Criteria:** confirm 시만 공개, 전 액션 감사, 임베드 미리보기, 권한 통제.
- **dependencies:** ARCH-VID-E2-T1, ARCH-VID-E2-T2
- **effort:** L · **priority:** P0

### [ARCH-CUR-E6-T2] 수상/선곡 추출 검수·곡 정규화 백엔드
- **설명:** 확정 전 비공개·곡 병합/분리·원본 대조·감사.
- **Acceptance Criteria:** 확정 전 비공개, 곡 병합/분리 정확, 원본 대조, 감사.
- **dependencies:** ARCH-E1-T3, ARCH-SONG-E3-T1
- **effort:** L · **priority:** P0

### [ARCH-CUR-E6-T3] 제보 검수·takedown 콘솔 백엔드
- **설명:** takedown 즉시 비공개+SLA 타이머·제보자 알림·차단 재매칭 제외.
- **Acceptance Criteria:** takedown 즉시 비공개+SLA 타이머, 제보자 알림, 차단 재매칭 제외.
- **dependencies:** ARCH-E1-T4, ARCH-VID-E2-T4
- **effort:** M · **priority:** P1

## [ARCH] QA-E7 · 품질검증(evals)·관측·캐시·성능

### [ARCH-QA-E7-T1] 추출·매칭 evals·회귀 게이트
- **설명:** 골든셋 품질 CI 보고·임계 미달 차단·회귀 감지.
- **Acceptance Criteria:** 골든셋 품질 CI 보고, 임계 미달 차단, 회귀 감지.
- **dependencies:** ARCH-E1-T3, ARCH-VID-E2-T1, ARCH-SONG-E3-T1, ARCH-SONG-E3-T2
- **effort:** M · **priority:** P0

### [ARCH-QA-E7-T2] 관측·정합성 모니터·캐시 전략
- **설명:** dead 급증 알람·provenance 누락 차단·캐시 신선도 SLA.
- **Acceptance Criteria:** dead 급증 알람, provenance 누락 차단, 캐시 신선도 SLA.
- **dependencies:** ARCH-VID-E2-T2, ARCH-STAT-E4-T1, ARCH-STAT-E4-T3
- **effort:** M · **priority:** P1

---

# F. NOTI 백엔드 (알림 코어·웹푸시·이메일·설정·날씨·스케줄러)

## [NOTI] EPIC-NOTI-01 · 알림 도메인 기반 (코어)

### [NOTI-T-01-01] 알림 데이터모델 스키마(PG)
- **설명:** up/down 무손실·dedupe_key unique·상태 전이 제약·PIPA 암호화/보존.
- **Subtasks:** notification_preferences·push_subscriptions·notification_outbox·notification_log·event_source, 인덱스, PII 분리
- **Acceptance Criteria:** up/down 무손실, dedupe_key unique 중복 차단, 상태 전이 제약, PIPA 암호화/보존.
- **dependencies:** NOTI-T-00-04
- **effort:** M · **priority:** P0

### [NOTI-T-01-02] 채널 추상화 Notifier+발송 코어(dispatch)
- **설명:** 유형/채널 Notifier 매핑·off/unsubscribe/quiet hours skip·dedupe 멱등·긴급 quiet 예외.
- **Subtasks:** Notifier 인터페이스, WebPush/Email/Alimtalk(스텁), 유형별 채널 우선순위, 설정/quiet hours 검사(KST 21~08), dedupe_key
- **Acceptance Criteria:** 유형/채널 올바른 Notifier, off/unsubscribe/quiet hours skip 또는 재스케줄+사유, dedupe 멱등, 긴급 quiet 예외.
- **dependencies:** NOTI-T-01-01
- **effort:** L · **priority:** P0

### [NOTI-T-01-03] 멱등·중복방지·재시도·레이트리밋·DLQ
- **설명:** 동시 워커 1회·일시 재시도/영구 즉시 실패·최대 초과 DLQ+알림·사용자 시간당 상한.
- **Subtasks:** Redis 락, 재시도(지수+지터), DLQ, 채널/사용자 레이트리밋, 중복방지 윈도우, 429 Retry-After
- **Acceptance Criteria:** 동시 워커 1회, 일시 재시도/영구 즉시 실패, 최대 초과 DLQ+알림, 사용자 시간당 상한.
- **dependencies:** NOTI-T-01-02
- **effort:** L · **priority:** P0

### [NOTI-T-01-04] 템플릿 엔진+i18n(ko/en/ja/zh)+채널별 렌더
- **설명:** 3채널 형식 무깨짐·4언어+ko 폴백·이메일 원클릭 수신거부+사업자정보·딥링크.
- **Subtasks:** 유형별 템플릿(D-day/신규/마감/찜변경/날씨/결제), 채널별 렌더(웹푸시/이메일/알림톡 변수), 딥링크+UTM, 법적 푸터, XSS 안전
- **Acceptance Criteria:** 3채널 형식 무깨짐, 4언어+ko 폴백, 이메일 원클릭 수신거부+사업자정보, 딥링크 정확.
- **dependencies:** NOTI-T-01-02
- **effort:** L · **priority:** P0

## [NOTI] EPIC-NOTI-02 · 웹푸시(FCM)

### [NOTI-T-02-02] 백엔드 푸시 구독 토큰 등록/갱신/해지
- **설명:** 동일 토큰 upsert·UNREGISTERED 제외·소유권 검증·기기 목록.
- **Acceptance Criteria:** 동일 토큰 upsert, UNREGISTERED 제외, 소유권 검증, 기기 목록.
- **dependencies:** NOTI-T-01-01 · FE: NOTI-T-02-01(SW UX)
- **effort:** M · **priority:** P0

### [NOTI-T-02-03] WebPushNotifier(FCM HTTP v1)
- **설명:** 단일/다기기 발송+message_id·무효 토큰 revoke·일시 재시도/영구 failed·딥링크.
- **Subtasks:** google-auth 토큰 캐싱, fcm_options.link, fan-out, TTL/urgent
- **Acceptance Criteria:** 단일/다기기 발송+message_id, 무효 토큰 revoke, 일시 재시도/영구 failed, 딥링크.
- **dependencies:** NOTI-T-01-02, NOTI-T-02-02
- **effort:** M · **priority:** P0

## [NOTI] EPIC-NOTI-03 · 이메일 채널

### [NOTI-T-03-01] 이메일 발송 인프라+도메인 인증
- **설명:** SPF/DKIM 패스·네이버/다음/지메일 도달·List-Unsubscribe·바운스 억제.
- **Subtasks:** SES(서울)/Outbound Mailer 추상화, 멀티파트, 바운스/컴플레인 웹훅
- **Acceptance Criteria:** SPF/DKIM 패스, 네이버/다음/지메일 도달, List-Unsubscribe, 바운스 억제.
- **dependencies:** NOTI-T-01-02
- **effort:** M · **priority:** P0

### [NOTI-T-03-02] 원클릭 수신거부+정보통신망법/PIPA
- **설명:** 로그인 없이 즉시 반영·광고성 야간 미발송/동의자만·거래성 무관 발송·재발송 안됨.
- **Subtasks:** unsubscribe_token 랜딩, 유형별 거부, 정보성/광고성 분류, (광고) 표기
- **Acceptance Criteria:** 로그인 없이 즉시 반영, 광고성 야간 미발송/동의자만, 거래성 무관 발송, 재발송 안됨.
- **dependencies:** NOTI-T-03-01, NOTI-T-04-01
- **effort:** M · **priority:** P0

## [NOTI] EPIC-NOTI-04 · 알림 설정 백엔드

### [NOTI-T-04-01] 알림 설정 백엔드 API
- **설명:** 부분 업데이트·전체 거부 시 정보성(결제) 제외·iOS 채널 가용성 반영.
- **Acceptance Criteria:** 부분 업데이트, 전체 거부 시 정보성(결제) 제외, iOS 채널 가용성 반영.
- **dependencies:** NOTI-T-01-01 · FE: NOTI-T-04-02
- **effort:** M · **priority:** P0

### [NOTI-T-04-03] 알림센터(인앱 목록) API
- **설명:** 시간 역순+읽음·딥링크·미읽음 동기화.
- **Acceptance Criteria:** 시간 역순+읽음, 딥링크, 미읽음 동기화.
- **dependencies:** NOTI-T-01-01
- **effort:** M · **priority:** P1

## [NOTI] EPIC-NOTI-05 · 기상청 날씨 연동

### [NOTI-T-05-01] 좌표→격자/지역코드 매핑+API 클라이언트
- **설명:** 격자/중기코드 정확·단/중/특보 정규화·쿼터/에러 graceful·baseDate/baseTime.
- **Subtasks:** LCC DFS 변환, 중기 regId 매핑, 단기/초단기/중기육상/중기기온/특보 클라이언트, PTY/POP/TMP/SKY 정규화, 캐싱
- **Acceptance Criteria:** 격자/중기코드 정확, 단/중/특보 정규화, 쿼터/에러 graceful, baseDate/baseTime 정확.
- **dependencies:** NOTI-T-00-02
- **effort:** L · **priority:** P0

### [NOTI-T-05-02] 날씨 스냅샷 저장+갱신 윈도우(D-14부터)
- **설명:** D-14 점진+D-3 빈도 상향·중기 미가용 준비중·특보 매칭·출처/base_time 보존.
- **Subtasks:** weather_snapshot·weather_alert, 갱신 윈도우 로직, 이력 보존, 다회차, 출처 보존(법무)
- **Acceptance Criteria:** D-14 점진+D-3 빈도 상향, 중기 미가용 준비중, 특보 매칭, 출처/base_time 보존.
- **dependencies:** NOTI-T-05-01
- **effort:** L · **priority:** P0

### [NOTI-T-05-03] 날씨 조회 API+상세 위젯 (백엔드)
- **설명:** 행사일 날씨/특보/출처/갱신시각·준비중/없음/특보 구분.
- **Endpoints:** `GET /contests/{id}/weather`
- **Acceptance Criteria:** 행사일 날씨/특보/출처/갱신시각, 준비중/없음/특보 구분, 4언어.
- **dependencies:** NOTI-T-05-02 · FE: DETAIL-T-07-01
- **effort:** M · **priority:** P0

## [NOTI] EPIC-NOTI-06 · 스케줄러·트리거·날씨 갱신

### [NOTI-T-06-01] arq cron 스케줄러+잡 오케스트레이션
- **설명:** KST 정시 실행·중복 멱등·실패 재실행/가시성·다운 후 과발송 없이 복구.
- **Subtasks:** cron(D-day/마감임박/날씨/특보 폴링), 멱등(분산락), KST cron, 백필 안전
- **Acceptance Criteria:** KST 정시 실행, 중복 멱등, 실패 재실행/가시성, 다운 후 과발송 없이 복구.
- **dependencies:** NOTI-T-01-03
- **effort:** L · **priority:** P0

### [NOTI-T-06-02] D-day 알림 트리거(찜 마감·행사일)
- **설명:** D-7/3/1/0 정확 1회·날짜 변경 재계산·quiet/설정 반영·중복 없음.
- **Acceptance Criteria:** D-7/3/1/0 정확 1회, 날짜 변경 재계산, quiet/설정 반영, 중복 없음.
- **dependencies:** NOTI-T-06-01, NOTI-T-01-02
- **effort:** M · **priority:** P0

### [NOTI-T-06-03] 마감임박·신규공고·찜대회 변경 트리거
- **설명:** 신규 관심조건 매칭만·변경 diff 포함·마감임박 1회·다이제스트 묶음.
- **Subtasks:** 수집 이벤트 구독, 변경 diff(LLM 요약), 마감임박 일괄, 다이제스트, 멱등
- **Acceptance Criteria:** 신규 관심조건 매칭만, 변경 diff 포함, 마감임박 1회, 다이제스트 묶음.
- **dependencies:** NOTI-T-06-01, NOTI-T-01-02
- **effort:** L · **priority:** P1

### [NOTI-T-06-04] 날씨 갱신 잡+날씨경보 트리거
- **설명:** 임박 행사 악천후/특보 1회·악화 시 업데이트·특보 quiet 예외·중복 없음.
- **Acceptance Criteria:** 임박 행사 악천후/특보 1회, 악화 시 업데이트, 특보 quiet 예외, 중복 없음.
- **dependencies:** NOTI-T-05-02, NOTI-T-06-01, NOTI-T-01-02
- **effort:** L · **priority:** P1

---

# G. COM 백엔드측 (법무 enforcement·보안·QA·관측)

### [COM-LEGAL-01-BE] 저작권/출처표기/임베드 정책 enforcement+takedown (백엔드)
- **설명:** SourceAttribution 의무화+결측 시 게시 차단·1차 출처 화이트리스트·공공누리 라이선스·robots 준수.
- **Acceptance Criteria:** 출처표기+원문링크, 공식 임베드만(재호스팅 0), takedown SLA, 출처 결측 시 게시 차단.
- **dependencies:** DATA-T-SCHEMA-03, INGEST-E7-T2
- **effort:** L · **priority:** P0

### [COM-LEGAL-02-BE] PIPA(처리방침/동의관리) 백엔드
- **설명:** 약관/방침/마케팅/푸시 동의 버전·위탁(NCP/AWS/LiteLLM Gemini)·만14세·열람/삭제 창구.
- **Acceptance Criteria:** 필수/선택 분리+이력, 위탁/국외이전 고지, 탈퇴 파기/보존 분리.
- **dependencies:** AUTH-E1-T1
- **effort:** L · **priority:** P0

### [COM-SEC-01-BE] CSP/보안헤더+안전 리다이렉트/임베드 (서버측)
- **설명:** unsafe-eval 없는 CSP·securityheaders A·XSS/오픈리다이렉트 0.
- **Acceptance Criteria:** CSP 적용, securityheaders A, 오픈리다이렉트 0.
- **dependencies:** BE-E11-T02
- **effort:** L · **priority:** P0

### [COM-SEC-02-BE] 인증/세션/토큰+CSRF+소셜·결제 콜백 (서버측)
- **설명:** httpOnly 토큰·콜백 위변조/CSRF 방어·권한 가드 정합.
- **Acceptance Criteria:** httpOnly 토큰, 콜백 위변조/CSRF 방어, 권한 가드 정합.
- **dependencies:** COM-SEC-01-BE, AUTH-E1-T2
- **effort:** L · **priority:** P0

### [COM-SEC-03-BE] Secrets/암호화+의존성·시크릿 스캐닝+회귀
- **설명:** 번들/이미지 secret 미포함·critical 0/시크릿 커밋 0·PII 마스킹.
- **Acceptance Criteria:** secret 미포함, critical 0/시크릿 커밋 0, PII 마스킹.
- **dependencies:** BE-E11-T03
- **effort:** M · **priority:** P0

### [COM-QA-01-BE] 단위/통합 테스트+커버리지 게이트 (백엔드)
- **설명:** 핵심 커버리지·API 회귀 감지·flaky 0 목표.
- **Acceptance Criteria:** 핵심 커버리지 충족, API 회귀 감지, flaky 0 목표.
- **dependencies:** BE-E11-T03
- **effort:** L · **priority:** P0

### [COM-QA-03-BE] 크롤러/수집 회귀+커버리지 수치화(PoC)+evals
- **설명:** 커버리지 리포트·추출/정규화 회귀 차단·출처 결측 게이트 실패. (INGEST/DATA와 공동 소유)
- **Acceptance Criteria:** 커버리지 리포트, 추출/정규화 회귀 차단, 출처 결측 시 게이트 실패.
- **dependencies:** COM-LEGAL-01-BE, DATA-T-EXT-06, ARCH-QA-E7-T1
- **effort:** XL · **priority:** P0

### [COM-QA-04-BE] CI/CD+품질 게이트+환경/릴리스 (백엔드)
- **설명:** 게이트 통과 머지·프리뷰 배포·롤백/캐시 무효화 안전.
- **Acceptance Criteria:** 게이트 통과 시 머지, 프리뷰 배포, 롤백/캐시 무효화 안전.
- **dependencies:** COM-QA-01-BE, COM-SEC-03-BE, BE-E11-T03/T04
- **effort:** L · **priority:** P0

### [COM-OBS-BE] 서버 관측성(로그·메트릭·알럿·RUM 서버 연계)
- **설명:** 구조화 로그·도메인 메트릭·알럿 임계·결제/수집/알림 핵심 지표.
- **Acceptance Criteria:** 구조화 로그 상관, 도메인 메트릭 노출, 임계 알럿, 핵심 퍼널 서버 진실.
- **dependencies:** BE-E01-T06, INGEST-E1-T5
- **effort:** M · **priority:** P1

---

# H. GROUNZ 벤치마크 반영 (BE 신규 Task)

> GROUNZ가 검증한 패턴을 채택. **법적 주의: GROUNZ DB 스크래핑 금지(경쟁사 가공DB·잡코리아v사람인 리스크) — 데이터는 1차 출처(공공 API·지자체)만.** (INGEST-E7-T1 가드로 강제)

### [GRNZ-BE-01] 게시요청(크라우드 제보) 인입·검수 백엔드 (GROUNZ 벤치)
- **설명:** 사용자 가요제 게시요청 폼 인입 + 검수 워크플로 + 1차 출처 보강 후 published.
- **Subtasks:** 게시요청 API, 스팸/캡차, pending→검수→1차 출처 cross-check→published, 제보자 알림, 중복 병합 연계
- **Acceptance Criteria:** 검수 전 비공개, 1차 출처 확인 후만 게시, 스팸 차단, 중복 병합.
- **dependencies:** BE-E04-T02, DATA-T-DEDUP-01, INGEST-E7-T1
- **effort:** M · **priority:** P1

### [GRNZ-BE-02] 조회수 집계·인기 랭킹(Top10) 백엔드 (GROUNZ 벤치)
- **설명:** 조회/찜/클릭 집계 → 기간별 Top10 랭킹 + 어뷰징 보정.
- **Subtasks:** Redis ZSET 랭킹, 기간 윈도우(일/주/월), 봇/어뷰징 보정, 인기 섹션 API
- **Acceptance Criteria:** 집계 랭킹 반환, 기간 토글, 어뷰징 보정, 캐시.
- **dependencies:** DETAIL-T-01-03, NAV-T-NAV-0312
- **effort:** M · **priority:** P1

### [GRNZ-BE-03] 카테고리/대상/지역/분야/상금구간 택소노미 모델+필터 인덱스 (GROUNZ 벤치)
- **설명:** GROUNZ식 다차원 택소노미를 데이터 모델화 + 필터 인덱스 + 상금 구간 버킷팅.
- **Subtasks:** taxonomy 마스터(카테고리/대상/지역/분야/상금구간), 매핑, 복합/부분 인덱스, facets 연동
- **Acceptance Criteria:** 다차원 필터 정확, 상금 구간 버킷 일관, facets 일치, 인덱스 적용.
- **dependencies:** DATA-T-SCHEMA-04, LIST-E5-T1
- **effort:** L · **priority:** P1

### [GRNZ-BE-04] 아티클(콘텐츠) CMS 백엔드 (GROUNZ 벤치)
- **설명:** 가요제 가이드/뉴스 아티클 CMS(작성/발행/SEO 메타/카테고리).
- **Subtasks:** article 모델, 발행 워크플로, SEO 메타/JSON-LD 데이터, 카테고리/태그, 공개 API
- **Acceptance Criteria:** 작성/발행/예약, SEO 메타 제공, 카테고리/태그 필터, 공개 API.
- **dependencies:** BE-E10-T01, BE-E07-T02
- **effort:** L · **priority:** P2

### [GRNZ-BE-05] 세그먼트 태깅(트로트/지역가요제/노래자랑/실버) 백엔드 (GROUNZ 벤치 — 차별화)
- **설명:** GROUNZ가 비운 데모(실버·트로트·지역가요제·노래자랑) 세그먼트 자동 태깅 + 필터.
- **Subtasks:** segment 마스터, LLM/룰 분류 → segment 태그, 신뢰도, 세그먼트 필터/추천 연동
- **Acceptance Criteria:** 세그먼트 자동 태깅+신뢰도, 필터/추천 반영, 미확신 검수큐.
- **dependencies:** DATA-T-DEDUP-03, DATA-T-EXT-05
- **effort:** M · **priority:** P1

### [GRNZ-BE-06] 음역대/장르/지역 개인화 추천 백엔드 (GROUNZ 벤치 — 차별화)
- **설명:** 온보딩 vocalProfile/장르/지역 + 행동 시드 기반 개인화 추천(Neo4j 관계/유사).
- **Subtasks:** 추천 피처(온보딩 시드+행동+위치), Neo4j 유사도, 추천 사유 태그, 게스트 폴백(인기/신규), 캐시 무효화
- **Acceptance Criteria:** 카드별 추천 사유, 게스트 폴백, 관심없음 반영, 1차 출처 표기.
- **Edge cases:** 추천 엔진 규칙 vs 그래프/임베딩 Phase1 범위(Open Q11).
- **dependencies:** NAV-T-NAV-0312, DATA-T-SCHEMA-06 · FE: NAV-T-NAV-0303
- **effort:** L · **priority:** P1

### [GRNZ-BE-07] 선정곡/수상곡 분석 집계 백엔드 (GROUNZ 벤치 — 차별화)
- **설명:** 선정곡/수상곡 경향(장르/난이도/아티스트/연도) 집계 — ARCH-STAT과 정합 확장.
- **Acceptance Criteria:** 차원별 집계, 표본 미달 표시, source/provenance, 캐시.
- **dependencies:** ARCH-STAT-E4-T1, ARCH-SONG-E3-T1
- **effort:** M · **priority:** P1

### [GRNZ-BE-08] 신청서 작성 도움 LLM 백엔드 강화 (GROUNZ 벤치 — 차별화)
- **설명:** GROUNZ에 없는 LLM 신청서 작성 도움 — APP-EP02와 정합, 차별화 포인트로 강조.
- **Acceptance Criteria:** 맥락 예시/다듬기, PII 마스킹, evals 회귀 차단, 쿼터.
- **dependencies:** APP-T-02-02
- **effort:** M · **priority:** P1

### [GRNZ-BE-09] 커뮤니티/Q&A/익명 백엔드 (GROUNZ 벤치 — 후순위)
- **설명:** 가요제 Q&A·익명 글 백엔드(모더레이션 포함). 후순위(P2~P3).
- **Subtasks:** post/comment 모델, 익명 처리, LLM 모더레이션, 신고/차단
- **Acceptance Criteria:** 작성/조회, 익명 처리, 모더레이션, 신고 처리.
- **dependencies:** BE-E02-T03
- **effort:** L · **priority:** P2

### [GRNZ-BE-10] 인기 검색어/실시간 트렌드 집계 백엔드 (GROUNZ 벤치)
- **설명:** 검색어 집계 → 인기 검색어/트렌드 + 비속어 필터.
- **Acceptance Criteria:** 인기 검색어 집계, 비속어 필터, 기간 윈도우, 캐시.
- **dependencies:** LIST-E5-T2
- **effort:** S · **priority:** P2
