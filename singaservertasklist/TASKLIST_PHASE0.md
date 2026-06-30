# NORI 백엔드 작업 리스트 — PHASE 0 (PoC / 인프라·데이터 토대)

> 코드명 NORI · 폴더 `singa-server` · 레포 `gayoje-server`
> 본 문서는 마스터(`MASTER_TASKLIST.md`)에서 **백엔드(서버/데이터/인프라) 스코프**만 추출·재구성한 것이다.
> FE(화면/컴포넌트/UI) Task는 의존성 교차참조(ID)로만 등장한다.
> Phase 0 목표: **한국 IP 크롤러 커버리지 검증·수치화, 외부 의존성(FCM/기상청/알림톡/PG/HWP) 실측, 데이터 모델·인프라 토대 구축.**

## Phase 0 에픽 인덱스

| Epic | 제목 | Tasks |
|---|---|---:|
| BE-EP-E01 | 프로젝트 부트스트랩·코어 인프라·설정/시크릿 | 6 |
| BE-EP-E11 | 인프라/DevOps — 컨테이너·CI/CD·Caddy/도메인 | 4 |
| DATA-EPIC-DSCHEMA | 데이터 모델·스키마(PostgreSQL + Neo4j) | 7 |
| INGEST-E1 | 수집 코어 인프라 & 공통 어댑터 프레임워크 | 5 |
| INGEST-E2 | 공식 공공 API 페처 | 8 |
| INGEST-E3 | eGovFrame 게시판 크롤러(Playwright) | 6 |
| INGEST-E7 | 법무·컴플라이언스·데이터 거버넌스 | 4 |
| INGEST-E13 | 데이터 수집 백엔드 연계 | 2 |
| NOTI-EPIC-NOTI-00 | 알림·날씨 발송 가능성 검증(PoC) | 4 |
| **합계** | **9 Epic** | **46 Task** |

---

## [BE] EP-E01 · 프로젝트 부트스트랩 · 코어 인프라 · 설정/시크릿

### [BE-E01-T01] FastAPI 앱 골격/모듈 구조
- **설명:** create_app 팩토리 기반 도메인 분리 앱 골격. 모든 라우터 `/api/v1` 자동등록, camelCase 직렬화, UTC 저장/KST 표기 일관.
- **Subtasks:**
  - app 팩토리(`create_app`), uvicorn 엔트리포인트
  - 도메인 디렉토리: festivals·search·geo·calendar·favorites·reports·subscriptions·payments·notifications·instructors·auth·users·admin·intake·ingestion
  - 레이어 분리: routers/services/repositories/schemas/models/deps
  - 버전 프리픽스(`/api/v1`), 타임존 util(UTC↔KST)
  - camelCase 직렬화 alias 규약
- **Acceptance Criteria:**
  - `/api/v1` 라우터가 도메인 추가만으로 자동 등록된다.
  - 응답이 camelCase로 직렬화되고 시각은 KST 표기로 일관된다.
  - 레이어 경계 위반(라우터에서 repo 직접 호출 등)이 lint/리뷰로 차단된다.
- **Edge cases:** naive datetime 혼입, alias 누락 필드, 순환 import.
- **Endpoints:** `GET /api/v1/version`
- **dependencies:** 없음
- **effort:** L · **priority:** P0

### [BE-E01-T02] 환경설정·시크릿(dev/stg/prod)·12-factor
- **설명:** pydantic-settings 기반 12-factor 설정, 필수 env 누락 시 fail-fast, prod는 시크릿 매니저.
- **Subtasks:**
  - pydantic-settings 계층(BaseSettings), 환경 분리(dev/stg/prod)
  - NCP/AWS(서울) Secret Manager 연동
  - 로깅 시 시크릿 마스킹
  - feature flag(결제/알림톡/크롤러 on·off)
- **Acceptance Criteria:**
  - 필수 env 누락 시 부팅이 fail-fast로 즉시 중단된다.
  - prod에서 평문 시크릿이 코드/이미지/로그에 노출되지 않는다.
  - feature flag 토글이 재배포 없이 환경값으로 적용된다.
- **Edge cases:** 환경별 키 오버라이드 충돌, 마스킹 누락.
- **dependencies:** BE-E01-T01
- **effort:** L · **priority:** P0

### [BE-E01-T03] DB 커넥션(PG·Neo4j·Redis)·풀링·헬스
- **설명:** 3개 데이터스토어 비동기 커넥션·풀·헬스, 누수 0.
- **Subtasks:**
  - asyncpg + SQLAlchemy async
  - neo4j async 드라이버(read/write 라우팅 분리)
  - redis-py async
  - 풀/타임아웃/재시도 정책
  - PG↔Neo4j 책임 경계 문서화(정형/시간여행 vs 탐색/추천)
- **Acceptance Criteria:**
  - 풀 기반 연결로 동작하고 deep 헬스체크가 3 스토어를 검증한다.
  - 부하 테스트 후 커넥션 누수 0.
- **Edge cases:** 풀 고갈, Neo4j 리더 장애 시 폴백, Redis 타임아웃.
- **dependencies:** BE-E01-T02
- **effort:** L · **priority:** P0

### [BE-E01-T04] DB 스키마·마이그레이션·시드
- **설명:** Alembic 기반 PG 스키마 + Neo4j 제약/인덱스 + 마스터 시드.
- **Subtasks:**
  - Alembic 구성, 핵심 PG 테이블
  - Neo4j 제약/인덱스(idempotent)
  - 공통 컬럼 규약(soft delete·source 메타)
  - 지역/장르 마스터 시드
  - CI에서 up/down 검증
- **Acceptance Criteria:**
  - `alembic upgrade head`가 빈 DB에서 재현 가능하다.
  - Neo4j 제약 생성이 멱등(반복 실행 안전)하다.
  - 지역/장르 시드가 적재된다.
- **Edge cases:** up/down 비대칭, 시드 중복, 제약 충돌.
- **dependencies:** BE-E01-T03
- **effort:** L · **priority:** P0

### [BE-E01-T05] 공통 미들웨어·예외·표준응답·요청컨텍스트
- **설명:** 통일 에러 스키마(traceId)·도메인 예외 계층·요청 컨텍스트.
- **Subtasks:**
  - 표준 에러 스키마(code/message/detail/traceId/fields)
  - 도메인 예외 계층 → HTTP 매핑
  - X-Request-ID 전파, CORS 화이트리스트, GZip, 본문 크기 제한
- **Acceptance Criteria:**
  - 모든 에러가 통일 스키마(traceId 포함)로 반환된다.
  - 검증 실패는 필드별 422로 반환된다.
  - 비허용 Origin의 CORS는 차단된다.
- **Edge cases:** 미들웨어 순서 의존성, 대용량 업로드 거부 메시지.
- **dependencies:** BE-E01-T01
- **effort:** M · **priority:** P0

### [BE-E01-T06] 구조화 로깅·트레이싱·헬스/레디니스
- **설명:** structlog JSON 로깅·OpenTelemetry·헬스/레디니스 엔드포인트.
- **Subtasks:**
  - structlog JSON, requestId 상관
  - OpenTelemetry 트레이싱
  - `/healthz`·`/readyz`·`/healthz/deep`·`/version`
  - PII 마스킹 로깅 필터
- **Acceptance Criteria:**
  - requestId로 로그 상관 추적이 가능하다.
  - 의존성 미준비 시 `/readyz`가 503을 반환한다.
  - PII가 로그에 남지 않는다.
- **Edge cases:** 레디니스 플래핑, 트레이스 컨텍스트 누락.
- **Endpoints:** `GET /healthz` · `GET /readyz` · `GET /healthz/deep` · `GET /version`
- **dependencies:** BE-E01-T05
- **effort:** M · **priority:** P0

---

## [BE] EP-E11 · 인프라/DevOps — 컨테이너·환경·CI/CD·Caddy/도메인

### [BE-E11-T01] 컨테이너화(API·워커·스케줄러)·오케스트레이션
- **설명:** 멀티스테이지 Dockerfile·docker-compose 로컬 스택·비루트 실행.
- **Subtasks:**
  - 멀티스테이지 Dockerfile(api/worker/scheduler)
  - docker-compose(api/worker/scheduler/postgres/neo4j/redis/caddy)
  - 레지스트리 푸시(git sha 태그)
  - NKS/ECS vs 단일호스트 운영 결정 ADR
  - Playwright 브라우저 이미지 격리
- **Acceptance Criteria:**
  - 단일 명령으로 로컬 풀스택이 기동한다.
  - 이미지가 git sha로 태깅된다.
  - 컨테이너가 비루트로 실행된다.
- **Edge cases:** Playwright OOM, 워커/스케줄러 헬스 분리.
- **dependencies:** BE-E01-T06
- **effort:** L · **priority:** P0

### [BE-E11-T02] Caddy 리버스프록시·자동 TLS·도메인·CDN
- **설명:** Caddy 기반 HTTPS 강제·서브도메인 라우팅·보안헤더·CDN.
- **Subtasks:**
  - Caddyfile(API/Admin/CDN·CORS·HSTS)
  - 자동 TLS, 도메인/DNS(프론트 Vercel 분리)
  - CDN 캐시 정책, 엣지 레이트리밋
- **Acceptance Criteria:**
  - HTTPS가 강제되고 보안헤더(HSTS 등)가 적용된다.
  - API/Admin/CDN 서브도메인이 올바르게 라우팅된다.
- **Edge cases:** 인증서 갱신 실패, 프론트(Vercel)와 CORS 경계.
- **dependencies:** BE-E11-T01
- **effort:** M · **priority:** P0

### [BE-E11-T03] CI 파이프라인(린트·테스트·빌드·보안스캔)
- **설명:** GitHub Actions PR 게이트·머지 차단·마이그/OpenAPI 회귀 감지.
- **Subtasks:**
  - ruff/black/mypy, pytest+커버리지
  - 마이그 up/down·OpenAPI diff 회귀 감지
  - trivy·gitleaks 보안스캔
  - 일회성 DB 컨테이너로 통합 테스트
- **Acceptance Criteria:**
  - PR 게이트 미통과 시 머지가 차단된다.
  - 마이그레이션/OpenAPI 회귀가 자동 감지된다.
- **Edge cases:** flaky 테스트, OpenAPI diff 노이즈.
- **dependencies:** BE-E11-T01
- **effort:** L · **priority:** P0

### [BE-E11-T04] CD(stg/prod 배포·마이그·무중단)
- **설명:** stg 자동·prod 승인 게이트·마이그 자동·롤백·스모크.
- **Subtasks:**
  - stg 자동·prod 승인 게이트
  - 마이그 자동실행·롤백
  - 롤링/블루그린, 시크릿 주입, 릴리스 태깅
  - 배포 후 스모크 테스트
- **Acceptance Criteria:**
  - stg는 자동, prod는 승인 후 배포된다.
  - 실패 시 롤백되고 스모크 테스트가 통과해야 완료된다.
- **Edge cases:** 마이그 실패 시 롤백 정합성, 무중단 중 세션.
- **dependencies:** BE-E11-T02, BE-E11-T03
- **effort:** L · **priority:** P0

---

## [DATA] EPIC-DSCHEMA · 데이터 모델·스키마 (PostgreSQL 정형 + Neo4j 그래프)

### [DATA-T-SCHEMA-01] 핵심 엔티티/관계 ERD·그래프 논리모델
- **설명:** 전 기능을 담는 ERD + PG/Neo4j 경계 매핑 + provenance + 상태머신.
- **Subtasks:**
  - 엔티티 정의: festival·edition·venue·organizer·source_record·attachment·poster·media_video·song·award·genre·region_code·weather_anchor·translation·tag·contact·schedule_slot·fee·prize·eligibility·user_report·data_quality_flag·audit_log·pipeline_run·dedup_cluster
  - PG(정형·시간여행) vs Neo4j(탐색·추천) 경계
  - ID 전략(UUIDv7 + slug + 자연키)
  - 생애주기 상태머신(draft→needs_review→published→archived→deleted)
- **Acceptance Criteria:**
  - 전 기능을 담는 ERD와 PG/Neo4j 경계 매핑표가 존재한다.
  - 전 엔티티에 provenance 필드가 정의된다.
  - 상태머신 전이가 명세된다.
- **Edge cases:** 동일 엔티티 다출처, slug 충돌.
- **dependencies:** 없음
- **effort:** L · **priority:** P0

### [DATA-T-SCHEMA-02] PG 물리 스키마: 가요제/회차/장소/기관
- **설명:** festival·edition·venue·organizer 물리 테이블 + 감사/메타 mixin.
- **Subtasks:**
  - festival·festival_edition·venue·organizer·edition_organizer
  - 공통 감사/메타 mixin(created/updated/소스 메타)
  - 날짜 timestamptz, `is_*_confirmed` 플래그
  - CHECK(시작<=종료), enum 카탈로그
- **Acceptance Criteria:**
  - PK·FK·감사·confidence·source_record_id가 존재한다.
  - 모든 시각이 timestamptz(UTC) 저장된다.
  - CHECK 불변식이 적용되고 Pydantic 스키마와 1:1이다.
- **Edge cases:** 날짜 미정(TBD), 다회차 동일 장소.
- **dependencies:** DATA-T-SCHEMA-01
- **effort:** XL · **priority:** P0

### [DATA-T-SCHEMA-03] PG: 출처·첨부·포스터·영상·곡·수상 (재호스팅 금지 강제)
- **설명:** 전 파생물 provenance 추적 + 임베드/원본 URL만 보관(재호스팅 금지) + 해시 무결성 + 수상자 PII 마스킹.
- **Subtasks:**
  - source_record(raw_payload_ref·hash)
  - attachment(hwp/pdf·parse_status)
  - poster_image(원본URL·thumbnail·phash·legal_clearance)
  - media_video(`is_rehosted=false` CHECK 강제)
  - song(메타만), award(PII 마스킹), video_song 연결
  - raw payload 오브젝트스토리지 오프로드
- **Acceptance Criteria:**
  - 모든 파생물에 provenance가 추적된다.
  - 영상/포스터는 임베드/원본 URL만 저장(`is_rehosted=false` CHECK).
  - 해시 무결성이 보장되고 수상자 PII가 마스킹된다.
- **Edge cases:** 원본 URL 만료, phash 충돌, 라이선스 모호.
- **dependencies:** DATA-T-SCHEMA-02
- **effort:** XL · **priority:** P0

### [DATA-T-SCHEMA-04] 행정구역·지오·장르·태그 마스터 + PostGIS
- **설명:** 전국 행정구역 계층·개편 이력·PostGIS 공간쿼리·장르 계층/다국어.
- **Subtasks:**
  - region_code(법정/행정동·parent·boundary_geom·valid_from/to)
  - region_code_history(개편 매핑)
  - genre(계층/i18n), tag
  - PostGIS GIST, EPSG:4326
  - 시드 적재 스크립트
- **Acceptance Criteria:**
  - 전국 코드 계층과 개편 이력이 표현된다.
  - GIST 공간쿼리가 동작한다.
  - 장르 계층/다국어가 매핑된다.
- **Edge cases:** 세종/제주 특수 케이스, 행정구역 개편 시점.
- **dependencies:** DATA-T-SCHEMA-02
- **effort:** L · **priority:** P0

### [DATA-T-SCHEMA-05] 시간여행·버전·감사·소프트삭제(SCD2+audit+history)
- **설명:** 과거시점 복원·actor/reason/diff 감사·soft-delete 유니크 보장·PII 감사 별도 보존.
- **Subtasks:**
  - history 패턴, audit_log(before/after/diff)
  - SCD2(valid_from/to·is_current)
  - 부분 인덱스(soft-delete 유니크)
  - actor 주입, reason enum, 보존/파기 정책
- **Acceptance Criteria:**
  - 과거 시점 복원이 가능하다.
  - actor/reason/diff 감사가 남는다.
  - soft-delete 후에도 유니크가 보장되고 PII 감사는 별도 보존된다.
- **Edge cases:** 대량 변경 history 폭증, 파기 정책 충돌.
- **dependencies:** DATA-T-SCHEMA-02, DATA-T-SCHEMA-03
- **effort:** L · **priority:** P0

### [DATA-T-SCHEMA-06] Neo4j 그래프 스키마·제약·인덱스·동기화 계약
- **설명:** Neo4j 노드/관계 스키마 + pg_id 1:1 유니크 + outbox 동기화 계약.
- **Subtasks:**
  - 노드 레이블/관계 타입 정의
  - unique 제약(pg_id 1:1), 전문검색 인덱스
  - 속성 최소화 원칙
  - outbox 동기화 계약, 무결성 점검 쿼리
- **Acceptance Criteria:**
  - pg_id 유니크 1:1이 보장된다.
  - 탐색 인덱스가 존재하고 MERGE가 멱등이다.
  - 무결성 점검 쿼리 결과가 0이다.
- **Edge cases:** 동기화 지연, 고아 노드.
- **dependencies:** DATA-T-SCHEMA-01, DATA-T-SCHEMA-02
- **effort:** L · **priority:** P0

### [DATA-T-SCHEMA-07] 마이그레이션·인덱스·시드 + PG↔Neo4j CDC/Outbox
- **설명:** 무중단 마이그 규칙·인덱스 카탈로그·outbox/CDC 워커·CI 드리프트 차단.
- **Subtasks:**
  - Alembic 컨벤션(nullable→백필→not null)
  - 인덱스 카탈로그(GIN/GIST/부분/유니크)
  - outbox 테이블, CDC 워커(arq)
  - 시드, CI 드리프트 게이트, 롤백 리허설
- **Acceptance Criteria:**
  - 무중단 마이그 규칙이 적용된다.
  - 전 인덱스가 카탈로그화되고 outbox가 멱등이다.
  - CI가 스키마 드리프트를 차단한다.
- **Edge cases:** 대용량 백필 잠금, outbox 중복.
- **dependencies:** DATA-T-SCHEMA-02, DATA-T-SCHEMA-03, DATA-T-SCHEMA-04, DATA-T-SCHEMA-06
- **effort:** L · **priority:** P0

---

## [INGEST] E1 · 수집 코어 인프라 & 공통 어댑터 프레임워크

### [INGEST-E1-T1] 수집 도메인 데이터 모델(raw+normalized+Source 메타)
- **설명:** 2단계(raw→normalized) 파이프라인 + 출처 NOT NULL 강제 + payload_hash 변경판별.
- **Subtasks:**
  - RawCollectionItem·정규화 Event·Source 마스터·Attachment·IngestRun
  - 출처 보존 불변식(NOT NULL)
  - Neo4j 그래프 모델
  - Alembic + 인덱스, JSONB 검증/버저닝
- **Acceptance Criteria:**
  - 2단계 파이프라인이 재현된다.
  - 출처가 NOT NULL로 강제되고 payload_hash로 O(1) 변경판별된다.
  - 첨부 sha256 보존, PG/Neo4j 경계가 문서화된다.
- **Edge cases:** JSONB 스키마 버전 충돌, 해시 미스.
- **dependencies:** 없음
- **effort:** L · **priority:** P0

### [INGEST-E1-T2] 추상 SourceAdapter 인터페이스·레지스트리
- **설명:** BaseAdapter(discover/fetch/parse/normalize/persist) + 레지스트리 + dry-run + 골든 픽스처.
- **Subtasks:**
  - BaseAdapter 라이프사이클 메서드
  - 어댑터 메타·버전 기록, AdapterRegistry
  - per-source 설정 주입
  - FetchResult/ParseResult, dry-run, 골든 픽스처 하네스
- **Acceptance Criteria:**
  - 신규 소스 추가 = 구현+등록+설정만으로 가능하다.
  - 어댑터 버전이 기록되고 dry-run/골든 픽스처가 동작한다.
- **Edge cases:** 어댑터 버전 호환 깨짐, 설정 누락.
- **dependencies:** INGEST-E1-T1
- **effort:** L · **priority:** P0

### [INGEST-E1-T3] arq 큐/워커 토폴로지·잡 오케스트레이션
- **설명:** 단계별 큐 분리·fan-out·멱등·DLQ·무중단 재배포.
- **Subtasks:**
  - 큐 분리(discovery/fetch_api/fetch_crawl/parse_llm/attachment/geocode/thumbnail/dedupe/dlq)
  - fan-out, 멱등성(중복 무시)
  - 단계별 재시도/백오프/DLQ, 백프레셔
  - trace_id 전파
- **Acceptance Criteria:**
  - 단계 체인이 동작하고 중복 잡은 멱등 무시된다.
  - DLQ 재처리·무중단 재배포가 가능하다.
- **Edge cases:** 잡 폭주 백프레셔, 워커 재시작 중 손실.
- **dependencies:** INGEST-E1-T1, INGEST-E1-T2
- **effort:** L · **priority:** P0

### [INGEST-E1-T4] 비밀키/설정·멀티 환경
- **설명:** pydantic-settings 계층 + 다중 API 키 그룹 모델 + 한국 IP 자격증명 회전.
- **Subtasks:**
  - pydantic-settings 계층
  - 다중 API 키 그룹 모델
  - 프록시/한국 IP 자격증명 회전
  - 민감정보 마스킹
- **Acceptance Criteria:**
  - 키가 노출되지 않고 환경이 분리된다.
  - 키 그룹 상태 조회가 가능하다.
- **Edge cases:** 키 그룹 부분 만료, 프록시 인증 실패.
- **dependencies:** INGEST-E1-T1
- **effort:** M · **priority:** P0

### [INGEST-E1-T5] 관측성: 구조화 로깅·메트릭·트레이싱
- **설명:** run_id 추적·소스별 성공률/신선도·LLM 비용 집계.
- **Subtasks:**
  - JSON 로깅, 메트릭(fetch/attachment/LLM 토큰/큐/DLQ)
  - 신선도(last_success) 추적
  - 트레이싱 스팬, `/metrics` + 알럿 임계
- **Acceptance Criteria:**
  - run_id로 추적 가능하고 소스별 성공률/신선도가 노출된다.
  - LLM 비용이 집계된다.
- **Edge cases:** 메트릭 카디널리티 폭증.
- **Endpoints:** `GET /metrics`
- **dependencies:** INGEST-E1-T3
- **effort:** M · **priority:** P1

---

## [INGEST] E2 · 공식 공공 API 페처

### [INGEST-E2-T1] 공통 API 페처 코어(HTTP·재시도·레이트·인증)
- **설명:** JSON/XML/EUC-KR 변환·공공API 에러코드 분기·쿼터 사전차단·페이지네이션 안전.
- **Subtasks:**
  - 비동기 HTTP 래퍼, XML/JSON 파서, EUC-KR 변환
  - 공공API 에러코드 분기(트래픽초과/키미등록/NODATA)
  - 토큰버킷 레이트, 재시도/백오프
  - 페이지네이션 가드, raw 보존
- **Acceptance Criteria:**
  - JSON/XML/EUC-KR 응답을 정상 변환한다.
  - 표준 에러코드를 분기 처리하고 쿼터를 사전 차단한다.
  - 페이지네이션이 안전(무한루프 방지)하다.
- **Edge cases:** NODATA vs 에러 구분, 인코딩 깨짐.
- **dependencies:** INGEST-E1-T2, INGEST-E1-T4
- **effort:** L · **priority:** P0

### [INGEST-E2-T2] 다중 인증키 로테이션·쿼터 관리 (한국 공공데이터포털 다중키)
- **설명:** 키 풀 라운드로빈·잔여쿼터·KST 자정 복구·키 부족 예측.
- **Subtasks:**
  - 키 풀 모델, 라운드로빈+잔여쿼터
  - 소진 감지(KST 리셋), 키 헬스/revoke
  - 관리 API, 키 부족 예측
- **Acceptance Criteria:**
  - 키 자동 로테이션·소진 즉시 제외/자정 복구가 동작한다.
  - 전소진 시 알럿이 발생한다.
  - 동시성 하에서 쿼터 카운트가 정확하다.
- **Edge cases:** 약관상 다중키 허용 범위(위반 시 단일키+레이트 폴백 — Open Q23), 동시 차감 경합.
- **dependencies:** INGEST-E2-T1
- **effort:** M · **priority:** P0

### [INGEST-E2-T3] 어댑터: 전국공연행사정보표준데이터
- **설명:** 표준데이터에서 가요제 선별·출처 메타·증분.
- **Subtasks:**
  - 표준데이터 필드 매핑, 가요제 키워드 필터 + LLM 분류
  - 지역 표준코드 매핑, 증분(해시)
  - 지오코드 enqueue, 결측 정규화
- **Acceptance Criteria:**
  - 가요제가 선별되고 출처 메타가 보존된다.
  - 증분 수집이 동작한다.
- **Edge cases:** 축제 내 가요제 부분 포함, 코드 미매핑.
- **dependencies:** INGEST-E2-T1, INGEST-E2-T2
- **effort:** M · **priority:** P0

### [INGEST-E2-T4] 어댑터: 전국문화축제표준데이터
- **설명:** 축제 내 가요제 추출 + dedupe 통합.
- **Acceptance Criteria:** 축제 내 가요제가 추출되고 dedupe로 통합된다.
- **dependencies:** INGEST-E2-T1, INGEST-E2-T2
- **effort:** M · **priority:** P1

### [INGEST-E2-T5] 어댑터: 문화예술공연(통합) OpenAPI
- **설명:** 목록+상세 결합(포스터/요강), 레이트 폭증 방지.
- **Acceptance Criteria:** 목록+상세가 결합되어 포스터/요강을 확보하고 레이트 폭증을 방지한다.
- **dependencies:** INGEST-E2-T1, INGEST-E2-T2
- **effort:** M · **priority:** P1

### [INGEST-E2-T6] 어댑터: TourAPI(축제·행사)
- **설명:** TourAPI 가요제 매핑·지역코드 정확.
- **Acceptance Criteria:** 가요제 매핑·지역코드가 정확하다.
- **dependencies:** INGEST-E2-T1, INGEST-E2-T2
- **effort:** M · **priority:** P1

### [INGEST-E2-T7] 어댑터: 한국문화정보원 API
- **설명:** 매핑·중복통합·라이선스 1차출처 적합 검증.
- **Acceptance Criteria:** 매핑·중복통합이 되고 라이선스가 1차출처로 적합하다.
- **dependencies:** INGEST-E2-T1, INGEST-E2-T2
- **effort:** M · **priority:** P2

### [INGEST-E2-T8] 기상청 연동 사전검증(수집 측 책임)
- **설명:** contest_date·좌표 결측 시 리뷰 분류, 기상청 격자 변환 가능 좌표 정합성.
- **Acceptance Criteria:** 날짜+좌표 결측 시 needs_review로 분류된다.
- **dependencies:** INGEST-E2-T3
- **effort:** S · **priority:** P2

---

## [INGEST] E3 · eGovFrame 게시판 크롤러 (Playwright)

### [INGEST-E3-T1] Playwright 크롤링 런타임·브라우저 풀
- **설명:** 동적 게시판 안정 추출·한국 IP 전량·OOM 없음.
- **Subtasks:**
  - 브라우저 풀/컨텍스트 재사용·메모리 상한
  - idle/셀렉터 대기, 리소스 차단
  - 한국 IP 프록시 필수, 정직한 UA, 스냅샷 캡처
  - 캡차 감지 시 중단(우회 금지)
- **Acceptance Criteria:**
  - 동적 게시판을 안정 추출한다.
  - 모든 요청이 한국 IP를 경유한다.
  - OOM 없이 장시간 동작한다.
- **Edge cases:** 캡차(우회 금지·중단), 무한 스크롤 게시판.
- **dependencies:** INGEST-E1-T2, INGEST-E1-T3
- **effort:** L · **priority:** P0

### [INGEST-E3-T2] robots/ToS 준수 게이트·레이트·IP풀
- **설명:** robots 비허용 차단·1차출처만 크롤·도메인 레이트 강제·한국 IP.
- **Subtasks:**
  - robots 파서/캐시·crawl-delay
  - legal_tier=1차출처 화이트리스트 하드차단
  - 도메인 토큰버킷, 백오프, IP 회전(분산 목적)
  - ToS 변경 감지
- **Acceptance Criteria:**
  - robots 비허용 URL이 차단된다.
  - 1차 출처만 크롤되고 도메인 레이트가 강제된다.
  - 한국 IP를 경유한다.
- **Edge cases:** robots 부재 사이트, crawl-delay 미준수 방지.
- **dependencies:** INGEST-E3-T1, INGEST-E1-T4
- **effort:** L · **priority:** P0

### [INGEST-E3-T3] 공통 eGovFrame 게시판 어댑터(목록→상세→첨부)
- **설명:** 표준 게시판 무오버라이드 추출·증분·첨부 식별·키워드 조기제외.
- **Subtasks:**
  - 목록/페이지네이션/상세/첨부 파서
  - goPage()/goView() 추상화
  - 키워드 사전 필터, LLM 추출 enqueue
  - 게시번호 증분, 사이트별 오버라이드 훅
- **Acceptance Criteria:**
  - 표준 게시판을 오버라이드 없이 추출한다.
  - 공지/일반 증분, 첨부 식별, 키워드 조기 제외가 동작한다.
- **Edge cases:** 비표준 게시판 변형, 공지 고정글 중복.
- **dependencies:** INGEST-E3-T1, INGEST-E3-T2, INGEST-E1-T2
- **effort:** XL · **priority:** P0

### [INGEST-E3-T4] 사이트별 오버라이드 카탈로그·우선 타깃 온보딩 (17개 광역시도)
- **설명:** 오버라이드 스키마 + 17개 광역시도·문화재단·방송국 온보딩 + 커버리지 수치 산출.
- **Subtasks:**
  - 오버라이드 스키마(YAML/DB)
  - 광역시도 17개·주요 기초·문화재단·방송국 온보딩
  - 사이트별 골든 픽스처, 온보딩 체크리스트
- **Acceptance Criteria:**
  - 17개 광역시도가 수집되고 무배포로 설정 추가가 가능하다.
  - 골든 픽스처와 커버리지 수치가 산출된다(★PoC 핵심 산출물).
- **Edge cases:** 사이트 구조 변경, 게시판 폐쇄.
- **dependencies:** INGEST-E3-T3
- **effort:** XL · **priority:** P0

### [INGEST-E3-T5] 첨부 다운로드 파이프라인(HWP/PDF/이미지)·스토리지
- **설명:** 요강 다운로드·검증·텍스트추출 연결·sha256 dedupe·재호스팅 금지 경계·위험파일 차단.
- **Subtasks:**
  - 다운로드 잡, MIME/시그니처 검증, sha256
  - 안전 저장
  - 법무 경계(요강=저장 / 포스터·영상=링크/썸네일)
  - HWP/PDF 텍스트 추출 enqueue
- **Acceptance Criteria:**
  - 요강을 다운로드·검증하고 텍스트추출에 연결한다.
  - sha256 dedupe, 포스터/영상 재호스팅 금지, 위험파일 차단이 동작한다.
- **Edge cases:** 위조 확장자, 매크로 포함 HWP.
- **dependencies:** INGEST-E3-T3, INGEST-E1-T1
- **effort:** L · **priority:** P0

### [INGEST-E3-T6] 포스터 다운로드+썸네일 연계(수집 책임 범위)
- **설명:** 포스터 URL+출처 메타 공급·재호스팅 범위 내 썸네일.
- **Acceptance Criteria:** 포스터 URL+출처 메타를 공급하고 재호스팅 범위 내 썸네일을 생성한다.
- **dependencies:** INGEST-E3-T5, INGEST-E2-T5
- **effort:** M · **priority:** P1

---

## [INGEST] E7 · 법무·컴플라이언스·데이터 거버넌스

### [INGEST-E7-T1] 1차 출처 화이트리스트·경쟁사 가공DB 크롤 금지 가드
- **설명:** legal_tier 강제·1차 출처 검토 게이트·차단 도메인 블록리스트.
- **Subtasks:**
  - legal_tier 강제
  - 1차 출처 검토 체크리스트 게이트
  - 차단 도메인 블록리스트(★GROUNZ 등 경쟁사 가공DB 포함)
  - 분류 근거 감사
- **Acceptance Criteria:**
  - 1차 외 출처는 수집되지 않는다.
  - 법무검토 없이 활성화되지 않는다.
- **Edge cases:** 1차/2차 경계 모호(잡코리아v사람인 리스크), 화이트리스트 우회 시도.
- **법적 주의:** GROUNZ 등 경쟁사 가공DB 스크래핑은 코드 차단. 데이터는 1차 출처(공공 API·지자체)만.
- **dependencies:** INGEST-E1-T1, INGEST-E3-T2
- **effort:** S · **priority:** P0

### [INGEST-E7-T2] 재호스팅 금지·출처표기·공식 임베드 집행
- **설명:** 포스터/영상 재호스팅 0·출처표기 부착·위반 코드 차단.
- **Acceptance Criteria:**
  - 포스터/영상 재호스팅이 0이다.
  - 출처표기가 부착되고 위반이 코드로 차단된다.
- **dependencies:** INGEST-E3-T5, INGEST-E3-T6, INGEST-E1-T1
- **effort:** M · **priority:** P0

### [INGEST-E7-T3] PIPA(제보자/주최 담당자 개인정보)
- **설명:** 최소수집·동의·기간경과 자동파기·접근통제.
- **Acceptance Criteria:** 최소수집·동의·기간경과 자동파기·접근통제가 적용된다.
- **dependencies:** AUTH-E1-T1(스키마), INGEST-E7-T1
- **effort:** M · **priority:** P0

### [INGEST-E7-T4] Takedown/이의제기 워크플로
- **설명:** 접수·추적·조치·재수집 방지(SLA 48h).
- **Acceptance Criteria:** 접수·추적·조치·재수집 방지가 동작한다.
- **dependencies:** INGEST-E1-T1, INGEST-E1-T5
- **effort:** M · **priority:** P1

---

## [INGEST] E13 · 데이터 수집 백엔드 연계

### [INGEST-E13-T1] 수집 잡 오케스트레이션·스케줄·레이트(arq)
- **설명:** 소스별 cron·큐/동시성 제한·robots/약관 가드·커버리지 수치 산출.
- **Subtasks:**
  - 소스별 cron, 큐/동시성 제한
  - robots/약관 가드, ingest_runs 영속화
  - 재시도/DLQ, 커버리지 메트릭(PoC 산출물)
- **Acceptance Criteria:**
  - 스케줄 실행·robots/레이트 준수·차단 백오프가 동작한다.
  - 커버리지 수치가 산출된다.
- **Edge cases:** cron 겹침, 차단 도메인 백오프.
- **dependencies:** BE-E01-T03, BE-E11-T01
- **effort:** XL · **priority:** P0

### [INGEST-E13-T2] 정규화 파이프라인 연계(LLM·HWP/PDF·지오코딩·중복제거)
- **설명:** 수집→정규화→검수큐 자동·출처 보존·임계미달 플래그·중복 병합 연계.
- **Subtasks:**
  - LLM 추출 잡 + evals 게이트
  - HWP/PDF 파싱·포스터 썸네일 트리거
  - 지오코딩 잡, 중복제거·신뢰도
  - 장르/상태 분류·검수큐, 출처 메타 보존
- **Acceptance Criteria:**
  - 수집→정규화→검수큐가 자동 흐른다.
  - 출처 보존·임계미달 플래그·중복 병합이 연계된다.
- **Edge cases:** 추출 실패 폴백, 검수큐 적체.
- **dependencies:** INGEST-E13-T1, BE-E07-T02, BE-E10-T02
- **effort:** XL · **priority:** P0

---

## [NOTI] EPIC-NOTI-00 · Phase0 PoC: 알림·날씨 발송 가능성 검증

### [NOTI-T-00-01] FCM 웹푸시 발송 PoC(SW+VAPID/HTTP v1)
- **설명:** 데스크탑+안드+iOS PWA 실도달 증빙·OS별 매트릭스·인증방식 결정·iOS 제약 결론.
- **Subtasks:**
  - Firebase/VAPID, firebase-messaging-sw.js, 권한 흐름
  - FCM HTTP v1 send
  - OS/브라우저 도달률·지연 측정
  - iOS standalone 제약 검증, 토큰 회전 처리
- **Acceptance Criteria:**
  - 데스크탑/안드/iOS PWA 실도달이 증빙된다.
  - OS별 매트릭스·인증방식 결정·iOS 제약 결론이 도출된다.
- **Edge cases:** iOS 미설치 시 미지원, 토큰 무효화.
- **dependencies:** 없음 · FE 연계: NOTI-T-02-01(SW UX)
- **effort:** M · **priority:** P0

### [NOTI-T-00-02] 기상청 단기/중기 API PoC(좌표·날짜 매핑)
- **설명:** 좌표3개·D-14~D-0 필드/정밀도 표·격자/중기코드 정확·단중기 경계 결론·코드 매핑표.
- **Subtasks:**
  - 단기(VilageFcst)/초단기/중기육상/중기기온/특보 키 발급
  - LCC DFS 격자 변환, 중기 regId 매핑
  - baseDate/baseTime 산출
  - PTY/POP/TMP/SKY 정규화, 쿼터/인코딩
- **Acceptance Criteria:**
  - 좌표3·D-14~D-0 필드/정밀도 표가 산출된다.
  - 격자/중기코드가 정확하고 단·중기 경계 결론·코드 매핑표가 도출된다.
- **Edge cases:** 중기예보 미가용 구간, 격자 경계.
- **dependencies:** 없음
- **effort:** M · **priority:** P0

### [NOTI-T-00-03] 카카오 알림톡 사전조사 + 이메일 발송 PoC
- **설명:** 알림톡 도입 체크리스트·이메일 실발송+SPF/DKIM·Phase별 채널순서 결론.
- **Subtasks:**
  - 알림톡 경로(직접 vs NHN/알리고/솔라피) 비교
  - 템플릿 정책, SMS 대체
  - SES(서울)/Outbound Mailer 선정, DKIM/DMARC
  - 스팸 점수 측정
- **Acceptance Criteria:**
  - 알림톡 도입 체크리스트가 작성된다.
  - 이메일 실발송 + SPF/DKIM이 검증되고 Phase별 채널순서 결론이 도출된다.
- **Edge cases:** 알림톡 템플릿 심사 리드타임(Open Q24), 네이버/다음 도달률.
- **dependencies:** 없음
- **effort:** S · **priority:** P1

### [NOTI-T-00-04] 알림·날씨 ADR + 데이터모델 초안
- **설명:** ADR 3건+·ERD 후속 매핑·Phase1/2 경계 확정.
- **Subtasks:**
  - 채널 추상화·알림유형 enum·스케줄링/멱등·quiet hours·날씨 갱신윈도우 결정
  - PG/Redis 데이터모델 초안
- **Acceptance Criteria:**
  - ADR 3건 이상이 작성된다.
  - ERD 후속 매핑·Phase1/2 경계가 확정된다.
- **dependencies:** NOTI-T-00-01, NOTI-T-00-02, NOTI-T-00-03
- **effort:** S · **priority:** P0
