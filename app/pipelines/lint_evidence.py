"""
Lint 결정적 evidence collector.

[배경]
기존 lint_pipeline 은 LLM 에게 "4 카테고리 × ≥4 rules 평가" 를 전부 위임 →
결정성 0, 12 files × 3.5KB head 만 보고 hallucinate. 두 번 돌리면 점수 다름.

[새 패턴 — lineage_pipeline 의 deterministic 매칭 확장]
spec 항목마다 코드 grep 으로 evidence 를 수집한 뒤, **명백한 매칭은 LLM 호출
자체를 생략** (applied=true 즉시 확정). evidence 0건 항목만 모아 LLM 에게
sample 파일과 함께 "정말 없는지" 검증 요청 (residual pass).

[지원 프레임워크 패턴]
- FastAPI / Flask: @router.<method>("/...")  /  @app.<method>("/...")
- Express:        router.<method>("/...")   /  app.<method>("/...")
- Spring:         @<Method>Mapping("/...")  /  @RequestMapping(value="/...", method=...)
- Vue Router:     { path: "/..." , name/component … }
- Django:         path("/...", view)  /  re_path
- React Router:   <Route path="/..." />

framework 별 패턴은 _ENDPOINT_PATTERNS / _CLASS_PATTERNS / _ROUTE_PATTERNS
배열에서 관리 — 신규 framework 추가는 패턴 한 줄만 더하면 됨.

[Limits — false positive 최소화]
- endpoint 매칭은 method+path 둘 다 일치해야 함
- class 매칭은 word boundary + Capitalized 확인
- token 매칭은 길이 ≥ 4 (짧은 단어 노이즈 차단)
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ─── Domain types ──────────────────────────────────────────────


@dataclass(frozen=True)
class FileSample:
    """파일 한 건의 full body (또는 truncated body)."""

    path: str
    content: str
    size: int = 0


@dataclass
class Evidence:
    """spec 항목 한 건에 대한 코드 grep 매칭 결과."""

    file: str
    line: int = 0
    snippet: str = ""
    kind: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "snippet": self.snippet[:200],
            "kind": self.kind,
        }


# ─── 공통 헬퍼 ─────────────────────────────────────────────────


_SNIPPET_MAX_LEN = 200


def _snippet(line_text: str) -> str:
    """매칭 라인을 최대 200자로 자르고 양옆 공백 trim."""
    s = (line_text or "").strip()
    if len(s) > _SNIPPET_MAX_LEN:
        return s[: _SNIPPET_MAX_LEN - 1] + "…"
    return s


def _iter_matches(
    content: str,
    pattern: re.Pattern[str],
    *,
    max_per_file: int = 5,
) -> List[Tuple[int, str]]:
    """
    pattern 매칭 → [(line_no, line_text), ...].
    한 파일에서 너무 많이 매칭되면 응답 폭주 → max_per_file 로 잘라냄.
    """
    out: List[Tuple[int, str]] = []
    if not content or not pattern:
        return out
    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        if pattern.search(raw_line):
            out.append((line_no, raw_line))
            if len(out) >= max_per_file:
                break
    return out


def _word_boundary_pattern(name: str) -> Optional[re.Pattern[str]]:
    """
    name 을 정확한 식별자로 매칭 (앞뒤 word boundary). 빈 문자열 → None.
    LLM 이 만든 이상한 name 도 한 번 검증 (한글/공백 → 매칭 안 됨).
    """
    if not name or not isinstance(name, str):
        return None
    safe = re.escape(name)
    try:
        return re.compile(rf"\b{safe}\b")
    except re.error:
        return None


def _normalize_method(method: object) -> str:
    if not isinstance(method, str):
        return ""
    return method.strip().upper()


def _normalize_endpoint_path(endpoint: object) -> str:
    r"""
    LLM 이 만든 endpoint 를 매칭용 정규식 안전 형태로 변환.

    경로 매개변수 변형 흡수:
      /users/{id}        ← FastAPI/Spring
      /users/:id         ← Express
      /users/<int:id>    ← Django
    위 셋 모두 같은 path 라 봐야 매칭 가능 → 매개변수 자리를 `[^/\s\"']+` 로 치환.
    """
    if not isinstance(endpoint, str):
        return ""
    p = endpoint.strip()
    if not p:
        return ""
    # leading /  보장
    if not p.startswith("/"):
        p = "/" + p
    # 매개변수 자리 (정확한 이름은 framework 별로 다름 — 통째로 placeholder 치환)
    # 1) {param} / {int:param}
    p = re.sub(r"\{[^}]+\}", "{__P__}", p)
    # 2) :param (express)
    p = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "{__P__}", p)
    # 3) <int:param> (django)
    p = re.sub(r"<[^>]+>", "{__P__}", p)
    return p


def _endpoint_regex_fragment(endpoint_path: str) -> Optional[str]:
    r"""
    `/users/{__P__}/refund` 같은 정규화된 path → 정규식 fragment 문자열 반환.
    매개변수 자리는 `[^/\s\"']+` 매칭. path 자체에 정규식 메타 글자가 있어도 escape.
    """
    if not endpoint_path:
        return None
    parts = endpoint_path.split("{__P__}")
    return r"[^/\s\"']+".join(re.escape(p) for p in parts)


# ─── SPACK.API: endpoint 매칭 ──────────────────────────────────


# decorator/method names per framework. 첫 group 이 method, 두 번째가 path.
def _api_patterns_for(method: str, endpoint_path: str) -> List[re.Pattern[str]]:
    """
    하나의 (method, endpoint) 페어에 대해 6개 프레임워크 정규식을 만들어 반환.

    [지원 framework 별 매칭 예시 — POST /tickets]
      FastAPI/Flask    @router.post('/tickets')  /  @app.post('/tickets')
      Express/Koa      router.post('/tickets', h)
      Spring           @PostMapping("/tickets")  /  @RequestMapping(value="/tickets", method=...)
      Django           path('/tickets', view)
      Vue Router       { path: '/tickets', ... }
      React Router     <Route path="/tickets" ... />

    한 framework 만 매칭 잡혀도 evidence 1건으로 인정. 운영에서 보통 한 프로젝트는
    1~2개 framework 만 쓰므로 6개 패턴 다 시도해도 비용 작음.
    """
    m_lower = method.lower()
    m_upper = method.upper()
    path_frag = _endpoint_regex_fragment(endpoint_path) or ""
    if not path_frag:
        return []

    patterns: List[re.Pattern[str]] = []

    # FastAPI / Flask / Sanic — @router.post("/x")  /  @app.post("/x")
    # method 변종: post, get, put, delete, patch
    try:
        patterns.append(
            re.compile(
                rf"""@\s*(?:[A-Za-z_][\w\.]*)\.{m_lower}\s*\(\s*["']{path_frag}["']"""
            )
        )
    except re.error:
        pass

    # Express / Koa — router.post("/x") / app.post("/x")  (decorator 아님)
    try:
        patterns.append(
            re.compile(
                rf"""(?:router|app|server)\.{m_lower}\s*\(\s*["']{path_frag}["']"""
            )
        )
    except re.error:
        pass

    # Spring — @PostMapping("/x") / @GetMapping("/x") / @RequestMapping(value = "/x", method = RequestMethod.POST)
    method_capitalized = m_upper.capitalize()  # POST → Post
    try:
        patterns.append(
            re.compile(
                rf"""@{method_capitalized}Mapping\s*\(\s*["']{path_frag}["']"""
            )
        )
    except re.error:
        pass
    try:
        # @RequestMapping(value = "/x", method = RequestMethod.POST)
        # path 와 method 가 같은 어노테이션 안에 있어야 함 — 줄 단위 매칭은 한계
        # 라 path 만 잡고 method 는 LLM 검증으로 위임.
        patterns.append(
            re.compile(
                rf"""@RequestMapping\s*\([^)]*["']{path_frag}["']"""
            )
        )
    except re.error:
        pass

    # Django — path("/x", view)  /  re_path(r"^/x$", view)
    try:
        patterns.append(
            re.compile(
                rf"""(?:path|re_path|url)\s*\(\s*r?["'][\^]?{path_frag}"""
            )
        )
    except re.error:
        pass

    # Vue Router — { path: '/x', ... }
    try:
        patterns.append(
            re.compile(
                rf"""path\s*:\s*["']{path_frag}["']"""
            )
        )
    except re.error:
        pass

    # React Router — <Route path="/x" />
    try:
        patterns.append(
            re.compile(
                rf"""<Route[^>]*\bpath\s*=\s*["']{path_frag}["']"""
            )
        )
    except re.error:
        pass

    return patterns


def collect_api_evidence(
    api: Dict[str, object], samples: Sequence[FileSample]
) -> List[Evidence]:
    """
    SPACK.API 한 건이 sample 파일 어느 줄에 구현돼 있는지 grep.

    [예시]
      api = {"method": "POST", "endpoint": "/tickets/{id}/refund"}
      → "@router.post('/tickets/{id}/refund')" 같은 줄을 찾으면 Evidence 반환.

    Args:
      api: {id, name, method, endpoint, ...} — Spack API 노드 properties
    Returns:
      매칭된 파일들의 Evidence 리스트 (최대 5건, dedupe).
      매칭 0건이면 빈 리스트 (이 항목은 LLM residual pass 로 넘어감).
    """
    endpoint_raw = api.get("endpoint") or ""
    method_raw = api.get("method") or ""
    endpoint_path = _normalize_endpoint_path(endpoint_raw)
    method = _normalize_method(method_raw)

    if not endpoint_path:
        return []

    patterns = _api_patterns_for(method, endpoint_path) if method else []
    if not patterns:
        # method 모르거나 패턴 빌드 실패 — path 자체만 substring 매칭으로 weak 시도
        try:
            patterns = [
                re.compile(
                    rf"""["']{_endpoint_regex_fragment(endpoint_path)}["']"""
                )
            ]
        except re.error:
            patterns = []
    if not patterns:
        return []

    evidence: List[Evidence] = []
    for s in samples:
        for pat in patterns:
            hits = _iter_matches(s.content, pat, max_per_file=3)
            for line_no, line in hits:
                evidence.append(
                    Evidence(
                        file=s.path,
                        line=line_no,
                        snippet=_snippet(line),
                        kind="endpoint",
                    )
                )
            if hits:
                break  # 한 파일에서 한 패턴이 잡히면 다음 파일로 — 중복 회피
    # 파일 단위 dedupe (한 endpoint 가 한 파일에서 여러 줄 매칭돼도 1건만)
    seen: set = set()
    deduped: List[Evidence] = []
    for e in evidence:
        key = (e.file, e.line)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped[:5]


# ─── SPACK.Entity / DDD.Aggregate / DDD.DomainEntity ──────────


# 다양한 언어의 class/schema 선언 — name 자리 placeholder
# 매칭 시 _word_boundary_pattern(name) 으로 추가 1차 필터.
_CLASS_DECLARATION_PATTERNS_TEMPLATES = [
    # Python:  class Foo(...): / class Foo:
    r"^\s*class\s+{name}\b",
    # Python decorators 위에 있어도 매칭 — 데코레이터 줄은 별도 매칭 안 함
    # TypeScript/JS: class Foo / interface Foo / type Foo =
    r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+{name}\b",
    r"^\s*(?:export\s+)?interface\s+{name}\b",
    r"^\s*(?:export\s+)?type\s+{name}\s*=",
    # Java/Kotlin: class Foo / @Entity class Foo / data class Foo
    r"^\s*(?:public|private|protected|internal)?\s*(?:abstract\s+|final\s+|data\s+|sealed\s+)?class\s+{name}\b",
    # Go: type Foo struct
    r"^\s*type\s+{name}\s+struct\b",
    # Rust: struct Foo / enum Foo
    r"^\s*(?:pub\s+)?(?:struct|enum)\s+{name}\b",
    # Vue 3 SFC: defineComponent / export default — name 식별이 어려움 → skip
]


def _class_patterns_for(name: str) -> List[re.Pattern[str]]:
    if not name:
        return []
    safe = re.escape(name)
    out: List[re.Pattern[str]] = []
    for tpl in _CLASS_DECLARATION_PATTERNS_TEMPLATES:
        try:
            out.append(re.compile(tpl.format(name=safe), re.MULTILINE))
        except re.error:
            continue
    return out


def collect_class_evidence(
    name: str,
    samples: Sequence[FileSample],
    *,
    kind_label: str = "class",
) -> List[Evidence]:
    """
    Entity / Aggregate / DomainEntity 처럼 클래스 이름으로 식별되는 spec 항목을 grep.

    [예시]
      name = "Ticket"
      Python:  "class Ticket(BaseModel):"   → 매칭
      TS:      "export interface Ticket {"  → 매칭
      Java:    "public class Ticket { }"    → 매칭

    [false positive 차단]
      - PascalCase 단일 합성어 (영문자 시작 + 영숫자 only) 만 매칭. 공백/한글/특수문자
        포함 name 은 즉시 빈 리스트 반환. 예: 'foo bar' / '티켓' / 'foo_bar' 거부.
      - 변수/함수/주석 등 클래스 선언이 아닌 등장은 패턴 자체가 안 잡음.
    """
    if not name or not name.strip():
        return []
    # PascalCase 단일 합성어만 허용 (공백 들어간 'Ticket Aggregate' 같은 표기 X)
    if not re.match(r"^[A-Z][A-Za-z0-9]*$", name):
        return []

    patterns = _class_patterns_for(name)
    if not patterns:
        return []

    evidence: List[Evidence] = []
    seen: set = set()
    for s in samples:
        for pat in patterns:
            hits = _iter_matches(s.content, pat, max_per_file=2)
            for line_no, line in hits:
                key = (s.path, line_no)
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(
                    Evidence(
                        file=s.path,
                        line=line_no,
                        snippet=_snippet(line),
                        kind=kind_label,
                    )
                )
    return evidence[:5]


# ─── 기획.Screen (2026-06 기획 항목 자동 검증) ──────────────────


def collect_screen_evidence(
    screen: Dict[str, object], samples: Sequence[FileSample]
) -> List[Evidence]:
    """
    Screen 한 건이 코드에 구현돼 있는지 grep — route 정의 또는 화면 컴포넌트.

    [예시]
      screen = {"name": "로그인 화면", "path": "/login"}
      Vue Router:   "path: '/login'"          → 매칭
      React Router: "<Route path='/login'"    → 매칭
      screen = {"name": "LoginPage", ...}
      컴포넌트:      "export class LoginPage"  → 매칭 (PascalCase 이름일 때만)

    [false positive 차단]
      - path 는 route 정의 문맥(`path:` / `path=` / <Route ...path=)에서만 매칭.
        따옴표 안 단순 등장("/login" 문자열 — axios 호출 등)은 안 잡음.
      - 루트 "/" 는 어떤 라우터에나 있어 증거 가치가 없음 → 패턴 생략.
      - 한글 name 은 class 패턴이 빈 리스트 → LLM residual 로 넘어감 (의미 매칭).
    """
    patterns: List[re.Pattern[str]] = []
    raw_path = screen.get("path") or ""
    norm = _normalize_endpoint_path(raw_path)
    if norm and norm != "/":
        frag = _endpoint_regex_fragment(norm)
        if frag:
            try:
                # Vue Router/Angular: path: '/login' | path = '/login'
                patterns.append(
                    re.compile(rf"""path\s*[:=]\s*["']{frag}["']""")
                )
                # React Router: <Route path="/login" .../> (속성 순서 무관, 같은 줄)
                patterns.append(
                    re.compile(rf"""<Route\b[^>]{{0,200}}path=["']{frag}["']""")
                )
            except re.error:
                pass

    name = str(screen.get("name") or "")
    class_patterns = _class_patterns_for(name) if re.match(
        r"^[A-Z][A-Za-z0-9]*$", name
    ) else []

    evidence: List[Evidence] = []
    seen: set = set()
    for s in samples:
        for pat in patterns:
            for line_no, line in _iter_matches(s.content, pat, max_per_file=2):
                key = (s.path, line_no)
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(Evidence(
                    file=s.path, line=line_no, snippet=_snippet(line),
                    kind="screen_route",
                ))
        for pat in class_patterns:
            for line_no, line in _iter_matches(s.content, pat, max_per_file=2):
                key = (s.path, line_no)
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(Evidence(
                    file=s.path, line=line_no, snippet=_snippet(line),
                    kind="screen_component",
                ))
    return evidence[:5]


# ─── DDD.DomainEvent ──────────────────────────────────────────


def collect_event_evidence(
    name: str, samples: Sequence[FileSample]
) -> List[Evidence]:
    """
    Domain event 가 코드에 정의 + 발행되어 있는지 grep.

    [예시]
      name = "TicketIssued"
      - 클래스 선언 매칭: "class TicketIssued: ..."          → kind='event_class'
      - 발행 호출 매칭:  "publish(TicketIssued(ticket_id=x))"→ kind='event_publish'

    클래스만 있고 발행 호출 없으면 dead code 가능성 → 그래도 evidence 1건은 인정.
    완전 없으면 LLM residual pass 에서 다시 검증.
    """
    if not name or not name.strip():
        return []
    if not re.match(r"^[A-Z][A-Za-z0-9]*$", name):
        return []

    class_ev = collect_class_evidence(name, samples, kind_label="event_class")
    # publish/emit 호출 추가 매칭
    try:
        publish_pat = re.compile(
            rf"""\b(?:publish|emit|dispatch|raise|send|fire)\b\s*[\(<].*\b{re.escape(name)}\b"""
        )
    except re.error:
        publish_pat = None

    publish_ev: List[Evidence] = []
    if publish_pat:
        for s in samples:
            hits = _iter_matches(s.content, publish_pat, max_per_file=2)
            for line_no, line in hits:
                publish_ev.append(
                    Evidence(
                        file=s.path,
                        line=line_no,
                        snippet=_snippet(line),
                        kind="event_publish",
                    )
                )

    return (class_ev + publish_ev)[:5]


# ─── DDD.BoundedContext ───────────────────────────────────────


def collect_context_evidence(
    name: str, samples: Sequence[FileSample]
) -> List[Evidence]:
    """
    BoundedContext 가 디렉토리/패키지 구조로 표현됐는지 확인.

    [예시]
      BoundedContext 'Funding' → 'src/funding/foo.py' 같은 경로에 매칭.
      BoundedContext 'UserAccount' → 'user_account' / 'user-account' / 'useraccount'
        세 변형 모두 매칭 (PascalCase → snake/kebab/concat 자동 생성).

    name 길이 < 3 자 면 너무 짧아 false positive 위험 → 빈 리스트.
    """
    if not name or not name.strip():
        return []
    lower = name.lower().strip()
    # 공백/하이픈/언더스코어 normalize
    variants = {lower, re.sub(r"[\s\-_]+", "", lower)}
    # PascalCase → snake/kebab
    snake = re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower()
    kebab = re.sub(r"([a-z])([A-Z])", r"\1-\2", name).lower()
    variants.update({snake, kebab})
    variants = {v for v in variants if v and len(v) >= 3}
    if not variants:
        return []

    seen: set = set()
    evidence: List[Evidence] = []
    for s in samples:
        path_lower = s.path.lower()
        for v in variants:
            # path segment 단위 매칭 — false positive 차단
            if f"/{v}/" in path_lower or path_lower.startswith(f"{v}/"):
                key = s.path
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(
                    Evidence(
                        file=s.path,
                        line=0,
                        snippet=s.path,
                        kind="context_dir",
                    )
                )
                break
    return evidence[:5]


# ─── Arch.Service / Database — tech_stack manifest 매칭 ───────


# tech_stack → manifest 키워드 (lowercase).
# 키워드는 substring 매칭이라 short string 은 false positive 위험 → 4+ 권장.
_TECH_STACK_KEYWORDS: Dict[str, List[str]] = {
    "fastapi": ["fastapi"],
    "flask": ["flask"],
    "django": ["django"],
    "spring boot": ["spring-boot", "springframework.boot", "org.springframework.boot"],
    "spring": ["org.springframework"],
    "express": ["express"],
    "nest.js": ["@nestjs/core", "@nestjs/common"],
    "nestjs": ["@nestjs/core", "@nestjs/common"],
    "next.js": ['"next":', "next/router"],
    "nextjs": ['"next":', "next/router"],
    "react": ['"react":'],
    "vue.js": ['"vue":'],
    "vue": ['"vue":'],
    "node.js": ['"node":'],
    "postgresql": ["postgres", "psycopg", "asyncpg", "pg8000"],
    "postgres": ["postgres", "psycopg", "asyncpg"],
    "mysql": ["mysql", "pymysql", "mysql-connector"],
    "mongodb": ["mongodb", "pymongo", "mongoose"],
    "redis": ["redis"],
    "kafka": ["kafka", "kafkajs", "spring-kafka"],
    "neo4j": ["neo4j"],
    "sqlite": ["sqlite", "aiosqlite"],
    "python": ["python_requires", "python-version"],
    "java": ["java.version", "<java.version>"],
    "kotlin": ["kotlin"],
    "go": ["module ", "go.mod"],
    "rust": ['[package]', "cargo"],
}


# manifest 파일들 — 무조건 sample 에 포함시켜야 함
MANIFEST_FILES: Tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "Pipfile",
    "poetry.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "Cargo.toml",
    "go.mod",
    "go.sum",
    "composer.json",
    "Gemfile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
)


def _is_manifest(path: str) -> bool:
    """basename 이 manifest 후보면 True."""
    base = os.path.basename(path)
    return base in MANIFEST_FILES


def _tech_stack_terms(tech_stack: object) -> List[str]:
    """
    tech_stack 은 string 또는 list 일 수 있음. 표준화 후 lowercase 리스트 반환.
    """
    out: List[str] = []
    if isinstance(tech_stack, str):
        # 콤마/슬래시 구분 가능
        for t in re.split(r"[,/]+", tech_stack):
            t = t.strip().lower()
            if t:
                out.append(t)
    elif isinstance(tech_stack, list):
        for t in tech_stack:
            if isinstance(t, str) and t.strip():
                out.append(t.strip().lower())
    return out


def collect_tech_stack_evidence(
    tech_stack: object, samples: Sequence[FileSample]
) -> List[Evidence]:
    """
    Architecture.Service / Database 의 tech_stack 이 의존성 파일(manifest) 에 있는지 grep.

    [예시]
      'Vue.js'       → package.json 의 '"vue":'             매칭
      'PostgreSQL'   → requirements.txt 의 'asyncpg'        매칭
      'Spring Boot'  → pom.xml 의 'org.springframework.boot' 매칭

    manifest 파일이 아닌 일반 소스 (src/main.py 등) 는 검사 대상에서 제외 →
    "코드 import 1번" 같은 약한 신호로 인한 false positive 차단.
    """
    terms = _tech_stack_terms(tech_stack)
    if not terms:
        return []

    manifests = [s for s in samples if _is_manifest(s.path)]
    if not manifests:
        return []

    evidence: List[Evidence] = []
    seen: set = set()
    for term in terms:
        # term 자체 + 매핑된 keywords
        keywords = _TECH_STACK_KEYWORDS.get(term, [])
        # term 자체도 매칭 후보 — 길이 3+ 일 때만 (false positive 차단)
        if len(term) >= 3:
            keywords = list(set(keywords + [term]))
        for s in manifests:
            content_lower = s.content.lower()
            for kw in keywords:
                if kw and kw in content_lower:
                    # 매칭된 첫 줄 찾기
                    line_no = 0
                    snippet = kw
                    for n, raw in enumerate(s.content.splitlines(), start=1):
                        if kw in raw.lower():
                            line_no = n
                            snippet = _snippet(raw)
                            break
                    key = (s.path, line_no, kw)
                    if key in seen:
                        continue
                    seen.add(key)
                    evidence.append(
                        Evidence(
                            file=s.path,
                            line=line_no,
                            snippet=snippet,
                            kind="manifest",
                        )
                    )
                    break  # 한 매니페스트당 한 키워드 매칭이면 충분
    return evidence[:5]


# ─── Rules — token 기반 weak 매칭 ──────────────────────────────
#
# [2026-06] collect_policy_evidence (Policy 4글자+ 토큰 substring 매칭) 삭제 —
# 'audit' 가 주석/변수명 어디에 있든 applied 처리되는, 카테고리 중 최악의
# 위양성원이었다. Policy 검증은 evaluator 가 전부 LLM residual (file:line
# 인용 강제)로 보낸다.


def collect_rule_evidence(
    rule: Dict[str, object], samples: Sequence[FileSample]
) -> List[Evidence]:
    """
    Rule Generator 가 만든 Skill 항목(코딩 규칙)이 코드에 적용됐는지 grep.

    [예시]
      {"name": "TypeScriptStrict", "tags": ["typescript", "strict"]}
        → 본문에 'typescript' / 'strict' 토큰 등장하면 매칭.

    Policy 와 동일한 weak 매칭. manifest 제외. 코딩 규칙 (들여쓰기/네이밍 등) 은
    토큰 매칭만으로 잡기 어려워 LLM residual 비중 ↑.
    """
    name = rule.get("name")
    tags = rule.get("tags")

    tokens: List[str] = []
    if isinstance(name, str):
        for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", name):
            if len(w) >= 4:
                tokens.append(w.lower())
    if isinstance(tags, list):
        for t in tags:
            # cat: 는 export 카테고리 분류용 내부 마커 — 코드 근거 매칭 토큰에서 제외(전 소비처 일관 규약).
            if isinstance(t, str) and not t.startswith("cat:") and len(t) >= 3:
                tokens.append(t.strip().lower())

    tokens = list(dict.fromkeys(t for t in tokens if t))
    if not tokens:
        return []

    evidence: List[Evidence] = []
    seen: set = set()
    for s in samples:
        if _is_manifest(s.path):
            continue
        content_lower = s.content.lower()
        for tok in tokens:
            if tok in content_lower:
                for n, raw in enumerate(s.content.splitlines(), start=1):
                    if tok in raw.lower():
                        key = (s.path, n)
                        if key in seen:
                            continue
                        seen.add(key)
                        evidence.append(
                            Evidence(
                                file=s.path,
                                line=n,
                                snippet=_snippet(raw),
                                kind="rule_token",
                            )
                        )
                        break
                break
    return evidence[:3]
