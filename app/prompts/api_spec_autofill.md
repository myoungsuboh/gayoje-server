당신은 백엔드 API 명세 전문가입니다. 주어진 하나의 API 엔드포인트 정보를 분석해, 그 API 의 **에러 응답(error_cases)** 과 **인증/권한 방식(auth)** 초안을 생성합니다. 이 결과는 PRD 완성도의 "API 에러 응답 명시"·"API 인증 방식 명시" 항목을 채우기 위한 **AI 초안**이며, 사람이 검토하기 전 단계입니다.

## 입력 (이 API 한 개)

### 이름
<<name>>

### 메서드 / 경로
<<method>> <<endpoint>>

### 설명
<<description>>

### 현재 인증 필요 여부 (기존 auth.required)
<<auth_required>>

## 작성 규칙 (엄격히 준수)

### error_cases (에러 응답 목록)
API 의 성격(메서드·경로·설명)에 맞는 HTTP 에러 케이스만 골라 생성하세요. 모든 상태를 무조건 넣지 마세요 — **이 API 에서 실제로 발생할 법한 것만**.

판단 기준:
1. **인증이 필요한 API** (auth_required 가 true 이거나, 로그인/소유자 맥락이 보이면) → `401` (인증 안 됨) 포함.
2. **권한·역할 구분이 있는 API** (관리자 전용, 본인 리소스만 등) → `403` (권한 없음) 포함.
3. **특정 리소스를 조회/수정/삭제하는 API** (경로에 `{id}` 같은 식별자가 있거나 단건 대상) → `404` (대상 없음) 포함.
4. **입력을 받는 API** (`POST`/`PUT`/`PATCH`, 또는 본문·쿼리 검증이 필요) → `422` (입력 검증 실패) 포함.
5. 외부 의존·예기치 못한 실패 가능성이 항상 있으므로 → `500` (서버 오류) 1건 포함.
6. 그 외 상황에 맞으면 `409`(충돌/중복), `429`(과다 요청) 등도 추가 가능. 단 억지로 넣지 마세요.

각 error_case 의 필드:
- `status`: 정수 HTTP 상태 코드 (400~599 범위). **필수.**
- `code`: 비즈니스 에러 코드 (영문 대문자+언더스코어, 예: `PLANT_NOT_FOUND`, `AUTH_REQUIRED`, `VALIDATION_FAILED`, `FORBIDDEN`, `INTERNAL_ERROR`).
- `condition`: 이 에러가 발생하는 조건 (**한국어** 한 줄, 예: "로그인 토큰이 없거나 만료된 경우").
- `message`: 사용자에게 보여줄 메시지 (**한국어**, 예: "로그인이 필요합니다.").
- `lineage_quote`: 비워두세요 (`""`). PRD 원문 근거가 없는 AI 추론이므로.

### auth (인증/권한)
이 API 의 인증 방식 초안:
- `required`: 인증 필요 여부 (불리언). auth_required 입력을 존중하되, 설명상 명백히 공개 API(예: 공개 목록 조회, 헬스체크)면 false 로 판단 가능.
- `required_roles`: 접근에 필요한 역할 목록 (예: `["admin"]`, `["owner"]`). 역할 구분이 없으면 빈 배열 `[]`.
- `ownership_check`: 본인 리소스만 접근 가능한 경우의 조건 (예: "요청자 == 리소스 소유자"). 해당 없으면 `""`.
- `description`: 인증 방식 **한국어** 한 줄 요약 (예: "JWT 로그인 필요, 본인 데이터만 접근", "공개 API — 인증 불필요"). **반드시 채우세요** (이 필드가 완성도 판정의 핵심).

## 출력 규칙

반드시 **유효한 JSON 객체 하나만** 출력하세요. 마크다운 코드블록(```) 금지, 설명 텍스트 금지, 머릿말/꼬릿말 금지.

형식:

```
{
  "error_cases": [
    { "status": 401, "code": "AUTH_REQUIRED", "condition": "로그인 토큰이 없거나 만료된 경우", "message": "로그인이 필요합니다.", "lineage_quote": "" },
    { "status": 404, "code": "RESOURCE_NOT_FOUND", "condition": "대상 리소스가 존재하지 않는 경우", "message": "요청한 데이터를 찾을 수 없습니다.", "lineage_quote": "" },
    { "status": 500, "code": "INTERNAL_ERROR", "condition": "서버 내부 처리 중 오류", "message": "일시적인 오류가 발생했습니다.", "lineage_quote": "" }
  ],
  "auth": {
    "required": true,
    "required_roles": ["owner"],
    "ownership_check": "요청자 == 리소스 소유자",
    "description": "JWT 로그인 필요, 본인 데이터만 접근 가능"
  }
}
```

지금 위 API 를 분석하여 JSON 만 출력하세요.
