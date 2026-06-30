"""
문서/도메인 그래프 노드의 멀티테넌시 스코프 키.

[배경]
CPS_Document / PRD_Document / Problem / Solution / Epic / Story / Screen / API
/ Entity … 모든 도메인 노드는 `project` property (그리고 `doc_*_{project}` 형태의
node id) 로 식별돼 왔다. 개인 프로젝트만 있을 땐 충분했지만, **팀 프로젝트가 동명
(同名)** 이면 같은 `project` 값을 공유해 노드가 섞이거나 덮어써진다
 (deleteProject 의 `WHERE n.project = $project`, lineage trace, fetch master 등이
 전부 `project` property 로 매칭하기 때문).

[해결]
team_id 를 `project` 값에 합성해 **스코프 키** 를 만든다. 모든 도메인 쿼리가 이미
`project` 로 매칭하므로, 쓰기/읽기/삭제/추적 진입점에서 project_name 을 스코프
키로 치환하기만 하면 그래프 전체가 균일하게 격리된다 (쿼리 Cypher 수정 불필요).

- 개인 프로젝트 (team_id 없음): 스코프 키 = project_name 그대로 → **기존 데이터 호환**
  (마이그레이션 불필요).
- 팀 프로젝트: 스코프 키 = ``::team::{team_id}::{project_name}``.

[ownership 레이어와의 관계]
`:Project {name, owner_email}` / `:Project {name, team_id}` 노드는 **raw 이름** 으로
유지(여기서 건드리지 않음). 그쪽이 접근 게이트(assert_access/claim)이고, 이 모듈은
게이트 통과 후 *도메인 노드* 의 project 값만 스코프한다.

[보안 — 스푸핑 차단]
team_id 는 claim 시 멤버십 검증을 통과한 server-trusted 값이고, sentinel 은
서버가 생성한다(이름에서 파생 X). 유일한 위조 경로는 "개인 프로젝트 이름을
스코프 키 문자열과 똑같이 짓는 것" 인데, project_name 에 sentinel(`::team::`) 이
들어가면 `assert_safe_project_name` 이 거부한다. 또한 모든 read/write 라우트는
ownership 게이트(assert_access)를 통과해야 하므로, 만들 수 없는 이름의 프로젝트
문서는 애초에 접근 불가.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, status

# 도메인 이름에 (거의) 등장하지 않는 server-only 구분자.
# 형식: ``::team::{team_id}::{project_name}``
SCOPE_SENTINEL = "::team::"


def assert_safe_project_name(name: Optional[str]) -> None:
    """project_name 에 예약 sentinel 이 포함되면 400.

    스코프 키 위조(개인 이름으로 팀 문서 노드에 도달)를 원천 차단한다.
    """
    if name and SCOPE_SENTINEL in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="프로젝트 이름에 사용할 수 없는 문자열이 포함되어 있습니다.",
        )


def scoped_project(name: str, team_id: Optional[str] = None) -> str:
    """(project_name, team_id) → 도메인 노드 스코프 키.

    개인(team_id 없음): 이름 그대로(기존 호환). 팀: sentinel 합성.

    [멱등성] 이미 스코프된 키(sentinel 로 시작)가 들어오면 그대로 반환한다 —
    내부 round-trip(admin cleanup 이 master.project 를 재사용, FE 가 반환된 키를
    되돌려줌 등) 에서 이중 스코프/오류를 피하기 위함. 위조 차단은 여기가 아니라
    **claim 시점**(assert_safe_project_name)이 담당한다: sentinel 포함 이름은
    프로젝트로 생성될 수 없고, 모든 read/write 는 ownership 게이트(assert_access)를
    통과해야 하므로 만들 수 없는 이름의 노드엔 접근할 수 없다.
    """
    if name and name.startswith(SCOPE_SENTINEL):
        return name  # 이미 스코프 키 — 그대로.
    if not team_id:
        return name
    return f"{SCOPE_SENTINEL}{team_id}{SCOPE_SENTINEL}{name}"


def is_scoped(value: Optional[str]) -> bool:
    return bool(value) and value.startswith(SCOPE_SENTINEL)


def unscope_project(value: Optional[str]) -> str:
    """스코프 키 → 사람이 읽는 project_name (FE 반환용).

    개인 키(=이름)거나 빈 값은 그대로. 팀 키면 sentinel/team_id 를 벗겨 원래 이름만.
    """
    if not value or not value.startswith(SCOPE_SENTINEL):
        return value or ""
    # 형식: ::team::{team_id}::{name}  — team_id 는 UUID 라 sentinel 미포함.
    rest = value[len(SCOPE_SENTINEL):]
    idx = rest.find(SCOPE_SENTINEL)
    if idx == -1:
        return value  # 형식 불일치 — 방어적으로 원본 반환.
    return rest[idx + len(SCOPE_SENTINEL):]


def team_id_of(value: Optional[str]) -> Optional[str]:
    """스코프 키에서 team_id 추출 (개인/비스코프면 None)."""
    if not value or not value.startswith(SCOPE_SENTINEL):
        return None
    rest = value[len(SCOPE_SENTINEL):]
    idx = rest.find(SCOPE_SENTINEL)
    if idx == -1:
        return None
    return rest[:idx]


# ─── 노드 ID 빌더 (단일 진실원) ─────────────────────────────
#
# [배경] 기존엔 id 생성이 여러 곳에 흩어져 미묘하게 불일치했다:
#   - delta id: 생성 경로(types.py)는 raw project, 삭제 경로는 normalized project
#   - log id:   생성은 raw, 삭제는 normalized
#   - master id: 생성은 email 없음, 삭제는 email_part 접두 → 운영에서 mismatch 가능
#     (점(.) 포함 프로젝트명 / email 설정 시 delete 가 노드를 못 찾아 중복/고아 발생)
# 여기로 통일해 위 잠재 버그를 함께 제거한다. **개인(비-점 이름)은 기존과 동일** id 생성.
#
# [스코프] 입력 project_key 는 이미 scoped_project() 를 거친 값 (개인=이름, 팀=sentinel
# 합성). 따라서 id 도 자동으로 팀별 격리되고, 개인은 기존 형식 보존.


def _norm_project(project_key: str) -> str:
    # 노드 id 는 LLM 프롬프트의 ID_NORMALIZATION 규칙(점 → underscore)과 동일하게
    # 정규화해야 기존 저장 데이터(LLM 생성 id)와 매칭된다. 모든 id 빌더 공통 적용.
    return (project_key or "").replace(".", "_")


def _norm_version(version: str) -> str:
    return (version or "").replace(".", "_")


def meeting_log_id(project_key: str, version: str) -> str:
    return f"log_{_norm_project(project_key)}_{_norm_version(version)}"


def cps_delta_id(project_key: str, version: str) -> str:
    return f"doc_cps_{_norm_project(project_key)}_{_norm_version(version)}"


def prd_delta_id(project_key: str, version: str) -> str:
    return f"doc_prd_{_norm_project(project_key)}_{_norm_version(version)}"


def cps_master_id(project_key: str) -> str:
    return f"doc_cps_master_{_norm_project(project_key)}"


def prd_master_id(project_key: str) -> str:
    return f"doc_prd_master_{_norm_project(project_key)}"


# ─── 그래프 스코프 변환 (LLM 출력 → 저장 직전) ───────────────


def scope_graph(
    graph: Dict[str, Any],
    *,
    project_key: str,
    doc_label: str,
    new_doc_id: str,
) -> Dict[str, Any]:
    """LLM 그래프 JSON 에 팀 스코프를 적용 (저장 직전, 순수 변환).

    1) 모든 노드의 `properties.project` 를 project_key(스코프 키)로 설정 — 읽기
       격리의 핵심 (모든 read 가 project property 로 매칭).
    2) doc_label(CPS_Document/PRD_Document) 노드의 LLM 생성 id 를 서버 authoritative
       `new_doc_id`(스코프 delta id)로 재조정 — 동명 팀/개인 delta MERGE 충돌(본문
       덮어쓰기/소실) 방지. 해당 id 를 가리키던 관계 endpoint 도 함께 재작성.

    [개인 프로젝트] project_key=이름, new_doc_id=기존 delta id 와 동일 → **무변환**
    (id_map identity, project 동일) → 기존 동작 100% 보존.

    [Problem/Solution 등] prb_01 같은 비-프로젝트-한정 id 는 기존 동작 유지(여기서
    안 바꿈). 읽기는 스코프된 doc 로부터 관계 traversal 로 도달하므로 격리 유지.
    """
    if not isinstance(graph, dict):
        return graph
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return graph

    # doc_label 노드의 (현재 id → new_doc_id) 매핑 수집.
    id_map: Dict[str, str] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        if n.get("label") == doc_label and n.get("id") is not None:
            old = str(n["id"])
            if old != new_doc_id:
                id_map[old] = new_doc_id

    new_nodes: list = []
    for n in nodes:
        if not isinstance(n, dict):
            new_nodes.append(n)
            continue
        props = n.get("properties")
        props = dict(props) if isinstance(props, dict) else {}
        props["project"] = project_key
        nid = n.get("id")
        if nid is not None and str(nid) in id_map:
            nid = id_map[str(nid)]
        new_nodes.append({**n, "id": nid, "properties": props})

    new_rels: list = []
    for r in graph.get("relationships") or []:
        if not isinstance(r, dict):
            new_rels.append(r)
            continue
        r2 = dict(r)
        src = r2.get("source")
        tgt = r2.get("target")
        if src is not None and str(src) in id_map:
            r2["source"] = id_map[str(src)]
        if tgt is not None and str(tgt) in id_map:
            r2["target"] = id_map[str(tgt)]
        new_rels.append(r2)

    return {**graph, "nodes": new_nodes, "relationships": new_rels}
