"""
build_implementation_checklist 단위 테스트 — 그래프 기반 결정적 전수 대조 체크리스트.

핵심 보증:
- LLM 없이 그래프 항목이 빠짐없이 체크리스트로 변환된다 (누락 구조적 불가)
- 평탄/properties 중첩 노드 모두 처리 (Neo4j 조회 경로 차이)
- 빈 그래프면 ("", 0) — 소비자(FE)가 파일을 생략
"""
from __future__ import annotations

from app.pipelines.create_md_pipeline import build_implementation_checklist
from app.service.query_repository import ArchitectureGraph, DddGraph, SpackGraph


def _empty():
    return SpackGraph(), DddGraph(), ArchitectureGraph()


def test_empty_graphs_return_empty():
    spack, ddd, arch = _empty()
    md, count, gaps = build_implementation_checklist("proj", spack, ddd, arch)
    assert md == ""
    assert count == 0
    assert gaps == 0


def test_items_rendered_and_counted():
    spack = SpackGraph(
        apis=[
            {"id": "API-1", "method": "post", "endpoint": "/login", "name": "로그인"},
            {"id": "API-2", "method": "get", "endpoint": "/users", "name": "유저 목록"},
        ],
        entities=[{"id": "E-1", "name": "User", "attributes": [{"n": "email"}, {"n": "pw"}]}],
        policies=[{"id": "POL-01", "description": "비밀번호는 8자 이상"}],
        screens=[{"id": "S-1", "name": "로그인 화면", "path": "/login"}],
    )
    ddd = DddGraph(domain_events=[{"id": "EV-1", "name": "UserRegistered"}])
    arch = ArchitectureGraph(
        services=[{"id": "SVC-1", "name": "auth-service", "tech_stack": "Spring Boot"}],
        databases=[{"id": "DB-1", "name": "main-db", "tech_stack": "PostgreSQL"}],
    )

    md, count, gaps = build_implementation_checklist("proj", spack, ddd, arch)

    # 합계: API 2 + Entity 1 + Policy 1 + Screen 1 + Event 1 + Svc 1 + DB 1 = 8
    assert count == 8
    # 스펙 갭: API-1(POST) 요청+응답 미정 2 + API-2(GET) 응답 미정 1 = 3 (Entity 는 속성 있음)
    assert gaps == 3
    assert "⚠️요청스펙미정·응답스펙미정" in md
    assert "스펙 갭 3건" in md
    assert "추측으로 채우지 마세요" in md
    assert f"모든 항목({count}개)" in md
    # 항목 렌더 확인
    assert "`POST /login` — 로그인" in md
    assert "Entity `User` (속성 2개)" in md
    assert "Policy `POL-01` — 비밀번호는 8자 이상" in md   # 본문을 라벨에 노출
    assert "Screen `로그인 화면` (`/login`)" in md
    assert "Domain Event `UserRegistered`" in md
    assert "Service `auth-service` (Spring Boot)" in md
    assert "Database `main-db` (PostgreSQL)" in md
    # 모든 항목이 미체크 박스 + 구현위치 칸 (헤더 사용법 문구에 1회 추가 등장)
    assert md.count("- [ ]") == count
    assert md.count("←구현위치:") == count + 1
    # 섹션 카운트
    assert "## APIs (2)" in md


def test_nested_properties_fallback():
    """Neo4j 노드가 {properties: {...}} 중첩으로 와도 추출된다."""
    spack = SpackGraph(apis=[{"properties": {"id": "API-9", "method": "delete", "endpoint": "/x", "name": "삭제"}}])
    ddd, arch = DddGraph(), ArchitectureGraph()
    md, count, _gaps = build_implementation_checklist("proj", spack, ddd, arch)
    assert count == 1
    assert "`DELETE /x` — 삭제" in md


def test_partial_sections_skip_empty():
    """비어 있는 섹션은 출력하지 않는다."""
    spack = SpackGraph(apis=[{"id": "A", "method": "get", "endpoint": "/a", "name": "a"}])
    md, count, _gaps = build_implementation_checklist("proj", spack, DddGraph(), ArchitectureGraph())
    assert count == 1
    assert "## APIs (1)" in md
    assert "Entities" not in md
    assert "Domain Events" not in md


def test_api_service_mapping_and_ddd_sections():
    """[Gemini 평가 반영] API→서비스 매핑 표기 + MSA 미지정 갭 + DDD 섹션 + JSON string 복원.

    [2026-06-14 오탐 픽스 회귀] Aggregate 는 invariants(정합성 경계)만, DomainEntity 가
    실제 attributes(데이터 모델)를 가진다. 데이터 모델 ⚠️속성미정 갭은 DomainEntity 에서만.
    """
    from app.service.query_repository import CrossMappingRel

    spack = SpackGraph(
        apis=[
            {"id": "API-1", "method": "get", "endpoint": "/a", "name": "a",
             "response_body": {"fields": [{"n": 1}]}},
            {"id": "API-2", "method": "get", "endpoint": "/b", "name": "b",
             "response_body": {"fields": [{"n": 1}]}},
        ],
        # JSON string 직렬화된 attributes — 복원돼 '속성 2개'로 판정돼야 (오탐 픽스)
        entities=[{"id": "E1", "name": "Ser", "attributes": '[{"n":"a"},{"n":"b"}]'}],
        screens=[{"id": "S1", "name": "홈", "path": "/", "calls_apis": ["API-1", "API-2"]}],
        api_service_rels=[CrossMappingRel(source_id="API-1", target_id="SVC-1", target_name="계정 서비스", type="HANDLED_BY")],
    )
    # 실제 파이프라인 모양: aggregate 는 invariants 만, domain_entity 는 attributes.
    ddd = DddGraph(
        aggregates=[
            {"id": "AG-1", "name": "Account", "invariants": ["balance >= 0", "status in {A,B}"]},
            {"id": "AG-2", "name": "Order"},  # 불변식 없음 → 정보 표기 없음, 갭 아님
        ],
        domain_entities=[
            {"id": "DE-1", "name": "AccountHolder", "attributes": [{"n": "id"}, {"n": "name"}]},
            {"id": "DE-2", "name": "OrderLine"},  # 속성 미정 → ⚠️ 갭
        ],
    )
    arch = ArchitectureGraph(services=[
        {"id": "SVC-1", "name": "계정 서비스", "type": "Backend", "owned_aggregate_names": ["Account"]},
        {"id": "SVC-2", "name": "운영 서비스", "type": "Backend"},
    ])

    md, count, gaps = build_implementation_checklist("proj", spack, ddd, arch)

    # API-1 은 서비스 표기, API-2 는 MSA(서비스 2개)에서 미지정 갭
    assert "`GET /a` — a [→ 계정 서비스]  ←구현위치:" in md
    assert "`GET /b` — b ⚠️서비스미지정" in md
    # JSON string attributes 복원 — 오탐 없이 '속성 2개'
    assert "Entity `Ser` (속성 2개)" in md
    # Screen → API 연결 표기
    assert "Screen `홈` (`/`) (→ API: API-1, API-2)" in md
    # Aggregates = 정합성 경계: invariants 정보 + 소유 서비스 표기, 속성미정 마커 절대 없음(오탐 회귀)
    assert "## Aggregates (정합성 경계) (2)" in md
    assert "Aggregate `Account` (불변식 2개) [→ 계정 서비스]" in md
    # Order 는 MSA 인데 소유 서비스 미지정 → ⚠️오너십미정 (API ⚠️서비스미지정 과 대칭)
    assert "Aggregate `Order` ⚠️오너십미정" in md
    # DomainEntity = 데이터 모델: attributes 로 ⚠️속성미정 판정
    assert "## Domain Entities (데이터 모델) (2)" in md
    assert "Domain Entity `AccountHolder` (속성 2개)" in md
    assert "Domain Entity `OrderLine` ⚠️속성미정" in md
    # 갭 합계: API-2 서비스미지정 1 + Order 오너십미정 1 + OrderLine 속성미정 1 = 3
    assert gaps == 3
    # 항목 수: API 2 + Entity 1 + Screen 1 + Aggregate 2 + DomainEntity 2 + Service 2 = 10
    assert count == 10


def test_aggregate_ownership_suppressed_for_single_service():
    """[2026-06-14] 단일 서비스면 Aggregate 갈 곳이 자명 → ⚠️오너십미정 표기하지 않는다."""
    ddd = DddGraph(aggregates=[{"id": "AG-1", "name": "Order"}, {"id": "AG-2", "name": "Account"}])
    arch = ArchitectureGraph(services=[{"id": "SVC-1", "name": "모놀리스", "type": "Backend"}])
    md, count, gaps = build_implementation_checklist("proj", SpackGraph(), ddd, arch)
    assert "⚠️오너십미정" not in md
    assert gaps == 0
    assert "Aggregate `Order`  ←구현위치:" in md


def test_aggregate_with_only_invariants_never_marked_attr_gap():
    """[2026-06-14 회귀] 실제 get_ddd_graph 산출물 — aggregate 는 invariants 만 보유.

    이전 버그: aggregate.attributes 를 세어 0 이면 ⚠️속성미정 → 모든 aggregate 100% 오탐.
    이제 aggregate 에는 속성 갭 자체가 없어야 한다(데이터 모델 갭은 DomainEntity 책임).
    """
    ddd = DddGraph(
        aggregates=[
            {"id": "AG-1", "name": "AgentDeployment", "invariants": []},
            {"id": "AG-2", "name": "AgentTask", "invariants": ["status transition valid"]},
            {"id": "AG-3", "name": "AccountAssignment"},
        ],
    )
    md, count, gaps = build_implementation_checklist("proj", SpackGraph(), ddd, ArchitectureGraph())
    assert count == 3
    # 핵심: invariants 만 가진 aggregate 가 ⚠️속성미정 으로 오탐되지 않는다.
    assert "⚠️속성미정" not in md
    assert gaps == 0
    assert "Aggregate `AgentDeployment`  ←구현위치:" in md
    assert "Aggregate `AgentTask` (불변식 1개)" in md


def test_spec_gap_markers():
    """[스펙 갭] 스키마가 채워진 API/Entity 는 마커 없음 + JSON string 직렬화 복원."""
    spack = SpackGraph(
        apis=[
            # 채워진 POST (dict 형태) — 갭 없음
            {
                "id": "A1", "method": "post", "endpoint": "/ok", "name": "ok",
                "request_body": {"fields": [{"name": "x"}]},
                "response_body": {"fields": [{"name": "y"}]},
            },
            # Neo4j JSON string 직렬화 형태 — 복원해 갭 없음 판정
            {
                "id": "A2", "method": "get", "endpoint": "/s", "name": "s",
                "response_body": '{"fields": [{"name": "z"}]}',
            },
            # 빈 POST — 요청+응답 모두 미정
            {"id": "A3", "method": "post", "endpoint": "/gap", "name": "gap"},
        ],
        entities=[{"id": "E1", "name": "NoAttr"}],  # 속성미정
    )
    md, count, gaps = build_implementation_checklist("proj", spack, DddGraph(), ArchitectureGraph())
    assert count == 4
    assert gaps == 3  # A3 의 2 + Entity 1
    assert "`POST /ok` — ok  ←구현위치:" in md          # 마커 없음
    assert "`GET /s` — s  ←구현위치:" in md             # string 복원 → 마커 없음
    assert "`POST /gap` — gap ⚠️요청스펙미정·응답스펙미정" in md
    assert "Entity `NoAttr` ⚠️속성미정" in md


def test_entity_dedup_exact_and_entity_suffix_pairs():
    """[중복 정리] 이름·속성수 동일한 정확 중복은 합치고, 'Foo'+'FooEntity' 짝은 표시(완전성 보존)."""
    spack = SpackGraph(
        entities=[
            {"id": "E1", "name": "Application", "attributes": [{"n": "id"}]},
            {"id": "E2", "name": "ApplicationEntity", "attributes": [{"n": "id"}]},
            # 정확 중복 (이름·속성수 동일) — 1개로 합쳐져야
            {"id": "E3", "name": "TopUsersChartEntity", "attributes": [{"n": "rank"}]},
            {"id": "E4", "name": "TopUsersChartEntity", "attributes": [{"n": "rank"}]},
        ],
    )
    md, count, _gaps = build_implementation_checklist("proj", spack, DddGraph(), ArchitectureGraph())
    # E1·E2·E3 만 남고 E4(정확 중복)는 합쳐짐 → 3개
    assert count == 3
    assert "## Entities (3)" in md
    # 'Foo' 짝이 있는 'FooEntity' 만 중복가능 표시 (Application 본체는 표시 안 함)
    assert "Entity `ApplicationEntity` (속성 1개) ⚠️중복가능(`Application` 와 동일 개념이면 통합)" in md
    assert "Entity `Application` (속성 1개)  ←구현위치:" in md  # 본체엔 dup 마커 없음
    # 정확 중복은 한 줄만
    assert md.count("Entity `TopUsersChartEntity`") == 1


def test_policy_empty_content_flagged():
    """[정책 갭] 본문(description) 없는 정책은 ⚠️정책내용미정 + 갭. 분류만 있으면 분류 표기.
    description 있으면 라벨에 본문 노출 (정책 노드엔 name 이 없어 id 로 라벨됨)."""
    spack = SpackGraph(
        policies=[
            {"id": "POL-01"},                                   # 아무것도 없음 → 갭
            {"id": "POL-02", "category": "보안"},                # 분류만(본문 없음) → 갭 + 분류표기
            {"id": "POL-03", "description": "환불은 7일 이내"},   # 본문 있음 → 라벨 노출, 갭 없음
        ],
    )
    md, count, gaps = build_implementation_checklist("proj", spack, DddGraph(), ArchitectureGraph())
    assert count == 3
    assert gaps == 2  # POL-01, POL-02 (둘 다 description 없음)
    assert "Policy `POL-01` ⚠️정책내용미정" in md
    assert "Policy `POL-02` (분류: 보안) ⚠️정책내용미정" in md
    assert "Policy `POL-03` — 환불은 7일 이내  ←구현위치:" in md


def test_policy_description_newlines_collapsed():
    """description 의 개행/연속공백은 한 줄로 정리 — md 리스트 항목이 깨지지 않게."""
    spack = SpackGraph(policies=[{"id": "POL-01", "description": "첫 줄\n\n둘째   줄"}])
    md, _count, _gaps = build_implementation_checklist("proj", spack, DddGraph(), ArchitectureGraph())
    assert "Policy `POL-01` — 첫 줄 둘째 줄  ←구현위치:" in md
