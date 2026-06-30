# Vibe Coding Entry — Phase 1 Design

작성: 2026-05-26 · scope = A (Vibe Coding entry) / S2 (CPS+PRD 검토) / D2 (균형 샘플링)

## 1. Why — 문제 정의

현재 시스템은 미팅 로그가 없으면 사용 가치가 없다. 신규 사용자가 시스템 첫 진입 시 "회의록을 업로드하세요"라는 진입 장벽이 크며, 특히 Cursor / Claude Code / Cline 등 **Vibe Coding 도구 사용자**(2026 폭증 중) 는 회의를 안 하므로 진입 자체가 불가능하다.

해결: GitHub repo URL 한 줄만 입력하면 AI 가 코드를 분석해 V1 "프로젝트 설명" 자동 생성 → 기존 CPS / PRD pipeline 자연 합류.

## 2. Goals & Non-Goals

**Goals**
- 신규 사용자가 회의록 없이 GitHub URL 만으로 시스템 시작 가능
- 5분 내 V1 생성 + CPS 검토 + PRD 검토 도달
- 기존 시스템 (회의록 흐름, batch chain, 검수 게이트) 정체성 100% 보존
- BE 새 코드 = pipeline 1 + prompt 1 + route 1 + job 1. 그 외 모두 기존 재활용

**Non-Goals**
- Design (SPACK / DDD / Architecture) 단계 자동화 — 기존 흐름 그대로
- Code / Lint 단계 — 기존 그대로
- 대화형 onboarding (B sub-project, Phase 2)
- Slack / Notion / zip / 음성 등 그 외 입력 (Phase 3+)
- private repo 분석 deep optimization (token 회전 / org repo) — 기본 동작만

## 3. User-Facing Flow

```
홈 → [새 프로젝트] 버튼
  ↓
모달 (Vuetify v-tabs)
  ┌─ 빈 프로젝트 (기존) ─┐  ┌─ GitHub 으로 시작 (신규) ─┐
  │ 이름 입력만          │  │ 이름 + GitHub URL          │
  └──────────────────────┘  └────────────────────────────┘
                                       ↓ submit
                            POST /api/v2/pipelines/onboard_from_github
                                       ↓ task_id
                            FE 폴링 (3s 간격, 기존 jobsStore 패턴)
                                       ↓ done
                            router.push('/plan') + store.projectName 설정
                                       ↓
                            plan.vue 자동 fetch → V1 표시
                                       ↓
                            사용자 V1 검토 → ✓ 누르면 CPS 생성 (기존 흐름)
                                       ↓
                            CPS 검토 → ✓ → PRD 생성 (기존 흐름)
                                       ↓
                            PRD 검토 → ✓ → Design (기존 흐름 그대로)
```

검수 게이트는 `auto_progress=false` 모드를 default 로 사용. 이미 시스템에 있음.

## 4. Architecture

### 4.1 BE 새 컴포넌트 (5 파일)

| 파일 | 역할 | 추정 LOC |
|---|---|---|
| `app/pipelines/github_onboard_pipeline.py` | Stage 1~4 orchestration | ~150 |
| `app/prompts/onboard_from_github.md` | LLM prompt (repo → V1 markdown) | ~80 |
| `app/api/v2_routes.py` (수정) | POST `/api/v2/pipelines/onboard_from_github` | ~50 |
| `app/queue/jobs.py` (수정) | `github_onboard_job` arq job | ~40 |
| `tests/pipelines/test_github_onboard.py` | FakeGemini + FakeGitHub + FakeNeo4j | ~150 |

### 4.2 BE 기존 재활용

- `GitHubClient.get_tree / get_file_content` ([app/clients/github_client.py](../../app/clients/github_client.py))
- `build_save_meeting_log_query` (V1 저장)
- `tracked_pipeline_context` (token quota 추적)
- CPS pipeline 전체 (V1 → CPS → ...)
- `auto_progress=false` 검수 게이트 모드
- 환각 가드 (build_merge_master_query / spec 노드 빈 필드)
- master full_markdown wipe 차단 가드

### 4.3 FE 새/수정 컴포넌트 (3 파일)

| 파일 | 역할 |
|---|---|
| `src/components/home/NewProjectModal.vue` (수정) | 탭 2개 (빈 프로젝트 / GitHub) |
| `src/components/home/GithubImportPanel.vue` (신규) | URL 입력 + repo 메타 미리보기 + submit |
| `src/store/jobs.js` (수정) | `githubOnboard` job kind 추가 |

### 4.4 FE 기존 재활용

- plan.vue 의 V1 자동 fetch + 표시 흐름
- 검수 게이트 UI (CPS 검토 화면, PRD 검토 화면)
- jobsStore 폴링 / 알림
- snackbar / 에러 핸들링

## 5. Pipeline Detail (BE)

### Stage 1 — GitHub repo fetch + 검증

```python
owner, repo = parse_github_url(github_url)
tree = await ctx.github.get_tree(owner, repo)  # 기존 client 재활용
```

URL 파싱 규칙:
- 허용: `https://github.com/owner/repo`, `https://github.com/owner/repo.git`, `git@github.com:owner/repo.git`
- 거부 (422): 그 외 모든 형식

### Stage 2 — D2 패턴 샘플링

LINT 단계의 `LINT_MAX_SAMPLE_FILES=40`, `LINT_TOTAL_BUDGET_BYTES=400000`, `LINT_PER_FILE_BYTES=64000` 환경변수 패턴 그대로 사용 (BE 의 다른 env 변수 신설 없이 onboard 만 별도 — `ONBOARD_MAX_SAMPLE_FILES` 등으로 분리하되 같은 default).

우선순위 (순서대로 채워서 budget 안 까지):
1. README.md / README.rst / README.txt (최우선)
2. package.json / pyproject.toml / Cargo.toml / go.mod / requirements.txt (메타)
3. 루트 + `src/` / `app/` 의 entry 파일 (main.py / index.ts / app.py / index.js / App.vue 등)
4. config 파일 (next.config.js / vite.config.js / Dockerfile 등)
5. 그 외 코드 파일 (file size 작은 것 우선)

`_BLOCKED_EXTENSIONS` (e.g., `.png`, `.jpg`, `.pdf`, `.zip`, `node_modules/*`, `.git/*`) 는 skip.

### Stage 3 — LLM V1 markdown 생성

`prompts/onboard_from_github.md` 의 prompt 로 Gemini 호출.

Prompt 구조 (5 sections):
1. **프로젝트 개요** — what + why (README 의 헤더 + tagline 참고)
2. **주요 기능** — 사용자가 할 수 있는 것 (README 의 features + 코드의 entry 추론)
3. **사용자 시나리오** — 구체적 use case (README 의 examples + 추론)
4. **기술 스택** — 코드에서 직접 추출 (package.json / requirements 등)
5. **NFR 추정** — 성능 / 보안 / 접근성 (코드 분석 + 추측)

이 5 sections 가 기존 CPS pipeline 의 입력으로 자연 사용 가능 (현재 CPS Agent 가 회의록 markdown 의 자유 형식을 받아 처리).

**길이 가드**:
- 최소 200자 — `len(v1_markdown.strip()) < 200` 이면 `ValueError("AI 가 V1 항목을 추출하지 못했습니다 ...")` — 빈 V1 누적 차단
- 최대 50000자 — 그 이상이면 5만자까지 truncate + warning log (CPS pipeline 입력 한계 보호)

**temperature**: 0.1 (다른 pipeline 과 통일).

### Stage 4 — V1 Meeting_Log 저장

```python
payload = CpsInput(
    project_name=...,
    version="v1.0",
    date=datetime.utcnow().isoformat(),
    meeting_content=v1_markdown,
)
save_query, save_params = build_save_meeting_log_query(payload)
await ctx.neo4j.run_in_transaction([(save_query, save_params)])
```

기존 cypher 100% 재활용. CPS pipeline 으로의 자동 트리거 X (검수 게이트 default).

### Stage 5 — 사용자가 V1 검토 후 명시 트리거

기존 plan.vue 의 V1 검토 → CPS 생성 흐름 (auto_progress=false 모드) 그대로. 이번 PR 의 새 코드 X.

## 6. API Specification

### POST `/api/v2/pipelines/onboard_from_github`

**Auth**: Bearer JWT (현재 사용자)
**Rate limit**: 3/min (SlowAPI, 기존 패턴)
**Ownership**: 신규 project — 사용자가 owner. **같은 사용자가 동일 project_name** 보유 시 409 (기존 ownership_repository 패턴). 다른 사용자가 같은 이름을 가져도 무관.

**Request**:
```json
{
  "project_name": "my-todo-app",
  "github_url": "https://github.com/myuser/my-todo-app"
}
```

**Response (202)**:
```json
{
  "status": "accepted",
  "task_id": "uuid-..."
}
```

**Error**:
| Status | Code | 시나리오 |
|---|---|---|
| 422 | `INVALID_GITHUB_URL` | URL 형식 위반 |
| 422 | `GITHUB_REPO_NOT_FOUND` | 404 from GitHub API |
| 422 | `GITHUB_REPO_PRIVATE_NEEDS_AUTH` | private + 사용자 GitHub OAuth 미연결 |
| 409 | `PROJECT_ALREADY_EXISTS` | 동일 project_name 보유 |
| 402 | `QUOTA_EXCEEDED` | 토큰 quota 초과 |
| 500 | LLM 환각 | Stage 3 의 ValueError → 500 detail 명시 |

GitHub OAuth 미연결의 경우 detail 에 `"GitHub 계정 연결이 필요합니다 — 프로필 → 연결된 계정 → GitHub"` 형태로 안내 (FE 가 그대로 노출).

## 7. Token Quota & Cost

D2 샘플링: README + package.json + entry 파일들 ~40 파일 × ~64KB → 최대 ~400KB input. Gemini flash 기준:
- 입력 토큰: ~100K (한국어/영어 혼합 가정)
- 출력 토큰: ~5-10K (V1 markdown ~10-20KB)
- 1회 onboard: ~110K 토큰 ≈ 일반 미팅 로그 5건 분량

Free 사용자 부담: 한 번의 onboard 가 quota 의 큰 부분 소비. Free 한도 점검 필요.
권장: Free `usage_total_tokens` 한도가 ~500K/월이면 1회 onboard 가 22% 소비. 사용자 안내 토스트 필요.

## 8. FE Detail

### NewProjectModal.vue (수정)

```vue
<v-tabs v-model="tab">
  <v-tab value="empty">빈 프로젝트</v-tab>
  <v-tab value="github">GitHub 으로 시작</v-tab>
</v-tabs>
<v-window v-model="tab">
  <v-window-item value="empty"><!-- 기존 UI --></v-window-item>
  <v-window-item value="github">
    <GithubImportPanel @created="onCreated" />
  </v-window-item>
</v-window>
```

### GithubImportPanel.vue (신규)

```vue
<template>
  <div>
    <v-text-field v-model="projectName" label="프로젝트 이름" />
    <v-text-field
      v-model="githubUrl"
      label="GitHub repo URL"
      placeholder="https://github.com/owner/repo"
      :error-messages="urlError"
    />
    <!-- 메타 미리보기 (선택) — paste 후 1초 debounce -->
    <RepoPreview v-if="repoMeta" :meta="repoMeta" />
    <v-btn
      :disabled="!canSubmit"
      :loading="submitting"
      @click="submit"
    >
      AI 가 분석 시작 ({{ etaText }})
    </v-btn>
  </div>
</template>
```

ETA 표시: "약 30-60초 소요" (D2 샘플링 + Gemini 응답 시간).

**"분석 시작" 버튼 활성 조건** (모두 true 여야 enable):
- 프로젝트 이름 ≥ 1자 + 중복 미존재 (기존 검증 패턴)
- GitHub URL format match (regex)
- 사용자 quota 여유 있음 (usageStore 체크)
- submit 진행 중 아님

**메타 미리보기 (RepoPreview)** — paste 후 1초 debounce → GitHub API 1회 호출 (proxy 경유). 표시 항목: repo name / description / stars / language / 마지막 push 일자. private repo 인 경우 "🔒 private — 분석 위해 GitHub 연결 필요" 안내.

### jobs.js (수정)

```js
case 'githubOnboard':
  onComplete: (finalInfo) => {
    const { project_name } = finalInfo
    store.projectName = project_name
    router.push('/plan')
    showSuccess(`'${project_name}' 분석 완료. V1 검토 시작.`)
  }
```

## 9. Error Handling

| 시나리오 | BE 동작 | FE UX |
|---|---|---|
| URL 형식 위반 | 422 + detail | 입력 필드 빨간 에러 메시지 |
| repo 404 | 422 + detail | toast + URL 재입력 유도 |
| private repo + OAuth 미연결 | 422 + detail | toast + "GitHub 연결" 버튼 (profile 페이지 링크) |
| 큰 repo (>1000 파일) | LINT 가드 패턴 그대로 — top 40 샘플링 + warning log | 정상 진행 (사용자에게는 영향 없음) |
| LLM 빈 응답 | 가드 발동 → ValueError → 500 detail | toast + "다시 시도" |
| Quota 초과 | 402 + upgrade dialog (기존) | upgrade dialog 자동 노출 (기존) |
| 동일 project_name | 409 + detail | toast + 다른 이름 유도 |

## 10. Testing

### BE Unit
- `test_parse_github_url` — owner/repo/branch/tag 파싱
- `test_select_onboard_files` — 40 파일 우선순위 선택
- `test_v1_minimum_length_guard` — 빈 LLM 응답 raise

### BE Integration (FakeGemini + FakeGitHub + FakeNeo4j)
- e2e: URL → V1 생성 + Meeting_Log 저장 + task_id 반환
- error: private repo → 422
- error: 404 repo → 422
- error: LLM 빈 응답 → 500
- 큰 repo → top 40 샘플링 (token budget 안)

### BE Regression
- 기존 batch chain / 회의록 흐름 / Lint pipeline 모든 pytest 통과

### FE Unit (vitest mount)
- NewProjectModal — 탭 전환 / 각 탭의 submit
- GithubImportPanel — URL validation / submit / 에러 표시
- jobsStore.startJob('githubOnboard') — onComplete 흐름

### FE Regression
- 기존 NewProjectModal 의 "빈 프로젝트" 흐름 — 동일 동작
- batch chain — 영향 없음

## 11. Migration / Rollout

- 기존 사용자: entry 신규 옵션. 기존 흐름 무변경. 영향 0.
- 신규 사용자: 첫 화면에서 즉시 선택 가능.
- 토큰 quota 사용량 증가 가능성 — 운영 모니터링 필요 (admin dashboard 의 사용자별 토큰 사용 추적 기존 기능 활용).
- A/B 분석: 신규 가입자의 GitHub entry 선택률 추적 (audit log 또는 events table — 기존 패턴).

## 12. Estimated Effort

| 작업 | 시간 |
|---|---|
| BE pipeline 코드 | ~4h |
| BE prompt 작성 + 튜닝 | ~2h |
| BE API route + arq job | ~2h |
| BE tests (unit + integration) | ~3h |
| FE NewProjectModal 수정 + GithubImportPanel | ~3h |
| FE tests (mount) | ~1h |
| 통합 검증 (수동 + dev 서버) | ~1h |
| **Total** | **~16h** |

## 13. Open Questions (사용자 검토 시점)

- **branch / tag 입력** — Phase 1 은 default branch 만. Phase 2 에서 사용자 선택 검토.
- **GitHub OAuth 미연결 사용자의 onboarding** — 현재 안내 메시지로 처리. 향후 OAuth 연결 in-place dialog 검토.
- **운영 시점 token 사용량 모니터링** — 첫 N건 onboard 후 평균 토큰 사용량 측정 → Free 한도 조정 또는 안내 강화 결정 (Phase 1 후 운영 데이터로).

(이전 open questions: V1 최대 길이, 미리보기, 비활성 조건 — Section 5/8 에서 명시 결정)

## 14. References

- 기존 GitHubClient: [app/clients/github_client.py](../../app/clients/github_client.py)
- Lint 단계 샘플링 패턴: [app/pipelines/lint_pipeline/sampling.py](../../app/pipelines/lint_pipeline/) 및 docker-compose 의 LINT_* 환경변수
- CPS pipeline 의 build_save_meeting_log_query: [app/pipelines/cps_pipeline/cypher.py](../../app/pipelines/cps_pipeline/cypher.py)
- 검수 게이트 (auto_progress=false): [docs/v2_pipelines.md](../v2_pipelines.md)
- jobsStore 폴링 패턴: [src/store/jobs.js](../../../harness/src/store/jobs.js)
