# NORI 백엔드 작업 리스트 — PHASE 2 (거래: 제출 대행·접수 SaaS·결제·정산)

> 코드명 NORI · 폴더 `singa-server` · 레포 `gayoje-server`
> Phase 2 목표: **★음원/악보 제출 대행, 주최측 접수 SaaS, 구독/결제(토스/아임포트/카카오·네이버페이), 정산/세금, 카카오 알림톡, 그로스.**
> 결제·정산은 전자금융거래법·통신판매업·PIPA 컴플라이언스를 전 Task에 횡단 적용한다.

## Phase 2 에픽 인덱스

| Epic | 영역 | 제목 | Tasks |
|---|---|---|---:|
| PAY-E1 | PAY | 요금제·플랜·정책 엔진 | 4 |
| PAY-E2 | PAY | 게이팅 & Quota Enforcement(BE 부분) | 2 |
| PAY-E3 | PAY | 결제 통합(토스/아임포트·카카오/네이버페이) | 6 |
| PAY-E4 | PAY | 구독 라이프사이클+빌링키 정기결제 | 6 |
| PAY-E5 | PAY | 영수증·세금계산서+웹훅·정합성·내역(BE 부분) | 5 |
| PAY-E6 | PAY | 쿠폰·프로모션+가격 A/B+결제 분석 | 4 |
| PAY-E7 | PAY | 법무·컴플라이언스·보안(전자금융·통신판매·PIPA) | 2 |
| BE-EP-E06 | BE | 구독·결제 API(PAY 정합, BE 소유) | 4 |
| BE-EP-E08 | BE | ★제출 대행+주최 접수 SaaS API | 4 |
| APP-EP04 | APP | 제출 대행 플로우(백엔드) | 5 |
| APP-EP05 | APP | 마감 리마인더·영수증/확인 알림(백엔드) | 2 |
| BIZ-E1 | BIZ | ★제출 대행 워크플로(운영/전달 엔진) | 4 |
| BIZ-E2 | BIZ | 주최측 B2B 접수 SaaS | 8 |
| BIZ-E4 | BIZ | 결제·정산·회계 인프라(공통) | 4 |
| BIZ-E5 | BIZ | 비즈니스 운영(CS·SLA·분쟁·정책) | 3 |
| BIZ-E6 | BIZ | 그로스·리텐션·바이럴 | 4 |
| NOTI-07 | NOTI | 카카오 알림톡 채널 | 3 |
| **합계** | | **17 Epic** | **66 Task** |

---

# A. PAY — 결제·구독·정책 엔진

## [PAY] E1 · 요금제·플랜·정책 엔진

### [PAY-E1-T1] 등급/플랜 상수·도메인 모델
- **설명:** subscription 상수(FREE/PREMIUM/HOST_*)·주기/통화·분류 헬퍼·User/Org 필드.
- **Acceptance Criteria:** worker 컨텍스트 import 성공, is_paid/is_host 단위테스트, FE TIER_META fallback.
- **dependencies:** 없음
- **effort:** M · **priority:** P0

### [PAY-E1-T2] 동적 가격(PricingConfig) 저장·공개 API
- **설명:** PricingConfig·`GET /pricing`·`PUT /admin/pricing`·final_price(VAT 포함)·100원 단위.
- **Acceptance Criteria:** admin 수정 후 즉시 갱신, 실패 시 fallback, 100원 단위.
- **Endpoints:** `GET /pricing` · `PUT /admin/pricing`
- **Edge cases:** PG 1차 선택(토스 단독 vs 아임포트 멀티 — Open Q8).
- **dependencies:** PAY-E1-T1
- **effort:** M · **priority:** P0

### [PAY-E1-T3] 동적 한도(QuotaConfig)·가요제 도메인 quota
- **설명:** LIMIT_TYPE(찜/D-day알림/채널/선곡분석/제출대행/주최 이벤트·인원)·등급별 dict·QuotaExceeded·월간 atomic reset.
- **Acceptance Criteria:** 등급 단조 증가, 월간 atomic reset, QuotaExceeded 키 충족.
- **Endpoints:** `GET /quota-config`
- **dependencies:** PAY-E1-T1
- **effort:** L · **priority:** P0

### [PAY-E1-T4] 플랜 비교/perks 동적 생성+VAT 분리
- **설명:** 한도 변경 perks 무코드 갱신·VAT 합 일치·무료 perks 정확.
- **Acceptance Criteria:** 한도 변경 시 perks 무코드 갱신, VAT 합 일치, 무료 perks 정확.
- **dependencies:** PAY-E1-T2, PAY-E1-T3
- **effort:** S · **priority:** P1

## [PAY] E2 · 무료↔유료 게이팅 & Quota Enforcement (BE 부분)

### [PAY-E2-T1] BE 게이팅 미들웨어+402 QUOTA_EXCEEDED
- **설명:** require_quota/increment_usage/require_paid/require_host·성공 후만 증가·유료 전용 403/402+upgrade_url.
- **Subtasks:** 게이트 대상 엔드포인트 적용, gate_hit 메트릭, locale 402
- **Acceptance Criteria:** 402+code 정확, 성공 후만 증가, 유료 전용 403/402+upgrade_url.
- **Edge cases:** 부분 성공 시 증가 방지, 동시 요청 한도 경합.
- **dependencies:** PAY-E1-T3 · FE: PAY-E2-T2/T3/T4
- **effort:** L · **priority:** P0

### [PAY-E2-T4-BE] 미리보기 게이팅(선곡분석 BE 미전송)
- **설명:** 무료 티저 시 민감 데이터 BE 미전송(블러는 FE, 데이터 차단은 BE).
- **Acceptance Criteria:** 무료 등급에 전체 데이터 미전송, 권한 시 투명 통과.
- **dependencies:** PAY-E2-T1, PAY-E1-T1 · FE: PAY-E2-T4
- **effort:** M · **priority:** P1

## [PAY] E3 · 결제 통합 (토스/아임포트·카카오/네이버페이)

### [PAY-E3-T1] PG 추상화+토스/아임포트 클라이언트
- **설명:** PGProvider(빌링키/승인/단건/취소/조회)·토스 confirm·빌링키·아임포트 폴백·금액 검증 가드.
- **Acceptance Criteria:** 금액 불일치 즉시 취소, confirm 실패 미전이, provider 플래그 폴백.
- **Edge cases:** PG 장애 폴백, 빌링키 지원 수단 범위(Open Q9).
- **dependencies:** PAY-E1-T2
- **effort:** XL · **priority:** P0

### [PAY-E3-T2] 주문(Order)·결제(Payment) 모델+멱등 생성
- **설명:** Order(kind: subscription/proxy_submission/host_plan)·Payment·Refund·상태 전이 가드·VAT 스냅샷.
- **Acceptance Criteria:** 동일 order_id 멱등, 서버 금액 산정, 불가 전이 거부.
- **dependencies:** PAY-E3-T1, PAY-E1-T2
- **effort:** L · **priority:** P0

### [PAY-E3-T4] 결제 승인 확정(confirm)·검증+후처리 분기
- **설명:** 멱등 1회 부여·불일치 취소·kind 분기(구독 활성/대행 착수/host 플랜).
- **Endpoints:** `POST /payments/confirm`
- **Acceptance Criteria:** 멱등 1회 부여, 불일치 취소, kind 분기, 실패 취소.
- **dependencies:** PAY-E3-T1, PAY-E3-T2
- **effort:** L · **priority:** P0

### [PAY-E3-T5] 단건 결제 ★음원/악보 제출 대행
- **설명:** 견적=결제 일치·결제 후만 대행 in_progress·무료/유료 분기.
- **Endpoints:** `POST /proxy/quote` · 주문 kind=proxy_submission
- **Acceptance Criteria:** 견적=결제 일치, 결제 후만 대행 in_progress, 무료 quota 분기.
- **dependencies:** PAY-E3-T2, PAY-E3-T4, PAY-E2-T1
- **effort:** L · **priority:** P0

### [PAY-E3-T6] 주최 SaaS 플랜 결제(B2B host_plan)
- **설명:** host quota 부여·사업자 검증 후 세금계산서·participant/host 독립.
- **Acceptance Criteria:** host quota 부여, 사업자 검증 후 세금계산서, participant/host 독립.
- **dependencies:** PAY-E3-T2, PAY-E3-T4, PAY-E1-T3
- **effort:** L · **priority:** P1

> 참고: PAY-E3-T3(FE 체크아웃 화면)는 FE 스코프 — 토스 SDK 결제위젯은 클라이언트.

## [PAY] E4 · 구독 라이프사이클 + 빌링키 정기결제

### [PAY-E4-T1] 구독 상태머신+current_period
- **설명:** 활성 즉시 유료 quota/만료 즉시 free·해지예약 기간 유지·period 일자 정확.
- **Acceptance Criteria:** 활성 즉시 유료 quota/만료 즉시 free, 해지예약 기간 유지, period 일자 정확.
- **dependencies:** PAY-E1-T1, PAY-E3-T4
- **effort:** L · **priority:** P0

### [PAY-E4-T2] 무료체험(카드/무카드)+남용 방지
- **설명:** 1회 체험·카드형 자동전환·무카드 만료·종료 사전 알림.
- **Acceptance Criteria:** 1회 체험, 카드형 자동전환, 무카드 만료, 종료 사전 알림.
- **dependencies:** PAY-E4-T1, PAY-E3-T1
- **effort:** M · **priority:** P1

### [PAY-E4-T3] 자동갱신 정기결제 스케줄러+멱등 청구
- **설명:** arq due 조회·빌링키 자동결제·멱등키·master_lock 중복 방지·결과 알림.
- **Acceptance Criteria:** 중복 청구 없음, 갱신 period 이동, 실패 시 past_due+dunning.
- **Edge cases:** 빌링키 미지원 수단(카드만 자동갱신? — Open Q9).
- **dependencies:** PAY-E4-T1, PAY-E3-T1, PAY-E3-T4
- **effort:** XL · **priority:** P0

### [PAY-E4-T4] 업/다운그레이드(proration)+해지/재구독
- **설명:** 업그레이드 차액·다운그레이드 다음 주기·해지 기간 유지.
- **Acceptance Criteria:** 업그레이드 차액 정확, 다운그레이드 다음 주기, 해지 기간 유지.
- **dependencies:** PAY-E4-T1, PAY-E3-T4
- **effort:** L · **priority:** P0

### [PAY-E4-T5] 연체(dunning)·재시도·유예·복구
- **설명:** D0→D+1/3/5 재시도·유예 제한·recover/expire·단계별 알림·admin dunning.
- **Acceptance Criteria:** 재시도 스케줄, 유예 제한, 복구 즉시 회복.
- **dependencies:** PAY-E4-T3, PAY-E4-T1
- **effort:** L · **priority:** P0

### [PAY-E4-T6] 환불 정책·처리(단건/구독 부분환불)+청약철회
- **설명:** 부분환불 원금 미초과·단계별 가능액·구독 환불 즉시 회수.
- **Acceptance Criteria:** 부분환불 원금 미초과, 단계별 가능액, 구독 환불 즉시 회수.
- **Edge cases:** 무료 재제출/귀책별 환불 차등(Open Q21).
- **dependencies:** PAY-E3-T1, PAY-E3-T4, PAY-E4-T1
- **effort:** L · **priority:** P0

## [PAY] E5 · 영수증·현금영수증·세금계산서+웹훅·정합성·내역

### [PAY-E5-T1] PG 웹훅 수신·서명검증·멱등
- **설명:** 서명 실패 거부·중복 1회·confirm 누락 보강.
- **Endpoints:** `POST /webhooks/pg`
- **Acceptance Criteria:** 서명 실패 거부, 중복 1회, confirm 누락 보강.
- **Edge cases:** 웹훅 재전송 폭주, 순서 역전.
- **dependencies:** PAY-E3-T4
- **effort:** L · **priority:** P0

### [PAY-E5-T2] 영수증·현금영수증(국내 연동)
- **설명:** 발급 전달+상태·환불 시 취소·공급가/부가세 분리.
- **Acceptance Criteria:** 발급 전달+상태, 환불 시 취소, 공급가/부가세 분리.
- **dependencies:** PAY-E3-T4, PAY-E5-T1
- **effort:** M · **priority:** P1

### [PAY-E5-T3] 세금계산서(팝빌/홈택스)
- **설명:** 사업자 검증 후만·금액 일치·국세청 전송 추적.
- **Acceptance Criteria:** 사업자 검증 후만, 금액 일치, 국세청 전송 추적.
- **dependencies:** PAY-E3-T6, PAY-E5-T1
- **effort:** L · **priority:** P2

### [PAY-E5-T4] 결제·정합성 리컨실+분쟁/오류
- **설명:** 불일치 자동 탐지·미반영 정합화·이중청구 환불큐.
- **Acceptance Criteria:** 불일치 자동 탐지, 미반영 정합화, 이중청구 환불큐.
- **dependencies:** PAY-E5-T1, PAY-E3-T4
- **effort:** L · **priority:** P1

### [PAY-E5-T5] 결제·구독 내역 API(마이페이지 Billing)
- **설명:** 최신순 금액/상태·항목 액션·결제수단 즉시 반영.
- **Acceptance Criteria:** 최신순 금액/상태, 항목 액션, 결제수단 즉시 반영.
- **dependencies:** PAY-E3-T4, PAY-E4-T1, PAY-E5-T2 · FE: PAY-E5-T5(화면)
- **effort:** M · **priority:** P1

## [PAY] E6 · 쿠폰·프로모션+가격 A/B+결제 분석

### [PAY-E6-T1] 쿠폰·프로모션 엔진
- **설명:** 만료/한도/비대상 거부·할인 정확·동시 소진 미초과.
- **Acceptance Criteria:** 만료/한도/비대상 거부, 할인 정확 반영, 동시 소진 미초과.
- **dependencies:** PAY-E3-T2
- **effort:** L · **priority:** P1

### [PAY-E6-T2] 리퍼럴/추천 프로모션
- **설명:** 양측 1회 보상·어뷰징 차단.
- **Acceptance Criteria:** 양측 1회 보상, 어뷰징 차단.
- **dependencies:** PAY-E6-T1
- **effort:** M · **priority:** P2

### [PAY-E6-T3] 가격 A/B 테스트 프레임워크
- **설명:** 동일 사용자 동일 variant·노출/전환 집계·승자 롤아웃.
- **Acceptance Criteria:** 동일 사용자 동일 variant, 노출/전환 집계, 승자 롤아웃.
- **dependencies:** PAY-E1-T2, PAY-E3-T3(FE)
- **effort:** L · **priority:** P2

### [PAY-E6-T4] 결제 퍼널 분석·관측·알림
- **설명:** 지표 집계·실패율 급증 알림·단계 이탈 추적.
- **Acceptance Criteria:** 지표 집계, 실패율 급증 알림, 단계 이탈 추적.
- **dependencies:** PAY-E3-T3, PAY-E4-T3, PAY-E5-T1
- **effort:** M · **priority:** P1

## [PAY] E7 · 법무·컴플라이언스·보안 (전자금융·통신판매·PIPA)

### [PAY-E7-T1] 결제 약관·전자상거래 고지·동의 관리
- **설명:** 미동의 결제 차단·동의 이력(버전/시각/IP)·통신판매업 법정 위치 고지.
- **Acceptance Criteria:** 미동의 결제 차단, 동의 이력(버전/시각/IP), 통신판매업 법정 위치 고지.
- **dependencies:** PAY-E3-T2
- **effort:** M · **priority:** P0

### [PAY-E7-T2] 결제정보 보안·토큰화·PIPA
- **설명:** 카드 원번호 미저장·빌링키 암호화·admin 감사·IDOR 차단.
- **Acceptance Criteria:** 카드 원번호 미저장, 빌링키 암호화, admin 감사, IDOR 차단.
- **dependencies:** PAY-E3-T1, PAY-E3-T2
- **effort:** M · **priority:** P0

---

# B. BE — 구독·결제 API + ★제출 대행/접수 SaaS API

## [BE] EP-E06 · 구독·결제 API (PAY와 정합, 백엔드 소유)

### [BE-E06-T01] 구독 상품·플랜·엔타이틀먼트
- **설명:** 플랜 노출·구독 상태 게이팅·쿠폰 정확.
- **Acceptance Criteria:** 플랜 노출, 구독 상태 게이팅, 쿠폰 정확.
- **dependencies:** BE-E02-T03 · 정합: PAY-E1
- **effort:** M · **priority:** P1

### [BE-E06-T02] 결제 게이트웨이 연동+웹훅
- **설명:** 서버 금액 검증·웹훅 서명/멱등·민감정보 미저장.
- **Acceptance Criteria:** 서버 금액 검증, 웹훅 서명/멱등, 민감정보 미저장.
- **dependencies:** BE-E06-T01 · 정합: PAY-E3/E5
- **effort:** XL · **priority:** P1

### [BE-E06-T03] 정기결제(빌링키)·갱신·연체
- **설명:** 자동 실행·실패 재시도+알림·해지 예약 반영.
- **Acceptance Criteria:** 자동 실행, 실패 재시도+알림, 해지 예약 반영.
- **dependencies:** BE-E06-T02, BE-E05-T02 · 정합: PAY-E4
- **effort:** L · **priority:** P1

### [BE-E06-T04] 환불·취소·정산·영수증/세금
- **설명:** 환불 승인 후 PG 일치·청약철회 규정·정산 정확.
- **Acceptance Criteria:** 환불 승인 후 PG 일치, 청약철회 규정, 정산 정확.
- **dependencies:** BE-E06-T02, BE-EP-E10
- **effort:** L · **priority:** P1

## [BE] EP-E08 · 제출 대행 + 주최측 접수 SaaS API (★핵심)

### [BE-E08-T01] 제출 대행 주문 라이프사이클 API
- **설명:** 결제 후 처리 큐·단계/증빙 추적·마감 임박 우선.
- **Acceptance Criteria:** 결제 후 처리 큐, 단계/증빙 추적, 마감 임박 우선.
- **dependencies:** BE-E06-T02, BE-E07-T03 · 정합: BIZ-E1, APP-EP04
- **effort:** XL · **priority:** P0

### [BE-E08-T02] 주최 SaaS 공고·접수폼 빌더 API
- **설명:** 공고/폼 생성·검증룰 적용·디렉토리 연계.
- **Acceptance Criteria:** 공고/폼 생성, 검증룰 적용, 디렉토리 연계.
- **dependencies:** BE-E02-T03, BE-E03-T01 · 정합: BIZ-E2
- **effort:** XL · **priority:** P1

### [BE-E08-T03] 주최 SaaS 지원자 접수·관리·심사
- **설명:** 검증 후 저장·심사 상태/점수+알림·접근 주최 권한.
- **Acceptance Criteria:** 검증 후 저장, 심사 상태/점수+알림, 접근 주최 권한.
- **dependencies:** BE-E08-T02, BE-E05-T02
- **effort:** XL · **priority:** P1

### [BE-E08-T04] SaaS 과금·정산·접수비 결제
- **설명:** 접수비 결제+정산 반영·수수료 차감·세금 데이터.
- **Acceptance Criteria:** 접수비 결제+정산 반영, 수수료 차감, 세금 데이터.
- **Edge cases:** 접수비 수납 주체 전자금융업/PG/에스크로 요건(Open Q20).
- **dependencies:** BE-E06-T02, BE-E08-T03
- **effort:** L · **priority:** P2

---

# C. APP — 제출 대행 플로우 (백엔드)

## [APP] EP-APP-04 · 제출 대행 플로우

### [APP-T-04-01] 제출 대행 도메인 모델·상태머신
- **설명:** 명확 상태머신+불법 전이 차단·결제 전/검증실패 전달 불가·전 시도 증빙·재제출 버전 체인·위임/제3자 제공 동의 보존.
- **Subtasks:** submission, 상태머신(created→paid→validating→ready→delivering→delivered→confirmed/rejected/refunded), delivery_attempts, receipt, 재제출 체인, Neo4j, 동의 필드
- **Acceptance Criteria:** 명확 상태머신+불법 전이 차단, 결제 전/검증실패 전달 불가, 전 시도 증빙, 재제출 버전 체인, 동의 보존.
- **Edge cases:** 제출 대행 법적 성격(심부름/대리 vs 위탁 — Open Q18).
- **dependencies:** APP-T-02-01, APP-T-03-01 · 정합: BIZ-E1
- **effort:** L · **priority:** P0

### [APP-T-04-02] 결제·위임 동의 연동(토스/아임포트/카카오·네이버페이)
- **설명:** 4종 결제·위임/제3자/환불 동의 버전·webhook 멱등·정책 환불+영수증/현금영수증.
- **Subtasks:** 수수료 산정, `/checkout`, webhook 검증, 동의 캡처, 환불 정책, 통신판매업 표기
- **Acceptance Criteria:** 4종 결제, 위임/제3자/환불 동의 버전, webhook 멱등, 정책 환불+영수증/현금영수증.
- **dependencies:** APP-T-04-01, 결제 공통(PAY-E3)
- **effort:** L · **priority:** P0

### [APP-T-04-03] 주최측 전달 엔진(이메일/포털/API/수기)+증빙
- **설명:** 채널별 전달+증빙·실패 재시도/폴백/에스컬레이션·마감 후 차단·수기 운영 콘솔·개인정보 최소 전송.
- **Subtasks:** 채널 어댑터, 이메일(메시지ID/바운스), 접수포털(Playwright, 주최 합의 채널만), API, manual(운영 콘솔), 증빙, 재시도/폴백, 마감 가드, 알림
- **Acceptance Criteria:** 채널별 전달+증빙, 실패 재시도/폴백/에스컬레이션, 마감 후 차단, 수기 운영 콘솔, 개인정보 최소 전송.
- **Edge cases:** 외부 접수포털 Playwright 자동화의 합법성(주최 사전합의 채널 한정 vs 전면 수기 — Open Q19).
- **dependencies:** APP-T-04-01, APP-T-04-02, APP-T-03-01 · 정합: BIZ-E1-T3
- **effort:** XL · **priority:** P0

### [APP-T-04-04] 제출 상태추적+영수증/확인증
- **설명:** 타임라인·영수증 PDF·주최 확인 구분·상태 변경 알림.
- **Acceptance Criteria:** 타임라인, 영수증 PDF, 주최 확인 구분, 상태 변경 알림.
- **dependencies:** APP-T-04-01, APP-T-04-03
- **effort:** M · **priority:** P0

### [APP-T-04-05] 재제출(재시도) 플로우
- **설명:** 사유+재제출 경로·변경분만 교체·재제출 수수료·마감 후 차단·최신본 전달.
- **Acceptance Criteria:** 사유+재제출 경로, 변경분만 교체, 재제출 수수료, 마감 후 차단, 최신본 전달.
- **dependencies:** APP-T-04-01, APP-T-04-03, APP-T-04-04
- **effort:** M · **priority:** P1

> 참고: APP-T-04-06(제출 대행 UI 위저드)은 FE 스코프 — BIZ-E1-T4와 정합.

## [APP] EP-APP-05 · 마감 리마인더 & 영수증/확인 알림 (횡단·백엔드)

### [APP-T-05-01] 신청·제출 마감 리마인더 스케줄링
- **설명:** 미완료 D-day 폴백 발송·마감 변경 재계산·시점/채널 조절·야간/중복 제한.
- **Acceptance Criteria:** 미완료 D-day 폴백 발송, 마감 변경 재계산, 시점/채널 조절, 야간/중복 제한.
- **dependencies:** APP-T-01-01, APP-T-04-01, 알림 도메인(NOTI-EPIC01)
- **effort:** M · **priority:** P0

### [APP-T-05-02] 제출 이벤트 알림(전달/확인/실패/환불/영수증)
- **설명:** 상태 전이 알림·실패+재제출 경로·영수증 링크·인앱 동기화.
- **Acceptance Criteria:** 상태 전이 알림, 실패+재제출 경로, 영수증 링크, 인앱 동기화.
- **dependencies:** APP-EP04, APP-T-05-01
- **effort:** S · **priority:** P0

---

# D. BIZ — 제출 대행 워크플로·접수 SaaS·정산/회계·CS

## [BIZ] E1 · 신청·음원/악보 제출 대행 워크플로 (★최우선)
> APP-EP04와 강결합 — BIZ는 운영/전달 엔진·콘솔 중심, APP은 사용자 플로우 중심. 통합 구현.

### [BIZ-E1-T1] 제출 대행 데이터 모델·상태머신(PG+Neo4j)
- **설명:** 상태머신 가드 통과만 커밋+불법 422·공고 스냅샷 동결·idempotency 단일 주문·금액 정수·soft delete 후 정산/감사 조회.
- **Subtasks:** submission_orders·submission_assets·submission_status_events·delivery_channels, 상태머신, 가드, Neo4j 관계, source_snapshot, audit
- **Acceptance Criteria:** 가드 통과만 커밋+불법 422, 공고 스냅샷 동결, idempotency 단일 주문, 금액 정수, soft delete 후 정산/감사 조회.
- **dependencies:** 가요제 정규화, 회원
- **effort:** L · **priority:** P0

### [BIZ-E1-T2] 첨부물 업로드·형식검증 파이프라인
- **설명:** 음원 위반 제출 전 fail+안내·HWP 필수항목 누락 탐지·악성/매크로 격리·4언어 리포트·10MB 30s p95.
- **Subtasks:** presigned, ffprobe(길이/LUFS), 악보(페이지/DPI/OCR), 신청서 LLM 필드 추출, ClamAV, 위변조 차단, 썸네일/파형, 재업로드 버전, evals
- **Acceptance Criteria:** 음원 위반 제출 전 fail+안내, HWP 필수항목 누락 탐지, 악성/매크로 격리, 4언어 리포트, 10MB 30s p95.
- **dependencies:** BIZ-E1-T1
- **effort:** XL · **priority:** P0

### [BIZ-E1-T3] 주최 전달채널 어댑터(이메일/우편/구글폼/eGov/팩스/알림톡)
- **설명:** 자동 채널 무개입+증빙·수동 채널 마감 역산 큐·실패 재시도/에스컬레이션+통지·증빙 영구 보존·반송/구조 변경 감지.
- **Subtasks:** deliver 인터페이스, 이메일(SPF/DKIM·반송), 구글폼(Playwright+스크린샷), eGov(운영자 큐), 우편(등기), 팩스, 알림톡, 재시도/폴백, SLA, 법무 가드
- **Acceptance Criteria:** 자동 채널 무개입+증빙, 수동 채널 마감 역산 큐, 실패 재시도/에스컬레이션+통지, 증빙 영구 보존, 반송/구조 변경 감지.
- **Edge cases:** 외부 접수포털 자동화 합법성(Open Q19).
- **dependencies:** BIZ-E1-T1, BIZ-E1-T2
- **effort:** XL · **priority:** P0

### [BIZ-E1-T5] 운영자 제출 처리 콘솔(Ops Console) 백엔드
- **설명:** 마감 임박 상단+SLA 경고·증빙 없이 SUBMITTED 불가·권한 분리·전 액션 audit.
- **Subtasks:** 큐(SLA 우선), 처리 카드, 수동 전달+증빙, 상태 강제 전환+사유, 에스컬레이션, RBAC, SLA 대시보드, 대량 처리
- **Acceptance Criteria:** 마감 임박 상단+SLA 경고, 증빙 없이 SUBMITTED 불가, 권한 분리, 전 액션 audit.
- **dependencies:** BIZ-E1-T1, BIZ-E1-T3
- **effort:** L · **priority:** P0

> 참고: BIZ-E1-T4(제출 대행 사용자 플로우 화면)는 FE 스코프 — APP-T-04-06과 정합.

## [BIZ] E2 · 주최측 B2B 접수 SaaS

### [BIZ-E2-T1] 주최 온보딩·계정·조직 관리
- **설명:** 미승인 publish 불가·역할 권한 강제·초대 만료/1회용·claim 검증 중복 방지.
- **Subtasks:** 기관 인증(사업자/공문), Organization-멤버 RBAC(owner/admin/judge/viewer), 브랜딩, 공고 claim/신규
- **Acceptance Criteria:** 미승인 publish 불가, 역할 권한 강제, 초대 만료/1회용, claim 검증 중복 방지.
- **dependencies:** 수집 가요제
- **effort:** L · **priority:** P1

### [BIZ-E2-T2] 접수폼 빌더
- **설명:** 코드 없이 음원 제한 폼·PIPA 동의 없이 공개 불가·수정 무결성·미리보기 일치·조건부 로직.
- **Subtasks:** 필드 타입, 음원/악보 스펙, 부문(개인/단체), 검증룰, 조건부, PIPA 동의 분리, 정원/대기열, 버전 관리, 템플릿 갤러리
- **Acceptance Criteria:** 코드 없이 음원 제한 폼, PIPA 동의 없이 공개 불가, 수정 무결성, 미리보기 일치, 조건부 로직.
- **dependencies:** BIZ-E2-T1
- **effort:** XL · **priority:** P1

### [BIZ-E2-T3] 참가자 접수 페이지(공개 렌더러) 백엔드
- **설명:** 모바일 음원 첨부 완료·정원 마감 차단/대기열·유료 결제 후 확정·비회원 인증.
- **Subtasks:** 공개 URL, 폼 렌더+조건부, 사전검증(BIZ-E1-T2), 비회원/회원, 접수비 결제, 완료 확인, 임시저장, 미성년자 동의
- **Acceptance Criteria:** 모바일 음원 첨부 완료, 정원 마감 차단/대기열, 유료 결제 후 확정, 비회원 인증.
- **dependencies:** BIZ-E2-T2, BIZ-E4
- **effort:** L · **priority:** P1

### [BIZ-E2-T4] 지원자 관리
- **설명:** 부문 필터+일괄 합/불·첨부 브라우저 재생·다운로드 audit+권한·보유기간 자동 파기.
- **Acceptance Criteria:** 부문 필터+일괄 합/불, 첨부 브라우저 재생, 다운로드 audit+권한, 보유기간 자동 파기.
- **dependencies:** BIZ-E2-T3
- **effort:** L · **priority:** P1

### [BIZ-E2-T5] 심사 도구
- **설명:** 블라인드 식별 미노출·가중/절사 자동 집계·동점 규칙·결과 잠금.
- **Subtasks:** 루브릭, 심사위원 배정(충돌 회피), 블라인드, 점수 입력+음원, 라운드(예선/본선), 집계/순위, 진행률, 이의/조정, lock
- **Acceptance Criteria:** 블라인드 식별 미노출, 가중/절사 자동 집계, 동점 규칙, 결과 잠금.
- **dependencies:** BIZ-E2-T4
- **effort:** XL · **priority:** P1

### [BIZ-E2-T6] 결과 발표·통지
- **설명:** 예약 자동 공개+통지·노출 정책 마스킹·본인만 상세·오발표 철회/정정. 수상 내역 데이터화→작년 수상곡 연계(ARCH).
- **Acceptance Criteria:** 예약 자동 공개+통지, 노출 정책 마스킹, 본인만 상세, 오발표 철회/정정.
- **dependencies:** BIZ-E2-T5
- **effort:** M · **priority:** P1

### [BIZ-E2-T7] 주최 통계·리포트 대시보드 백엔드
- **설명:** 접수 추이/분포 실시간·유입 전환·PDF/Excel·벤치마크 익명.
- **Acceptance Criteria:** 접수 추이/분포 실시간, 유입 전환, PDF/Excel, 벤치마크 익명.
- **dependencies:** BIZ-E2-T3, BIZ-E2-T4, BIZ-E2-T5
- **effort:** M · **priority:** P2

### [BIZ-E2-T8] 주최 SaaS 요금제·빌링
- **설명:** 건당 미터링 정확·갱신/해지/환불+인보이스·한도 게이팅·세금계산서·공공 후불.
- **Subtasks:** Free/Pay-per-applicant/Pro/Enterprise, 미터링, 정기결제, 세금계산서, 나라장터/입찰 폴백
- **Acceptance Criteria:** 건당 미터링 정확, 갱신/해지/환불+인보이스, 한도 게이팅, 세금계산서, 공공 후불.
- **dependencies:** BIZ-E2-T1, BIZ-E4
- **effort:** L · **priority:** P1

## [BIZ] E4 · 결제·정산·회계 인프라 (공통)

### [BIZ-E4-T1] 결제 게이트웨이 통합(PG Abstraction)
- **설명:** 4수단 결제/취소·웹훅 서명만 반영·멱등 단일 결제·빌링키 자동 청구+재시도·PG 장애 폴백.
- **Subtasks:** createPayment/confirm/cancel/getStatus/billingKey, 토스/아임포트/카카오·네이버페이/계좌, 멱등, 웹훅 검증, 상태 원장
- **Acceptance Criteria:** 4수단 결제/취소, 웹훅 서명만 반영, 멱등 단일 결제, 빌링키 자동 청구+재시도, PG 장애 폴백.
- **dependencies:** 없음 · 정합: PAY-E3, BE-E06-T02
- **effort:** XL · **priority:** P0

### [BIZ-E4-T2] 정산 엔진·회계 원장(Ledger)
- **설명:** 차/대변 0 분개·PG↔원장 일자 대사 차이 0·환불 자동 차감/반환·에스크로 확정 전 미정산.
- **Subtasks:** 이중기입 원장, 수익 인식(대행/구독/건당/광고/리드), 정산 주기 배치, 에스크로/홀드백, 리컨실리에이션
- **Acceptance Criteria:** 차/대변 0 분개, PG↔원장 일자 대사 차이 0, 환불 자동 차감/반환, 에스크로 확정 전 미정산.
- **dependencies:** BIZ-E4-T1
- **effort:** XL · **priority:** P0

### [BIZ-E4-T3] 세금·전자세금계산서·현금영수증
- **설명:** 부가세 분리·세금계산서/현금영수증·원천징수·수정세금계산서.
- **Subtasks:** 팝빌/바로빌, 역발행/수정, 프리랜서 3.3%, 사업자 진위 검증
- **Acceptance Criteria:** 부가세 분리, 세금계산서/현금영수증 정확, 원천징수 반영, 수정세금계산서.
- **dependencies:** BIZ-E4-T2
- **effort:** L · **priority:** P1

### [BIZ-E4-T4] 환불·분쟁·정책 엔진
- **설명:** 사유/시점 자동 적용·우리 귀책 전액 자동·차지백 증빙 자동·원장/세금 반영.
- **Subtasks:** 환불 정책 규칙, 자동 트리거(마감초과/주최취소/검증불가), 부분환불, 차지백 대응
- **Acceptance Criteria:** 사유/시점 자동 적용, 우리 귀책 전액 자동, 차지백 증빙 자동, 원장/세금 반영.
- **dependencies:** BIZ-E4-T2
- **effort:** L · **priority:** P0

## [BIZ] E5 · 비즈니스 운영 (CS·SLA·분쟁·정책)

### [BIZ-E5-T1] 통합 고객지원(CS) 시스템
- **설명:** 주문/결제/제출 컨텍스트 자동 노출·SLA 추적·FAQ 4언어·AI 1차+휴먼 인계.
- **Subtasks:** 티켓팅, 인앱/이메일/알림톡·채널톡, FAQ, 매크로, 고객 360, CSAT, LiteLLM 1차 응대
- **Acceptance Criteria:** 주문/결제/제출 컨텍스트 자동 노출, SLA 추적, FAQ 4언어, AI 1차+휴먼 인계.
- **dependencies:** BIZ-E1, BIZ-E4
- **effort:** L · **priority:** P1

### [BIZ-E5-T2] SLA·운영 모니터링·온콜
- **설명:** 마감 임박 미처리 임계 알람·전달 실패율/정산 불일치 감지·SLA 위반 기록.
- **Acceptance Criteria:** 마감 임박 미처리 임계 알람, 전달 실패율/정산 불일치 감지, SLA 위반 기록.
- **dependencies:** BIZ-E1, BIZ-E4
- **effort:** M · **priority:** P1

### [BIZ-E5-T3] 법무·약관·정책·컴플라이언스
- **설명:** 결제/제출 동의 이력+버전·주최 전달 제3자 제공 동의·통신판매/환불 표시·takedown 처리·정책 변경 재동의.
- **Acceptance Criteria:** 결제/제출 동의 이력+버전, 주최 전달 제3자 제공 동의, 통신판매/환불 표시, takedown 처리, 정책 변경 재동의.
- **Edge cases:** 제출 대행 법적 성격·SLA·MR 제공 금지 문구(Open Q18).
- **dependencies:** BIZ-E1, BIZ-E2, BIZ-E4
- **effort:** M · **priority:** P0

## [BIZ] E6 · 그로스·리텐션·바이럴

### [BIZ-E6-T1] 추천·바이럴 루프(Referral) 백엔드
- **설명:** 추천 귀속·양면 보상 자동·카톡 공유 OG·셀프/어뷰징 차단.
- **Acceptance Criteria:** 추천 귀속, 양면 보상 자동, 카톡 공유 OG, 셀프/어뷰징 차단.
- **dependencies:** 회원, BIZ-E4
- **effort:** M · **priority:** P1

### [BIZ-E6-T2] 라이프사이클 캠페인(이메일/푸시/알림톡) 백엔드
- **설명:** 세그먼트/트리거 자동+전환·수신동의/야간 준수·채널 폴백·알림톡 사전심사·빈도 제한.
- **Acceptance Criteria:** 세그먼트/트리거 자동+전환, 수신동의/야간 준수, 채널 폴백, 알림톡 사전심사, 빈도 제한.
- **dependencies:** 회원, 분석
- **effort:** L · **priority:** P1

### [BIZ-E6-T3] 채널 시딩·파트너십 추적 백엔드
- **설명:** 채널별 코드 분리 측정·지역 큐레이션 자동·제휴 전환 추적.
- **Acceptance Criteria:** 채널별 코드 분리 측정, 지역 큐레이션 자동, 제휴 전환 추적.
- **dependencies:** BIZ-E6-T1, 디렉토리
- **effort:** M · **priority:** P2

### [BIZ-E6-T4] 전환·리텐션 분석·실험 플랫폼 백엔드
- **설명:** 제출 퍼널 이탈 측정·A/B 통계적 유의·GMV/수수료 집계·비식별.
- **Acceptance Criteria:** 제출 퍼널 이탈 측정, A/B 통계적 유의, GMV/수수료 집계, 비식별.
- **dependencies:** 전 영역 이벤트
- **effort:** M · **priority:** P2

---

# E. NOTI — 카카오 알림톡 채널

## [NOTI] EPIC-NOTI-07 · 카카오 알림톡 채널

### [NOTI-T-07-01] 알림톡 발신프로필·템플릿 등록·심사 운영
- **설명:** 정보성 템플릿 승인·변수/버튼 동작·광고성 미포함.
- **Subtasks:** 경로 선정(직접/대행), 유형별 정보성 템플릿(결제/구독/제출 접수확인/결과), 변수/버튼, SMS 대체
- **Acceptance Criteria:** 정보성 템플릿 승인, 변수/버튼 동작, 광고성 미포함.
- **Edge cases:** 알림톡 경로·심사 리드타임·단가(Open Q24).
- **dependencies:** NOTI-T-00-03
- **effort:** M · **priority:** P1

### [NOTI-T-07-02] AlimtalkNotifier+SMS 대체+결과 동기화
- **설명:** 알림톡 발송+결과 동기화·실패 SMS 폴백·미연동/미동의 제외.
- **Acceptance Criteria:** 알림톡 발송+결과 동기화, 실패 SMS 폴백, 미연동/미동의 제외.
- **dependencies:** NOTI-T-07-01, NOTI-T-01-02
- **effort:** L · **priority:** P1

### [NOTI-T-07-03] 결제·구독·제출 대행 알림 연동(거래 알림)
- **설명:** 상태 변화 1회·webhook 중복 무중복·정보성 야간/거부 무관·영수증/접수증 딥링크.
- **Acceptance Criteria:** 상태 변화 1회, webhook 중복 무중복, 정보성 야간/거부 무관, 영수증/접수증 딥링크.
- **dependencies:** NOTI-T-07-02, NOTI-T-01-02
- **effort:** M · **priority:** P1

---

> COM-LEGAL-03(전자금융·통신판매) Phase2 강화: 사업자/통신판매업 신고번호·전자금융·세금계산서·청약철회를 푸터/결제 UX에 강제 적용(백엔드는 고지 데이터·동의 이력 제공). 정합: PAY-E7, BIZ-E5-T3. (Open Q22: 신고번호 확정 시점·Phase 매핑)
