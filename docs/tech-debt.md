# Tech Debt — v2 파이프라인 후속 작업

이 파일은 **즉시 수정하지 않고 한 번에 몰아서 처리할 항목**을 모아둔다.
독립 감사(2026-05-12)와 후속 PR 진행 중 발견된 사항들이다.

상태값: 🔴 High / 🟡 Medium / 🟢 Low / ✅ Resolved

---

## ✅ Resolved — auth 외부 의존성 제거 (PR5 에서 해결)

이전 `user_repository.py` 가 createUser / getUserByEmail / updateUser / deleteUser
를 외부 워크플로우로 위임 → 외부 tunnel 불안정성 + 파이프라인 일관성 결여 문제.

**PR5 (`feat/auth-direct-neo4j`)** 에서 `app.clients.neo4j_client` 직접 호출로 교체.
Cypher 는 기존 동작과 byte-equivalent — 기존 데이터 무중단 호환.

운영 의미: 백엔드가 외부 tunnel 의존성 없이 인증 처리. 더 이상 tunnel
flicker 가 502 의 원인이 되지 않음.

---

## 🔴 High — Cypher Injection via LLM-controlled label/type

**위치**: [app/pipelines/cps_pipeline.py:176](../app/pipelines/cps_pipeline.py)
`build_save_cps_query` 의 `MERGE (n{idx}:{label} ...)` 와 `MERGE (s)-[r:{rtype}]->(t)`.

`label` / `rtype` 가 LLM JSON 에서 sanitize 없이 Cypher 에 직접 보간된다.
미팅로그에 프롬프트 인젝션이 섞이면 `Foo) DETACH DELETE n0 //` 같은 라벨로
임의 Cypher 실행 가능 — Neo4j 전 데이터 삭제까지 가능한 수준.

**원인**: `Save CPS Code` 단계의 충실 포팅으로 인해 그대로 옮겨옴.

**해결안**:
- `label` / `rtype` 를 `^[A-Za-z_][A-Za-z0-9_]*$` whitelist regex 로 강제.
- 위반 시 해당 노드/관계 skip + warning 로그.
- PRD 파이프라인(`build_save_prd_query`)에도 동일한 검증 필요.

**영향 범위**: 향후 추가되는 모든 그래프-쓰기 Cypher 빌더에 공통 헬퍼로 빼야 함.

---

## 🟡 Medium — PR description 에 stage reorder 가 명시 안 됨

**위치**: [PR #1](https://github.com/myoungsuboh/harness-server/pull/1), [app/pipelines/cps_pipeline.py:550-562](../app/pipelines/cps_pipeline.py)

기존 동작은 `Save Meeting Log → Save CPS` 순서. Python 포팅은
`Save CPS → Save Meeting Log` 로 reorder 했다.

이유: 기존 순서에선 첫 실행 때 `MATCH (doc:CPS_Document)` 가 실패해서
`EXTRACTED_FROM` 관계가 안 만들어졌음. doc 을 먼저 생성하도록 순서 변경.

**버그 픽스이지만 "1:1 충실 포팅"에서 일탈한 부분** — PR description 에
명시되어 있지 않다. 후속 PR 머지 전에 PR #1 description 에 추가하거나,
별도 PR 로 `docs/v2_pipelines.md` 에 명시.

---

## 🟡 Medium — `?wait=true` 동기 모드가 운영 환경에서도 허용됨

**위치**: [app/api/v2_routes.py:93-123](../app/api/v2_routes.py)

`?wait=true` 면 큐를 우회하고 FastAPI 워커 안에서 CPS 파이프라인 (30-120 초)
을 직접 실행한다. Fly.io 무료 티어 `web=1` 환경에서 호출되면 다른 요청
전부 블로킹됨.

주석엔 "디버깅용" 이라 명시되어 있지만 인증된 사용자면 누구나 호출 가능.

**해결안**: `if not settings.is_production` 가드 또는 admin-only 권한 체크.

---

## 🟡 Medium — 워커 메모리 부족 가능성

**위치**: [docker-compose.yml](../docker-compose.yml), [app/queue/worker.py:37](../app/queue/worker.py)

현재 backend 컨테이너 하나에 FastAPI + arq 워커가 함께 떠 있음. neo4j async driver +
httpx + Gemini 응답 (수십 KB) × `ARQ_MAX_JOBS` 동시 = 4GB 호스트에서 OOM 위험.

**해결안 (택1)**:
- `docker-compose.yml` 에 별도 worker 서비스 분리 (entrypoint 를 `arq app.queue.worker.WorkerSettings` 로).
- `ARQ_MAX_JOBS=1` 로 보수적 시작 후 메트릭 보고 늘리기.
- Neo4j 힙/페이지캐시 메모리 한도 조정 (현재 합산 ~1.5GB 점유).

---

## 🟢 Low — `job.result` 실패를 swallow

**위치**: [app/queue/client.py:107-112](../app/queue/client.py)

job 이 `complete` 상태인데 result fetch 가 실패하면 `error` 만 채우고
status 는 그대로 `complete` 반환. 클라이언트가 모호한 상태 받음.

**해결안**: result fetch 실패 시 `status='failed'` 로 명시.

---

## 🟢 Low — CORS + credentials 위험 조합

**위치**: [app/api/main.py:65-71](../app/api/main.py), [app/core/config.py:51](../app/core/config.py)

`allow_credentials=True` + `CORS_ORIGINS=*` 입력 시 `["*"]` 그대로 반환.
브라우저가 거부하지만 운영에서 실수 방지용으로 settings 에서 명시적 reject.

**해결안**: `cors_origins_list` 에서 `["*"]` + `allow_credentials=True` 조합 시 ValueError.

---

## ✅ Resolved — legacy passthrough 라우트 제거

이전 `passthrough_routes.py` 는 외부 워크플로우로 투명 프록시하던 무인증 라우트.
클라이언트가 v2 로 완전 이전하면서 모듈 자체를 삭제 — 무인증 노출 표면 제거 완료.

---

## 📌 다음에 추가될 항목

PR3 이후 작업 중 새로 발견된 항목은 아래에 시간순으로 누적한다.

<!-- NEW_ITEMS_BELOW -->

---

## 🟡 Medium — PR8 Lineage: name variants regex 동작 메모

**위치**: [app/pipelines/lineage_pipeline.py:_name_variants](../app/pipelines/lineage_pipeline.py)

기존 `Build Lineage Context.matchByName` 의 JS 구현은:
```js
const lower = normalized.toLowerCase();
variants.add(lower.replace(/([a-z])([A-Z])/g, '$1-$2').toLowerCase());  // lower 에 regex 적용
```
`lower` 가 이미 소문자라 `[A-Z]` 가 매칭 안 됨 → **사실상 noop**. PascalCase →
kebab-case/snake-case 변형이 의도는 있었으나 작동 안 함.

Python 포팅은 `name` (원본 case) 에 regex 적용 → 의도대로 동작:
- `"ToolApplication"` → `{"toolapplication", "tool-application", "tool_application"}`

**영향**: 이전 기준 매칭과 결과 차이.
- 이전 구현에선 `ToolApplication.java` 가 `tool-application.java` 파일과 매칭 안 됨
- Python 에선 매칭됨 → 더 많은 implementations 발견 → missingImpl 감소

**의도된 동작이라고 판단**: 사용자의 시스템에서 lineage 매칭은 더 정확할수록
좋으므로 Python 의 동작이 더 바람직. 단, 이전 결과와 byte-equivalent 보장 불가.

**해결 옵션**:
- 그대로 유지 (현재) — 더 정확한 매칭
- 이전 동작 복원하려면 `_name_variants` 에서 `name` 대신 `lower` 사용

---

## 🟢 Low — PR8 Lineage: GitHub rate limit 위험

**위치**: [app/clients/github_client.py:fetch_repo_trees_bulk](../app/clients/github_client.py)

GitHub API 무인증 호출은 IP 당 60 req/hr. analyzeLineage 한 번에 repo 당 2 호출
(get_repo + get_tree) 발생. 프로젝트에 repo 5개 등록 + 10명 동시 분석 = 100 호출.

**해결안**:
- `GITHUB_TOKEN` env 설정 권장 (5,000 req/hr)
- 또는 결과 캐싱 (default_branch + tree 를 짧은 TTL 로)

---

## 🟢 Low — PR8 Lineage: 매칭이 sequential

**위치**: [app/clients/github_client.py:fetch_repo_trees_bulk](../app/clients/github_client.py)

여러 repo 를 순차 fetch. 5 repos × ~2초 = 10초 지연. asyncio.gather 로 병렬화하면
2초 이내 가능. 다만 GitHub rate limit 와 충돌할 수 있어 순차가 안전.

**해결안**: rate limit 여유 확인 후 `asyncio.gather` 도입.

---

## 🟢 Low — PR8 Lineage: 단/복수 자동 변환 없음

**위치**: [app/pipelines/lineage_pipeline.py:_match_by_name](../app/pipelines/lineage_pipeline.py)

`tickets` (복수) 와 `Ticket` (단수) 가 서로 매칭 안 됨 — 현재 매칭 알고리즘의 한계.
영문 stemming 라이브러리(예: nltk) 도입 시 개선 가능하지만 의존성 비용 큼.

---

## 🟢 Low — PRD Section Filter1 기본 후보가 실제 마스터 헤더와 매칭 실패

**위치**: [app/pipelines/prd_pipeline.py](../app/pipelines/prd_pipeline.py) `_PRD_DEFAULT_CANDIDATES`

PRD 마스터의 실제 섹션명은 `### 2. Epic & User Story Map (기능 계층도)` 인데
기본 후보는 `'Epic & Story Map'` 으로 정의되어 있어 substring 매칭이 안 됨
(`'Epic & User Story Map'` 안에 `'Epic & Story Map'` 이 substring 으로 존재하지 않음
— 가운데 'User' 가 끼어 있어서).

결과: impact JSON 이 비어있을 때 Epic 섹션이 영향 섹션으로 잡히지 않음.
`PRD Section Filter1` 단계의 현재 동작이 이렇지만,
실 운영에서 점진적 영향 분석이 부정확해질 수 있음.

**해결안**:
- `_PRD_DEFAULT_CANDIDATES = ['Epic', 'Screen Architecture']` 로 더 짧게 → 양방향 substring 매칭 가능.
- 또는 `_PRD_FALLBACK_KEYS` 로직을 unconditional 로 변경 (default 매칭 이후에도 누락된 키 보충).

**발견 시점**: PR3 작업 중 (2026-05-12). 테스트는 현재 동작을 그대로 검증하도록 작성됨.
