# BE 이식 가이드 — harness-server → gayoje-server

> `harness-server`(FastAPI+Neo4j+Redis+arq+LiteLLM)는 gayoje-server와 **스택이 사실상 동일**하다.
> 인프라·인증·LLM·큐·관측 골격을 **바닥부터 짜지 말고 이식**한다. 아래는 실제 코드 스캔 기반 부품 목록이다.
> 판정: **copy**(거의 그대로) · **adapt**(가요제 도메인에 맞게 수정) · **pattern**(구조만 참고) · **skip**(harness 전용).
> ⚠️ harness 로컬 경로 기준(`C:\project\harness-server\...`). 도메인 로직(CPS/PRD/SPACK/DDD/Lint/회의록)은 전부 버린다.

---

## 1. 그대로 복사 (copy-as-is) — 즉시 시간 절약

| 파일 | 역할 | 절약 | 가요제 매핑 |
|---|---|:--:|---|
| `app/clients/neo4j_client.py` | Neo4j 드라이버·세션·트랜잭션·재시도(`run_cypher`/`run_in_transaction`) | L | BE 부트스트랩, DATA |
| `app/core/prompt_render.py` | 프롬프트 인젝션 방어 렌더러(`<<key>>` 단일패스 치환 + untrusted delimiter 무력화). **의존성 0** | M | INGEST(공고 본문 삽입), DETAIL(신청서 삽입) |
| `app/core/token_blacklist.py` | 토큰 블랙리스트(arq Redis 재사용) | M | AUTH |
| `app/core/session_registry.py` · `service/session_helper.py` | 세션 레지스트리·헬퍼 | M | AUTH |
| `app/core/token_encryption.py` | 토큰/민감필드 암호화 | M | AUTH, PAY, DATA |
| `app/core/limiter.py` | rate limiter(email>IP 키) | S | AUTH, 부트스트랩 |
| `app/core/request_context.py` | 요청 컨텍스트(request-id 등) | M | 부트스트랩 |
| `app/core/observability.py` | 관측(로깅·trace) 셋업 | L | 부트스트랩 |
| `app/core/body_size_limit.py` | 바디 크기 제한 미들웨어 | S | 부트스트랩, INGEST(업로드) |
| `app/core/metrics.py` | 메트릭(fetch/LLM토큰/큐/DLQ) | M | INGEST-E1-T5, COM-OBS |
| `Dockerfile` · `run.py` · `app/__init__.py` · `pytest.ini` | 부트스트랩 | S | 부트스트랩 |
| `Caddyfile` | 리버스프록시(Let's Encrypt 자동) | M | 배포 |

## 2. 수정해서 이식 (adapt) — 핵심 부품

| 파일 | 역할 | 절약 | adapt 포인트 |
|---|---|:--:|---|
| **`app/clients/gemini_client.py`** | **LLM 레이어 핵심.** httpx async 래퍼: LiteLLM proxy(멀티키 로테이션·429 재시도) ↔ 단일키 자동폴백, structured output(response_schema), 지수백오프, TokenUsage 추출, 에러 분류(quota/auth/transient), 안전필터 완화, flash-lite 공백폭주 회피 | **XL** | 도메인 문구 교체: json_schema name `'harness_response'`(L851), `'회의록'` 문구(L364). `GeminiClient→LLMClient` 리네임 권장 |
| `litellm/config.yaml` | 멀티키(3키) simple-shuffle 라운드로빈 → 무료티어 RPM 회피, 429 1분 cooldown, drop_params | L | `model_name`·`GEMINI_API_KEY_*` env명 교체. **타임아웃 정합 주석 유지(아래 gotcha)** |
| `app/pipelines/base.py` | JSON 추출/재시도 유틸: `generate_json_with_retry`, `extract_json_object`(선형 find/rfind), `strip_code_blocks`(선형), `canonicalize_*`, `PipelineContext` | L | Cypher/그래프 유틸(`canonicalize_graph`/`escape_cypher_string` 등)·CPS/PRD placeholder 정리는 **버리고 JSON/텍스트 유틸만 추출** |
| `app/core/output_language.py` | ko/en/ja/zh 출력 언어 강제(prepend+reminder 샌드위치, ko=무주입, BCP47 흡수). **gayoje i18n과 정확히 일치** | L | PRD/CPS 섹션헤더·마커 보존 규칙 삭제, 가요제 보존 토큰만. 골격은 그대로 |
| `app/queue/settings.py` · `worker.py` · `client.py` | **arq 큐/워커.** 토폴로지·재시도/백오프·fan-out·멱등·백프레셔·메트릭 | **XL** | 도메인 잡 본문(cps/prd/design/lint job) 전량 버림. **잡 인프라/오케스트레이션만.** ⚠️DLQ는 harness에 없음 → 신규 구현 |
| `app/core/concurrency.py` | 소스별 큐/동시성 제한·차단 도메인 백오프 | L | `HEAVY_JOBS`/`MASTER_WRITE_JOBS` frozenset을 **gayoje 잡 이름으로 전면 재정의**(안 하면 게이트 무력) |
| `app/core/master_lock.py` | 분산 락(직렬화) | L | DATA-DEDUP 병합·INGEST 정규화 동시쓰기·PAY/BIZ 정산 멱등 |
| `app/core/security.py` · `service/auth_service.py` · `api/auth_routes.py` | JWT 인증·로그인·리프레시 로테이션 | L | **User SOR을 Neo4j→PostgreSQL로**, Google/GitHub→**카카오/네이버** provider 분기 추가 |
| `app/core/google_oauth.py` | OAuth state·콜백 패턴 | M | 카카오/네이버 OAuth로 포팅(state secret·PKCE 패턴 재사용) |
| `app/core/email.py` | 이메일 발송 | M | NOTI. 하드코딩 `kaki3010@naver.com` 교체 |
| **`app/core/config.py`** | pydantic-settings + **운영 부팅 fail-fast 검증**(JWT placeholder/길이, CORS '*' 거부, 암호화키 강제) | XL | Paddle/Notion/GitHub/Gemini등급 설정 블록 → **토스·카카오맵·FCM·오브젝트스토리지**로 교체. fail-fast 검증·`Optional+호출시점 실패`·int env `:-default` 패턴은 유지 |
| `docker-compose.yml` | api/worker/scheduler 컨테이너 분리·오케스트레이션 | XL | 운영 IP/도메인/external network 교체. int env `:-default` 패턴 유지(Portainer 사고) |
| `.github/workflows/test.yml` | CI 테스트 | M | repo owner 교체 |

## 3. 패턴만 참고 (pattern)

| 파일 | 가져올 패턴 |
|---|---|
| `evals/scorer.py` · `run_eval.py` | 데이터 품질 **점수화 엔진 구조**(metric 정의는 가요제 기준으로 새로) |
| `app/queue/extract_cache.py` | LLM 추출 결과 **캐시·single-flight**(중복 호출/썸네일 폭주 방지) |
| `app/core/quota.py` | 무료↔유료 **게이팅 구조**(한도 숫자는 버림) → PAY |
| `app/service/usage_repository.py` | 사용량 집계 → PAY |
| `app/api/_quota_helpers.py` | LLM 클라 **라이프사이클**(요청당 생성·정리, 워커 startup 풀) |
| `app/api/main.py` | 미들웨어 **등록 순서**(LIFO — CORS 최내측) |
| `app/queue/status_guard.py` | 잡 상태 조회 **IDOR/소유권 가드** |
| `app/service/*_repository.py`(notice/inquiry/audit) | Neo4j 레포지토리 패턴(i18n JSON 직렬화 포함) |

## 4. 삭제할 것 → `STRIP_PLAN.md` 참조

전체 카피 방식이라 '안 가져옴'이 아니라 '지움'이다. **실제 파일 경로 기준 정확한 삭제 목록 + `git rm` 블록**은 같은 폴더의 **`STRIP_PLAN.md`** 에 정리했다(prompts·도메인 pipelines·도메인 repos·mcp·skill/lineage·paddle/구독 등).

## 5. 이식 시 반드시 바꿀 것 (보안·인프라)

- 🔑 **시크릿**: 전부 env 참조라 하드코딩 없음(확인). 그래도 실값은 **Portainer/.env**로만. `config.py` JWT default `'change-me-...'`는 placeholder(운영 거부 로직 동봉) — 운영에 그대로 두지 말 것
- 🌐 **인프라 식별자 제거**: 운영 IP `158.247.196.111`(Vultr), 도메인 `api.harness-system.com`/`app.harness.so`, deploy.yml repo owner `myoungsuboh`, `kaki3010@naver.com` → 전부 gayoje 값으로
- 🔁 **OAuth/결제 교체**: Google/GitHub → **카카오/네이버**, Paddle → **토스 결제위젯**
- 🗄️ **SOR 전환**: harness는 User를 Neo4j에 저장 → gayoje는 **PostgreSQL이 사용자/결제 주력**, Neo4j는 관계(아티스트-곡-팀-출연) 보조

## 6. 🎁 공짜로 얻는 운영 사고 교훈 (이게 진짜 보물)

1. **LLM 타임아웃 정합**: 클라이언트 timeout > `request_timeout × (num_retries+1)`. 어기면 진행중 생성을 끊고 **중복요청 증폭**(실사고). gayoje LLM 호출에 그대로 적용
2. **flash-lite structured output 버그**: schema 강제 시 빈 공백을 한도까지 폭주(실측 52만자). `_schema_unsupported`로 회피
3. **정규식 O(n²) 동결**: code-fence 제거를 정규식으로 하면 대용량 출력에서 워커 **이벤트루프 전체 동결**(arq timeout조차 발화 불가) → 선형(find/rfind/rstrip) 버전 필수
4. **Gemini 안전필터 오탐**: 정상 텍스트를 content_filter로 빈응답 → `BLOCK_NONE` 완화. 단 **가요제 도메인(공연/팬 콘텐츠)은 재검토**
5. **Neo4j는 중첩 dict 저장 불가** → **i18n 본문(ko/en/ja/zh)은 JSON 문자열로 직렬화 후 저장**, 읽을 때 `_decode_i18n`. 가요제 다국어 콘텐츠 전반에 적용
6. **refresh token rotation**: 1회 사용 즉시 blacklist(탈취 감지). FE가 새 refresh를 저장해야 정상
7. **fail-fast 부팅**: JWT placeholder/길이·CORS '*'·암호화키 미설정 거부 — 그대로 이식 권장
8. **⚠️ harness엔 DLQ·cron이 없다** → gayoje는 **DLQ(재처리)와 스케줄러(KST cron: D-day·날씨·정기결제·정산·수집)를 신규 구현** 해야 함(INGEST-E1-T3/E13)
9. **docker-compose int env `:-default`**: Portainer가 stack env를 빈문자열로 떨궈 pydantic crash 난 실사고 대응 — 유지
10. **미들웨어 LIFO 순서**: BodySize→RequestId→Metrics→SlowAPI→CORS 순 add(CORS 최내측)

---
*출처: harness-server 8-서브시스템 코드 스캔. 가요제 작업리스트(`singaservertasklist/`)와 함께 이 가이드를 빌드 세션에 제공할 것.*
