"""
Design lineage Neo4j 저장 단위 테스트 (B4).

[검증 대상]
- _to_neo4j_story_id: 'Story-XX.Y' → 'story_XX_Y' 변환
- _extract_lineage_edges: 노드 list 에서 엣지 평탄화
- build_save_*_query: lineage_confidence properties + DERIVED_FROM 엣지 cypher
"""
from __future__ import annotations

from app.pipelines.design_pipeline import (
    _extract_lineage_edges,
    _to_neo4j_story_id,
    build_save_architecture_query,
    build_save_ddd_query,
    build_save_spack_query,
)


# ─── _to_neo4j_story_id ────────────────────────────────────────


class TestToNeo4jStoryId:
    def test_basic_conversion(self):
        assert _to_neo4j_story_id("Story-01.1") == "story_01_1"
        assert _to_neo4j_story_id("Story-12.3") == "story_12_3"

    def test_already_padded_preserved(self):
        assert _to_neo4j_story_id("Story-09.2") == "story_09_2"

    def test_invalid_returns_none(self):
        assert _to_neo4j_story_id("") is None
        assert _to_neo4j_story_id("garbage") is None
        assert _to_neo4j_story_id("Story 1.1") is None  # 정규화 안 된 형태
        assert _to_neo4j_story_id(None) is None


# ─── _extract_lineage_edges ────────────────────────────────────


class TestExtractLineageEdges:
    def test_no_lineage_no_edges(self):
        items = [{"id": "ENT-01", "name": "X"}]
        assert _extract_lineage_edges(items) == []

    def test_confidence_none_no_edges(self):
        items = [{
            "id": "ENT-01",
            "lineage": {"confidence": "none", "related_stories": []},
        }]
        assert _extract_lineage_edges(items) == []

    def test_direct_and_inferred_both_create_edges(self):
        """결정 3-B: direct + inferred 모두 엣지 생성."""
        items = [
            {
                "id": "ENT-01",
                "lineage": {
                    "confidence": "direct",
                    "related_stories": [{"story_id": "Story-01.1", "quote": "a"}],
                },
            },
            {
                "id": "ENT-02",
                "lineage": {
                    "confidence": "inferred",
                    "related_stories": [{"story_id": "Story-02.1", "quote": "b"}],
                },
            },
        ]
        edges = _extract_lineage_edges(items)
        assert len(edges) == 2
        confidences = {e["confidence"] for e in edges}
        assert confidences == {"direct", "inferred"}

    def test_multiple_stories_per_node_all_edges(self):
        items = [{
            "id": "AGG-01",
            "lineage": {
                "confidence": "direct",
                "related_stories": [
                    {"story_id": "Story-01.1", "quote": "x"},
                    {"story_id": "Story-02.3", "quote": "y"},
                ],
            },
        }]
        edges = _extract_lineage_edges(items)
        assert len(edges) == 2
        assert edges[0]["src_id"] == "AGG-01"
        assert edges[0]["story_neo4j_id"] == "story_01_1"
        assert edges[1]["story_neo4j_id"] == "story_02_3"

    def test_unnormalizable_story_id_dropped(self):
        items = [{
            "id": "ENT-01",
            "lineage": {
                "confidence": "direct",
                "related_stories": [
                    {"story_id": "Story-01.1", "quote": "ok"},
                    {"story_id": "garbage", "quote": "drop"},
                ],
            },
        }]
        edges = _extract_lineage_edges(items)
        assert len(edges) == 1
        assert edges[0]["story_neo4j_id"] == "story_01_1"


# ─── build_save_spack_query — API/Screen Story 연결 (2026-06 연결 fix) ──────


class TestApiScreenStoryLinkage:
    """[연결 0% 근본 원인] API/Screen 의 related_story_id('Story-XX.Y')를 Story 노드
    id('story_XX_Y') 형식으로 변환해 매칭해야 IMPLEMENTS/RENDERS 엣지가 생긴다.
    이전엔 raw 매칭이라 엣지가 절대 안 만들어져 연결 0% 고착."""

    def test_api_related_story_id_converted_and_stored(self):
        spack = {
            "apis": [{
                "id": "API-01", "name": "login", "method": "POST",
                "endpoint": "/login", "related_story_id": "Story-01.1",
            }],
            "entities": [], "policies": [],
        }
        cypher, params = build_save_spack_query("p1", spack)

        # 1) 매칭용 id 가 story_01_1 로 변환돼 params 에 실림
        assert params["apis"][0]["_story_match_id"] == "story_01_1"
        # 2) IMPLEMENTS 매칭이 변환된 id 를 사용 (raw 아님)
        assert "OPTIONAL MATCH (s:Story {id: apiData._story_match_id" in cypher
        assert "MERGE (api)-[:IMPLEMENTS]->(s)" in cypher
        # 3) related_story_id 를 노드 속성으로도 저장(안전망)
        assert "api.related_story_id = apiData.related_story_id" in cypher

    def test_screen_related_story_id_converted_and_stored(self):
        spack = {
            "apis": [], "entities": [], "policies": [],
            "screens": [{
                "id": "SCREEN-01", "name": "로그인", "path": "/login",
                "related_story_id": "Story-02.3",
            }],
        }
        cypher, params = build_save_spack_query("p1", spack)

        assert params["screens"][0]["_story_match_id"] == "story_02_3"
        assert "OPTIONAL MATCH (s:Story {id: scData._story_match_id" in cypher
        assert "MERGE (sc)-[:RENDERS]->(s)" in cypher
        assert "sc.related_story_id = scData.related_story_id" in cypher

    def test_already_neo4j_form_preserved(self):
        """related_story_id 가 이미 story_XX_Y 형식이면 그대로 매칭(변환 불가 → 원본)."""
        spack = {
            "apis": [{"id": "API-01", "name": "x", "method": "GET",
                      "endpoint": "/x", "related_story_id": "story_03_2"}],
            "entities": [], "policies": [],
        }
        _, params = build_save_spack_query("p1", spack)
        assert params["apis"][0]["_story_match_id"] == "story_03_2"


# ─── build_save_spack_query — Entity lineage ───────────────────


class TestSpackQueryWithLineage:
    def test_entity_lineage_properties_in_cypher(self):
        spack = {
            "apis": [],
            "entities": [{
                "id": "ENT-01", "name": "Ticket",
                "lineage": {
                    "confidence": "direct",
                    "related_stories": [{"story_id": "Story-01.1", "quote": "발행"}],
                },
            }],
            "policies": [],
        }
        cypher, params = build_save_spack_query("p1", spack)

        assert "ent.lineage_confidence = entData._lineage_confidence" in cypher
        assert "MERGE (src)-[r:DERIVED_FROM]->(story)" in cypher
        assert "entity_lineage_edges" in params
        edges = params["entity_lineage_edges"]
        assert len(edges) == 1
        assert edges[0]["story_neo4j_id"] == "story_01_1"

    def test_no_lineage_edges_means_no_derived_from_chunk_with_data(self):
        spack = {
            "apis": [],
            "entities": [{
                "id": "ENT-01", "name": "Ticket",
                "lineage": {"confidence": "none", "related_stories": []},
            }],
            "policies": [],
        }
        _, params = build_save_spack_query("p1", spack)
        # cypher 구조는 들어가지만 UNWIND 가 빈 list 면 실제 실행은 0건
        assert params["entity_lineage_edges"] == []


# ─── build_save_ddd_query — Aggregate lineage ──────────────────


class TestDddQueryWithLineage:
    def test_aggregate_lineage_in_cypher(self):
        ddd = {
            "contexts": [],
            "aggregates": [{
                "id": "AGG-01", "name": "Ticket", "context_id": "CTX-01",
                "lineage": {
                    "confidence": "direct",
                    "related_stories": [
                        {"story_id": "Story-01.1", "quote": "발행"},
                        {"story_id": "Story-02.3", "quote": "조회"},
                    ],
                },
            }],
            "entities": [],
            "events": [],
            "spack_entity_mapping": [],
        }
        cypher, params = build_save_ddd_query("p1", ddd)
        assert "agg.lineage_confidence" in cypher
        assert "aggregate_lineage_edges" in params
        assert len(params["aggregate_lineage_edges"]) == 2


# ─── build_save_architecture_query — Service lineage ───────────


class TestArchQueryWithLineage:
    def test_service_lineage_in_cypher(self):
        arch = {
            "services": [{
                "id": "SVC-01", "name": "Frontend", "type": "Frontend",
                "tech_stack": "Vue.js",
                "lineage": {
                    "confidence": "inferred",
                    "related_stories": [
                        {"story_id": "Story-01.1", "quote": "모바일"},
                    ],
                },
            }],
            "databases": [],
            "connections": [],
            "api_service_mapping": [],
        }
        cypher, params = build_save_architecture_query("p1", arch)
        assert "svc.lineage_confidence" in cypher
        assert "service_lineage_edges" in params
        edges = params["service_lineage_edges"]
        assert len(edges) == 1
        assert edges[0]["confidence"] == "inferred"

    def test_build_does_not_mutate_input_with_underscore_fields(self):
        """[점검 8 — 2026-05] build_save_*_query 가 원본 dict 에 _lineage_*
        필드를 추가하지 않음을 보장. 응답 구조 보존 (frontend 호환) 의 핵심."""
        spack = {
            "apis": [],
            "entities": [{
                "id": "ENT-01", "name": "Ticket",
                "lineage": {"confidence": "direct",
                            "related_stories": [{"story_id": "Story-01.1", "quote": "q"}]},
            }],
            "policies": [],
        }
        original_keys = set(spack["entities"][0].keys())
        build_save_spack_query("p1", spack)
        after_keys = set(spack["entities"][0].keys())
        # 원본 entity 에 _lineage_* 같은 underscore prefix 필드가 추가되면 안 됨.
        assert original_keys == after_keys, (
            f"build_save_spack_query 가 원본을 mutate 함! "
            f"추가된 키: {after_keys - original_keys}"
        )

        ddd = {
            "contexts": [], "entities": [], "events": [],
            "aggregates": [{
                "id": "AGG-01", "name": "Ticket", "context_id": "CTX-01",
                "lineage": {"confidence": "direct",
                            "related_stories": [{"story_id": "Story-01.1", "quote": "q"}]},
            }],
            "spack_entity_mapping": [],
        }
        orig = set(ddd["aggregates"][0].keys())
        build_save_ddd_query("p1", ddd)
        assert orig == set(ddd["aggregates"][0].keys())

        arch = {
            "services": [{
                "id": "SVC-01", "name": "Front", "type": "Frontend",
                "tech_stack": "Vue.js",
                "lineage": {"confidence": "inferred",
                            "related_stories": [{"story_id": "Story-01.1", "quote": "q"}]},
            }],
            "databases": [], "connections": [], "api_service_mapping": [],
        }
        orig = set(arch["services"][0].keys())
        build_save_architecture_query("p1", arch)
        assert orig == set(arch["services"][0].keys())

    def test_cross_story_service_has_multiple_edges(self):
        arch = {
            "services": [{
                "id": "SVC-02", "name": "Ticket Service", "type": "Backend API",
                "tech_stack": "Spring Boot",
                "lineage": {
                    "confidence": "direct",
                    "related_stories": [
                        {"story_id": "Story-01.1", "quote": "발행"},
                        {"story_id": "Story-02.3", "quote": "조회"},
                        {"story_id": "Story-03.1", "quote": "정산"},
                    ],
                },
            }],
            "databases": [],
            "connections": [],
            "api_service_mapping": [],
        }
        _, params = build_save_architecture_query("p1", arch)
        edges = params["service_lineage_edges"]
        assert len(edges) == 3
        assert {e["story_neo4j_id"] for e in edges} == {
            "story_01_1", "story_02_3", "story_03_1",
        }


# ─── [C — 2026-05] DomainEntity / Database lineage ─────────────


class TestDomainEntityLineage:
    def test_ddd_domain_entity_lineage_in_cypher(self):
        from app.pipelines.design_pipeline import build_save_ddd_query
        ddd = {
            "contexts": [],
            "aggregates": [],
            "entities": [{
                "id": "DENT-01", "name": "TicketTransaction",
                "aggregate_id": "AGG-01", "description": "d",
                "lineage": {
                    "confidence": "direct",
                    "related_stories": [
                        {"story_id": "Story-01.1", "quote": "충전/사용 내역"},
                    ],
                },
            }],
            "events": [],
            "spack_entity_mapping": [],
        }
        cypher, params = build_save_ddd_query("p1", ddd)
        assert "dent.lineage_confidence" in cypher
        assert "domain_entity_lineage_edges" in params
        assert len(params["domain_entity_lineage_edges"]) == 1
        # _lineage_cypher_chunk 가 DomainEntity 라벨로 호출됐는지
        assert ":DomainEntity {" in cypher


class TestDatabaseLineage:
    def test_arch_database_lineage_in_cypher(self):
        from app.pipelines.design_pipeline import build_save_architecture_query
        arch = {
            "services": [],
            "databases": [{
                "id": "DB-01", "name": "RDBMS", "type": "Relational Database",
                "tech_stack": "PostgreSQL",
                "lineage": {
                    "confidence": "inferred",
                    "related_stories": [
                        {"story_id": "Story-01.1", "quote": "원장 데이터 보관"},
                    ],
                },
            }],
            "connections": [], "api_service_mapping": [],
        }
        cypher, params = build_save_architecture_query("p1", arch)
        assert "db.lineage_confidence" in cypher
        assert "database_lineage_edges" in params
        assert ":ArchDatabase {" in cypher
        edges = params["database_lineage_edges"]
        assert len(edges) == 1
        assert edges[0]["confidence"] == "inferred"

    def test_database_mutation_blocked(self):
        """build_save_architecture_query 가 database 원본 mutate 하지 않음."""
        from app.pipelines.design_pipeline import build_save_architecture_query
        arch = {
            "services": [],
            "databases": [{
                "id": "DB-01", "name": "RDBMS", "tech_stack": "PostgreSQL",
                "lineage": {"confidence": "inferred",
                            "related_stories": [{"story_id": "Story-01.1", "quote": "q"}]},
            }],
            "connections": [], "api_service_mapping": [],
        }
        orig = set(arch["databases"][0].keys())
        build_save_architecture_query("p1", arch)
        assert orig == set(arch["databases"][0].keys())


# ─── [A-1 — 2026-05-25] Entity attributes 직렬화/역직렬화 ──────────────


class TestEntityAttributesSerialization:
    """Neo4j 저장 시 attributes 가 JSON string 으로 직렬화되는지, read 헬퍼가
    그 string 을 객체 list 로 복원하는지 round-trip 검증."""

    def test_attributes_serialized_as_json_string_in_params(self):
        """build_save_spack_query 의 params 에서 entity.attributes 가 string 형."""
        spack = {
            "apis": [],
            "entities": [
                {
                    "id": "ENT-01",
                    "name": "Plant",
                    "attributes": [
                        {"name": "id", "type": "uuid", "required": True,
                         "constraint": "", "description": ""},
                        {"name": "height", "type": "double", "required": True,
                         "constraint": ">0", "description": "cm 단위"},
                    ],
                    "lineage": {"confidence": "direct",
                                "related_stories": [{"story_id": "Story-01.1", "quote": "q"}]},
                }
            ],
            "policies": [],
        }
        _, params = build_save_spack_query("p1", spack)
        ent_params = params["entities"][0]
        # JSON string 으로 직렬화돼야 함 — Neo4j primitive 제약 우회.
        assert isinstance(ent_params["attributes"], str)
        # 비ASCII 보존
        assert "cm 단위" in ent_params["attributes"]
        # 원본 dict mutate 금지 확인
        assert isinstance(spack["entities"][0]["attributes"], list)
        assert spack["entities"][0]["attributes"][0]["name"] == "id"

    def test_legacy_string_list_attributes_serialized_too(self):
        """legacy schema (string list) 가 들어와도 JSON string 으로 직렬화."""
        spack = {
            "apis": [],
            "entities": [
                {
                    "id": "ENT-01",
                    "name": "Plant",
                    "attributes": ["id", "height"],
                }
            ],
            "policies": [],
        }
        _, params = build_save_spack_query("p1", spack)
        serialized = params["entities"][0]["attributes"]
        assert isinstance(serialized, str)
        # 객체 list 로 마이그레이트된 형태로 저장
        assert '"name": "id"' in serialized
        assert '"type": "unknown"' in serialized

    def test_round_trip_via_decode_entities_attributes(self):
        """직렬화 → decode 가 원래 객체 list 로 복원."""
        from app.pipelines.design_validator.attributes import (
            decode_entities_attributes,
        )

        original = [
            {"name": "id", "type": "uuid", "required": True,
             "constraint": "", "description": ""},
            {"name": "height", "type": "double", "required": True,
             "constraint": ">0", "description": "cm"},
        ]
        spack = {
            "apis": [],
            "entities": [
                {"id": "ENT-01", "name": "Plant", "attributes": original}
            ],
            "policies": [],
        }
        _, params = build_save_spack_query("p1", spack)
        # Neo4j 가 반환한다고 가정한 fake row
        fake_neo_response = [{"attributes": params["entities"][0]["attributes"],
                              "name": "Plant", "id": "ENT-01"}]
        restored = decode_entities_attributes(fake_neo_response)
        assert restored[0]["attributes"] == original


# ─── [A-2 — 2026-05-25] API payload 직렬화/복원 ─────────────────────────


class TestApiPayloadSerialization:
    """API 노드 저장 시 4개 payload 필드가 JSON string 으로 직렬화되는지,
    read 가 객체로 복원하는지 round-trip 검증."""

    def test_api_payload_serialized_as_json_strings_in_params(self):
        spack = {
            "apis": [
                {
                    "id": "API-01",
                    "name": "기록 생성",
                    "method": "POST",
                    "endpoint": "/api/v1/plants/{plantId}/growth",
                    "description": "생장 데이터 기록",
                    "related_story_id": "Story-03.1",
                    "path_params": [
                        {"name": "plantId", "type": "uuid", "required": True,
                         "constraint": "", "description": "식물 식별자"}
                    ],
                    "query_params": [],
                    "request_body": {
                        "content_type": "application/json",
                        "fields": [
                            {"name": "height", "type": "double", "required": True,
                             "constraint": ">0", "description": "cm 단위"},
                        ],
                        "example": "",
                    },
                    "response_body": {
                        "status": 201,
                        "content_type": "application/json",
                        "fields": [
                            {"name": "id", "type": "uuid", "required": True,
                             "constraint": "", "description": ""},
                        ],
                        "example": "",
                    },
                }
            ],
            "entities": [],
            "policies": [],
        }
        _, params = build_save_spack_query("p1", spack)
        api_params = params["apis"][0]
        # 4개 필드 모두 string 으로 직렬화
        assert isinstance(api_params["path_params"], str)
        assert isinstance(api_params["query_params"], str)
        assert isinstance(api_params["request_body"], str)
        assert isinstance(api_params["response_body"], str)
        # 비ASCII 보존
        assert "cm 단위" in api_params["request_body"]
        # 원본 mutate 회피
        assert isinstance(spack["apis"][0]["request_body"], dict)
        assert spack["apis"][0]["request_body"]["fields"][0]["name"] == "height"

    def test_round_trip_via_decode_apis_payload(self):
        from app.pipelines.design_validator.api_payload import decode_apis_payload

        original_req = {
            "content_type": "application/json",
            "fields": [
                {"name": "height", "type": "double", "required": True,
                 "constraint": ">0", "description": "cm 단위"},
            ],
            "example": "",
        }
        spack = {
            "apis": [
                {
                    "id": "API-01", "name": "create", "method": "POST",
                    "endpoint": "/x", "description": "...",
                    "path_params": [], "query_params": [],
                    "request_body": original_req,
                    "response_body": {
                        "status": 201, "content_type": "", "fields": [], "example": ""
                    },
                }
            ],
            "entities": [], "policies": [],
        }
        _, params = build_save_spack_query("p1", spack)
        # Neo4j 반환 시뮬레이션
        ap = params["apis"][0]
        fake_row = [{
            "id": "API-01", "name": "create", "method": "POST",
            "endpoint": "/x", "description": "...",
            "path_params": ap["path_params"],
            "query_params": ap["query_params"],
            "request_body": ap["request_body"],
            "response_body": ap["response_body"],
        }]
        restored = decode_apis_payload(fake_row)
        assert restored[0]["request_body"] == original_req
        assert restored[0]["response_body"]["status"] == 201

    def test_cypher_contains_payload_set_clauses(self):
        """SET 절에 4개 payload 필드가 모두 포함."""
        spack = {
            "apis": [{
                "id": "API-01", "name": "x", "method": "GET", "endpoint": "/x",
                "description": "y",
            }],
            "entities": [], "policies": [],
        }
        cypher, _ = build_save_spack_query("p1", spack)
        assert "api.path_params = apiData.path_params" in cypher
        assert "api.query_params = apiData.query_params" in cypher
        assert "api.request_body = apiData.request_body" in cypher
        assert "api.response_body = apiData.response_body" in cypher


# ─── [A-3 — 2026-05-25] error_cases + auth 직렬화/복원 ──────────────────


class TestApiErrorAndAuthSerialization:
    def test_error_cases_and_auth_serialized_in_params(self):
        spack = {
            "apis": [{
                "id": "API-01", "name": "create growth", "method": "POST",
                "endpoint": "/plants/{plantId}/growth", "description": "...",
                "related_story_id": "Story-03.1",
                "error_cases": [
                    {"status": 401, "code": "AUTH_REQUIRED",
                     "message": "인증 필요"},
                    {"status": 422, "code": "VALIDATION_ERROR"},
                ],
                "auth": {
                    "required": True,
                    "required_roles": ["owner"],
                    "ownership_check": "Plant.ownerId == requester.userId",
                    "description": "본인 식물만",
                },
            }],
            "entities": [], "policies": [],
        }
        _, params = build_save_spack_query("p1", spack)
        ap = params["apis"][0]
        # 둘 다 string 직렬화
        assert isinstance(ap["error_cases"], str)
        assert isinstance(ap["auth"], str)
        # 비ASCII 보존
        assert "인증 필요" in ap["error_cases"]
        assert "본인 식물만" in ap["auth"]
        # status 가 sort 됨 (결정성)
        assert ap["error_cases"].index('"status": 401') < ap["error_cases"].index('"status": 422')

    def test_cypher_contains_error_cases_and_auth_set(self):
        spack = {
            "apis": [{
                "id": "API-01", "name": "x", "method": "GET",
                "endpoint": "/x", "description": "y",
            }],
            "entities": [], "policies": [],
        }
        cypher, _ = build_save_spack_query("p1", spack)
        assert "api.error_cases = apiData.error_cases" in cypher
        assert "api.auth = apiData.auth" in cypher

    def test_round_trip_error_cases_and_auth(self):
        from app.pipelines.design_validator.api_payload import decode_apis_payload

        spack = {
            "apis": [{
                "id": "API-01", "name": "x", "method": "POST",
                "endpoint": "/x", "description": "y",
                "error_cases": [
                    {"status": 404, "code": "NOT_FOUND",
                     "condition": "리소스 없음", "message": "찾을 수 없음"},
                ],
                "auth": {
                    "required": True, "required_roles": ["admin"],
                    "ownership_check": "",
                    "description": "관리자만",
                },
            }],
            "entities": [], "policies": [],
        }
        _, params = build_save_spack_query("p1", spack)
        ap = params["apis"][0]
        fake_neo_row = [{
            "id": "API-01", "name": "x", "method": "POST",
            "endpoint": "/x", "description": "y",
            "path_params": ap["path_params"], "query_params": ap["query_params"],
            "request_body": ap["request_body"], "response_body": ap["response_body"],
            "error_cases": ap["error_cases"], "auth": ap["auth"],
        }]
        restored = decode_apis_payload(fake_neo_row)
        # error_cases 복원
        assert len(restored[0]["error_cases"]) == 1
        assert restored[0]["error_cases"][0]["status"] == 404
        assert restored[0]["error_cases"][0]["condition"] == "리소스 없음"
        # auth 복원
        assert restored[0]["auth"]["required"] is True
        assert restored[0]["auth"]["required_roles"] == ["admin"]
        assert restored[0]["auth"]["description"] == "관리자만"

    def test_legacy_api_without_error_cases_auth_yields_defaults(self):
        """기존 API 노드 (필드 미존재) 도 read 시 안전 default."""
        from app.pipelines.design_validator.api_payload import decode_apis_payload

        legacy = [{"id": "API-01", "method": "GET", "endpoint": "/x"}]
        restored = decode_apis_payload(legacy)
        assert restored[0]["error_cases"] == []
        # auth default — required=True (보수적), 빈 roles
        assert restored[0]["auth"]["required"] is True
        assert restored[0]["auth"]["required_roles"] == []


# ─── [D-1 — 2026-05-25] DDD detail 직렬화/복원 ───────────────────────────


class TestDddDetailSerialization:
    def test_aggregate_invariants_serialized(self):
        ddd = {
            "contexts": [],
            "aggregates": [{
                "id": "AGG-01", "name": "Plant", "context_id": "CTX-01",
                "lineage": {"confidence": "direct",
                            "related_stories": [{"story_id": "Story-01.1", "quote": "q"}]},
                "invariants": ["leafCount >= 0", "temperatureMin < temperatureMax"],
            }],
            "entities": [], "events": [],
            "spack_entity_mapping": [],
        }
        cypher, params = build_save_ddd_query("p1", ddd)
        agg_p = params["aggregates"][0]
        assert isinstance(agg_p["invariants"], str)
        assert "leafCount" in agg_p["invariants"]
        # SET 절에 invariants
        assert "agg.invariants = aggData.invariants" in cypher
        # 원본 mutate 회피
        assert isinstance(ddd["aggregates"][0]["invariants"], list)

    def test_domain_entity_attributes_serialized(self):
        ddd = {
            "contexts": [],
            "aggregates": [],
            "entities": [{
                "id": "DE-01", "name": "PlantGrowthData",
                "aggregate_id": "AGG-01",
                "lineage": {"confidence": "direct",
                            "related_stories": [{"story_id": "Story-01.1", "quote": "q"}]},
                "attributes": [
                    {"name": "height", "type": "double", "required": True,
                     "constraint": ">0", "description": "cm"},
                ],
            }],
            "events": [],
            "spack_entity_mapping": [],
        }
        cypher, params = build_save_ddd_query("p1", ddd)
        de_p = params["entities"][0]
        assert isinstance(de_p["attributes"], str)
        assert "height" in de_p["attributes"]
        assert "dent.attributes = entData.attributes" in cypher

    def test_domain_event_payload_serialized(self):
        ddd = {
            "contexts": [], "aggregates": [], "entities": [],
            "events": [{
                "id": "EVT-01", "name": "PlantGrowthDataRecorded",
                "description": "생장 기록됨",
                "published_by_aggregate_id": "AGG-01",
                "payload_fields": [
                    {"name": "growthDataId", "type": "uuid", "required": True},
                    {"name": "plantId", "type": "uuid", "required": True},
                    {"name": "occurredAt", "type": "datetime", "required": True},
                ],
            }],
            "spack_entity_mapping": [],
        }
        cypher, params = build_save_ddd_query("p1", ddd)
        ev_p = params["events"][0]
        assert isinstance(ev_p["payload_fields"], str)
        assert "growthDataId" in ev_p["payload_fields"]
        assert "evt.payload_fields = evtData.payload_fields" in cypher

    def test_ddd_round_trip_all_detail_preserved(self):
        from app.pipelines.design_validator.ddd_detail import (
            decode_aggregates_detail,
            decode_domain_entities_detail,
            decode_domain_events_detail,
        )

        ddd = {
            "contexts": [],
            "aggregates": [{
                "id": "AGG-01", "name": "Plant", "context_id": "CTX-01",
                "lineage": {"confidence": "direct",
                            "related_stories": [{"story_id": "Story-01.1", "quote": "q"}]},
                "invariants": ["rule1", "rule2"],
            }],
            "entities": [{
                "id": "DE-01", "name": "X", "aggregate_id": "AGG-01",
                "lineage": {"confidence": "direct",
                            "related_stories": [{"story_id": "Story-01.1", "quote": "q"}]},
                "attributes": [{"name": "a", "type": "int", "required": True,
                               "constraint": "", "description": ""}],
            }],
            "events": [{
                "id": "EVT-01", "name": "Ev",
                "published_by_aggregate_id": "AGG-01",
                "payload_fields": [{"name": "x", "type": "uuid", "required": True,
                                    "constraint": "", "description": ""}],
            }],
            "spack_entity_mapping": [],
        }
        _, params = build_save_ddd_query("p1", ddd)
        # Neo4j 응답 시뮬레이션
        agg_fake = [{**params["aggregates"][0]}]
        de_fake = [{**params["entities"][0]}]
        ev_fake = [{**params["events"][0]}]
        agg_r = decode_aggregates_detail(agg_fake)
        de_r = decode_domain_entities_detail(de_fake)
        ev_r = decode_domain_events_detail(ev_fake)
        assert agg_r[0]["invariants"] == ["rule1", "rule2"]
        assert de_r[0]["attributes"][0]["name"] == "a"
        assert ev_r[0]["payload_fields"][0]["name"] == "x"
