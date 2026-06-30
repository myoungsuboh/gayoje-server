# NORI 백엔드 작업 리스트 — PHASE 3 (제휴 마켓)

> 코드명 NORI · 폴더 `singa-server` · 레포 `gayoje-server`
> Phase 3 목표: **제휴 마켓(보컬강사·녹음실·반주·의상), 리드/송객/광고 수익, 리뷰.**
> 강사 마켓·스토어/레슨·커미션은 GROUNZ가 검증한 영역(차별화 아님). 데이터는 1차 출처·자체 제휴사 등록만 사용.

## Phase 3 에픽 인덱스

| Epic | 영역 | 제목 | Tasks |
|---|---|---|---:|
| BE-EP-E09 | BE | 강사·제휴 마켓 API | 2 |
| BIZ-E3 | BIZ | 제휴 마켓(Partner Marketplace) | 4 |
| GRNZ-P3 | GROUNZ | 벤치마크 반영(구인구직·스토어/레슨 백엔드) | 2 |
| **합계** | | **3 Epic** | **8 Task** |

---

# A. BE · 강사·제휴 마켓 API

## [BE] EP-E09 · 강사·제휴 마켓 API

### [BE-E09-T01] 강사·제휴 리스팅·프로필·승인
- **설명:** 승인만 노출·필터/검색/정렬·승인/정지 즉시.
- **Subtasks:** 리스팅/프로필 모델, 카테고리(보컬강사/녹음실/반주/의상), 필터/검색/정렬, 승인 워크플로, 정지
- **Acceptance Criteria:** 승인만 노출, 필터/검색/정렬, 승인/정지 즉시 반영.
- **Edge cases:** 미승인 노출 방지, 지역/장르 매칭 정확.
- **dependencies:** BE-E02-T03, BE-E07-T01 · FE: NAV-T-NAV-0703, DETAIL-T-08-03
- **effort:** M · **priority:** P2

### [BE-E09-T02] 연결·문의·예약·리뷰
- **설명:** 문의 전달+추적·리뷰 집계 정렬·연결 수수료 기록.
- **Subtasks:** 문의 라우팅+추적ID, 예약, 리뷰 집계/정렬, 연결 수수료 기록
- **Acceptance Criteria:** 문의 전달+추적, 리뷰 집계 정렬, 연결 수수료 기록.
- **dependencies:** BE-E09-T01, BE-E05-T02
- **effort:** M · **priority:** P2

---

# B. BIZ · 제휴 마켓 (Partner Marketplace)

## [BIZ] E3 · 제휴 마켓

### [BIZ-E3-T1] 제휴사 온보딩·프로필·디렉토리
- **설명:** 미승인 미노출·지역/장르/가격 필터·스폰서 명확 표기·포트폴리오 출처/임베드 준수.
- **Subtasks:** 카테고리(보컬강사/녹음실/반주/의상), 검증·심사, 디렉토리(필터/지도/정렬), 노출 랭킹(스폰서 표기)
- **Acceptance Criteria:** 미승인 미노출, 지역/장르/가격 필터, 스폰서 명확 표기, 포트폴리오 출처/임베드 준수.
- **Edge cases:** 포트폴리오 재호스팅 금지(출처/임베드만).
- **dependencies:** 회원
- **effort:** L · **priority:** P2

### [BIZ-E3-T2] 리드·송객·예약 시스템
- **설명:** 문의 즉시 전달+추적ID·예약 확정/취소 통지+충돌 방지·과금 기록·전환 퍼널.
- **Subtasks:** 리드 폼, 예약(슬롯/보증금), 송객 추적, 광고 슬롯(CPC/CPM), CPL/CPA, 응답률 랭킹, 인앱 메시징
- **Acceptance Criteria:** 문의 즉시 전달+추적ID, 예약 확정/취소 통지+충돌 방지, 과금 기록, 전환 퍼널.
- **dependencies:** BIZ-E3-T1, BIZ-E4
- **effort:** L · **priority:** P2

### [BIZ-E3-T3] 리뷰·평점·품질관리
- **설명:** 거래 검증 후만 리뷰·허위/악성 모더레이션·품질지표 노출 조정·단계 제재+이의.
- **Subtasks:** LLM 모더레이션, 가중 집계, 답글, 품질지표(응답/취소/분쟁), 어뷰징 탐지
- **Acceptance Criteria:** 거래 검증 후만 리뷰, 허위/악성 모더레이션, 품질지표 노출 조정, 단계 제재+이의.
- **dependencies:** BIZ-E3-T2
- **effort:** M · **priority:** P2

### [BIZ-E3-T4] 제휴 정산·수수료
- **설명:** 모델별 분개 정산·원천징수 정확·환불 자동 차감/보류·예치금 노출 동기화.
- **Subtasks:** CPC/CPM/CPL/CPA/구독 원장, 페이아웃, 원천징수(3.3%), 세금계산서, 광고 예치금
- **Acceptance Criteria:** 모델별 분개 정산, 원천징수 정확, 환불 자동 차감/보류, 예치금 노출 동기화.
- **dependencies:** BIZ-E3-T2, BIZ-E4
- **effort:** M · **priority:** P2

---

# C. GROUNZ 벤치마크 반영 (Phase3 백엔드 신규)

> GROUNZ가 검증한 구인구직·스토어/레슨/커미션(강사 마켓) 영역. 차별화는 아니나 BM 확장 옵션으로 P3 배치.
> **법적 주의:** GROUNZ DB 스크래핑 금지 — 제휴사/구인 데이터는 자체 등록·1차 출처만.

### [GRNZ-P3-01] 구인구직 백엔드 (GROUNZ 벤치 — P3)
- **설명:** 가요제 관련 구인구직(반주자·세션·행사 스태프) 리스팅·지원 백엔드.
- **Subtasks:** job posting 모델, 지원 워크플로, 카테고리/지역 필터, 모더레이션, 알림 연동
- **Acceptance Criteria:** 공고 등록/마감, 지원 처리, 필터, 모더레이션, 지원 알림.
- **dependencies:** BE-E02-T03, BE-E05-T02
- **effort:** L · **priority:** P3

### [GRNZ-P3-02] 스토어/레슨·커미션(강사 마켓) 백엔드 (GROUNZ 벤치 — P3)
- **설명:** 레슨/커미션 상품·주문·커미션 수수료 백엔드(강사 마켓 확장).
- **Subtasks:** 상품(레슨/커미션) 모델, 주문/결제 연동, 커미션 수수료 원장, 정산 연계
- **Acceptance Criteria:** 상품 등록/노출, 주문/결제, 커미션 수수료 정산, 환불 연계.
- **dependencies:** BE-E09-T01, BIZ-E3-T4, BIZ-E4
- **effort:** L · **priority:** P3
