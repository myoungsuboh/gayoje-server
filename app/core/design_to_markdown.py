"""
설계 그래프(SPACK / DDD / Architecture) → 사람이 읽기 좋은 마크다운 (export 용, 순수 함수).

그래프는 nodes/edges 구조라 그대로 Notion 에 못 넣는다. SPACK 은 표, DDD 는 영역별
목록, Architecture 는 mermaid 다이어그램 + 텍스트 폴백으로 렌더. 빈 그래프도 안전하게
"아직 생성되지 않았습니다" 안내를 반환(크래시 없음).

graph 객체는 query_repository 의 SpackGraph/DddGraph/ArchitectureGraph(pydantic) 를
받지만, dict/attr 양쪽 안전하게 접근(_field)해 결합도를 낮춘다.
"""
from __future__ import annotations

import re
from typing import Any, List


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _list(obj: Any, key: str) -> List[Any]:
    return _field(obj, key) or []


def _safe_id(raw: Any) -> str:
    return re.sub(r"\W", "_", str(raw or "n"))


def _cell(s: Any) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ").strip()


def spack_to_markdown(spack: Any) -> str:
    apis = _list(spack, "apis")
    entities = _list(spack, "entities")
    policies = _list(spack, "policies")
    screens = _list(spack, "screens")
    if not (apis or entities or policies or screens):
        return "## 기능 명세 (SPACK)\n\n아직 생성되지 않았습니다.\n"
    lines: List[str] = ["## 기능 명세 (SPACK)", ""]
    if apis:
        lines += ["### API", "", "| API | Method | Path | 설명 |", "| --- | --- | --- | --- |"]
        for a in apis:
            # API 컬럼 = 이름(없으면 endpoint/id), Path 컬럼 = endpoint.
            # API 노드는 경로를 'path' 가 아니라 'endpoint' 에 저장한다
            # (design_pipeline schema / graph_repository 기준). 'path' 는 legacy fallback.
            name = _cell(_field(a, "name") or _field(a, "endpoint") or _field(a, "id"))
            path = _cell(_field(a, "endpoint") or _field(a, "path"))
            lines.append(
                f"| {name} | {_cell(_field(a, 'method'))} | {path} | {_cell(_field(a, 'description'))} |"
            )
        lines.append("")
    if entities:
        lines += ["### 핵심 데이터 (Entity)", "", "| Entity | 설명 |", "| --- | --- |"]
        for e in entities:
            nm = _cell(_field(e, "name") or _field(e, "id"))
            lines.append(f"| {nm} | {_cell(_field(e, 'description'))} |")
        lines.append("")
    if policies:
        lines += ["### 비즈니스 규칙 (Policy)", ""]
        for p in policies:
            nm = _field(p, "name") or _field(p, "id") or ""
            lines.append(f"- **{nm}**: {_field(p, 'description') or ''}")
            for rule in _list(p, "rules"):
                lines.append(f"  - {rule}")
        lines.append("")
    if screens:
        lines += ["### 화면 (Screen)", "", "| 화면 | 경로 | 설명 |", "| --- | --- | --- |"]
        for s in screens:
            nm = _cell(_field(s, "name") or _field(s, "id"))
            lines.append(f"| {nm} | {_cell(_field(s, 'path'))} | {_cell(_field(s, 'description'))} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def ddd_to_markdown(ddd: Any) -> str:
    contexts = _list(ddd, "contexts")
    aggregates = _list(ddd, "aggregates")
    entities = _list(ddd, "domain_entities")
    events = _list(ddd, "domain_events")
    if not (contexts or aggregates or entities or events):
        return "## 도메인 모델 (DDD)\n\n아직 생성되지 않았습니다.\n"
    lines: List[str] = ["## 도메인 모델 (DDD)", ""]
    for c in contexts:
        lines.append(f"### {_field(c, 'name') or _field(c, 'id')}")
        if _field(c, "description"):
            lines.append(str(_field(c, "description")))
        lines.append("")

    def _section(title: str, items: List[Any]) -> None:
        if not items:
            return
        lines.append(f"**{title}**")
        for it in items:
            lines.append(f"- {_field(it, 'name') or _field(it, 'id')}")
        lines.append("")

    _section("핵심 묶음 (Aggregate)", aggregates)
    _section("개별 데이터 (Entity)", entities)
    _section("일어난 사건 (Event)", events)
    return "\n".join(lines) + "\n"


def architecture_to_markdown(arch: Any) -> str:
    services = _list(arch, "services")
    databases = _list(arch, "databases")
    connections = _list(arch, "connections")
    if not (services or databases or connections):
        return "## 시스템 아키텍처\n\n아직 생성되지 않았습니다.\n"
    names = {}
    for node in list(services) + list(databases):
        nid = _field(node, "id")
        if nid:
            names[nid] = _field(node, "name") or nid
    lines: List[str] = ["## 시스템 아키텍처", "", "```mermaid", "graph LR"]
    for nid, nm in names.items():
        lines.append(f'  {_safe_id(nid)}["{nm}"]')
    for c in connections:
        s, t = _field(c, "source_id"), _field(c, "target_id")
        if not s or not t:
            continue
        label = _field(c, "protocol") or _field(c, "type") or ""
        auth = _field(c, "auth")
        if auth and str(auth).lower() != "none":
            label = f"{label}/{auth}" if label else str(auth)
        if label:
            lines.append(f"  {_safe_id(s)} -->|{label}| {_safe_id(t)}")
        else:
            lines.append(f"  {_safe_id(s)} --> {_safe_id(t)}")
    lines += ["```", ""]
    if services:
        lines.append("**서비스**")
        for s in services:
            lines.append(f"- {_field(s, 'name') or _field(s, 'id')}")
        lines.append("")
    if databases:
        lines.append("**데이터베이스**")
        for d in databases:
            lines.append(f"- {_field(d, 'name') or _field(d, 'id')}")
        lines.append("")
    return "\n".join(lines) + "\n"


def design_to_markdown(spack: Any, ddd: Any, arch: Any) -> str:
    return (
        "# 🏗️ 시스템 설계\n\n"
        + spack_to_markdown(spack)
        + "\n"
        + ddd_to_markdown(ddd)
        + "\n"
        + architecture_to_markdown(arch)
    )
