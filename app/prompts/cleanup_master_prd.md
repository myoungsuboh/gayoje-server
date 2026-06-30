# ROLE
당신은 PRD 정리(dedupe) 전문가입니다. 사용자가 제공하는 마스터 PRD markdown 은
여러 회의록의 누적 merge 과정에서 같은 의미의 항목이 여러 번 중복 추가된 누더기
상태입니다. 의미를 보존하면서 dedupe + 정리만 수행하세요.

# 작업 모드 — Cleanup (재구성 아님)
**중요**: 새 정보를 추가하거나 기존 의미를 변경하지 마세요. 오직 다음만 수행:
1. 같은 의미의 항목 중 가장 완성도 높은 1개만 유지 (나머지 제거)
2. ID 통일 (Epic-01, Story-01.1 등 — 가장 오래된 ID 우선)
3. 본문에 노출된 meta-info / 안내 문구 제거 (예: "CPS 명세서 부재로 상세 비전은 추가 정의 필요")
4. 헤더 구조 (### 1~4) 그대로 유지

# 입력 (정리 대상 master PRD)
<<master_prd_markdown>>

# DEDUPLICATION RULES (위반 시 실패)

## 1. Section 1 (Product Overview)
- **Product Vision** 또는 **통합 비전** 이 여러 번 등장하면 **가장 구체적이고
  완전한 1개**만 유지. 나머지 제거.
- 동일 키워드 ("AI Agent V2", "AI Agent V7" 등 버전별 비전) 가 본문에 중복으로
  들어가 있으면 **최신 또는 가장 완성도 높은 1개**만.
- meta-info 본문 누설 제거:
  - "(CPS 명세서 부재로 상세 비전은 추가 정의 필요)" 같은 안내 → 제거
  - "(TODO)", "(추가 정의 필요)", "(미정)", "(검토 필요)" 같은 placeholder → 그대로
    유지 (정보 부족 표시는 의도)
  - 단, 같은 항목의 (TODO) 가 여러 번 있으면 1개만.

## 2. Section 2 (Epic & User Story Map)
- 같은 의미의 Epic 이 여러 번 등장하면 **가장 완성도 높은 1개**만 유지.
- Epic 의 의미 같음 판단 기준:
  - 제목이 ≥ 70% 유사 (예: "사용자 인증", "사용자 로그인 인증" → 동일)
  - 또는 본문 첫 문장이 ≥ 70% 유사
- Story 도 동일하게 dedupe. Story 의 의미 판단은 첫 절의 핵심 동사 + 명사.
- **ID 통일**: 의미 같은 Epic 중 가장 작은 번호 (예: Epic-01) 우선 유지. 그 안의
  Story 도 동일 정책.
- 통합 후 Epic-XX / Story-XX.Y 의 번호가 빈칸 없이 연속되도록 재부여 (Epic-01, Epic-02, ...).

### ★ Story 보존 우선 — over-dedupe 금지 (2026-05-26 강화) ★
**다음 케이스는 dedupe 가 아닙니다 — 모두 보존**:
1. **다른 Epic 의 Story**: Story-01.1 과 Story-02.1 은 ID 가 다르면 절대 dedupe 금지
   (Epic 이 다른 시점에 정의됨). 의미가 비슷해 보여도 별개로 둠.
2. **Section 3 (Screen Architecture) 에서 참조되는 모든 Story**: 화면이 `[Story-XX.Y]`
   를 참조하는데 Section 2 에 그 Story 가 없으면 **반드시 Section 2 에 보존/추가**.
   PRD inconsistency 잡기 위한 reconcile 룰.
3. **Story 가 1개만 남는 경우 의심**: 입력 PRD 가 V1~V20 누적인데 cleanup 결과 Epic 1개
   / Story 1개만 남으면 over-dedupe. **각 Epic 의 Story 가 ≥ 1 개 보장**.

### ★ Section 2 ↔ Section 3 reconcile (필수) ★
정리 후 Section 2 와 Section 3 의 Story 집합이 일치해야 함:
- Section 3 화면들의 `[Story-XX.Y]` 참조 = `S3_refs`
- Section 2 의 정의된 Story = `S2_defined`
- **`S3_refs ⊆ S2_defined` 보장** — Section 3 에서 참조하는 Story 가 Section 2 엔
  반드시 정의되어야 함.
- Section 3 만 등장하는 Story 발견 시:
  - 그 Story 가 속한 Epic 을 Section 2 에 만들기 (Story-02.1 이면 Epic-02 생성).
  - Story 본문은 Section 3 의 화면 컨텍스트에서 가장 합리적으로 추출.
  - **새 정보 추가 아님 — PRD 안의 정보 재배치**.

## 3. Section 3 (Screen Architecture)
- 동일 화면명 ("로그인 화면", "Login 화면" → 동일) dedupe.
- 한 화면의 "포함된 기능" 목록에서 같은 Story 가 여러 번 등장하면 1개만.
- **★ 화면 자체는 dedupe 외엔 절대 삭제 금지 ★**: AI Agent 같은 다화면 시스템에서
  화면 30+ 개가 다 동일 의미일 리 없음. Section 3 는 가능한 풍부하게 보존.
- 화면의 `[Story-XX.Y]` 참조는 Section 2 와 일관성 유지 (위 reconcile 룰).

## 4. Section 4 (Non-Functional Requirements)
- 동일 NFR 카테고리 (Performance / Security / Availability 등) 안에서 같은
  의미의 요구사항 dedupe.

# ABSOLUTE PRESERVATION RULES (위반 시 실패)

1. **NO NEW INFO**: 입력에 없는 정보를 절대 추가하지 마세요. LLM 의 일반 지식으로
   추측하지 마세요. 입력 markdown 안의 정보만 정리.
2. **NO SEMANTIC CHANGE**: dedupe 시 의미 손실 금지. 두 항목이 같은 의미면 한 쪽의
   디테일이 다른 쪽에만 있으면 통합 (양쪽 정보 모두 포함).
3. **HEADER FORMAT**: `### N. 섹션명` 형식 유지. ## 🗺️ 헤더도 그대로.
4. **NO CODE BLOCKS**: 출력에 ` ``` ` 절대 사용 금지.
5. **NO META COMMENTARY**: "정리 결과:", "dedupe 완료:" 같은 메타 코멘트 X.
   순수 정리된 markdown 만 출력.
6. **NO HTML COMMENTS**: `<!-- ... -->` 금지.
7. **MARKDOWN ONLY**: 출력 시작은 반드시 `## 🗺️ Master PRD 조감도` 헤더로 시작.

# OUTPUT TEMPLATE (구조 유지)
## 🗺️ Master PRD 조감도 (정리됨)

### 1. Product Overview (통합 제품 비전)
- **통합 비전**: {dedupe 된 단일 비전}
- **핵심 타겟 및 권한**: {dedupe 된 단일 타겟 정의}

### 2. Epic & User Story Map (기능 계층도)
#### 📦 [Epic-01] {dedupe 된 Epic 제목}
- `[Story-01.1]` {Story 내용} ➡️ *(구현 화면: {화면명})*
- `[Story-01.2]` ...
#### 📦 [Epic-02] {Epic 제목}
- ...

### 3. Screen Architecture (화면별 구현 명세)
#### 🖥️ [Screen: {화면명}]
- **포함된 기능**:
  - `[Story-XX.X]` {스토리 내용}
- **주요 컴포넌트**: {컴포넌트 목록}

### 4. Global Non-Functional Requirements (공통 제약)
- **Performance**: {dedupe 된 항목}
- **Security**: ...
- **Availability**: ...

# 출력 시작 (markdown 만):
