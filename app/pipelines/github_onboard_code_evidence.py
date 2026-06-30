"""
GitHub Onboard 결정적 코드 단서 추출 — 프롬프트에 사실 표 주입용.

[목적]
LLM 프롬프트가 README 우선 위계로 V1 을 만들어 사용자가 "README 만 읽은 느낌" 으로
체감(자연어 가드는 L1-2 #70 처럼 LLM 행동 강제 못함). 결정적 코드 추출 → markdown 표
주입으로 LLM 이 무시 못 하게. L1-1 backstop 디자인과 동일 철학 (is_meaningful_spec_node).

[추출 범위]
- manifest facts: package.json / pyproject.toml / requirements.txt 등의 의존성 + 프레임워크 힌트.
- entry signals: Python AST + JS/TS regex 로 route/export/component 추출.
- repo stats: tree 메타에서 디렉토리·확장자 분포.

추가 GitHub API 호출 0 (이미 fetch 한 samples + tree_blobs 활용). 추출 실패는 silent.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore

logger = logging.getLogger(__name__)


# 알려진 프레임워크/라이브러리 힌트 — dependencies 에서 발견되면 framework_hints 에 추가.
_FRAMEWORK_HINTS = {
    # JS/TS
    "vue", "react", "next", "nuxt", "svelte", "solid-js", "preact",
    "express", "fastify", "hono", "nestjs",
    "vite", "webpack", "rollup",
    "pinia", "vuex", "redux", "zustand", "jotai",
    # Python
    "fastapi", "flask", "django", "starlette", "sanic", "tornado",
    "sqlalchemy", "neo4j", "psycopg2", "psycopg2-binary",
    "pydantic", "celery", "arq",
    # Go
    "gin", "echo", "fiber", "chi", "gorilla",
    # Rust
    "actix-web", "axum", "rocket", "tokio", "warp", "tower",
    # Java / Kotlin
    "spring-boot", "spring-boot-starter-web", "ktor",
    # PHP / Ruby
    "laravel", "symfony", "rails", "sinatra",
}

# 매니페스트 파일 → 언어 매핑.
_MANIFEST_TO_LANGS = {
    "package.json": ["javascript", "typescript"],
    "pyproject.toml": ["python"],
    "requirements.txt": ["python"],
    "Pipfile": ["python"],
    "go.mod": ["go"],
    "Cargo.toml": ["rust"],
    "pom.xml": ["java"],
    "build.gradle": ["java", "kotlin"],
    "build.gradle.kts": ["java", "kotlin"],
    "composer.json": ["php"],
    "Gemfile": ["ruby"],
}


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _is_framework_hint(name: str) -> bool:
    n = name.lower()
    if n in _FRAMEWORK_HINTS:
        return True
    # @scoped 패키지 — @nestjs/core, @vue/cli 등도 인정.
    if n.startswith("@"):
        parts = n.split("/", 1)
        if parts[0][1:] in _FRAMEWORK_HINTS:
            return True
    return False


def _parse_package_json(content: str) -> Dict[str, List[str]]:
    """package.json → {deps, dev_deps}. 파싱 실패 시 빈."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return {"deps": [], "dev_deps": []}
    if not isinstance(data, dict):  # [] / null / "str" 등 dict 아닌 유효 JSON 방어
        return {"deps": [], "dev_deps": []}
    deps_obj = data.get("dependencies")
    dev_obj = data.get("devDependencies")
    deps = list(deps_obj.keys()) if isinstance(deps_obj, dict) else []
    dev_deps = list(dev_obj.keys()) if isinstance(dev_obj, dict) else []
    return {"deps": deps, "dev_deps": dev_deps}


def _strip_version(spec: str) -> str:
    """ "fastapi>=0.115" → "fastapi" """
    return re.split(r"[<>=!~\s\[]", spec, maxsplit=1)[0].strip().lower()


def _parse_pyproject(content: str) -> Dict[str, Any]:
    """pyproject.toml → {deps, runtime}. 파싱 실패 시 빈."""
    if tomllib is None:
        return {"deps": [], "runtime": None}
    try:
        data = tomllib.loads(content)
    except Exception:  # noqa: BLE001 — tomllib 다양한 예외
        return {"deps": [], "runtime": None}
    project = data.get("project")
    if not isinstance(project, dict):  # project = [] 같은 비정상 구조 방어
        return {"deps": [], "runtime": None}
    deps_raw = project.get("dependencies")
    if not isinstance(deps_raw, list):
        deps_raw = []
    deps = [_strip_version(d) for d in deps_raw if isinstance(d, str)]
    runtime = project.get("requires-python")
    return {"deps": deps, "runtime": runtime}


def _parse_requirements_txt(content: str) -> List[str]:
    """requirements.txt → [pkg names]. 주석/빈줄 무시."""
    out: List[str] = []
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-"):
            continue
        out.append(_strip_version(s))
    return out


def _parse_go_mod(content: str) -> List[str]:
    """go.mod require 블록/라인 → 모듈 마지막 segment 목록 (예: gin-gonic/gin → gin)."""
    out: List[str] = []
    in_block = False
    for raw in content.splitlines():
        s = raw.strip()
        if s.startswith("require ("):
            in_block = True
            continue
        if in_block and s.startswith(")"):
            in_block = False
            continue
        if not (in_block or s.startswith("require ")):
            continue
        m = re.match(r"(?:require\s+)?([a-zA-Z0-9][\w.\-]*(?:/[\w.\-]+)*)\s+v\d", s)
        if m:
            out.append(m.group(1).rsplit("/", 1)[-1].lower())
    return out


def _parse_cargo_toml(content: str) -> List[str]:
    """Cargo.toml [dependencies]/[dev-dependencies] 키 목록."""
    if tomllib is None:
        return []
    try:
        data = tomllib.loads(content)
    except Exception:  # noqa: BLE001 — tomllib 다양한 예외
        return []
    out: List[str] = []
    for section in ("dependencies", "dev-dependencies"):
        deps = data.get(section)
        if isinstance(deps, dict):
            out.extend(k.lower() for k in deps.keys())
    return out


def _parse_gradle(content: str) -> List[str]:
    """build.gradle(.kts) implementation/api 'group:artifact:ver' → artifact."""
    out: List[str] = []
    pat = r"(?:implementation|api|compile|testImplementation)\s*[\(\s]\s*['\"]([^'\"]+)['\"]"
    for m in re.finditer(pat, content):
        parts = m.group(1).split(":")
        if len(parts) >= 2 and parts[1]:
            out.append(parts[1].lower())
    return out


def _parse_composer_json(content: str) -> List[str]:
    """composer.json require 키 (vendor/package). php 버전 키 등 비-패키지 제외."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):  # [] / null 등 dict 아닌 유효 JSON 방어
        return []
    req = data.get("require") or {}
    if not isinstance(req, dict):
        return []
    return [k.lower() for k in req.keys() if "/" in k]


def _parse_gemfile(content: str) -> List[str]:
    """Gemfile gem 'x' → x."""
    return [m.group(1).lower() for m in re.finditer(r"gem\s+['\"]([^'\"]+)['\"]", content)]


def _parse_pom_xml(content: str) -> List[str]:
    """pom.xml <artifactId>x</artifactId> → x."""
    return [m.group(1).strip().lower() for m in re.finditer(r"<artifactId>([^<]+)</artifactId>", content)]


def extract_manifest_facts(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """매니페스트 파일들에서 언어 + 의존성 + 프레임워크 힌트 결정적 추출.

    여러 매니페스트가 있으면 모두 합침(monorepo / multi-language).
    파싱 실패는 silent — 다른 파일은 계속 처리.
    """
    languages: List[str] = []
    deps: List[str] = []
    dev_deps: List[str] = []
    runtime = None

    for s in samples:
        path = s.get("path") or ""
        base = _basename(path)
        content = s.get("content") or ""
        langs = _MANIFEST_TO_LANGS.get(base)
        if not langs:
            continue
        for lang in langs:
            if lang not in languages:
                languages.append(lang)
        if base == "package.json":
            r = _parse_package_json(content)
            deps.extend(r["deps"])
            dev_deps.extend(r["dev_deps"])
        elif base == "pyproject.toml":
            r = _parse_pyproject(content)
            deps.extend(r["deps"])
            if r["runtime"] and not runtime:
                runtime = r["runtime"]
        elif base == "requirements.txt":
            deps.extend(_parse_requirements_txt(content))
        elif base == "go.mod":
            deps.extend(_parse_go_mod(content))
        elif base == "Cargo.toml":
            deps.extend(_parse_cargo_toml(content))
        elif base in ("build.gradle", "build.gradle.kts"):
            deps.extend(_parse_gradle(content))
        elif base == "composer.json":
            deps.extend(_parse_composer_json(content))
        elif base == "Gemfile":
            deps.extend(_parse_gemfile(content))
        elif base == "pom.xml":
            deps.extend(_parse_pom_xml(content))

    framework_hints = sorted({d for d in (deps + dev_deps) if _is_framework_hint(d)})

    return {
        "languages": languages,
        "runtime": runtime,
        "deps": sorted(set(deps)),
        "dev_deps": sorted(set(dev_deps)),
        "framework_hints": framework_hints,
    }


# ─── Entry signals — Python AST + JS/TS regex ────────────────────

import ast


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
# JS/TS: `app.get('/path', ...)` 또는 `router.post("/path", ...)` 매칭.
_JS_ROUTE_RE = re.compile(
    r"\b(?:app|router|bp|api)\.(get|post|put|patch|delete|head|options)\s*\(\s*['\"]([^'\"]+)['\"]"
)
_VUE_SCRIPT_SETUP_RE = re.compile(r"<script\s+[^>]*setup", re.IGNORECASE)


def _first_str_arg(call: ast.Call):
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def _methods_kwarg(call: ast.Call):
    for kw in call.keywords:
        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
            out = []
            for el in kw.value.elts:
                if isinstance(el, ast.Constant) and isinstance(el.value, str):
                    out.append(el.value)
            return out or None
    return None


def _extract_python_decorator_route(dec: ast.expr):
    """단일 데코레이터에서 (method, path) 추출. 매칭 안 되면 None."""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if isinstance(func, ast.Attribute):
        attr = func.attr.lower()
        if attr in _HTTP_METHODS:
            path = _first_str_arg(dec)
            if path:
                return {"kind": "route", "method": attr.upper(), "path": path}
        if attr == "route":
            path = _first_str_arg(dec)
            methods = _methods_kwarg(dec) or ["GET"]
            if path and methods:
                return {"kind": "route", "method": methods[0].upper(), "path": path}
    return None


def _python_routes_from_ast(content: str, path: str) -> List[Dict[str, Any]]:
    """Python AST 로 @app.get / @router.post / @bp.route 데코레이터 추출."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        # 깨진 문법(SyntaxError)·null 바이트(ValueError)·과도한 중첩(RecursionError, 생성/압축
        # 코드) 모두 조용히 skip — 한 파일 실패가 전체 추출을 막지 않게.
        return []
    out: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            sig = _extract_python_decorator_route(dec)
            if sig is None:
                continue
            sig["file"] = path
            sig["name"] = node.name
            out.append(sig)
    return out


def _js_routes_from_regex(content: str, path: str) -> List[Dict[str, Any]]:
    """JS/TS regex 로 app.get/router.post/... 매칭."""
    out: List[Dict[str, Any]] = []
    for m in _JS_ROUTE_RE.finditer(content):
        out.append({
            "kind": "route",
            "method": m.group(1).upper(),
            "path": m.group(2),
            "file": path,
            "name": "",
        })
    return out


def _vue_component(path: str, content: str):
    if not path.endswith(".vue"):
        return None
    base = _basename(path)
    if "." in base:
        name = base.rsplit(".", 1)[0]
    else:
        name = base
    return {"kind": "component", "name": name, "file": path, "method": None, "path": None}


# ─── Multi-language route 추출 (Django / NestJS / Go / Spring) ─────────────────
# [정밀도 우선] 거짓 route(오탐)는 LLM 이 '없는 기능'을 지어내게 만들어 환각 가드 철학에
# 위배된다. 그래서 메서드+경로가 명확한 패턴만 매칭하고, 미매칭/parse 실패는 silent.

_HTTP_METHODS_ALT = "GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS"

# NestJS: @Get('x') / @Post("x") 메서드 데코레이터.
_NEST_METHOD_RE = re.compile(
    r"@(Get|Post|Put|Patch|Delete|Options|Head|All)\s*\(\s*(?:['\"]([^'\"]*)['\"])?"
)
_NEST_CONTROLLER_RE = re.compile(r"@Controller\s*\(\s*['\"]([^'\"]*)['\"]")
# Go: gin/echo `r.GET("/x", ...)` (대문자 메서드) + net/http `mux.HandleFunc("/x", ...)`.
_GO_ROUTE_RE = re.compile(r"\.(" + _HTTP_METHODS_ALT + r")\s*\(\s*\"([^\"]+)\"")
_GO_HANDLEFUNC_RE = re.compile(r"\.HandleFunc\s*\(\s*\"([^\"]+)\"")
# Spring: @GetMapping("x") / @RequestMapping(value="x") (Java/Kotlin).
_SPRING_METHOD_RE = re.compile(
    r"@(Get|Post|Put|Patch|Delete)Mapping\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?['\"]([^'\"]+)['\"]"
)
_SPRING_REQUEST_RE = re.compile(
    r"@RequestMapping\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?['\"]([^'\"]+)['\"]"
)


def _norm_join(prefix: str, sub: str) -> str:
    """controller prefix + route path 결합 — 중복 슬래시 정리.
    prefix='/' (루트 컨트롤러) 같은 경우도 '//x' 가 되지 않도록 strip 후 재구성."""
    p = prefix.strip("/")
    s = sub.strip("/")
    a = "/" + p if p else ""
    if a and s:
        return f"{a}/{s}"
    if a:
        return a
    return "/" + s if s else "/"


def _django_routes(content: str, path: str) -> List[Dict[str, Any]]:
    """Django urls.py — path('x', ...) / re_path. view 가 method 처리하므로 method 불명(빈값).
    [오탐 방지] urlpatterns / django.urls 단서가 있거나 파일명이 urls.py 인 경우만."""
    if (
        "urlpatterns" not in content
        and "django.urls" not in content
        and not path.endswith("urls.py")
    ):
        return []
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        # 깨진 문법(SyntaxError)·null 바이트(ValueError)·과도한 중첩(RecursionError, 생성/압축
        # 코드) 모두 조용히 skip — 한 파일 실패가 전체 추출을 막지 않게.
        return []
    out: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
        if fname not in ("path", "re_path"):
            continue
        p = _first_str_arg(node)
        if p is None:
            continue
        out.append({"kind": "route", "method": "", "path": p, "file": path, "name": ""})
    return out


def _nest_routes(content: str, path: str) -> List[Dict[str, Any]]:
    """NestJS — @Controller('prefix') + @Get/@Post 메서드 데코레이터 path 결합."""
    ctrl = _NEST_CONTROLLER_RE.search(content)
    prefix = ctrl.group(1) if ctrl else ""
    out: List[Dict[str, Any]] = []
    for m in _NEST_METHOD_RE.finditer(content):
        method = m.group(1).upper()
        method = "ANY" if method == "ALL" else method
        out.append({
            "kind": "route", "method": method,
            "path": _norm_join(prefix, m.group(2) or ""), "file": path, "name": "",
        })
    return out


def _go_routes(content: str, path: str) -> List[Dict[str, Any]]:
    """Go — gin/echo `.GET("x")` (대문자 메서드) + net/http `.HandleFunc("x")`(method 불명)."""
    out: List[Dict[str, Any]] = []
    for m in _GO_ROUTE_RE.finditer(content):
        out.append({"kind": "route", "method": m.group(1).upper(), "path": m.group(2), "file": path, "name": ""})
    for m in _GO_HANDLEFUNC_RE.finditer(content):
        out.append({"kind": "route", "method": "", "path": m.group(1), "file": path, "name": ""})
    return out


def _spring_routes(content: str, path: str) -> List[Dict[str, Any]]:
    """Spring — @GetMapping("x") 등 + @RequestMapping(method 불명) (Java/Kotlin)."""
    out: List[Dict[str, Any]] = []
    for m in _SPRING_METHOD_RE.finditer(content):
        out.append({"kind": "route", "method": m.group(1).upper(), "path": m.group(2), "file": path, "name": ""})
    for m in _SPRING_REQUEST_RE.finditer(content):
        out.append({"kind": "route", "method": "", "path": m.group(1), "file": path, "name": ""})
    return out


# ─── Phase C: 추가 언어 route (Vue Router / Rails / Laravel / Rust / C#) ───
_VUE_ROUTER_RE = re.compile(r"\bpath\s*:\s*['\"]([^'\"]+)['\"]")  # \b: filepath/basepath 오탐 차단
_RAILS_VERB_RE = re.compile(r"\b(get|post|put|patch|delete)\s+['\"]([^'\"]+)['\"]")
_RAILS_RES_RE = re.compile(r"\bresources?\s+:(\w+)")
_LARAVEL_RE = re.compile(r"Route::(get|post|put|patch|delete|any|match)\s*\(\s*['\"]([^'\"]+)['\"]")
_RUST_ACTIX_RE = re.compile(r"#\[(get|post|put|patch|delete)\s*\(\s*\"([^\"]+)\"")
_RUST_AXUM_RE = re.compile(r"\.route\s*\(\s*\"([^\"]+)\"")
_CSHARP_ATTR_RE = re.compile(r"\[Http(Get|Post|Put|Patch|Delete)\s*\(\s*\"([^\"]+)\"")
_CSHARP_MAP_RE = re.compile(r"\.Map(Get|Post|Put|Patch|Delete)\s*\(\s*\"([^\"]+)\"")


def _vue_router_routes(content: str, path: str) -> List[Dict[str, Any]]:
    """Vue Router — routes 배열의 path. [오탐 방지] createRouter/vue-router 단서 있을 때만."""
    if "createRouter" not in content and "vue-router" not in content:
        return []
    return [
        {"kind": "route", "method": "", "path": m.group(1), "file": path, "name": ""}
        for m in _VUE_ROUTER_RE.finditer(content)
    ]


def _rails_routes(content: str, path: str) -> List[Dict[str, Any]]:
    """Rails routes.rb — get '/x' / resources :x. [오탐 방지] routes.draw/routes.rb 단서만."""
    if "routes.draw" not in content and not path.endswith("routes.rb"):
        return []
    out: List[Dict[str, Any]] = []
    for m in _RAILS_VERB_RE.finditer(content):
        out.append({"kind": "route", "method": m.group(1).upper(), "path": m.group(2), "file": path, "name": ""})
    for m in _RAILS_RES_RE.finditer(content):
        out.append({"kind": "route", "method": "", "path": "/" + m.group(1), "file": path, "name": ""})
    return out


def _laravel_routes(content: str, path: str) -> List[Dict[str, Any]]:
    """Laravel — Route::get('/x', ...)."""
    out: List[Dict[str, Any]] = []
    for m in _LARAVEL_RE.finditer(content):
        method = m.group(1).upper()
        out.append({
            "kind": "route", "method": "ANY" if method in ("ANY", "MATCH") else method,
            "path": m.group(2), "file": path, "name": "",
        })
    return out


def _rust_routes(content: str, path: str) -> List[Dict[str, Any]]:
    """Rust — actix `#[get("/x")]` + axum `.route("/x", ...)`(method 불명)."""
    out: List[Dict[str, Any]] = []
    for m in _RUST_ACTIX_RE.finditer(content):
        out.append({"kind": "route", "method": m.group(1).upper(), "path": m.group(2), "file": path, "name": ""})
    for m in _RUST_AXUM_RE.finditer(content):
        out.append({"kind": "route", "method": "", "path": m.group(1), "file": path, "name": ""})
    return out


def _csharp_routes(content: str, path: str) -> List[Dict[str, Any]]:
    """C# — `[HttpGet("x")]` + `app.MapGet("/x", ...)`."""
    out: List[Dict[str, Any]] = []
    for m in _CSHARP_ATTR_RE.finditer(content):
        out.append({"kind": "route", "method": m.group(1).upper(), "path": m.group(2), "file": path, "name": ""})
    for m in _CSHARP_MAP_RE.finditer(content):
        out.append({"kind": "route", "method": m.group(1).upper(), "path": m.group(2), "file": path, "name": ""})
    return out


# ─── Phase E: export / 함수 시그니처 (route 없는 라이브러리·CLI 기능 단서) ───
_JS_EXPORT_RE = re.compile(
    r"export\s+(?:default\s+)?(?:async\s+)?(?:function|const|class|let)\s+([A-Za-z_$][\w$]*)"
)


def _python_exports(content: str, path: str) -> List[Dict[str, Any]]:
    """top-level public def/class 이름 — route 가 없을 때 기능 추론 단서."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        # 깨진 문법(SyntaxError)·null 바이트(ValueError)·과도한 중첩(RecursionError, 생성/압축
        # 코드) 모두 조용히 skip — 한 파일 실패가 전체 추출을 막지 않게.
        return []
    out: List[Dict[str, Any]] = []
    for node in tree.body:  # top-level 만 (중첩 함수/메서드 제외)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("_"):
                continue
            out.append({"kind": "export", "name": node.name, "file": path, "method": None, "path": None})
    return out


def _js_exports(content: str, path: str) -> List[Dict[str, Any]]:
    """JS/TS export function/const/class/let 이름."""
    return [
        {"kind": "export", "name": m.group(1), "file": path, "method": None, "path": None}
        for m in _JS_EXPORT_RE.finditer(content)
    ]


def _dedup_signals(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """(kind, method, path, name, file) 동일 시그널 중복 제거 — route/component/export 공통.
    [주의] export 는 path 가 없어 name 을 키에 포함해야 같은 파일 export 들이 뭉개지지 않는다."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for s in signals:
        key = (s.get("kind"), s.get("method"), s.get("path"), s.get("name"), s.get("file"))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# [오탐 방지] 테스트·예제·문서 경로 — 여기 담긴 코드의 route/export 는 '실제 기능'이
# 아니라 샘플/픽스처라, 권위적 단서로 쓰면 LLM 이 없는 기능을 지어낸다. signal 추출에서 제외.
_SKIP_SIGNAL_SEGMENTS = {
    "test", "tests", "__tests__", "spec", "specs", "example", "examples",
    "docs", "doc", "fixtures", "fixture", "mocks", "__mocks__", "sample", "samples",
}


def _is_test_or_doc_path(path: str) -> bool:
    lower = path.lower().replace("\\", "/")
    segs = lower.split("/")
    if any(seg in _SKIP_SIGNAL_SEGMENTS for seg in segs[:-1]):  # 디렉토리 세그먼트
        return True
    base = segs[-1]
    return (
        ".test." in base or ".spec." in base
        or base.endswith("_test.py") or base.endswith("_test.go")
        or base.endswith("_spec.rb") or base.startswith("test_")
    )


def extract_entry_signals(samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """entry/code 파일들에서 route(다언어) + Vue component 결정적 추출.

    parse 실패는 silent. 매칭된 시그널이 LLM 의 권위적 입력이 된다(README 회귀 차단).
    언어/프레임워크: Python(FastAPI/Flask 데코 + Django urls), JS/TS(Express + NestJS),
    Go(gin/echo/net-http), Java·Kotlin(Spring), Vue(component).
    테스트/예제/문서 경로는 가짜 route 오탐 방지를 위해 건너뛴다.
    """
    out: List[Dict[str, Any]] = []
    for s in samples:
        path = s.get("path") or ""
        content = s.get("content") or ""
        if not path or not content:
            continue
        if _is_test_or_doc_path(path):
            continue
        lower = path.lower()
        if lower.endswith(".py"):
            out.extend(_python_routes_from_ast(content, path))
            out.extend(_django_routes(content, path))
            out.extend(_python_exports(content, path))
        elif lower.endswith((".js", ".jsx", ".ts", ".tsx")):
            out.extend(_js_routes_from_regex(content, path))
            out.extend(_nest_routes(content, path))
            out.extend(_vue_router_routes(content, path))
            out.extend(_js_exports(content, path))
        elif lower.endswith(".vue"):
            comp = _vue_component(path, content)
            if comp:
                out.append(comp)
        elif lower.endswith(".go"):
            out.extend(_go_routes(content, path))
        elif lower.endswith((".java", ".kt")):
            out.extend(_spring_routes(content, path))
        elif lower.endswith(".rb"):
            out.extend(_rails_routes(content, path))
        elif lower.endswith(".php"):
            out.extend(_laravel_routes(content, path))
        elif lower.endswith(".rs"):
            out.extend(_rust_routes(content, path))
        elif lower.endswith(".cs"):
            out.extend(_csharp_routes(content, path))
    return _dedup_signals(out)


# ─── Repo stats + format ─────────────────────────────────────────

from collections import Counter


def extract_repo_stats(tree_blobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """tree 메타에서 디렉토리·확장자 분포 결정적 추출. 추가 API 호출 0.

    top_dirs: 파일 수 내림차순 (동률은 디렉토리명 알파벳).
    language_breakdown: 확장자 → 파일 수.
    """
    total = 0
    dir_counter: Counter = Counter()
    ext_counter: Counter = Counter()
    for item in tree_blobs:
        if (item or {}).get("type") != "blob":
            continue
        path = item.get("path") or ""
        if not path:
            continue
        total += 1
        first = path.split("/", 1)[0] if "/" in path else "(root)"
        dir_counter[first] += 1
        base = _basename(path)
        if "." in base:
            ext = "." + base.rsplit(".", 1)[-1].lower()
            ext_counter[ext] += 1
    top_dirs = sorted(dir_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return {
        "total_files": total,
        "top_dirs": top_dirs,
        "language_breakdown": dict(ext_counter.most_common(10)),
    }


def format_code_evidence_block(
    manifest: Dict[str, Any],
    signals: List[Dict[str, Any]],
    stats: Dict[str, Any],
) -> str:
    """3개 추출 결과를 LLM 프롬프트용 markdown 표로 직렬화.

    빈 evidence → 빈 문자열 반환 → 프롬프트가 안전하게 무시(fallback README).
    """
    has_manifest = bool(manifest.get("languages") or manifest.get("deps"))
    has_signals = bool(signals)
    has_stats = bool(stats.get("total_files"))
    if not (has_manifest or has_signals or has_stats):
        return ""

    parts: List[str] = []

    if has_manifest:
        parts.append("### 매니페스트 사실 (결정적 추출)")
        if manifest.get("languages"):
            parts.append(f"- **언어**: {', '.join(manifest['languages'])}")
        if manifest.get("runtime"):
            parts.append(f"- **런타임**: {manifest['runtime']}")
        if manifest.get("framework_hints"):
            parts.append(f"- **프레임워크 힌트**: {', '.join(manifest['framework_hints'])}")
        if manifest.get("deps"):
            shown = manifest["deps"][:20]
            more = "" if len(manifest["deps"]) <= 20 else f" (외 {len(manifest['deps']) - 20}개)"
            parts.append(f"- **주요 의존성**: {', '.join(shown)}{more}")

    if has_signals:
        parts.append("")
        parts.append("### Entry Signals (route / component / export — 사용자 시나리오 추론의 권위)")
        routes = [s for s in signals if s["kind"] == "route"]
        comps = [s for s in signals if s["kind"] == "component"]
        exports = [s for s in signals if s["kind"] == "export"]
        if routes:
            parts.append("- **Routes (각 entry 가 곧 사용자 기능)**:")
            for r in routes[:30]:
                parts.append(f"  - `{r['method']} {r['path']}` ({r['file']})")
            if len(routes) > 30:
                parts.append(f"  - … 외 {len(routes) - 30}개")
        if comps:
            parts.append("- **Components**:")
            for c in comps[:20]:
                parts.append(f"  - `{c['name']}` ({c['file']})")
        if exports:
            parts.append("- **주요 export / 함수 (route 가 없으면 이걸로 기능 추론)**:")
            for e in exports[:20]:
                parts.append(f"  - `{e['name']}` ({e['file']})")
            if len(exports) > 20:
                parts.append(f"  - … 외 {len(exports) - 20}개")

    if has_stats:
        parts.append("")
        parts.append("### Repo 통계")
        parts.append(f"- **총 파일 수**: {stats['total_files']}")
        if stats.get("top_dirs"):
            dirs_str = ", ".join(f"{d} ({n})" for d, n in stats["top_dirs"])
            parts.append(f"- **상위 디렉토리**: {dirs_str}")
        if stats.get("language_breakdown"):
            langs_str = ", ".join(f"{ext} ({n})" for ext, n in stats["language_breakdown"].items())
            parts.append(f"- **확장자 분포**: {langs_str}")

    return "\n".join(parts)
