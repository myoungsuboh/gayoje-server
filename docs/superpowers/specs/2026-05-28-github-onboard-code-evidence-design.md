# GitHub Onboard — Code Evidence Injection (README 편향 해소)

작성일: 2026-05-28
관련: `app/pipelines/github_onboard_pipeline.py`, `app/prompts/onboard_from_github.md` (고아 아님, 사용 중)

## 문제

GitHub URL 로 프로젝트를 자동 생성할 때 사용자 체감: "README 만 읽고 만든 느낌."

진짜 원인: fetch 단계는 README + manifest + entry + config + 일반 코드까지 잘 가져오지만 **프롬프트가 LLM 에게 'README 우선, 코드는 fallback' 으로 지시**.
- Section 1 "README 의 헤더/tagline/Overview 를 한국어로 정리"
- Section 2 "README 의 Features section + 코드의 entry 파일에서 추론"
- Section 3 "README 의 example/usage/quickstart 가 있으면 **그대로** 풀어쓰기. **없으면** entry 에서 추론"

자연어 가드만으로 LLM 행동을 강제하는 건 L1-2(#70) 실패에서 학습한 패턴 — 쉬운 길(README) 로 회귀.

## 목표

**결정적 코드 추출 → 프롬프트에 사실 표로 주입 → LLM 이 무시 못 함.** README 는 cross-check, 코드 사실이 권위. L1-1 backstop 디자인과 동일 철학(`is_meaningful_spec_node`).

## Non-goals

- 새 GitHub API 호출 추가 — 이미 fetch 한 tree + samples 만 활용 (비용/지연 0).
- 모든 언어 perfect coverage — 1차 컷은 **Python + JS/TS** 만 깊게, 나머지(Go/Rust/Java)는 manifest 정도만.
- LLM 호출 추가 — 1회 그대로.

## 설계

### 신규 모듈 `app/pipelines/github_onboard/code_evidence.py`

순수함수 4개 + 통합 헬퍼 1개:

1. **`extract_manifest_facts(samples) -> ManifestFacts`** — package.json / pyproject.toml / requirements.txt / go.mod / Cargo.toml 등 매니페스트 파일을 결정적으로 파싱.
   - 결과: `{language: str, runtime: str|None, deps: list[str], dev_deps: list[str], framework_hints: list[str]}`
   - `framework_hints`: dependencies 에서 알려진 프레임워크 감지 (vue, react, next, fastapi, express, django, spring-boot 등).
   - 여러 매니페스트가 있으면 모두 파싱해 합침(monorepo / multi-language).

2. **`extract_entry_signals(samples) -> list[EntrySignal]`** — entry 우선순위 파일에서 결정적으로 시그널 추출.
   - **Python**: `ast` 모듈로 함수/클래스 + decorator. FastAPI/Flask route 감지: `@app.get(...)`, `@router.post(...)`, `@bp.route(...)`.
   - **JS/TS**: regex 로 `app.get(...)`, `router.post(...)`, `export function ...`, `export default ...`, `defineEventHandler(...)`, Vue SFC 의 `<script setup>` 파일명.
   - 결과: `[{file, kind: "route"|"export"|"component", method: str|None, path: str|None, name: str}]`.
   - parse 실패는 silent skip — fixture 테스트로 핵심 케이스 검증.

3. **`extract_repo_stats(tree_blobs) -> RepoStats`** — 이미 fetch 한 tree 메타에서 추출(추가 API 호출 0).
   - 결과: `{total_files: int, top_dirs: list[(dir, count)], language_breakdown: dict[ext, count]}`.

4. **`format_code_evidence_block(manifest, signals, stats) -> str`** — 위 3개를 프롬프트에 넣을 markdown 표로 포맷.
   - 비어 있으면 빈 문자열 반환(프롬프트가 안전하게 무시).

### 프롬프트 재작성 `app/prompts/onboard_from_github.md`

위계 변경: **코드 단서 = 권위, README = 보조/맥락.**

추가 input section:
```
## 3. 코드 단서 (결정적 추출 — 이 표의 사실은 우선 반영)
<<code_evidence>>
```

기존 task 지시문 수정:
- Section 1 (프로젝트 개요): "**의존성·entry signals 가 가리키는 도메인**을 1~2 문장으로 요약. README 의 tagline 은 cross-check 으로만 사용 — 코드 단서와 충돌 시 코드 따름."
- Section 2 (주요 기능): "**entry signals 의 각 route/export 가 곧 사용자 기능**. README 의 Features 는 보충용. 추측 금지 — signals 에 없는 기능은 (추정) 명시."
- Section 3 (사용자 시나리오): "**route signals 의 method+path 조합으로 시나리오 재구성** (예: `POST /api/login → GET /api/me` = '로그인 → 프로필 조회'). README example 은 corroboration."
- Section 4 (기술 스택): "**manifest_facts.framework_hints 의 표를 그대로 사용**. 추측 X."
- Section 5 (NFR): unchanged.

### Wire-in

`call_onboard_llm` 의 `_render` 호출에 `code_evidence` 추가:
```python
code_evidence = format_code_evidence_block(
    extract_manifest_facts(samples),
    extract_entry_signals(samples),
    extract_repo_stats(tree_blobs),  # tree_blobs 도 인자로 전달
)
prompt = _render(..., code_evidence=code_evidence)
```

### 데이터 안전

- 기존 fetch / select / V1 검증 / CPS 위임 로직 0 변경 — 완전 additive.
- 결정적 추출 실패는 silent (빈 evidence). LLM 은 빈 evidence 면 기존처럼 README 의존 fallback.
- 토큰 비용 변화: evidence block 추가로 prompt 가 ~1~3KB 증가. 무시할 수준.

### 테스트 전략 (TDD)

1. `extract_manifest_facts` — Python (pyproject + requirements), JS/TS (package.json), 다중 매니페스트, 빈 입력.
2. `extract_entry_signals` — Python FastAPI route decorator, Python Flask, JS Express, TS Hono, Vue SFC, parse 실패 silent skip.
3. `extract_repo_stats` — language breakdown, top-N dirs, 빈 트리.
4. `format_code_evidence_block` — 빈 입력 → "", 정상 입력 → markdown 표.
5. 통합: 기존 e2e fixture(`test_e2e_onboard_returns_v1_and_cps`) 가 새 prompt 로도 동일 결과 — 회귀 0.

### Out of scope / 후속

- Go / Rust / Java entry signals (manifest 만으로도 framework 추정 가능).
- README 분석 자체(섹션 구조 파싱)은 LLM 에 위임 유지 — 결정적 파싱 가치 낮음.
- 사용자별 추가 단서 (commit history, README 에 명시된 데모 URL fetch 등) — YAGNI.
