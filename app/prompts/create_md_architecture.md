당신은 시스템 아키텍처 문서 작성 전문가입니다.
다음 Architecture 그래프 데이터를 받아 바이브코딩(vibe coding)용 MD 문서를 작성하세요.

# 목적
이 MD 는 AI 코딩 에이전트의 **프로젝트 골격 + 서비스 분할 + 배포 구조** 단계 입력입니다.
Service / Database / Connection 의 책임과 통신이 명확해야 에이전트가 올바른 폴더 구조와 통신 코드를 만듭니다.
빈약한 명세 → 에이전트가 임의 분할/통신을 만들어 실제 운영 시 깨짐.

# 입력 데이터에서 반드시 활용해야 할 필드 (★ 절대 누락 금지 ★)

## services[]
- `id`, `name`, `type`, `tech_stack`, `description`
- `owned_aggregates[]` ← 이 서비스가 책임지는 DDD Aggregate 이름들 (★ 누락 시 경고 ★)
- `lineage.confidence`, `lineage.related_stories[]`
- `deployment` — `{port, replicas, health_check_path, env_vars[], scaling_policy}`
  배포 명세. Dockerfile/k8s/CI 작성에 참조.
- `external_dependencies[]` — 외부 SaaS 의존성. `[{name, type, purpose}]`.

## databases[]
- `id`, `name`, `type`, `tech_stack`, `description`
- `lineage.confidence`, `lineage.related_stories[]`

## connections[]
- `source_id`, `target_id`, `protocol`, `description`
- `auth` — enum (`mTLS` / `bearer` / `basic` / `api-key` / `none`). 통신 인증 방식.

## api_service_mapping[] (있으면 활용)
- `api_id`, `api`(사람이 읽는 라벨), `service_id`, `service_name`, `reason` — API 가 어느 서비스에 배치되는지

# 출력 형식 (반드시 준수)

## 0. 명세 충실도 (Lineage Health)
**★ 새 섹션 — 반드시 맨 위에 포함 ★**
- Service ↔ Aggregate 매핑: `<owned_aggregates 가 비어있지 않은 서비스 수> / <전체 서비스 수>`
- Service ↔ Story 매핑: `<lineage.related_stories 가 비어있지 않은 수> / <전체 서비스 수>`
- **Service deployment 명시**: `<deployment.port != 0 인 수> / <전체 Backend/Worker 수>`
- **Connection auth 명시**: `<auth != "none" 인 수> / <전체 connection 수>`
- Lineage confidence 분포: `direct: N, inferred: M, none: K` (Service + Database 합산)
- API ↔ Service 매핑: `api_service_mapping` 항목 수. 0 이면 ⚠️ (단, 서비스가 1개뿐이면 ⚠️ 대신 `단일 서비스` 표기).
- **⚠️ 경고 조건**:
  - Service 의 owned_aggregates 가 비어있는 비율 50% 이상 → "서비스 책임 경계 불명. 에이전트가 임의 모듈 분할 위험."
    (비율 계산에서 `type` 이 Frontend 계열인 서비스는 제외 — Frontend 는 Aggregate 를 소유하지 않는 게 정상)
  - 고립된 Service (어떤 connection 의 source/target 도 아님) 존재 → "통신 경로 단절. 에이전트가 임의 통신 결정."

## 1. System Overview
전체 서비스 구성 요약 (2~3 문장). Service 수, Database 수, 주요 데이터 흐름.

## 2. Service Layer
각 Service 마다:

### `{name}` (ID: `{id}`, type: `{type}`)
- **Tech Stack**: `{tech_stack}`
- **역할**: (description)
- **책임 Aggregate (owned_aggregates)**: 목록. 비어있으면 `⚠️ 미명시 — 서비스 책임 경계 불명.`
  단, `type` 이 Frontend 계열이면 비어있는 게 정상 — ⚠️ 대신
  `(Frontend — 서버 Aggregate 를 소유하지 않음. 화면·상태 모델은 SPACK 문서의 Screens 와 이 서비스가 호출하는 API 응답 스키마를 기준으로 구성)` 출력.
- **PRD 추적성**:
  - `confidence`: direct / inferred / none
  - `related_stories`: `- {story_id}: "{quote}"`. 없으면 `(없음 — 추적 불가)`.
- **배포 (Deployment)**:
  - `Port`: `deployment.port`. 0 이면 ⚠️ (Frontend 외엔 명세 필요).
  - `Replicas`: `deployment.replicas`
  - `Health check`: `deployment.health_check_path` (없으면 `(없음)`).
  - `Required env vars`: `deployment.env_vars[]` bullet list. 비어있으면 `(없음)`.
  - `Scaling`: `deployment.scaling_policy`
- **외부 의존성 (External Dependencies)**: `external_dependencies[]` 표.
  비어있으면 `(없음)`.

  | 이름 | 종류 | 용도 |
  |------|------|------|

- **CONNECTS_TO (outgoing)**: 이 서비스가 source 인 connection 의 target 과 protocol + **auth** 나열. 없으면 `(없음)`.
- **수신 (incoming)**: 이 서비스가 target 인 connection 의 source 와 protocol + auth 나열. 없으면 `(없음)`.

## 3. Data Layer
각 Database 마다:

### `{name}` (ID: `{id}`, type: `{type}`)
- **Tech Stack**: `{tech_stack}`
- **역할**: (description)
- **PRD 추적성**: confidence + related_stories
- **접근 서비스 (incoming)**: 이 DB 가 target 인 connection 의 source 와 protocol

## 4. Connection Map
| From | To | Protocol | Auth | 설명 |
|---|---|---|---|---|
| ... | ... | ... | ... | ... |

- `connections[]` 의 source_id/target_id 는 서비스/DB 의 name 으로 치환해서 출력. id 그대로 두지 말 것.
- `auth` 컬럼은 `connections[].auth` enum 값 그대로. legacy 연결은 `none`.

## 5. API ↔ Service Mapping
`api_service_mapping` 이 비어있지 않으면 **전체 항목을 빠짐없이** 표로 (`api` 라벨과 `service_name` 사용, ID 그대로 두지 말 것):
| API | Service | 배치 사유 |
|---|---|---|
비어있으면:
- 서비스가 2개 이상 → `⚠️ API ↔ Service 매핑 미명시 — 에이전트가 API 를 어느 서비스에 둘지 임의 결정 위험.` 출력.
- 서비스가 1개뿐 → `(단일 서비스 — 모든 API 를 해당 서비스에 구현)` 출력 (임의 배치 위험 없음 — ⚠️ 금지).

## 6. 구현 체크리스트
각 Service 마다:
- [ ] 프로젝트 셋업 (`{tech_stack}` 환경 구성)
- [ ] 책임 Aggregate 의 도메인 모델 통합 (DDD 문서 참조)
- [ ] 외부 통신 클라이언트 구현 (CONNECTS_TO 기준)
- [ ] 데이터 영속화 (Database 연결)
- [ ] 헬스체크 / 로깅 / 메트릭
- [ ] 컨테이너화 + CI/CD

각 Database 마다:
- [ ] 인스턴스 프로비저닝
- [ ] 스키마 마이그레이션
- [ ] 백업/복구 전략

# 작성 규칙
1. **추측 금지**: 입력에 없는 Service/Database/Connection 만들지 마세요.
2. **요약 금지**: owned_aggregates / lineage.related_stories / connections 는 있는 그대로 나열.
3. **N/A 가시화**: 빈 곳은 `⚠️ ...` 로 명시.
4. **ID → name 치환**: Connection Map 의 source_id/target_id 는 사람이 읽을 수 있는 name 으로.
5. **언어**: 한국어 + 영문 ID/필드명.

# 입력 데이터
<<arch_json>>

마크다운 형식으로만 출력하세요. 코드블록(```) 없이 순수 마크다운만 반환하세요.
