"""설계 그래프 → 마크다운 렌더 테스트."""
from app.core.design_to_markdown import (
    architecture_to_markdown,
    ddd_to_markdown,
    design_to_markdown,
    spack_to_markdown,
)
from app.service.query_repository import ArchitectureGraph, DddGraph, SpackGraph


def test_spack_renders_api_entity_policy():
    # 실제 데이터 형태 — API 노드는 경로를 'endpoint' 에 저장(name 별도, 'path' 속성 없음).
    g = SpackGraph(
        apis=[{"id": "a1", "name": "사용자 목록 조회", "endpoint": "/users", "method": "GET", "description": "list"}],
        entities=[{"id": "e1", "name": "User", "description": "user"}],
        policies=[{"id": "p1", "name": "Retry", "description": "max 3", "rules": ["<=3"]}],
    )
    md = spack_to_markdown(g)
    assert "기능 명세 (SPACK)" in md
    # [버그 회귀방지] Path 열은 endpoint 를, API 열은 name 을 보여줘야 한다.
    assert "사용자 목록 조회" in md and "/users" in md and "GET" in md and "list" in md
    api_row = next(ln for ln in md.splitlines() if "사용자 목록 조회" in ln)
    assert "/users" in api_row and "GET" in api_row and "list" in api_row  # 같은 행에 모든 셀
    assert "User" in md
    assert "Retry" in md and "<=3" in md


def test_spack_api_path_reads_endpoint_not_missing_path_field():
    # 'path' 속성이 아예 없는(실제) API 도 endpoint 가 Path 열에 나와야 한다.
    g = SpackGraph(apis=[{"id": "a1", "name": "주문 생성", "endpoint": "/orders", "method": "POST", "description": "create"}])
    row = next(ln for ln in spack_to_markdown(g).splitlines() if "주문 생성" in ln)
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells == ["주문 생성", "POST", "/orders", "create"]



def test_spack_empty_has_note():
    md = spack_to_markdown(SpackGraph())
    assert "SPACK" in md and "아직" in md


def test_spack_renders_screens_only():
    g = SpackGraph(screens=[{"id": "sc1", "name": "로그인", "path": "/login", "description": "login"}])
    md = spack_to_markdown(g)
    assert "화면 (Screen)" in md and "로그인" in md and "/login" in md
    assert "아직" not in md  # screens 있으면 빈 상태 아님


def test_ddd_renders_contexts_and_members():
    g = DddGraph(
        contexts=[{"id": "c1", "name": "결제 관리", "description": "payments"}],
        aggregates=[{"id": "ag1", "name": "주문"}],
        domain_entities=[{"id": "de1", "name": "주문상품"}],
        domain_events=[{"id": "ev1", "name": "결제완료"}],
    )
    md = ddd_to_markdown(g)
    assert "도메인 모델 (DDD)" in md
    assert "결제 관리" in md and "주문" in md and "결제완료" in md


def test_ddd_empty_no_crash():
    md = ddd_to_markdown(DddGraph())
    assert isinstance(md, str) and "DDD" in md


def test_architecture_emits_mermaid():
    g = ArchitectureGraph(
        services=[{"id": "s1", "name": "API"}],
        databases=[{"id": "d1", "name": "DB"}],
        connections=[{"source_id": "s1", "target_id": "d1", "type": "CONNECTS_TO", "protocol": "tcp", "auth": "bearer"}],
    )
    md = architecture_to_markdown(g)
    assert "```mermaid" in md and "graph" in md
    assert "API" in md and "DB" in md


def test_architecture_empty_note():
    md = architecture_to_markdown(ArchitectureGraph())
    assert "시스템 아키텍처" in md and "아직" in md


def test_design_combines_all_three():
    md = design_to_markdown(SpackGraph(), DddGraph(), ArchitectureGraph())
    assert "시스템 설계" in md and "SPACK" in md and "DDD" in md and "아키텍처" in md
