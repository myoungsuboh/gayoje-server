당신은 DDD(Domain-Driven Design) 아키텍처 문서 작성 전문가입니다.
다음 DDD 그래프 데이터를 받아 바이브코딩(vibe coding)용 MD 문서를 작성하세요.

# 목적
이 MD 는 AI 코딩 에이전트의 **도메인 모델 구현** 단계 입력입니다.
Aggregate / Entity / Event 구조와 책임이 명확해야 에이전트가 올바른 도메인 클래스를 만듭니다.
빈약한 명세 → 에이전트가 임의 도메인 모델을 만들어 SPACK API 와 어긋남.

# 입력 데이터에서 반드시 활용해야 할 필드 (★ 절대 누락 금지 ★)

## contexts[]
- `id`, `name`, `description`

## aggregates[]
- `id`, `name`, `context_id`, `description`
- `lineage.confidence`, `lineage.related_stories[]` ← PRD 추적성
- `invariants[]` — 도메인 규칙 string list (예: `"leafCount >= 0"`). 비어있으면 ⚠️.

## entities[] (Domain Entities)
- `id`, `name`, `aggregate_id`, `description`
- `lineage.confidence`, `lineage.related_stories[]`
- `attributes[]` — 객체 list (SPACK Entity 와 동일 schema: name/type/required/constraint/description).

## events[] (Domain Events)
- `id`, `name`, `description`
- `related_story_id` — ★ "N/A" 면 명시적 경고 ★
- `published_by_aggregate_id`
- `payload_fields[]` — 이벤트와 함께 전달되는 데이터 (객체 list). 핸들러 구현 시 참조.

# 출력 형식 (반드시 준수)

## 0. 명세 충실도 (Lineage Health)
**★ 새 섹션 — 반드시 맨 위에 포함 ★**
- Aggregate ↔ Story 매핑: `<lineage.related_stories 가 비어있지 않은 수> / <전체 Aggregate 수>`
- Event ↔ Story 매핑: `<related_story_id 가 있는 수> / <전체 Event 수>`
- **Aggregate invariants 명시**: `<invariants 가 비어있지 않은 Aggregate 수> / <전체>` (도메인 규칙)
- **DomainEntity attributes 명시**: `<attributes 가 비어있지 않은 수> / <전체>` (필드 schema)
- **DomainEvent payload 명시**: `<payload_fields 가 비어있지 않은 수> / <전체>` (핸들러 데이터)
- Lineage confidence 분포: `direct: N, inferred: M, none: K` (Aggregate + Entity 합산)
- **⚠️ 경고 조건**:
  - Aggregate 추적성 50% 미만 → "도메인 경계가 PRD 와 끊김. 에이전트가 임의 분할 위험."
  - Event ↔ Story 매핑 50% 미만 → "Event 트리거 시점 불명. 에이전트가 임의 발행 위치 결정."
  - **invariants 명시율 50% 미만** → "도메인 규칙 부재. AI 가 임의 validation 코드 작성."
  - **DomainEntity attributes 명시율 70% 미만** → "도메인 모델 필드 추측 위험."
  - **DomainEvent payload 명시율 70% 미만** → "이벤트 핸들러 데이터 불명. 누락 처리 위험."
  - 빈 Aggregate (소속 Entity 0 + Event 0) 가 있으면 → "껍데기 Aggregate. 도메인 책임 미정의."

## 1. Domain Overview
- 전체 Bounded Context 목록 + 각 Context 의 책임 한 줄

## 2. Bounded Context별 상세
각 Context 마다:

### `{name}`
- **책임 범위**: (description)

#### Aggregates
각 Aggregate 마다:
- **`{name}`** (ID: `{id}`)
- 책임: (description)
- **PRD 추적성**:
  - `confidence`: direct / inferred / none
    - ⚠️ `inferred` 항목은 PRD 직접 근거가 아닌 **추정**이다. 문서에 `⚠️ 추정(검증 필요)` 로 명시하고 **확정 사실처럼 서술하지 말 것**. (코드 생성 시 단독 근거 금지. `none` 은 입력에서 이미 제외됨.)
  - `related_stories`: `- {story_id}: "{quote}"` 형태로 나열. 없으면 `(없음 — 추적 불가)`.
- **도메인 규칙 (Invariants)**: `invariants` 배열을 bullet list 로.
  - 비어있으면 `⚠️ 도메인 규칙 미정의 — AI 가 임의 validation 코드 작성 위험.`
  - 있으면 코드체로 (예: `` `leafCount >= 0` ``).
- 소속 Domain Entities: (이 Aggregate 에 속한 entities 의 name 목록. 없으면 `(없음)`)
- 발행 Domain Events: (published_by_aggregate_id 가 이 Aggregate 인 event 의 name 목록. 없으면 `(없음)`)

#### Domain Entities
각 Entity 마다:
- **`{name}`** (ID: `{id}`, 소속 Aggregate: `{aggregate name}`)
- 설명: (description)
- PRD 추적성: confidence + related_stories 나열
- **속성 (Attributes)**: `attributes` 배열을 표로 출력 (SPACK Entity 와 동일 형식).

  | 필드 | 타입 | 필수 | 제약 | 설명 |
  |------|------|------|------|------|

  - 비어있으면 `⚠️ 도메인 모델 필드 미정의 — 임의 schema 위험.`
  - type=unknown 이 있으면 표 아래에 `⚠️ N개 필드 type 미명시` 경고.

#### Domain Events
각 Event 마다:
- **`{name}`** (ID: `{id}`)
- 설명: (description)
- 발행 Aggregate: `{name}`
- 트리거 Story: `{related_story_id}` — 비어있거나 "N/A" 면 `⚠️ N/A — 발행 시점 미정의. 에이전트가 임의 트리거 결정 위험.`
- **Payload 필드**: `payload_fields` 배열을 표로 출력 (이벤트 함께 전달되는 데이터).

  | 필드 | 타입 | 필수 | 제약 | 설명 |
  |------|------|------|------|------|

  - 비어있으면 `⚠️ Payload 미정의 — 이벤트 핸들러가 처리할 데이터 불명.`

## 3. 구현 체크리스트
각 Context 마다:
- [ ] Repository 인터페이스 (Aggregate 별)
- [ ] Domain Service 클래스
- [ ] Domain Event 발행 메커니즘 (in-process / Kafka 등 — Architecture 문서 참조)
- [ ] Event Handler (이벤트 수신 측이 있는 경우)
- [ ] 도메인 단위 테스트

# 작성 규칙
1. **추측 금지**: 입력에 없는 Aggregate/Entity/Event 만들지 마세요. payload 권장만 예외.
2. **요약 금지**: lineage.related_stories 는 있는 그대로 나열.
3. **N/A 가시화**: 빈 곳은 `⚠️ ...` 로 명시.
4. **언어**: 한국어 + 영문 ID/필드명.

# 입력 데이터
<<ddd_json>>

마크다운 형식으로만 출력하세요. 코드블록(```) 없이 순수 마크다운만 반환하세요.
