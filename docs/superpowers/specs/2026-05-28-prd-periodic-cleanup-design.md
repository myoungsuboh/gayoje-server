# Periodic Master PRD Cleanup (L1-3 — Vision/KPI/NFR Consolidation)

작성일: 2026-05-28
관련: L1-1(spec_count backstop, #69), L1-2(롤백 #70), 캐시 버전 bump(#73), `app/prompts/cleanup_master_prd.md`(고아 상태)

## 문제

증분 `run_prd_merge` 는 Section 2(Epic/Story)는 인벤토리 기반으로 강력하게 dedup 하지만, **Section 1(Product Overview — Vision/KPI/타겟)과 Section 4(Global NFR)는 ADD-ONLY** 로 명시돼 있다(`app/prompts/prd_merge.md:56,59`). 그래서 같은 비전·같은 NFR 규칙이 버전마다 누적되어 다버전(예: 5+) 처리 후 master PRD 가 누더기가 된다.

`app/prompts/cleanup_master_prd.md` 에 의미 기반 dedup·reconcile·over-dedup 가드까지 완비된 정리 프롬프트가 이미 있지만 **어떤 코드도 이를 호출하지 않는 고아** 상태(`grep cleanup` 0건).

## 목표

다버전 누적 시 Vision/KPI/NFR 중복을 자동으로 정리하되, **내용 손실은 절대 발생시키지 않는다** (사용자가 입력한 내용이 사라지면 신뢰가 깨진다).

## Non-goals

- Section 2(Epic/Story) dedup 재설계 — 현재 인벤토리 메커니즘이 검증됨, 건드리지 않는다.
- 수동 cleanup 엔드포인트/UI — 자동 트리거 한 가지만 도입(YAGNI).
- 전체 rebuild 파이프라인(`rebuild_prd.md`) 연결 — 별도 주제.

## 설계

### 트리거: 임계 버전 자동

매 `run_prd_merge` 끝에 incremental merge 가 성공적으로 영속화된 직후, 누적 PRD 카운트와 master 노드의 `cleanup_at_version_count` 차이가 임계치를 넘으면 cleanup pass 1회 실행.

- 임계치: `PRD_CLEANUP_VERSION_INTERVAL` env, 기본 `5`.
- master 노드 첫 생성 시 `cleanup_at_version_count = prd_total` 로 초기화 → 첫 cleanup 은 5버전 누적 후.
- 평상시 4/5 merge 는 cleanup 0건, 비용 영향 없음.

### Cleanup pass

1. `_load_prompt("cleanup_master_prd.md")` + `_render(template, master_prd_markdown=<merged_content>)`.
2. `ctx.gemini.generate(prompt, temperature=_TEMPERATURE)` 1회 호출, `strip_code_blocks` + `strip_template_placeholders` 후처리.
3. `validate_cleanup_output(input_md, output_md)` 통과 시에만 master 에 영속화 + `cleanup_at_version_count = prd_total` 갱신.
4. 통과 실패/LLM 실패 시 incremental merge 결과 유지, `cleanup_at_version_count` 도 갱신 안 함 → 다음 merge 에서 재시도.

### 데이터 안전 가드 (over-dedup·손실 차단)

`validate_cleanup_output` 가 다음을 모두 확인:

1. **비어 있지 않음** + 길이 ≥ 입력의 70% — 대량 삭제 차단.
2. **Section 1~4 헤더 모두 존재** — `### 1.`, `### 2.`, `### 3.`, `### 4.` 정규식.
3. **모든 Epic-XX / Story-XX.Y ID 보존** — 입력에서 추출한 ID 집합이 출력에 모두 존재해야 함. **이게 가장 중요한 가드** (cleanup 이 Epic/Story 를 실수로 삭제하는 일을 0 으로).

검증 실패 시:
- master 덮어쓰지 않음 (incremental 결과 유지).
- `logger.warning("prd cleanup validation failed: ...")` — 운영 가시성.
- `cleanup_at_version_count` 갱신 안 함 → 다음 trigger 에서 자연스럽게 재시도.

기존 `build_merge_master_prd_query` 의 "빈 merged_content → ValueError" 가드는 cleanup persist 에도 그대로 재사용(같은 함수 호출).

### 모듈 구조

- 신규: `app/pipelines/prd_cleanup.py`
  - `should_run_cleanup(prd_total: int, last_cleanup_count: int, interval: int) -> bool` — 순수함수.
  - `validate_cleanup_output(input_md: str, output_md: str) -> tuple[bool, str]` — (passed, reason).
  - `extract_spec_ids(md: str) -> set[str]` — `Epic-NN`, `Story-NN.M` 추출.
  - `call_prd_cleanup_agent(ctx, master_content: str) -> str` — LLM 호출.
  - `run_prd_cleanup_if_due(ctx, project_name, prd_total, last_cleanup_count, current_master_md) -> Optional[str]` — 오케스트레이션; 반환값 None=skipped, str=새 정리된 master.

- 수정: `app/pipelines/prd_pipeline.py`
  - `_GET_ALL_PRD_QUERY` 에 `m.cleanup_at_version_count AS cleanup_at_version_count` RETURN 추가.
  - `fetch_prd_master_and_latest` 반환에 `cleanup_at_version_count: int` 추가(null→0 coerce).
  - `build_merge_master_prd_query` 에 필수 인자 `cleanup_at_version_count: int` 추가 → 항상 `SET master.cleanup_at_version_count = $cleanup_at_version_count` 포함. 호출자는 다음 두 값 중 하나를 전달:
    - **Incremental save**: `prd_state.get("cleanup_at_version_count") or prd_total` — 기존 값 보존하되 첫 저장(null) 시엔 `prd_total` 로 init(이 시점에 cleanup 한 적 없으므로 baseline = 현재 카운트).
    - **Cleanup success save**: `prd_total` — baseline 갱신.
  - `run_prd_merge`: line 917 의 incremental save 호출은 위 init 로직으로 `cleanup_at_version_count` 전달.
  - 그 다음 `run_prd_cleanup_if_due(...)` 호출. 반환값이 str 이면 `build_merge_master_prd_query(..., cleanup_at_version_count=prd_total)` 로 한 번 더 영속화.

- 신규 테스트: `tests/pipelines/test_prd_cleanup.py`
  - `should_run_cleanup` 경계값.
  - `extract_spec_ids` 다양한 형식.
  - `validate_cleanup_output` — pass / 빈 출력 / 길이 70% 미만 / 섹션 누락 / Epic ID 손실 / Story ID 손실 각각.
  - `run_prd_cleanup_if_due` — skip 케이스, run-but-validation-fails 케이스(원본 유지), run-and-succeeds 케이스(새 md 반환).

- 통합 테스트: `tests/pipelines/test_prd_merge_periodic_cleanup.py`
  - FakeNeo4j + Stub Gemini 로 `run_prd_merge` 5회 호출 후 cleanup 한 번 트리거되는지, cleanup_at_version_count 갱신 확인.

### 흐름도

```
run_prd_merge
  ...
  build_merge_master_prd_query(merged)        # incremental save
  await neo4j.run_cypher                       # ← line 917
  ──────────────────────────────────────
  if should_run_cleanup(prd_total, last, 5):   # ← NEW
      cleaned = call_prd_cleanup_agent(merged) # LLM 1회
      ok, why = validate_cleanup_output(merged, cleaned)
      if ok:
          q, p = build_merge_master_prd_query(
              ..., cleanup_at_version_count=prd_total)
          await neo4j.run_cypher(q, p)
      else:
          logger.warning(...)
  ──────────────────────────────────────
  return PrdResult(...)
```

### 비용/지연

- 5버전마다 LLM 1회 추가(gemini-2.5-flash, temperature 0.1). master full_markdown 크기 ~수 KB → 1초 이내 응답.
- Neo4j write 1회 추가(SET only) — 무시할 수준.

### 실패/엣지 케이스

- LLM 호출 timeout/4xx: try/except, warning 로그, merge 결과 그대로.
- Cleanup 출력이 placeholder 흔적 포함: `strip_template_placeholders` 가 처리.
- 동시성: post_meeting 워커가 같은 project 키로 직렬화 — race 없음. 만약 발생해도 둘 다 같은 master 읽어 같은 출력 → 마지막 SET 만 남음(idempotent 에 가까움).
- 첫 실행: master 새로 만든 직후 prd_total=1, last=1 (방금 init) → diff 0 → cleanup skip. 정상.

### 마이그레이션

- 기존 master 노드는 `cleanup_at_version_count` 필드 없음(null) → fetch 에서 0 으로 coerce, 다음 merge 에서 `last_cleanup_count = prd_total` 로 초기화. 첫 cleanup 은 그 시점 + 5버전 후.

## 테스트 전략 (TDD)

RED → GREEN 순서로:

1. `should_run_cleanup` (순수함수, 가장 작은 단위).
2. `extract_spec_ids`.
3. `validate_cleanup_output` 의 각 검증 가지.
4. `call_prd_cleanup_agent` (Stub Gemini, prompt 렌더링 검증).
5. `run_prd_cleanup_if_due` (오케스트레이션, 분기별).
6. `run_prd_merge` 통합 (FakeNeo4j + Stub Gemini, 5회 호출 시 cleanup 1회).

전체 BE 스위트 회귀 없음 확인.

## Out of scope / 후속

- Rebuild 파이프라인 연결 — 별도.
- 사용자가 명시적으로 정리하고 싶을 때 호출하는 수동 엔드포인트 — YAGNI(자동 충분).
- Section 2 Epic/Story dedup 강화 — 현재 인벤토리 메커니즘이 충분히 검증됨.
