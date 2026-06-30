# v2 Pipelines

백엔드 내부에서 동작하는 파이프라인 일체.

## 왜?

이전 외부 워크플로우 정의 (323KB 단일 JSON) 은 git diff·테스트·리팩터링이 불가능했다.
파이프라인 로직을 Python 코드로 옮겨 다음을 확보한다:

- **회귀 추적** — 프롬프트 변경이 PR diff 에 잡힘.
- **단위 테스트** — Gemini/Neo4j fake 로 결정적 검증.
- **단일 SPOF 감소** — 외부 의존성 제거.
- **인증/멀티테넌트 제어 가능** — `/api/v2/*` 는 `get_current_user` 강제.

## 현재 범위 (PR1 ~ PR11) — **외부 의존성 제거 완료**

**파이프라인**:
- ✅ PR1: `postMeeting → CPS` 슬라이스
- ✅ PR2: arq + Redis 큐 도입
- ✅ PR3: `postMeeting → PRD` 슬라이스 추가 — postMeeting 전체 흐름 완성
- ✅ PR4: `createDesign → Spack/DDD/Architecture` 슬라이스 추가
- ✅ PR5: 인증의 외부 의존 제거 — `user_repository` 가 Neo4j 직접 호출
- ✅ PR6: Rule Generator 슬라이스 — Skill CRUD 5개 + `recommendSkillsByAI` LLM 파이프라인
- ✅ PR7: Lint 슬라이스 — `runLint` + `generateFixSpec` + Repo CRUD
- ✅ PR8: Lineage 슬라이스 — `analyzeLineage` (deterministic 매칭) + `getLastLineage`
- ✅ PR9: 조회 라우트 — getCPS / getPRD / getDDD / getSpack / getArchitecture / getMeetingLogs / getMeetingVersions
- ✅ PR10: 삭제/재빌드 — `deleteProject` + `deleteMeeting` (Master LLM 재구성)
- ✅ **PR11: createMD** — Spack/DDD/Architecture → 바이브코딩용 MD 3종 (LLM × 3 병렬)

**실행 모델**:
- **PR1**: BackgroundTasks fire-and-forget. 결과 추적 불가.
- **PR2**: arq + Redis 큐. 별도 워커 프로세스에서 처리, 상태 조회 API 제공.
- **PR3**: CPS+PRD 체이닝 job 추가 (`post_meeting_pipeline_job`).
- **PR4**: Design 파이프라인 추가 (`design_pipeline_job` — Spack→DDD→Architecture 직렬).
- **PR5**: `app/service/user_repository.py` 가 `app.clients.neo4j_client` 직접 사용. App 부팅 시 `ensure_user_constraints` 로 `User.email UNIQUE` 제약 idempotent 생성.
- **PR6**: `app/service/skill_repository.py` (Skill CRUD) + `app/pipelines/skill_recommend_pipeline.py` (LLM 추천 — CPS/PRD 직접 fetch). 큐 job `recommend_skills_job` 추가.
- **PR7**: `app/pipelines/lint_pipeline.py` + `app/pipelines/fix_spec_pipeline.py` + `app/clients/github_client.py` (GitHub REST 래퍼) + `app/service/{lint,repo}_repository.py`. 큐 job `run_lint_job`, `generate_fix_spec_job` 추가.
- **PR8**: `app/pipelines/lineage_pipeline.py` (서버측 deterministic 매칭만 — LLM 미사용) + `app/service/lineage_repository.py`. 큐 job `analyze_lineage_job` 추가. `github_client` 에 `fetch_repo_trees_bulk` 추가.
- **PR9**: `app/service/query_repository.py` (7개 read-only Cypher) + `app/api/query_routes.py` (7개 GET). 큐/LLM 없음 — 단순 조회.
- **PR10**: `app/pipelines/delete_pipeline.py` (`delete_project` 단순 + `run_delete_meeting_pipeline` 11-stage). 큐 job `delete_meeting_job` 추가. Rebuild 시 `prompts/rebuild_{cps,prd}.md` 사용.
- **PR11**: `app/pipelines/create_md_pipeline.py` (LLM × 3 `asyncio.gather` 병렬). `prompts/create_md_{spack,ddd,architecture}.md`. 큐 job `create_md_job` 추가. PR9 의 `query_repository.get_*_graph` 재활용.

## ⚠️ 이전 동작 대비 의도적 차이점

| 위치 | 차이 | 이유 |
|---|---|---|
| CPS Stage 순서 | 이전: Save Meeting Log → Save CPS. 현재: **Save CPS → Save Meeting Log** | 이전 순서에선 첫 실행 시 `MATCH (doc:CPS_Document)` 가 실패해 `EXTRACTED_FROM` 관계가 생성되지 않던 버그를 픽스. doc 노드를 먼저 만들도록 reorder. |
| CPS Impact parse-fail 기본값 | 이전: `['Problem','Solution']`. 현재: `[]` → filter 단계가 `['Problem','Solution','Pending']` fallback. | 현재 구현이 약간 더 보수적/넓게 매칭. 실 운영 영향 미미. |
| PRD Impact parse-fail 기본값 | 이전: `['Epic & Story Map','Screen Architecture']`. 현재: `[]` → filter fallback 동일. | 위와 같은 이유. |

## API

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/api/v2/pipelines/post_meeting` | **postMeeting** — CPS + PRD 체이닝 |
| `POST` | `/api/v2/pipelines/cps` | CPS 단독 (디버그 / 수동) |
| `POST` | `/api/v2/pipelines/prd` | PRD 단독 — `cps_graph` 를 직접 입력으로 받음 |
| `POST` | `/api/v2/pipelines/design` | **createDesign** — Spack/DDD/Architecture 생성 (PRD 마스터 필요) |
| `POST` | `/api/v2/pipelines/recommend_skills` | **recommendSkillsByAI** — CPS/PRD 기반 카탈로그 추천 (PR6) |
| `POST` | `/api/v2/pipelines/lint` | **runLint** — Spack/DDD/Arch/Skill 명세 대비 GitHub 코드 적용률 분석 (PR7) |
| `POST` | `/api/v2/pipelines/generate_fix_spec` | **generateFixSpec** — Lint 실패 항목 → 한국어 마크다운 수정 지시서 (PR7) |
| `GET`  | `/api/v2/pipelines/lint/last` | **getLastLintResult** — 가장 최근 Lint 결과 조회 (PR7) |
| `POST` | `/api/v2/pipelines/lineage` | **analyzeLineage** — 산출물 ↔ GitHub 파일 deterministic 매칭 (PR8) |
| `GET`  | `/api/v2/pipelines/lineage/last` | **getLastLineage** — 가장 최근 Lineage 결과 (PR8) |
| `?wait=true` | 위 9개 라우트 공통 | 동기 실행 (큐 미사용, 디버그용) |
| `GET`  | `/api/v2/pipelines/status/{task_id}` | 작업 상태 + 결과 조회 (모든 파이프라인 공용) |
| `GET`  | `/api/v2/pipelines/cps/status/{task_id}` | 위와 동일 — PR1 호환용 legacy 경로 |

### Skill CRUD (PR6 — Rule Generator)

| Method | Path | 엔드포인트 |
|---|---|---|
| `POST`   | `/api/v2/skills`                       | postSkill (bulk upsert, ArchService.tech_stack 매칭으로 자동 GOVERNED_BY) |
| `GET`    | `/api/v2/skills?project_name=X`        | getAllSkill |
| `GET`    | `/api/v2/skills/duplicate?project_name=X&name=Y` | getDuplicateSkill |
| `GET`    | `/api/v2/skills/{id}?project_name=X`   | getSkill |
| `DELETE` | `/api/v2/skills/{id}?project_name=X`   | deleteSkill |

### Project Repo CRUD (PR7)

| Method | Path | 엔드포인트 |
|---|---|---|
| `POST`   | `/api/v2/projects/repos`              | addProjectRepo |
| `GET`    | `/api/v2/projects/repos?project_name=X` | getProjectRepos |
| `DELETE` | `/api/v2/projects/repos`              | deleteProjectRepo (body: project_name + url) |

### 조회 라우트 (PR9 — passthrough 제거 목적)

| Method | Path | 엔드포인트 |
|---|---|---|
| `GET` | `/api/v2/cps?project_name=X`                          | getCPS |
| `GET` | `/api/v2/prd?project_name=X`                          | getPRD |
| `GET` | `/api/v2/ddd?project_name=X`                          | getDDD |
| `GET` | `/api/v2/spack?project_name=X`                        | getSpack |
| `GET` | `/api/v2/architecture?project_name=X`                 | getArchitecture |
| `GET` | `/api/v2/meetings/logs?project_name=X&version=Y`      | getMeetingLogs |
| `GET` | `/api/v2/meetings/versions?project_name=X`            | getMeetingVersions |

**프론트엔드 호출**: `${BASE}/api/v2/cps?project_name=X` 같은 형태로 직접 호출.
외부 tunnel 의존성 0.

### 삭제 / 재빌드 (PR10)

| Method | Path | 엔드포인트 |
|---|---|---|
| `DELETE` | `/api/v2/projects/{project_name}`               | deleteProject (5-hop DETACH DELETE) |
| `POST`   | `/api/v2/pipelines/delete_meeting`              | deleteMeeting + Master CPS/PRD LLM 재구성 |
| `POST`   | `/api/v2/pipelines/delete_meeting?wait=true`    | 동기 실행 |
| `GET`    | `/api/v2/pipelines/delete_meeting/status/{id}`  | 비동기 결과 조회 |

**재구성 분기 로직** (`IF Has Any Deltas` 와 동등):
- 남은 CPS+PRD delta 0개 → LLM 호출 없이 "no rebuild" 메시지 반환 (Gemini 토큰 0)
- CPS delta 만 남음 → CPS 만 rebuild
- PRD delta 만 남음 → PRD 만 rebuild
- 둘 다 남음 → 둘 다 rebuild

LLM 빈 응답 시 save 건너뜀 (`_skip_save` 메커니즘과 동등).

상태 값: `queued` / `in_progress` / `complete` / `not_found` / `deferred` (arq JobStatus)

## 스테이지 ↔ code 매핑

### CPS 슬라이스
| 스테이지 | 코드 |
|---|---|
| `postMeeting` (webhook) | `POST /api/v2/pipelines/post_meeting` (전체) / `/cps` (CPS만) |
| `Save Meeting Log Code` + `... ExecuteQuery` | `build_save_meeting_log_query` + `run_cypher` |
| `CPS Agent` | `call_cps_agent` (prompts/cps_extract.md) |
| `Save CPS Code` + `... ExecuteQuery` | `build_save_cps_query` |
| `Get All CPS2` | `fetch_master_and_latest` |
| `CPS Impact Analyzer1` | `call_impact_analyzer` (prompts/cps_impact.md) |
| `CPS Section Filter1` | `filter_affected_sections` |
| `Merge CPS Agent2` | `call_merge_agent` (prompts/cps_merge.md) |
| `CPS Reassembler1` | `reassemble_master` |
| `Merge CPS Code2` + `... ExecuteQuery2` | `build_merge_master_query` |

### PRD 슬라이스 (PR3)
| 스테이지 | 코드 |
|---|---|
| `Code_CPS_Parser` | `parse_cps_for_prd` (cps_pipeline 결과 → markdown + problems) |
| `PRD Agent1` (markdown) | `call_prd_extract` (prompts/prd_extract.md) |
| `PRD Agent2` (graph JSON) | `call_prd_graph` (prompts/prd_graph.md) |
| `Save PRD Code` + `... ExecuteQuery` | `build_save_prd_query` (Save CPS Code 와 byte-identical → alias) |
| `Get All PRD2` | `fetch_prd_master_and_latest` |
| `PRD Impact Analyzer1` | `call_prd_impact_analyzer` (prompts/prd_impact.md) |
| `PRD Section Filter1` | `filter_affected_prd_sections` (CPS 와 동일 알고리즘 + PRD fallback 키워드) |
| `Merge PRD Agent2` | `call_prd_merge_agent` (prompts/prd_merge.md) |
| `PRD Reassembler1` | `reassemble_master` (CPS 헬퍼 재사용 — 알고리즘 동일) |
| `Merge PRD Code2` + `... ExecuteQuery2` | `build_merge_master_prd_query` (CPS Master 와 BASED_ON 연결 추가) |

### Design 슬라이스 (PR4)

★중요★ 원래 흐름도상 3개 Agent 가 PRD Section Extractor 에서 병렬 분기되지만,
DDD Agent 가 `$('Spack Agent').output` 을, Architecture Agent 가 `$('Spack')+$('DDD')` 를
직접 참조하기 때문에 실제로는 **strict sequential**: Spack → DDD → Architecture.
포팅 시 이 의존성을 그대로 유지.

| 스테이지 | 코드 |
|---|---|
| `createDesign` (webhook) | `POST /api/v2/pipelines/design` |
| `ExecuteQuery Get PRD` | `fetch_master_prd` |
| `PRD Section Extractor` | `extract_prd_sections` (spack_input / ddd_input / arch_input 분할) |
| `Spack Agent` | `call_spack_agent` (prompts/design_spack.md) |
| `Spack Code` + `ExecuteQuery Create Spack` | `build_save_spack_query` (Wipe-and-Redraw) |
| `DDD Agent` | `call_ddd_agent` (prompts/design_ddd.md) — Spack 결과 의존 |
| `DDD Code` + `ExecuteQuery Create DDD` | `build_save_ddd_query` (Wipe-and-Redraw) |
| `Architecture Agent` | `call_architecture_agent` (prompts/design_architecture.md) — Spack+DDD 의존 |
| `Architecture Code` + `ExecuteQuery Create Architecture` | `build_save_architecture_query` (Wipe-and-Redraw) |
| `Wait All Design Branches` | (생략 — Python 직렬 실행이라 불필요) |

### Skill / Rule Generator 슬라이스 (PR6)

| 스테이지 | 코드 |
|---|---|
| `postSkill` + `Post Skill Code` + `ExecuteQuery Post Skill` | `skill_repository.create_skills` (bulk upsert, parameterized) |
| `getSkill` + `ExecuteQuery Get Skill` | `skill_repository.get_skill` |
| `getAllSkill` + `ExecuteQuery Get All Skill` | `skill_repository.get_all_skills` |
| `deleteSkill` + `ExecuteQuery Delete Skill` | `skill_repository.delete_skill` |
| `getDuplicateSkill` + `ExecuteQuery Get Duplicate Skill` | `skill_repository.find_duplicate_skill` |
| `recommendSkillsByAI` + Prepare Input + Fetch CPS/PRD + Build Context + Skill Picker AI + Parse&Validate | `skill_recommend_pipeline.run_skill_recommend_pipeline` |

**차이점 (의도된 단순화)**: 이전 `recommendSkillsByAI` 는 CPS/PRD 를 자기 자신의
`getCPS`/`getPRD` 엔드포인트로 internal HTTP 호출하지만, Python 버전은 Neo4j 직접
쿼리(`_FETCH_CPS_PRD_CYPHER`)로 한 단계 단축. 성능 + 외부 tunnel 의존성 제거.

### Lint 슬라이스 (PR7 → 2026-05 hybrid 리팩토링)

| 스테이지 | 코드 |
|---|---|
| `runLint` + Parse Input | `lint_pipeline._parse_input` (URL → owner/repo via `github_client.parse_github_url`) |
| Get Spack / Get DDD / Get Architecture / Get Rules | `lint_pipeline._fetch_specs` (Cypher 직접) |
| Get Repo Info + GitHub Tree | `github_client.GitHubClient.{get_repo, get_tree}` (REST) |
| Select sample paths (manifest + anchor + token-matched) | `lint_pipeline._select_sample_paths` (40 파일, manifest 무조건 포함) |
| Fetch full bodies (병렬, per-file 64KB, total 400KB) | `lint_pipeline._fetch_full_bodies` |
| **Phase A — Deterministic evidence** | `lint_evidence.collect_*` 함수들 (API/Class/Context/Event/tech_stack/Policy/Rule) |
| **Phase B — LLM residual (evidence 0건 항목만)** | `lint_pipeline._residual_llm_pass` (prompts/lint_residual.md) |
| Hallucination 차단 | `lint_pipeline._apply_residual_verdicts` (LLM 인용 file:line 을 sample 에서 검증) |
| Convergence + Score | `lint_pipeline._compute_score` (4 카테고리 가중평균, 각 25%) |
| Prepare Lint Save + Save LintResult | `lint_repository.save_lint_result` (cases 는 base64 유지) |
| `getLastLintResult` 전체 | `lint_repository.get_last_lint_result` (cases base64 decode) |
| `generateFixSpec` + Parse Failures | `fix_spec_pipeline._parse_failures` (실패 0 → early return 100%) |
| Get Full Spec | `fix_spec_pipeline._fetch_full_spec` (Skill 을 Rule 의미로) |
| Fix Spec AI Agent + Format Fix Spec | `fix_spec_pipeline.call_fix_spec_agent` + `_format` (prompts/fix_spec.md) |
| `addProjectRepo` / `getProjectRepos` / `deleteProjectRepo` | `repo_repository.{add_repo, get_repos, delete_repo}` |

**2026-05 리팩토링 — evidence-first hybrid**:
- **기존**: 12 files × 3.5KB head 만 보고 LLM 이 4 카테고리 평가 → 결정성 0, hallucination 다수.
- **현재**: spec 항목별로 코드 grep 으로 evidence 수집 (FastAPI/Express/Spring/Django/Vue/React Router/Manifest)
  → evidence 1건 이상이면 `applied=true` 즉시 확정, LLM 호출 생략.
  → evidence 0건 항목만 LLM 에게 sample 본문과 함께 검증 요청 (residual pass).
  → LLM 이 인용한 `evidence_file:line` 이 실제 sample 에 존재 + 해당 줄 비어있지 않을 때만 `applied=true` 인정 (hallucination 차단).
- 응답 schema 확장: `LintCaseRule.evidence: [{file, line, snippet, kind}]` + `detection_method: 'deterministic'|'llm'|'fallback'`.
- 결과: 같은 입력 같은 출력 (Phase A 만으로 매칭된 항목은 LLM 비결정성 0). 토큰 비용 ↓ (LLM 호출 항목 수 자체가 줄어듦).
- Legacy `_normalize_result` 함수는 backward compat 목적으로 유지.

**중요한 의미 매핑 (의도된 차이)**:
- 이전 `runLint` 와 `generateFixSpec` 은 `MATCH (r:Rule)` 로 Rule 라벨 노드를 쿼리했지만,
  Rule Generator 는 `:Skill` 노드만 생성. Lint 는 Skill 을 'rules' 의미로 사용
  (사용자 재정의 "Lint = Skill 적용률 평가" 와 일치).
- Skill → Rule schema 매핑: `description=scope`, `category=severity=priority`, `pattern=""`.

### Lineage 슬라이스 (PR8)

| 스테이지 | 코드 |
|---|---|
| `analyzeLineage` (webhook) + Parse Lineage Input | `LineageInput` 검증 |
| Get Stories/Aggregates/APIs/Services/Repos | `lineage_pipeline._fetch_artifacts_and_repos` (5개 Cypher) |
| Fetch All Repo Trees | `github_client.fetch_repo_trees_bulk` (REST, repo 별 독립 실패 허용) |
| Build Lineage Context | `lineage_pipeline._build_lineage_result` (서버측 deterministic 매칭) |
| `Lineage AI Agent` | **SKIP** — 서버 매칭이 이미 deterministic + 정확. LLM 호출은 환각 위험만 추가. |
| Normalize Lineage | (내장 — `verified=True` 보장은 서버 매칭이라 자동) |
| Prepare Lineage Save + Save Lineage Neo4j | `lineage_repository.save_lineage_result` (dataB64 base64 유지) |
| `getLastLineage` 전체 | `lineage_repository.get_last_lineage` (dataB64 decode) |

**매칭 알고리즘 (`Build Lineage Context` 와 byte-equivalent)**:
- name 변형: PascalCase / snake_case / kebab-case 모두 시도
- confidence 3단계:
  - **high**: 파일명 정확 일치 또는 시작/끝 (e.g. `Ticket.java`, `TicketController.java`)
  - **medium**: 파일명 부분 매칭 또는 폴더명 일치 (variant length ≥ 4)
  - **low**: 경로 어딘가에 포함 (variant length ≥ 5)
- API endpoint segment 키워드로 추가 매칭 (`api`, `v\d+`, 짧은 segment 제외)
- Service name 은 stopword 제외 후 단어별 매칭 (`Service`, `API`, `Module` 등 제외)

**의도된 단순화**:
이전 워크플로우는 서버측 매칭 후 추가로 LLM agent 를 거쳤지만, 프롬프트가
"fileTree 에 실제 존재하는 경로만" 을 강제하므로 사실상 서버 매칭 결과를
재확인하는 noop. PR8 은 LLM 호출 제거 — 토큰 0, 결정성 100%.

## 실행

### 로컬

```bash
# 1) Redis 띄우기 (Docker)
docker run -d --name redis -p 6379:6379 redis:7-alpine

# 2) 의존성
pip install -r requirements-dev.txt

# 3) 환경변수 (.env 또는 export)
export GEMINI_API_KEY=... NEO4J_URI=... NEO4J_PASSWORD=...
export REDIS_URL=redis://localhost:6379/0

# 4) 워커 실행 (별도 터미널)
arq app.queue.worker.WorkerSettings

# 5) 웹 실행
uvicorn app.api.main:app --reload
```

### 호출 예시

```bash
# enqueue
curl -X POST http://localhost:8000/api/v2/pipelines/cps \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"project_name":"test","version":"v1.1","date":"2026-05-12","meeting_content":"..."}'
# → {"status":"accepted","task_id":"<uuid>"}

# 폴링
curl http://localhost:8000/api/v2/pipelines/cps/status/<uuid> \
  -H "Authorization: Bearer <jwt>"
# → {"task_id":"<uuid>","status":"complete","result":{...}}
```

## 배포 (Vultr + Docker Compose)

현재 운영 구성은 단일 Docker Compose 스택. backend 컨테이너 안에서 FastAPI(웹) 와
큐 워커가 같은 프로세스에 떠 있다 (`run.py` 부팅 시 등록). Redis 는 같은 compose 의 별도 서비스.

부하 분리가 필요해지면 `docker-compose.yml` 에 별도 worker 서비스를 추가:

```yaml
worker:
  build: .
  command: ["arq", "app.queue.worker.WorkerSettings"]
  env_file: .env
  depends_on: [redis]
```

배포 흐름은 README.md 의 "배포 (자동)" 섹션 참고 — master push → GitHub Actions → SSH → `docker compose up -d --build`.

## 테스트

```bash
# 단위 + e2e fakes (외부 의존 0) — CI 에서 항상 실행
pytest

# 실제 Gemini + Neo4j 통합 (자격증명 필요)
RUN_INTEGRATION=1 GEMINI_API_KEY=... NEO4J_URI=... NEO4J_PASSWORD=... \
  pytest tests/pipelines/test_cps_integration.py -v
```

## 마이그레이션 전략

1. ✅ **PR1**: 신/구 라우트 병행. 동기 BackgroundTasks.
2. ✅ **PR2**: 큐 도입. enqueue + status 폴링.
3. ✅ **PR3**: PRD 파이프라인 추가. `post_meeting` 으로 postMeeting 전체 흐름 완성.
4. ✅ **검증**: 동일 미팅로그로 v2 결과를 골든 fixture 와 수동 비교.
5. ✅ **전환**: 프론트가 `/api/v2/*` 직접 호출로 전환.
6. ✅ **확장**: Design (Spack/DDD/Architecture), Rule Generator, Lint, Lineage 파이프라인을 같은 패턴으로 추가.
7. ✅ **제거**: 외부 워크플로우 비활성화 → legacy passthrough route 삭제.

## 후속 작업 (Tech Debt)

`docs/tech-debt.md` 참조. 우선순위 정리:
- 🔴 Cypher injection (LLM-controlled label/type) — PRD 파이프라인에도 동일하게 존재
- 🟡 `?wait=true` 운영 환경 차단
- 🟡 워커 메모리 / max_jobs 튜닝
