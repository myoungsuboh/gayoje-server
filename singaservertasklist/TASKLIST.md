# NORI 백엔드(BE) 마스터 작업 리스트 — 개요

> 코드명 **NORI** · 폴더 `singa-server` · 레포 `gayoje-server`
> 본 문서는 통합 마스터(`MASTER_TASKLIST.md`, 13개 영역)에서 **백엔드(서버/데이터/인프라) 스코프**만 추출·재구성한 BE 전용 작업 리스트의 개요다.
> FE(화면/컴포넌트/UI/디자인시스템/PWA UI/클라이언트 라우팅)는 제외하되, 의존성 교차참조용으로 FE Task ID를 dependencies에 명시한다.

## 제품 한줄

전국에 흩어진 가요제·노래대회 정보를 **1차 공공 출처(공공데이터포털·지자체 eGov)** 에서 자동 수집·정규화하여 통합 디렉토리로 무료 제공하고, **음원/악보 제출 대행·주최측 접수 SaaS·제휴 마켓**으로 수익화하는 PWA 플랫폼의 백엔드.

---

## 1. Phase별 목표 (BE 관점)

| Phase | 목표 | BE 핵심 산출물 |
|---|---|---|
| **Phase 0 (PoC)** | 한국 IP 크롤러 커버리지 검증·수치화, 외부 의존성(FCM/기상청/알림톡/PG/HWP) 실측, 데이터 모델·인프라 토대 | 부트스트랩(FastAPI), 컨테이너/Caddy/CI-CD, PG+Neo4j 스키마/마이그레이션, 수집 코어·공공API·eGov 어댑터(17개 광역시도), 법무 가드, 알림·날씨 발송 PoC, 커버리지 리포트 |
| **Phase 1 (무료 디렉토리)** | 목록·검색·지도·캘린더·상세 API, 인증·찜·제보, 알림(웹푸시·이메일)+날씨, LLM 추출·정규화 파이프라인+evals, 신청 가이드·LLM 작성 도움 백엔드, 작년 정보·영상 매칭·선곡분석, Admin 검수 콘솔 | 공개 읽기 API + 정규화 파이프라인 + 알림 스케줄러 + Admin 백엔드 |
| **Phase 2 (거래)** | 플랜/게이팅/quota 엔진, 결제(토스/아임포트/카카오·네이버페이)+빌링키 정기결제, 정산/회계 원장, ★음원/악보 제출 대행, 주최 접수 SaaS, 카카오 알림톡, 그로스 | 결제·정산 엔진, 제출 대행 워크플로+전달 엔진, 접수 SaaS, 빌링/웹훅/세금 |
| **Phase 3 (마켓)** | 제휴 마켓(보컬강사·녹음실·반주·의상) 리스팅·리드·예약·리뷰·정산 | 파트너 디렉토리 API, 리드/송객/광고 정산 |

---

## 2. 시스템 아키텍처

```
[클라이언트(Vue3 PWA, Vercel)]  ── HTTPS ──▶  [Caddy 리버스프록시 / 자동 TLS / 보안헤더 / CDN]
                                                      │  (API · Admin · CDN 서브도메인 라우팅)
                                                      ▼
                          ┌───────────────────────────────────────────────┐
                          │  FastAPI (async)  ── /api/v1 ──                │
                          │  routers / services / repositories / schemas   │
                          │  RBAC · JWT · 표준에러(traceId) · OpenTelemetry │
                          └───────────────────────────────────────────────┘
                              │             │              │            │
            ┌─────────────────┘             │              │            └──────────────┐
            ▼                               ▼              ▼                           ▼
   [PostgreSQL + PostGIS]           [Neo4j (read/write)]  [Redis]              [오브젝트스토리지 + CDN]
   정형·시간여행(SCD2)·감사·         탐색·추천·유사도·     캐시·세션·ZSET랭킹·    포스터 썸네일/원본 캐시,
   원장·결제·구독·outbox            그래프 통계            레이트리밋·락·큐백엔드   첨부(요강 HWP/PDF), 음원/악보
            ▲                               ▲              ▲
            │   outbox/CDC(arq)             │              │
            └───────────────┬───────────────┘              │
                            ▼                               ▼
                   [arq 워커 / 스케줄러(cron, KST)]   ◀──── 큐: discovery/fetch_api/fetch_crawl/
                   수집·정규화·지오코딩·썸네일·dedupe·         parse_llm/attachment/geocode/
                   알림 발송·D-day·날씨·정기결제·정산           thumbnail/dedupe/notify/dlq
                            │
        ┌───────────────────┼────────────────────────────────────────────┐
        ▼                   ▼                       ▼                      ▼
 [공공 API 페처]      [eGov 크롤러]            [LiteLLM]            [외부 연동]
 공공데이터포털 표준    Playwright(한국 IP)      추출·정규화·요약·     FCM(웹푸시) · SES/Outbound Mailer(서울) ·
 데이터·TourAPI·       robots/ToS 가드·         난이도·모더레이션     기상청(단기/중기/특보) · 카카오 알림톡 ·
 문화정보원(다중키)     도메인 레이트            (evals 게이트)       PG(토스/아임포트/카카오·네이버페이) ·
                                                                    지오코딩(카카오/네이버/VWorld) · 팝빌(세금)
```

- **언어/프레임워크:** Python · FastAPI(async) · SQLAlchemy async(asyncpg) · neo4j async · redis-py async · arq(잡 큐/cron) · LiteLLM(LLM 게이트웨이) · Playwright(크롤러) · Alembic(마이그레이션) · Pydantic(스키마).
- **인프라:** Caddy(리버스프록시·자동 TLS·보안헤더·CDN) · 컨테이너(API/워커/스케줄러 분리) · GitHub Actions(CI/CD) · **한국 IP 호스팅(NCP / AWS 서울)** · 오브젝트스토리지(NCP/S3 호환)+CDN.
- **데이터스토어 경계:** PostgreSQL(정형·시간여행·감사·원장·결제) / Neo4j(탐색·추천·유사도·그래프 통계) / Redis(캐시·세션·랭킹·레이트리밋·락·큐). PG↔Neo4j는 outbox/CDC(arq)로 멱등 동기화. (경계 정밀 분할은 Open Q10.)

---

## 3. 데이터 흐름

```
수집(INGEST)  ─▶  raw 보존(source_record·payload_hash·sha256)  ─▶  문서 전처리(HWP/HWPX/PDF→텍스트·표, OCR)
   │                                                                          │
   ▼                                                                          ▼
LLM 구조화 추출(스키마 강제·근거 span·신뢰도)  ─▶  evals 품질게이트(임계 미만 → 검수큐)
   │                                                                          │
   ▼                                                                          ▼
정규화 룰엔진(날짜/금액/주소/기관/부문)  ─▶  지오코딩(좌표+정확도, PostGIS)  ─▶  포스터 phash/썸네일
   │                                                                          │
   ▼                                                                          ▼
중복제거(블로킹·매칭·dedup_cluster)  ─▶  병합(survivorship·field-level lineage)  ─▶  자동분류(장르/상태/지역/세그먼트)
   │                                                                          │
   ▼                                                                          ▼
검증·품질게이트(정합성·결측·신선도·출처결측 차단)  ─▶  Admin 검수/승인  ─▶  published(캐시 무효화)
   │                                                                          │
   ▼                                                                          ▼
읽기 API(목록/상세/검색/지도/캘린더/홈/아카이브, Redis 캐시)  ◀── Neo4j 동기화(outbox/CDC)
```

**핵심 불변식(BE 계약):**
1. 모든 엔티티/파생물에 **출처 메타(source_url·기관·수집시각·수집방식)** 보존(NOT NULL). 결측 시 게시 차단.
2. **포스터/영상 재호스팅 금지** — 원본 URL/공식 임베드/썸네일만(`media_video.is_rehosted=false` CHECK).
3. **시간/마감/D-day는 전 영역 Asia/Seoul 서버 단일 계산**(클라 계산 금지). 저장은 UTC(timestamptz), 표기는 KST.
4. **온보딩 산출물(user_preferences)** 표준 스키마가 홈 추천·D-day·선곡분석·제출대행의 공통 시드.
5. **결제/제출/정산/통지/주문**은 idempotency_key로 멱등 보장. 카드 원번호 미저장(PG 토큰만), 빌링키 암호화, 웹훅 서명검증.

---

## 4. 전체 Epic / Task 수 요약 (BE 스코프)

| Phase | 파일 | Epic(영역) | Task(약) |
|---|---|---:|---:|
| Phase 0 | `TASKLIST_PHASE0.md` | 9 | 46 |
| Phase 1 | `TASKLIST_PHASE1.md` | 33 | 146 |
| Phase 2 | `TASKLIST_PHASE2.md` | 17 | 66 |
| Phase 3 | `TASKLIST_PHASE3.md` | 3 | 8 |
| **합계** | | **62** | **~266** |

> Phase 1은 BE 코어 API + DATA 파이프라인 + AUTH + LIST/DETAIL/NAV/APP 백엔드 + ARCH + NOTI + COM 백엔드측 + GROUNZ 벤치(10) 통합으로 비중이 가장 크다. Phase 1 Task에는 마스터 BE Task 보존 + GROUNZ 신규(GRNZ-) 보강이 포함된다.

### 영역별 분포(요약)

| 영역 | 주요 Epic | 비고 |
|---|---|---|
| BE(코어 API·인프라·Admin) | E01·E02·E03·E04·E05·E06·E07·E08·E09·E10·E11 | 부트스트랩~Admin~제출SaaS API |
| DATA | DSCHEMA·DEXTRACT·DMEDIA·DEDUP | 스키마·LLM추출·미디어·중복제거(evals 게이트) |
| INGEST | E1·E2·E3·E7·E13 | 수집 코어·공공API·eGov·법무·BE연계 |
| NOTI | 00~07 | 알림 코어·웹푸시·이메일·설정·날씨·스케줄러·알림톡 |
| PAY | E1~E7 | 플랜·게이팅·결제·구독·세금·쿠폰·법무 |
| BIZ | E1~E6 | 제출대행·접수SaaS·정산회계·CS·그로스·제휴 |
| AUTH | E1~E8 | JWT·소셜·세션·프로필·마이·2FA·법무(서버측) |
| ARCH | E1·VID-E2·SONG-E3·STAT-E4·CUR-E6·QA-E7 | 아카이브·영상매칭·선정곡·통계·큐레이션 |
| APP | EP01~EP05 | 신청가이드·LLM작성도움·음원검증·제출대행·리마인더(백엔드) |
| COM(백엔드측) | LEGAL·SEC·QA·OBS | 법무 enforcement·보안·QA·관측 |
| GROUNZ 벤치 | GRNZ-* | 신규 Task(접두사 GRNZ-) |

---

## 5. 추천 빌드 순서 (BE 트랙)

> 스프린트 ≈ 2주. BE 중심 재구성.

- **S0 (Phase0):** `BE-E01`(부트스트랩) · `BE-E11`(컨테이너/Caddy/CI/CD) · `DATA-DSCHEMA`(T01~T07) · `INGEST-E1`(수집 코어) · `INGEST-E2/E3`(공공API·eGov, 17개 광역시도) · `INGEST-E7`(법무 가드) · `INGEST-E13`(오케스트레이션) · `NOTI-00`(발송 PoC) → **커버리지 수치화 산출**
- **S1:** `BE-E02`/`AUTH-E1~E3`(인증·소셜) · `DATA-DEXTRACT`(LLM 추출 T01~T07) · `COM-SEC/LEGAL`(서버측)
- **S2:** `BE-E03`/`LIST-E5`(목록·검색·지도·캘린더 API) · `NAV-0312/0402`(홈·자동완성) · `DATA-DEDUP`(중복·병합·분류) · `DATA-DMEDIA`(지오코딩·썸네일) · `GRNZ-BE-02/03/06`(랭킹·택소노미·추천)
- **S3:** `DETAIL-EP01`(상세 BFF) · `BE-E07`(미디어) · `BE-E04`(찜·제보) · `APP-EP01`(신청가이드)
- **S4:** `NOTI-01~06`(알림 코어·웹푸시·이메일·설정·날씨·스케줄러) · `BE-E05`(알림 BE) · `AUTH-E5/E6`(프로필·마이·탈퇴) · `APP-EP02/03`(작성도움·음원검증)
- **S5:** `ARCH-E1/VID-E2/SONG-E3/STAT-E4`(아카이브·영상·선곡·통계) · `ARCH-CUR-E6`(큐레이션) · `BE-E10`(Admin) · `GRNZ-BE-01/05/07`(제보·세그먼트·선곡집계)
- **S6 (Phase2):** `PAY-E1~E5`/`BE-E06`/`BIZ-E4`(플랜·게이팅·결제·구독·정산·세금) · `PAY-E7`(전자금융/통신판매)
- **S7:** `APP-EP04/05`·`BIZ-E1`·`BE-E08-T01`(★제출 대행 워크플로·전달 엔진) · `NOTI-07`(알림톡)
- **S8:** `BIZ-E2`·`BE-E08-T02~T04`(주최 접수 SaaS) · `BIZ-E5`(CS/SLA/법무) · `BIZ-E6`/`PAY-E6`(그로스·쿠폰)
- **S9 (Phase3):** `BIZ-E3`·`BE-E09`(제휴 마켓) · `GRNZ-P3`(구인구직·강사 마켓)

### 크리티컬 패스
```
수집 코어(INGEST-E1) ─▶ 데이터 스키마(DATA-DSCHEMA) ─▶ LLM 추출/정규화(DATA-DEXTRACT)
   ─▶ 중복제거/분류(DATA-DEDUP) ─▶ 목록/상세 API(BE-E03/DETAIL-EP01) ─▶ FE 전 화면
인증(BE-E02/AUTH-E1) ─▶ 찜/제보/알림설정 ─▶ 알림 코어(NOTI-01) ─▶ D-day/날씨 스케줄러(NOTI-06)
결제 게이트웨이(PAY-E3/BIZ-E4-T1) ─▶ 정산 엔진(BIZ-E4-T2/PAY) ─▶ ★제출 대행(BIZ-E1/APP-EP04) ─▶ 접수 SaaS(BIZ-E2)
```

---

## 6. 파일 안내

| 파일 | 내용 |
|---|---|
| `TASKLIST.md` | (본 문서) 개요·아키텍처·데이터흐름·요약·빌드순서·크로스커팅·GROUNZ 반영·Open Questions |
| `TASKLIST_PHASE0.md` | PoC: 부트스트랩·인프라·스키마·수집(공공API/eGov)·법무·알림/날씨 PoC |
| `TASKLIST_PHASE1.md` | 무료 디렉토리: BE 코어 API·DATA 파이프라인·AUTH·LIST/DETAIL/NAV/APP 백엔드·ARCH·NOTI·COM 백엔드측·GROUNZ 벤치 |
| `TASKLIST_PHASE2.md` | 거래: PAY·BE-E06/E08·APP 제출대행·BIZ 워크플로/SaaS/정산·NOTI 알림톡 |
| `TASKLIST_PHASE3.md` | 제휴 마켓: BE-E09·BIZ-E3·GROUNZ 벤치(P3) |

> 각 Phase 파일은 에픽 섹션 → Task별 `[ID] 제목 / 설명 / subtask 불릿 / acceptance criteria / edge cases / 엔드포인트·스키마(있으면) / dependencies(FE ID 포함) / effort(S/M/L/XL) / priority(P0/P1/P2/P3)` 형식을 따른다.

---

## 7. BE 크로스커팅 체크리스트

### 보안 (Security)
- [ ] 토큰: access 메모리(비영속) / refresh HttpOnly+Secure+SameSite, 회전·재사용 탐지 (AUTH-E1-T2, COM-SEC-02-BE)
- [ ] CSP unsafe-eval 없음, 보안헤더 A등급, XSS/오픈리다이렉트 0 (COM-SEC-01-BE, BE-E11-T02)
- [ ] argon2id, 이메일 열거 방지(동일 응답), 레이트리밋·계정잠금·캡차, 새 기기 알림 (AUTH-E1-T3)
- [ ] 결제 카드 원번호 미저장(PG 토큰만), 빌링키 암호화, 웹훅 서명검증·멱등 (PAY-E7-T2, PAY-E5-T1)
- [ ] Secrets 번들/이미지 미포함, gitleaks/Dependabot/trivy CI, PII 로깅 마스킹 (COM-SEC-03-BE, BE-E11-T03)
- [ ] 멱등성: 결제/제출/정산/통지/주문 idempotency_key (전 거래 엔티티)
- [ ] IDOR/소유권 검사, RBAC 가드 (BE-E02-T03)
- [ ] 첨부 MIME/시그니처 검증, ClamAV, 매크로 HWP/DOCX 차단 (BIZ-E1-T2, INGEST-E3-T5, DETAIL-T-01-05)

### 법무/저작권 (Legal/Copyright)
- [ ] 1차 출처만 가공, **경쟁사 가공DB(GROUNZ 등) 크롤 금지 — 코드 차단** (INGEST-E7-T1)
- [ ] 포스터/영상 재호스팅 금지: 출처표기+공식 임베드/썸네일만, `is_rehosted=false` CHECK (DATA-T-SCHEMA-03, INGEST-E7-T2, ARCH-VID-E2-T4)
- [ ] 모든 카드/섹션/추천에 출처 4종 표기, 결측 시 게시 차단 (COM-LEGAL-01-BE)
- [ ] takedown/이의제기 워크플로 SLA 48h, 차단 재수집 방지 (INGEST-E7-T4, ARCH-VID-E2-T4)
- [ ] 공공누리 라이선스 유형별 사용범위 구분
- [ ] robots/ToS 준수, 한국 IP 경유, crawl-delay (INGEST-E3-T2)

### PIPA (개인정보)
- [ ] 동의 채널별(이메일/SMS/푸시/알림톡)·목적별 분리 저장, consent_history append-only+버전 (AUTH-E1-T1)
- [ ] 위치 정밀도 축소 최소수집, 분석 동의 전 비식별
- [ ] 위탁·국외이전 고지(NCP/AWS/LiteLLM Gemini), 만14세 처리 (COM-LEGAL-02-BE)
- [ ] 신청서 민감항목 sensitive 태깅·at-rest 암호화·LLM 전송 전 마스킹 (APP-T-02-01/02)
- [ ] 제출 대행 = 주최 제3자 제공 → 제출 건마다 명시 동의 보존 (APP-T-04-01, BIZ-E5-T3)
- [ ] 탈퇴 soft delete→유예→purge, 법적 보관 익명화/식별 삭제 분리 (AUTH-E6-T5)
- [ ] 지원자/제보자 개인정보 보유기간 자동 파기, 다운로드 audit (BIZ-E2-T4, INGEST-E7-T3)

### 전자금융/통신판매 (Phase2~)
- [ ] 통신판매업 신고번호·사업자정보 법정 위치 고지(데이터 제공) (PAY-E7-T1, COM-LEGAL-03)
- [ ] 결제 전 고지, 환불/청약철회/해지 일관, 전자세금계산서/현금영수증 (BIZ-E4-T3, PAY-E5)
- [ ] 접수비 수납 주체 전자금융업/PG 등록·에스크로 요건 검토 (BIZ-E4, Open Q20)

### 관측/QA
- [ ] 단위/통합 테스트+커버리지 게이트, API 회귀 감지 (COM-QA-01-BE)
- [ ] 크롤러/수집 회귀+커버리지 수치화(PoC)+LLM 추출 evals 게이트 (COM-QA-03-BE, DATA-T-EXT-06, ARCH-QA-E7-T1)
- [ ] CI/CD 게이트(린트/타입/테스트/보안스캔), 마이그 up/down·OpenAPI diff, 롤백 (BE-E11-T03/T04, COM-QA-04-BE)
- [ ] 구조화 로그(requestId 상관)·도메인 메트릭·알럿 임계·핵심 퍼널 서버 진실 (BE-E01-T06, INGEST-E1-T5, COM-OBS-BE)
- [ ] 캐시 스탬피드 방지, 커서 페이징, 지도 서버 클러스터, 홈/캘린더 경량 응답 (LIST-E5, NAV-0312)

---

## 8. GROUNZ 벤치마크 반영표 (BE 관련)

> 데이터는 1차 출처(공공 API·지자체)·자체 등록만. **GROUNZ DB 스크래핑 금지**(경쟁사 가공DB·잡코리아v사람인 리스크 — INGEST-E7-T1 코드 차단).

| 채택 패턴 | 성격 | BE Task ID | Phase/우선순위 |
|---|---|---|---|
| 게시요청(크라우드 제보) 인입·검수 | GROUNZ 검증 | GRNZ-BE-01 | P1 |
| 조회수 집계·인기 랭킹(Top10) | GROUNZ 검증 | GRNZ-BE-02 | P1 |
| 카테고리/대상/지역/분야/상금구간 택소노미+필터 인덱스 | GROUNZ 검증 | GRNZ-BE-03 | P1 |
| 아티클(콘텐츠) CMS | GROUNZ 검증 | GRNZ-BE-04 | P2 |
| 인기 검색어/실시간 트렌드 집계 | GROUNZ 검증 | GRNZ-BE-10 | P2 |
| 커뮤니티/Q&A/익명 | GROUNZ 검증(후순위) | GRNZ-BE-09 | P2 |
| 구인구직 | GROUNZ 검증 | GRNZ-P3-01 | P3 |
| 스토어/레슨·커미션(강사 마켓) | GROUNZ 검증 | GRNZ-P3-02 | P3 |
| **음역대/장르/지역 개인화 추천** | ★차별화 | GRNZ-BE-06 | P1 |
| **선정곡/수상곡 분석 집계** | ★차별화 | GRNZ-BE-07 | P1 |
| **신청서 작성 도움 LLM** | ★차별화 | GRNZ-BE-08 | P1 |
| **기상청 날씨 매핑** | ★차별화 | NOTI-EPIC-05/06 | P0~P1 |
| **세그먼트 태깅(트로트/지역가요제/노래자랑/실버 — GROUNZ가 비운 데모)** | ★차별화 | GRNZ-BE-05 | P1 |

---

## 9. Open Questions / 리스크 (BE 관련)

**기술/아키텍처**
- **Q5.** 검색 백엔드: PostgreSQL FTS(nori) 시작 vs OpenSearch 초기 도입(초성/오타보정 요구 대비 비용). → LIST-E5-T2, BE-E03-T03
- **Q6.** 지도 SDK 1차: 카카오맵 vs 네이버맵(쿼터·약관·길찾기 딥링크·국내 정확도). → BE-E03-T04
- **Q7.** 홈/목록 집계: 단일 `/home` 엔드포인트 vs 섹션별 분할(초기 렌더 vs 캐시 효율). → NAV-0312
- **Q8.** PG 1차: 토스페이먼츠 단독 vs 아임포트(멀티PG, 카카오/네이버페이 커버). → PAY-E3, BIZ-E4-T1
- **Q9.** 빌링키 정기결제 지원 수단 범위(카카오/네이버페이 빌링 지원 여부 → 카드만 자동갱신?). → PAY-E4-T3
- **Q10.** PG/Neo4j 경계: 어디까지 그래프로(선곡분석·유사추천 그래프 vs PG MV). → DATA, BE
- **Q11.** 추천 엔진: 규칙 기반 시작 vs Neo4j 관계/임베딩 Phase1 포함 범위. → GRNZ-BE-06, NAV-0303

**LLM/데이터**
- **Q12.** HWP/HWPX→PDF 변환 현실적 성공률·비용(OSS 한계 측정 후 상용 SDK 결정). → DATA-T-EXT-02, DETAIL-T-01-05
- **Q13.** 곡/수상곡 메타 저작권 범위(제목·아티스트·연도만 vs 가사), KOMCA 검토. → ARCH-SONG-E3
- **Q14.** 다국어: 공공데이터 한국어 원문 LLM 번역 vs 원문+안내. → COM-I18N-03
- **Q15.** 선곡 난이도 스코어 공개 노출 여부(오판 리스크) 및 통계 최소 표본 임계. → ARCH-SONG-E3-T2, STAT-E4
- **Q16.** LLM 추출 오탐 허용치·human-review 진입 confidence 임계. → DATA-T-EXT-06

**법무/사업**
- **Q17.** 포스터 자체 썸네일 생성/CDN 캐싱이 '재호스팅 금지'와 충돌하는지(법무 확정). → DATA-T-MED-02, INGEST-E7-T2
- **Q18.** 제출 대행 법적 성격(심부름/대리 vs 위탁), 책임 범위·마감 보장 SLA·MR 제공 금지 문구. → BIZ-E1, BIZ-E5-T3, APP-EP04
- **Q19.** 주최 전달 채널 합법성: 외부 접수포털 Playwright 자동화를 주최 사전합의 채널로 한정 vs 전면 수기. → BIZ-E1-T3, APP-T-04-03
- **Q20.** 접수비 수납/정산 주체의 전자금융업/PG 등록·에스크로 요건. → BIZ-E2, BIZ-E4, BE-E08-T04
- **Q21.** 제출 대행 수수료 체계(정액/정률/대회별 가산)+구독 할인, 무료 재제출 횟수·귀책별 환불 차등. → PAY-E1, BIZ-E4-T4
- **Q22.** 사업자/통신판매업 신고번호 확정 시점과 Phase 매핑. → COM-LEGAL-03
- **Q23.** 다중 공공API 키 운용이 약관상 허용 범위인지(위반 시 단일키+레이트). → INGEST-E2-T2

**인프라/알림**
- **Q24.** 카카오 알림톡: 직접(비즈메시지) vs 대행사(NHN/솔라피/알리고), 심사 리드타임·단가. → NOTI-07
