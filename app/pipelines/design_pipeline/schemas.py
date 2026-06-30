from __future__ import annotations

from typing import Any, Dict


# ─── Structured Output Schemas (2026-05 결정성 강화) ─────────────────────────────
# Gemini responseSchema 로 LLM 출력 형식 강제. normalize_* 가 추가 검증 수행.

# Lineage 공통 구조 — Entity / Aggregate / Service 가 동일 lineage 형태 공유.
# confidence 가 "none" 일 때 related_stories 는 빈 배열.
_LINEAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "confidence": {
            "type": "string",
            "enum": ["direct", "inferred", "none"],
        },
        "related_stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "story_id": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["story_id", "quote"],
            },
        },
    },
    "required": ["confidence", "related_stories"],
}

# [A-2 — 2026-05-25] API payload field 공통 schema.
# request body / response body / path params / query params 의 각 field 가 모두
# 같은 형태 (Entity attribute 와 동일). 한 정의 재사용해 LLM 일관성 보장.
_PAYLOAD_FIELD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string"},
        "required": {"type": "boolean"},
        "constraint": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["name", "type"],
}

# [A-2] request/response body — fields + 예시.
# example 은 자유 형태 (LLM 이 객체 또는 string 으로 줘도 처리).
_REQUEST_BODY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "content_type": {"type": "string"},  # 보통 "application/json"
        "fields": {"type": "array", "items": _PAYLOAD_FIELD_SCHEMA},
        "example": {"type": "string"},  # JSON string 또는 텍스트
    },
}

_RESPONSE_BODY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "integer"},  # 200 / 201 / 204 등 성공 상태
        "content_type": {"type": "string"},
        "fields": {"type": "array", "items": _PAYLOAD_FIELD_SCHEMA},
        "example": {"type": "string"},
    },
}

# [A-3 — 2026-05-25] HTTP 분기 명세.
# PRD 의 에러 케이스 (401/403/404/422 등) 를 SPACK 그래프에 보존해
# 에이전트가 ProblemDetails / 비즈니스 에러 코드 / 메시지를 추측 없이 생성.
_ERROR_CASE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "integer"},      # 4xx/5xx HTTP 상태
        "code": {"type": "string"},          # 비즈니스 에러 코드 (예: PLANT_NOT_FOUND)
        "condition": {"type": "string"},     # 발생 조건 (한국어 한 줄)
        "message": {"type": "string"},       # 사용자 표시 메시지
        "lineage_quote": {"type": "string"}, # PRD 원문 발췌 (있으면)
    },
    "required": ["status"],
}

# [A-3] API 별 인증/권한 매트릭스.
# required=False 면 익명 허용. required_roles=[] 면 인증만 (역할 무관).
# ownership_check 는 자유 텍스트 (LLM 또는 사람이 비교 표현으로 작성).
_AUTH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "required": {"type": "boolean"},
        "required_roles": {"type": "array", "items": {"type": "string"}},
        "ownership_check": {"type": "string"},  # "" 면 ownership 미요구
        "description": {"type": "string"},
    },
}

# [D-2 — 2026-05-25] Service deployment 명세.
# 컨테이너 포트, replica 수, env var 명세, health check path.
# AI 가 Dockerfile / CI/CD / k8s manifest 작성 시 참조.
_DEPLOYMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "port": {"type": "integer"},               # 컨테이너 listen 포트
        "replicas": {"type": "integer"},            # 기본 replica 수 (>=1)
        "health_check_path": {"type": "string"},   # 예: /actuator/health
        "env_vars": {                               # 필요한 환경변수 이름 list
            "type": "array", "items": {"type": "string"},
        },
        "scaling_policy": {"type": "string"},      # manual | auto-cpu | auto-memory
    },
}

# [D-2] 외부 서비스 의존성 (Stripe / OAuth provider / SMS 등).
# Service 별로 어떤 외부 SaaS 와 통합되는지 명세.
_EXTERNAL_DEPENDENCY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},     # Stripe, Auth0, SendGrid 등
        "type": {"type": "string"},     # Payment gateway / OAuth / SMS / 등
        "purpose": {"type": "string"},  # 어디에 쓰는지
    },
    "required": ["name"],
}

SPACK_AGENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "apis": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "method": {"type": "string"},
                    "endpoint": {"type": "string"},
                    "description": {"type": "string"},
                    "related_story_id": {"type": "string"},
                    # [A-2 — 2026-05-25] API payload schema 객체화.
                    # 이전: method/endpoint/description 만 → 에이전트가 request/
                    # response 본문, 경로 파라미터 모두 추측.
                    # 이후: 4개 필드로 PRD 의 contract 보존.
                    # Neo4j 저장은 JSON string 직렬화. read 시 복원.
                    "path_params": {
                        "type": "array",
                        "items": _PAYLOAD_FIELD_SCHEMA,
                    },
                    "query_params": {
                        "type": "array",
                        "items": _PAYLOAD_FIELD_SCHEMA,
                    },
                    "request_body": _REQUEST_BODY_SCHEMA,
                    "response_body": _RESPONSE_BODY_SCHEMA,
                    # [A-3 — 2026-05-25] error_cases + auth.
                    # Neo4j 저장은 JSON string 직렬화. read 시 복원.
                    "error_cases": {
                        "type": "array",
                        "items": _ERROR_CASE_SCHEMA,
                    },
                    "auth": _AUTH_SCHEMA,
                },
                # [2026-06] required 강제 — method/endpoint/description 가 빈 채로 저장되던
                # 비대칭 버그 수정(architecture/policies/screens 는 이미 required 명시).
                # structured output 에 '이 필드는 반드시 채움' 시그널 → 이름만 있는 API 방지.
                "required": ["id", "name", "method", "endpoint", "description"],
            },
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    # [A-1 — 2026-05-25] attributes 객체화 (string list → object list).
                    # 이전: ["plantId", "height"] — 타입/제약 미보존 → 에이전트 추측.
                    # 이후: [{name, type, required, constraint, description}, ...]
                    # Neo4j 저장은 JSON string 직렬화 (primitive list 제약 우회);
                    # read 시 normalize_entity_attributes 가 객체 list 로 복원.
                    # backward compat: 기존 string list 도 read 시 자동 마이그레이트.
                    "attributes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string"},
                                "required": {"type": "boolean"},
                                "constraint": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["name", "type"],
                        },
                    },
                    "description": {"type": "string"},
                    # [B2 — 2026-05 lineage] Entity 는 PRD ↔ design 추적성 필수.
                    "lineage": _LINEAGE_SCHEMA,
                },
                # [2026-06] required 강제 — description 이 빈 채로 저장되던 버그 수정.
                "required": ["id", "name", "description"],
            },
        },
        # [2026-05-28] Policy items 에 required 강제 — id 만 채우고 category/description
        # 비우는 stub 환각 차단. structured output 단계에서 schema validation 으로 retry
        # 유도. normalize_spack 의 POLICY_STUB_DROPPED 가 fallback 안전망.
        "policies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "related_entity": {"type": "string"},
                },
                "required": ["id", "category", "description"],
            },
        },
        # [#3 — 2026-05-25] Screen ↔ API 매핑.
        # 화면 코드 생성에 필요한 정보. AI 에이전트가 라우터/컴포넌트
        # 분할 + API 호출 코드 작성 시 참조. Neo4j 에 :Screen 노드 + RENDERS
        # 관계 (Story-Screen), CALLS_API 관계 (Screen-API) 로 저장.
        "screens": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},          # "SCREEN-01"
                    "name": {"type": "string"},        # "식물 등록 화면"
                    "path": {"type": "string"},        # "/plants/new"
                    "description": {"type": "string"},
                    "related_story_id": {"type": "string"},
                    # 이 화면이 호출하는 SPACK API ID list.
                    "calls_apis": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    # 사용자 화면 흐름 — 이 화면이 어떤 다른 화면으로 전이하나.
                    # ["/plants", "/plants/{id}"] 같은 path list.
                    "next_screens": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["id", "name", "path"],
            },
        },
    },
}

DDD_AGENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "contexts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
        "aggregates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "context_id": {"type": "string"},
                    "description": {"type": "string"},
                    # [B2 — 2026-05 lineage] Aggregate 는 cross-story 가능 (1~3개 Story).
                    "lineage": _LINEAGE_SCHEMA,
                    # [D-1 — 2026-05-25] Aggregate 의 도메인 규칙 (invariants).
                    # 예: "leafCount >= 0", "temperatureMin < temperatureMax".
                    # 한국어 또는 코드식 표현 자유. AI 가 도메인 로직 작성 시 참조.
                    # Neo4j 저장은 JSON string 직렬화 (primitive 제약).
                    "invariants": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "aggregate_id": {"type": "string"},
                    "description": {"type": "string"},
                    # [C — 2026-05 lineage] DomainEntity 도 PRD 추적성 강제
                    "lineage": _LINEAGE_SCHEMA,
                    # [D-1 — 2026-05-25] Domain Entity 의 attributes.
                    # SPACK Entity attributes 와 동일 schema (객체 list) 재사용.
                    "attributes": {
                        "type": "array",
                        "items": _PAYLOAD_FIELD_SCHEMA,
                    },
                },
            },
        },
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "related_story_id": {"type": "string"},
                    "published_by_aggregate_id": {"type": "string"},
                    # [D-1 — 2026-05-25] Event payload — 발행 시 함께 전달되는 데이터.
                    # 예: PlantGrowthDataRecorded → {growthDataId, plantId, height, ...}.
                    # AI 가 event handler 작성 시 무엇을 처리해야 하는지 알 수 있음.
                    "payload_fields": {
                        "type": "array",
                        "items": _PAYLOAD_FIELD_SCHEMA,
                    },
                },
            },
        },
    },
}

# [2026-05-20 Data Layer 누락 버그 수정]
# 이전: required 미정의 → Gemini 가 databases: [] 반환해도 schema 통과.
# Cypher generation 이 빈 배열 시 ArchDatabase MERGE 스킵 → Neo4j 노드 0개 →
# 프론트 Data Layer "0". 시스템 코어 결함이라 schema 레벨에서 강제.
ARCHITECTURE_AGENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "services": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "tech_stack": {"type": "string"},
                    "description": {"type": "string"},
                    # [B2 — 2026-05 lineage] Service 는 1~4 Story 와 연관 가능.
                    "lineage": _LINEAGE_SCHEMA,
                    # [2026-05-20] 프롬프트(design_architecture.md L27, L30, L40)에서
                    # 명시적으로 요구되나 스키마 누락 시 Gemini 가 silently drop.
                    "owned_aggregates": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    # [D-2 — 2026-05-25] deployment / external_dependencies.
                    # Neo4j 저장은 JSON string 직렬화. read 시 복원.
                    "deployment": _DEPLOYMENT_SCHEMA,
                    "external_dependencies": {
                        "type": "array",
                        "items": _EXTERNAL_DEPENDENCY_SCHEMA,
                    },
                },
                "required": ["id", "name", "type", "lineage"],
            },
        },
        "databases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "tech_stack": {"type": "string"},
                    "description": {"type": "string"},
                    # [C — 2026-05 lineage] Database 도 PRD 추적성 강제
                    "lineage": _LINEAGE_SCHEMA,
                },
                "required": ["id", "name", "type", "lineage"],
            },
        },
        "connections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "target_id": {"type": "string"},
                    "protocol": {"type": "string"},
                    "description": {"type": "string"},
                    # [D-2 — 2026-05-25] 연결 인증 방식.
                    # mTLS / bearer / basic / none. AI 가 통신 클라이언트
                    # 구현 시 어떤 토큰/인증 헤더 사용할지 알 수 있음.
                    "auth": {
                        "type": "string",
                        "enum": ["mTLS", "bearer", "basic", "api-key", "none"],
                    },
                },
                "required": ["source_id", "target_id"],
            },
        },
        # [2026-05-20] API ↔ Service 매핑 — 프롬프트(L24, L37, L39)에서 요구되나
        # 스키마 누락 시 LLM 이 출력해도 Gemini 가 drop. 스키마에 명시 → 유지.
        "api_service_mapping": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "api_id": {"type": "string"},
                    "service_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["api_id", "service_id"],
            },
        },
    },
    # [2026-05-20 핵심 수정] 세 필드 모두 LLM 이 반드시 포함하도록 강제.
    # Gemini 구조화 출력에서 required 는 "필드 존재" 강제 + LLM 에 "이 필드는
    # 반드시 채워야 함" 시그널 역할. 프롬프트의 "Database 는 반드시 포함" 지시와
    # 결합돼 LLM 이 빈 배열로 응답할 확률을 크게 낮춤.
    "required": ["services", "databases", "connections"],
}

# Temperature 통일 — 결정성 정책.
_TEMPERATURE = 0.1
