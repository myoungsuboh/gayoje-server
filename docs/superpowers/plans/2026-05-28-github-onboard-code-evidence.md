# GitHub Onboard — Code Evidence Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans 또는 subagent-driven-development. 체크박스 추적.

**Goal:** GitHub URL onboard 흐름에서 README 편향 해소 — 결정적 코드 추출(manifest deps + entry signals + repo stats)을 프롬프트에 사실 표로 주입해 LLM 이 코드 근거 기반 V1 markdown 을 생성하도록 강제.

**Architecture:** 신규 `app/pipelines/github_onboard/code_evidence.py` 가 이미 fetch 된 samples 와 tree_blobs 로부터 4개 순수함수로 사실을 추출 → `format_code_evidence_block` 으로 markdown 표 생성 → 프롬프트의 새 input section `<<code_evidence>>` 로 주입. 추가 GitHub API 호출 0, 추가 LLM 호출 0.

**Tech Stack:** Python 3.13 — `json` (package.json), `tomllib` (pyproject.toml), `ast` (Python entry parse), `re` (JS/TS 시그널). pytest-asyncio.

**Spec:** [docs/superpowers/specs/2026-05-28-github-onboard-code-evidence-design.md](../specs/2026-05-28-github-onboard-code-evidence-design.md)

---

## File Structure

- **Create** `app/pipelines/github_onboard/__init__.py` (있으면 skip — 기존 디렉토리 구조 확인 필요).
- **Create** `app/pipelines/github_onboard/code_evidence.py` — 4 순수함수 + 1 헬퍼.
- **Modify** `app/pipelines/github_onboard_pipeline.py` — `call_onboard_llm` 에 code_evidence 주입, `tree_blobs` 를 거기까지 전달.
- **Modify** `app/prompts/onboard_from_github.md` — 위계 변경 + `<<code_evidence>>` 슬롯.
- **Create** `tests/pipelines/github_onboard/test_code_evidence.py` — 단위 테스트.

> **NOTE**: `app/pipelines/github_onboard/` 디렉토리는 현재 없을 수 있음 — `app/pipelines/github_onboard_pipeline.py` 가 단일 파일. 신규 모듈은 같은 디렉토리에 `app/pipelines/github_onboard_code_evidence.py` 로 두는 게 단순. (테스트 디렉토리는 `tests/pipelines/github_onboard/` 가 이미 있음 — 거기 추가.)

---

## Task 1: `extract_manifest_facts` — 매니페스트 파싱

**Files:**
- Create: `app/pipelines/github_onboard_code_evidence.py`
- Test: `tests/pipelines/github_onboard/test_code_evidence.py`

- [ ] **Step 1: Write failing tests**

```python
"""
GitHub Onboard 의 결정적 코드 단서 추출.

[배경]
LLM 프롬프트가 README 우선 위계로 V1 markdown 을 만들어 사용자가 "README 만
읽은 느낌" 으로 체감. fetch 는 코드까지 잘 가져오지만 프롬프트 자연어 가드만으로는
LLM 행동 강제 불가(L1-2 #70 동일 패턴). 결정적 추출 → 프롬프트 사실 표 주입으로
LLM 이 무시 못 하게.

핵심 검증:
  - extract_manifest_facts: 언어/의존성/프레임워크 힌트.
  - extract_entry_signals: route 데코레이터 / export / Vue component.
  - extract_repo_stats: 디렉토리·확장자 분포.
  - format_code_evidence_block: markdown 표로 직렬화.
"""
from __future__ import annotations

import pytest

from app.pipelines.github_onboard_code_evidence import extract_manifest_facts


def test_extract_manifest_facts_parses_package_json():
    samples = [{
        "path": "package.json",
        "content": '{"dependencies":{"vue":"^3.4","pinia":"^2.0"},"devDependencies":{"vite":"^5.0"}}',
    }]
    facts = extract_manifest_facts(samples)
    assert "javascript" in facts["languages"] or "typescript" in facts["languages"]
    assert "vue" in facts["deps"]
    assert "pinia" in facts["deps"]
    assert "vite" in facts["dev_deps"]
    assert "vue" in facts["framework_hints"]


def test_extract_manifest_facts_parses_pyproject_toml():
    samples = [{
        "path": "pyproject.toml",
        "content": (
            '[project]\n'
            'name = "myapp"\n'
            'requires-python = ">=3.13"\n'
            'dependencies = ["fastapi>=0.115", "neo4j>=5.0"]\n'
        ),
    }]
    facts = extract_manifest_facts(samples)
    assert "python" in facts["languages"]
    assert "fastapi" in facts["deps"]
    assert "neo4j" in facts["deps"]
    assert "fastapi" in facts["framework_hints"]
    assert facts["runtime"] == ">=3.13"


def test_extract_manifest_facts_parses_requirements_txt():
    samples = [{
        "path": "requirements.txt",
        "content": "django==5.0\npsycopg2-binary==2.9\n# comment\n\nrequests>=2",
    }]
    facts = extract_manifest_facts(samples)
    assert "python" in facts["languages"]
    assert "django" in facts["deps"]
    assert "requests" in facts["deps"]
    assert "django" in facts["framework_hints"]


def test_extract_manifest_facts_combines_multi_language():
    """모노레포 — JS + Python 매니페스트 둘 다."""
    samples = [
        {"path": "package.json", "content": '{"dependencies":{"react":"^18.0"}}'},
        {"path": "pyproject.toml", "content": '[project]\ndependencies = ["fastapi"]\n'},
    ]
    facts = extract_manifest_facts(samples)
    assert "python" in facts["languages"]
    assert "javascript" in facts["languages"] or "typescript" in facts["languages"]
    assert "react" in facts["framework_hints"]
    assert "fastapi" in facts["framework_hints"]


def test_extract_manifest_facts_empty_input_returns_empty_struct():
    facts = extract_manifest_facts([])
    assert facts["languages"] == []
    assert facts["deps"] == []
    assert facts["dev_deps"] == []
    assert facts["framework_hints"] == []


def test_extract_manifest_facts_handles_malformed_json_silently():
    """package.json 이 깨졌어도 다른 매니페스트는 계속 파싱."""
    samples = [
        {"path": "package.json", "content": "{ broken json"},
        {"path": "requirements.txt", "content": "flask==3.0"},
    ]
    facts = extract_manifest_facts(samples)
    assert "flask" in facts["deps"]   # 다른 파일은 계속 파싱
    assert "flask" in facts["framework_hints"]
```

- [ ] **Step 2: Run — verify FAIL** — `python -m pytest tests/pipelines/github_onboard/test_code_evidence.py -v -p no:warnings`. Expected: ImportError.

- [ ] **Step 3: Create `app/pipelines/github_onboard_code_evidence.py`**

```python
"""
GitHub Onboard 결정적 코드 단서 추출 — 프롬프트에 사실 표 주입용.

[목적]
LLM 프롬프트가 README 우선 위계로 V1 을 만들어 사용자가 "README 만 읽은 느낌" 으로
체감(자연어 가드는 L1-2 #70 처럼 LLM 행동 강제 못함). 결정적 코드 추출 → markdown 표
주입으로 LLM 이 무시 못 하게. L1-1 backstop 디자인과 동일 철학 (is_meaningful_spec_node).

[추출 범위]
- manifest facts: package.json / pyproject.toml / requirements.txt / go.mod / Cargo.toml
  의 의존성 + 프레임워크 힌트.
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
    "express", "fastify", "hono", "nestjs", "@nestjs/core",
    "vite", "webpack", "rollup",
    "pinia", "vuex", "redux", "zustand", "jotai",
    # Python
    "fastapi", "flask", "django", "starlette", "sanic", "tornado",
    "sqlalchemy", "neo4j", "psycopg2", "psycopg2-binary",
    "pydantic", "celery", "arq",
    # Other (manifest 만)
    "spring-boot", "gin", "actix-web", "rocket",
}

# 매니페스트 파일 → 언어 매핑.
_MANIFEST_TO_LANGS = {
    "package.json": ["javascript", "typescript"],   # tsconfig.json 있으면 ts 도
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
    return n in _FRAMEWORK_HINTS or any(n.startswith(f + "-") or n.startswith("@" + f) for f in _FRAMEWORK_HINTS)


def _parse_package_json(content: str) -> Dict[str, List[str]]:
    """package.json → {deps, dev_deps}. 파싱 실패 시 빈."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return {"deps": [], "dev_deps": []}
    deps = list((data.get("dependencies") or {}).keys())
    dev_deps = list((data.get("devDependencies") or {}).keys())
    return {"deps": deps, "dev_deps": dev_deps}


def _parse_pyproject(content: str) -> Dict[str, Any]:
    """pyproject.toml → {deps, runtime}. 파싱 실패 시 빈."""
    if tomllib is None:
        return {"deps": [], "runtime": None}
    try:
        data = tomllib.loads(content)
    except Exception:  # noqa: BLE001 — tomllib raises various errors
        return {"deps": [], "runtime": None}
    project = data.get("project") or {}
    deps_raw = project.get("dependencies") or []
    # "fastapi>=0.115" → "fastapi"
    deps = [_strip_version(d) for d in deps_raw if isinstance(d, str)]
    runtime = project.get("requires-python")
    return {"deps": deps, "runtime": runtime}


def _strip_version(spec: str) -> str:
    """ "fastapi>=0.115" → "fastapi" """
    return re.split(r"[<>=!~\s\[]", spec, maxsplit=1)[0].strip().lower()


def _parse_requirements_txt(content: str) -> List[str]:
    """requirements.txt → [pkg names]. 주석/빈줄 무시."""
    out: List[str] = []
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-"):
            continue
        out.append(_strip_version(s))
    return out


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
        # 기타 매니페스트(go.mod, Cargo.toml, pom.xml) 는 1차 컷에서 언어만 표시.

    framework_hints = sorted({d for d in (deps + dev_deps) if _is_framework_hint(d)})

    return {
        "languages": languages,
        "runtime": runtime,
        "deps": sorted(set(deps)),
        "dev_deps": sorted(set(dev_deps)),
        "framework_hints": framework_hints,
    }
```

- [ ] **Step 4: Run — verify PASS**

Run: `python -m pytest tests/pipelines/github_onboard/test_code_evidence.py -v -p no:warnings`. Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/pipelines/github_onboard_code_evidence.py tests/pipelines/github_onboard/test_code_evidence.py
git commit -m "feat(onboard): extract_manifest_facts — 결정적 의존성·프레임워크 추출"
```

---

## Task 2: `extract_entry_signals` — Python AST + JS/TS regex

**Files:**
- Modify: `app/pipelines/github_onboard_code_evidence.py`
- Test: `tests/pipelines/github_onboard/test_code_evidence.py`

- [ ] **Step 1: Write failing tests** — append:

```python
from app.pipelines.github_onboard_code_evidence import extract_entry_signals


def test_entry_signals_python_fastapi_routes():
    samples = [{
        "path": "main.py",
        "content": (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "\n"
            "@app.get('/api/users')\n"
            "def list_users(): ...\n"
            "\n"
            "@app.post('/api/users')\n"
            "def create_user(): ...\n"
        ),
    }]
    sigs = extract_entry_signals(samples)
    routes = [s for s in sigs if s["kind"] == "route"]
    assert len(routes) == 2
    methods_paths = {(r["method"], r["path"]) for r in routes}
    assert ("GET", "/api/users") in methods_paths
    assert ("POST", "/api/users") in methods_paths


def test_entry_signals_python_router_decorator():
    samples = [{
        "path": "app/api/auth.py",
        "content": (
            "router = APIRouter()\n"
            "@router.post('/login')\n"
            "async def login(): ...\n"
        ),
    }]
    sigs = extract_entry_signals(samples)
    assert any(s["kind"] == "route" and s["path"] == "/login" for s in sigs)


def test_entry_signals_python_flask():
    samples = [{
        "path": "app.py",
        "content": (
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            "\n"
            "@app.route('/hello', methods=['GET'])\n"
            "def hello(): ...\n"
        ),
    }]
    sigs = extract_entry_signals(samples)
    routes = [s for s in sigs if s["kind"] == "route"]
    assert any(r["path"] == "/hello" for r in routes)


def test_entry_signals_js_express_routes():
    samples = [{
        "path": "server.js",
        "content": (
            "const app = express();\n"
            "app.get('/api/health', (req, res) => res.send('ok'));\n"
            "app.post('/api/items', handler);\n"
            "router.delete('/api/items/:id', remove);\n"
        ),
    }]
    sigs = extract_entry_signals(samples)
    routes = [s for s in sigs if s["kind"] == "route"]
    methods_paths = {(r["method"], r["path"]) for r in routes}
    assert ("GET", "/api/health") in methods_paths
    assert ("POST", "/api/items") in methods_paths
    assert ("DELETE", "/api/items/:id") in methods_paths


def test_entry_signals_vue_sfc_component():
    samples = [{
        "path": "src/App.vue",
        "content": "<script setup>\nconst x = 1\n</script>\n<template>\n<div />\n</template>",
    }]
    sigs = extract_entry_signals(samples)
    comps = [s for s in sigs if s["kind"] == "component"]
    assert any(c["name"] == "App" and c["file"] == "src/App.vue" for c in comps)


def test_entry_signals_python_syntax_error_silently_skipped():
    """Python 파싱 실패해도 다른 파일은 계속."""
    samples = [
        {"path": "broken.py", "content": "def broken("},
        {"path": "main.py", "content": "@app.get('/ok')\ndef ok(): ..."},
    ]
    sigs = extract_entry_signals(samples)
    assert any(s.get("path") == "/ok" for s in sigs)


def test_entry_signals_empty_input():
    assert extract_entry_signals([]) == []
```

- [ ] **Step 2: Run — verify FAIL** (import error).

- [ ] **Step 3: Append to `app/pipelines/github_onboard_code_evidence.py`**

```python
import ast


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
# JS/TS: `app.get('/path', ...)` 또는 `router.post("/path", ...)` 매칭.
_JS_ROUTE_RE = re.compile(
    r"\b(?:app|router|bp|api)\.(get|post|put|patch|delete|head|options)\s*\(\s*['\"]([^'\"]+)['\"]"
)
# Vue SFC: 파일명 stem 이 component 명. <script setup> 만 있으면 SFC 로 간주.
_VUE_SCRIPT_SETUP_RE = re.compile(r"<script\s+[^>]*setup", re.IGNORECASE)


def _python_routes_from_ast(content: str, path: str) -> List[Dict[str, Any]]:
    """Python AST 로 @app.get / @router.post / @bp.route 데코레이터 추출."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
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


def _extract_python_decorator_route(dec: ast.expr) -> Dict[str, Any] | None:
    """단일 데코레이터에서 (method, path) 추출. 매칭 안 되면 None."""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    # @app.get('/path') / @router.post('/path') 형태
    if isinstance(func, ast.Attribute):
        attr = func.attr.lower()
        # FastAPI/Flask: get/post/...
        if attr in _HTTP_METHODS:
            path = _first_str_arg(dec)
            if path:
                return {"kind": "route", "method": attr.upper(), "path": path}
        # Flask: @app.route('/path', methods=['GET'])
        if attr == "route":
            path = _first_str_arg(dec)
            methods = _methods_kwarg(dec) or ["GET"]
            if path and methods:
                # 첫 method 만 representative — 시그널은 시나리오 추론용이라 충분.
                return {"kind": "route", "method": methods[0].upper(), "path": path}
    return None


def _first_str_arg(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def _methods_kwarg(call: ast.Call) -> List[str] | None:
    for kw in call.keywords:
        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
            out = []
            for el in kw.value.elts:
                if isinstance(el, ast.Constant) and isinstance(el.value, str):
                    out.append(el.value)
            return out or None
    return None


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


def _vue_component(path: str, content: str) -> Dict[str, Any] | None:
    if not path.endswith(".vue"):
        return None
    if not _VUE_SCRIPT_SETUP_RE.search(content):
        # script setup 없어도 SFC 일 수 있지만 1차 컷은 가장 흔한 케이스만.
        # 그래도 파일명만 표시 — 사용자가 component 위치 알기에 충분.
        pass
    base = _basename(path)
    if "." in base:
        name = base.rsplit(".", 1)[0]
    else:
        name = base
    return {"kind": "component", "name": name, "file": path, "method": None, "path": None}


def extract_entry_signals(samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """entry/code 파일들에서 route 데코레이터 + Vue component 결정적 추출.

    parse 실패는 silent. 매칭된 시그널이 LLM 의 권위적 입력이 된다(README 회귀 차단).
    """
    out: List[Dict[str, Any]] = []
    for s in samples:
        path = s.get("path") or ""
        content = s.get("content") or ""
        if not path or not content:
            continue
        lower = path.lower()
        if lower.endswith(".py"):
            out.extend(_python_routes_from_ast(content, path))
        elif lower.endswith((".js", ".jsx", ".ts", ".tsx")):
            out.extend(_js_routes_from_regex(content, path))
        elif lower.endswith(".vue"):
            comp = _vue_component(path, content)
            if comp:
                out.append(comp)
    return out
```

- [ ] **Step 4: Run — verify PASS**.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(onboard): extract_entry_signals — Python AST + JS/TS regex 라우트 추출"
```

---

## Task 3: `extract_repo_stats` + `format_code_evidence_block`

**Files:**
- Modify: `app/pipelines/github_onboard_code_evidence.py`
- Test: `tests/pipelines/github_onboard/test_code_evidence.py`

- [ ] **Step 1: Write failing tests** — append:

```python
from app.pipelines.github_onboard_code_evidence import (
    extract_repo_stats,
    format_code_evidence_block,
)


def test_repo_stats_counts_files_and_top_dirs():
    tree_blobs = [
        {"path": "src/api/users.py", "type": "blob", "size": 100},
        {"path": "src/api/items.py", "type": "blob", "size": 100},
        {"path": "src/web/App.vue", "type": "blob", "size": 100},
        {"path": "README.md", "type": "blob", "size": 100},
        {"path": "tests/test_x.py", "type": "blob", "size": 100},
    ]
    stats = extract_repo_stats(tree_blobs)
    assert stats["total_files"] == 5
    # src 가 top-1 (3 files)
    assert stats["top_dirs"][0][0] == "src"
    assert stats["top_dirs"][0][1] == 3
    # 확장자 분포
    assert stats["language_breakdown"].get(".py") == 3


def test_repo_stats_skips_trees_and_empty():
    tree_blobs = [
        {"path": "src", "type": "tree", "size": 0},
        {"path": "", "type": "blob", "size": 0},
    ]
    stats = extract_repo_stats(tree_blobs)
    assert stats["total_files"] == 0


def test_format_block_empty_for_no_evidence():
    """모든 입력이 비어 있으면 빈 문자열 — 프롬프트가 안전하게 무시."""
    out = format_code_evidence_block(
        {"languages": [], "runtime": None, "deps": [], "dev_deps": [], "framework_hints": []},
        [],
        {"total_files": 0, "top_dirs": [], "language_breakdown": {}},
    )
    assert out.strip() == ""


def test_format_block_renders_manifest_signals_stats():
    out = format_code_evidence_block(
        {
            "languages": ["python"],
            "runtime": ">=3.13",
            "deps": ["fastapi", "neo4j"],
            "dev_deps": ["pytest"],
            "framework_hints": ["fastapi"],
        },
        [
            {"kind": "route", "method": "POST", "path": "/login", "file": "app/api/auth.py", "name": "login"},
            {"kind": "route", "method": "GET", "path": "/me", "file": "app/api/auth.py", "name": "me"},
        ],
        {
            "total_files": 42,
            "top_dirs": [("app", 30), ("tests", 12)],
            "language_breakdown": {".py": 35, ".md": 5},
        },
    )
    # 프롬프트 LLM 이 무시 못 하도록 명시 헤더 + 표.
    assert "fastapi" in out
    assert "POST" in out and "/login" in out
    assert "app" in out and "30" in out
    assert "42" in out   # total_files
```

- [ ] **Step 2: Run — verify FAIL** (import error).

- [ ] **Step 3: Append to `app/pipelines/github_onboard_code_evidence.py`**

```python
from collections import Counter


def extract_repo_stats(tree_blobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """tree 메타에서 디렉토리·확장자 분포 결정적 추출. 추가 API 호출 0.

    top_dirs: 파일 수 내림차순 (동률은 디렉토리명 알파벳).
    language_breakdown: 확장자 → 파일 수.
    """
    total = 0
    dir_counter: Counter[str] = Counter()
    ext_counter: Counter[str] = Counter()
    for item in tree_blobs:
        if (item or {}).get("type") != "blob":
            continue
        path = item.get("path") or ""
        if not path:
            continue
        total += 1
        # top-level dir
        first = path.split("/", 1)[0] if "/" in path else "(root)"
        dir_counter[first] += 1
        # 확장자
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

    빈 evidence (모두 비었으면) → 빈 문자열 반환 → 프롬프트가 안전하게 무시.
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
        parts.append("### Entry Signals (route / component — 사용자 시나리오 추론의 권위)")
        routes = [s for s in signals if s["kind"] == "route"]
        comps = [s for s in signals if s["kind"] == "component"]
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
```

- [ ] **Step 4: Run — verify PASS**.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(onboard): extract_repo_stats + format_code_evidence_block"
```

---

## Task 4: Wire-in + 프롬프트 재작성

**Files:**
- Modify: `app/pipelines/github_onboard_pipeline.py` (call_onboard_llm)
- Modify: `app/prompts/onboard_from_github.md`
- Test: `tests/pipelines/github_onboard/test_code_evidence.py` (wire test)

- [ ] **Step 1: Write failing wire-in test** — append:

```python
import pytest
from unittest.mock import patch


@pytest.mark.asyncio
async def test_call_onboard_llm_injects_code_evidence(tmp_path):
    """call_onboard_llm 이 code_evidence 를 프롬프트에 주입한다."""
    from app.pipelines import github_onboard_pipeline as pl
    from tests.conftest import FakeGemini
    from app.pipelines.base import PipelineContext

    gemini = FakeGemini(responses=["## 1. 프로젝트 개요\n내용\n" * 60])   # 300자 이상
    ctx = PipelineContext(gemini=gemini, neo4j=None, idempotency_key="t")
    samples = [
        {"path": "pyproject.toml", "content": '[project]\ndependencies=["fastapi"]\n'},
        {"path": "main.py", "content": "@app.get('/api/health')\ndef h(): ..."},
    ]
    tree_blobs = [
        {"path": "main.py", "type": "blob", "size": 50},
        {"path": "README.md", "type": "blob", "size": 100},
    ]

    await pl.call_onboard_llm(
        ctx, repo_full_name="r/x", project_name="x", samples=samples, tree_blobs=tree_blobs,
    )
    prompt = gemini.calls[0]["prompt"]
    # 결정적 추출이 프롬프트에 박혔는가
    assert "fastapi" in prompt
    assert "/api/health" in prompt
    assert "GET" in prompt
```

- [ ] **Step 2: Run — verify FAIL** (call_onboard_llm 시그니처가 tree_blobs 인자 안 받음).

- [ ] **Step 3: Modify `call_onboard_llm` 시그니처 + 호출처**

`app/pipelines/github_onboard_pipeline.py`:

```python
# Import 추가 (파일 상단)
from app.pipelines.github_onboard_code_evidence import (
    extract_entry_signals,
    extract_manifest_facts,
    extract_repo_stats,
    format_code_evidence_block,
)
```

`call_onboard_llm` 함수 수정 (현재 약 line 332):

```python
async def call_onboard_llm(
    ctx: PipelineContext,
    repo_full_name: str,
    project_name: str,
    samples: List[Dict[str, Any]],
    tree_blobs: List[Dict[str, Any]],   # ← 추가
) -> str:
    """Stage: LLM V1 markdown 생성.

    [2026-05-28 코드 단서 주입] manifest deps + entry signals + repo stats 를 결정적
    추출해 프롬프트의 새 input section 으로 주입 → LLM 이 README 회귀 못 함.
    """
    code_evidence = format_code_evidence_block(
        extract_manifest_facts(samples),
        extract_entry_signals(samples),
        extract_repo_stats(tree_blobs),
    )
    prompt = _render(
        _load_prompt("onboard_from_github.md"),
        repo_full_name=repo_full_name,
        project_name=project_name,
        files_block=_format_files_block(samples),
        file_count=str(len(samples)),
        code_evidence=code_evidence,   # ← 추가
    )
    result = await ctx.gemini.generate(prompt, temperature=0.1)
    return strip_template_placeholders(strip_code_blocks(result.text)).strip()
```

호출처도 업데이트 — `call_onboard_llm` 을 부르는 곳에서 `tree_blobs` 전달:

```python
# 원본:
# v1_markdown = await call_onboard_llm(ctx, repo_full_name, project_name, samples)
v1_markdown = await call_onboard_llm(
    ctx, repo_full_name, project_name, samples, tree_blobs
)
```

찾기: `grep -n "call_onboard_llm" app/pipelines/github_onboard_pipeline.py`

- [ ] **Step 4: Rewrite `app/prompts/onboard_from_github.md`**

기존 input section 다음에 새 section 추가 + task 지시문 위계 변경. 전체 새 내용:

````markdown
# ROLE
당신은 GitHub repository 분석 전문가입니다. 제공된 결정적 코드 단서(매니페스트·entry signals·repo 통계) 와 샘플링 파일·README 를 종합해 그 프로젝트가 "무엇을 하는지" + "어떤 사용자를 위한 것인지" + "기술 구성은 어떤지" 를 한국어 마크다운으로 정리합니다.

이 출력은 시스템의 V1 회의록을 대체하는 "프로젝트 첫 설명서" 입니다. 다음 단계 (CPS / PRD 추출 AI) 가 이 문서를 입력으로 받아 명세를 추출합니다.

# 위계 원칙 (★ 핵심)
**코드 단서(아래 ## 3) = 권위. README/문서 = 보조 cross-check.**
README 가 marketing 카피를 적었더라도, 실제 코드(routes, exports, dependencies)가 가리키는 사실이 우선입니다. README 와 코드 단서가 충돌하면 코드 단서를 따릅니다.

# INPUT DATA

## 1. Repository
- **Full name**: <<repo_full_name>>
- **사용자가 지정한 프로젝트 이름**: <<project_name>>
- **샘플링된 파일 수**: <<file_count>>

## 2. 샘플링된 파일 (README + 매니페스트 + entry + 코드)
<<files_block>>

## 3. 코드 단서 (결정적 추출 — 이 표의 사실은 우선 반영)
<<code_evidence>>

# CORE TASK
위 입력을 분석해 다음 5 sections 의 markdown 을 작성하세요. 각 section 은 명시된 헤더로 시작해야 합니다.

## 1. 프로젝트 개요
- 한 줄 요약: "이 프로젝트는 [무엇] 을 [누구를 위해] [어떻게] 제공한다" 형태.
- **의존성·entry signals 가 가리키는 도메인**을 1-2 문장으로 요약. README 의 tagline 은 cross-check 으로만 사용.
- 영문 README 인 경우 한국어로 의역 (직역 X).

## 2. 주요 기능
사용자가 이 시스템에서 할 수 있는 일을 bullet 5-10 개로 정리.
- **각 bullet 은 ## 3 의 entry signal (route / component / export) 에 매핑돼야 한다.** signal 에 없는 기능은 (추정) 명시 강제.
- 예: ` POST /api/login` route → "로그인 — JWT 발급". ` GET /api/me` → "내 정보 조회 — 토큰 보유 사용자".
- README 의 Features section 은 보충용 — signals 가 비어 있을 때만 단독 인용 허용 (이 경우 (추정) 표기).

## 3. 사용자 시나리오
구체적 use case 3-5 개를 짧은 시나리오 형태로:
- "사용자는 ... 한다. 그 후 ... 한다." 형식.
- **route signals 의 method+path 조합을 시나리오로 재구성**. 예: `POST /api/login → GET /api/me` = "로그인하고 자기 프로필을 확인한다".
- README example 은 corroboration. README 에만 있고 signals 에 없는 시나리오는 (추정).

## 4. 기술 스택
**## 3 의 manifest facts 를 그대로 인용.** 추측 X.
- **언어**: ## 3 의 "언어" 표 값.
- **런타임**: ## 3 의 "런타임" (있으면).
- **프레임워크**: ## 3 의 "프레임워크 힌트" 표.
- **주요 의존성**: ## 3 의 "주요 의존성" 표 중 framework_hints 외 핵심 5-10 개.
- **배포 / 인프라**: files_block 의 Dockerfile / docker-compose 등에서 발견된 것만.

## 5. NFR 추정 (비기능 요구사항)
- **성능**: 코드에 명시된 timeout / concurrency / cache 가드. 없으면 "프로젝트 규모상 [범위] 가정" 1 문장.
- **보안**: signals 안의 auth route / dependencies 안의 JWT 라이브러리 등에서 발견된 것만.
- **접근성 / 호환성**: web app 인 경우 vite.config / browserslist 등.
- **운영**: 로깅 / 에러 처리 / 헬스체크 / Docker healthcheck 등.
- 각 항목 근거 없으면 "(추정)" 1 문장.

# ABSOLUTE CONSTRAINTS (위반 시 실패)

1. **LANGUAGE — 한국어 (CRITICAL)**: 모든 자유 텍스트 한국어. 단 라이브러리 이름 / 코드 식별자 / 파일명 / 경로는 영문 유지.

2. **CODE EVIDENCE OVER README (★)**: ## 3 코드 단서에 있는 사실(routes, dependencies)을 Section 2/3/4 에 반드시 인용. README 만 단독 인용한 bullet 은 (추정) 표기 강제.

3. **NO HALLUCINATION**: 입력 파일·코드 단서에 없는 정보 추가 금지. 추측 필요 시 "(추정)" 또는 "(추측)" 명시.

4. **HEADER FORMAT**: 각 section `## N. <섹션명>` 형식.

5. **NO CODE BLOCKS IN OUTPUT**: ` ```language ... ``` ` 금지. 코드 인용 필요 시 `inline code`.

6. **NO META COMMENTARY**: "이 프로젝트에 대해:" 같은 메타 코멘트 X.

7. **MINIMUM LENGTH**: 출력 최소 300자.

8. **MAXIMUM LENGTH**: 출력 50,000자 이하 (보통 1,000-5,000자).

# OUTPUT TEMPLATE (구조 유지)

## 1. 프로젝트 개요
...

## 2. 주요 기능
- ...

## 3. 사용자 시나리오
...

## 4. 기술 스택
- **언어**: ...
- **프레임워크**: ...
- **데이터베이스**: ...
- **배포 / 인프라**: ...

## 5. NFR 추정
- **성능**: ...
- **보안**: ...
- **접근성 / 호환성**: ...
- **운영**: ...

# 출력 시작 (markdown 만):
````

- [ ] **Step 5: Run wire-in test — verify PASS**.

- [ ] **Step 6: Run full onboard suite + full BE — verify no regression**

```bash
python -m pytest tests/pipelines/github_onboard/ -v -p no:warnings
python -m pytest -q -p no:warnings --ignore=tests/integration/test_neo4j_backup_restore_drill.py
```

기존 e2e 테스트(`test_e2e_onboard_returns_v1_and_cps`)가 새 시그니처 누락으로 깨질 수 있음 → `tree_blobs` 인자 추가하여 수정.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(onboard): code evidence 프롬프트 주입 + 위계 변경 (README → cross-check, 코드 → 권위)"
```

---

## Task 5: PR + 머지

- [ ] **Step 1**: `git push -u origin feat/github-onboard-code-evidence`
- [ ] **Step 2**: `gh pr create --base master` with full PR body referencing spec + 4-layer diagnosis.
- [ ] **Step 3**: Wait for CI → squash merge → sync master.

---

## Self-Review

- ✅ 4 추출 함수 + 1 헬퍼 모두 task 로 매핑됨.
- ✅ Wire-in 에서 기존 e2e 테스트 회귀 가능성 명시.
- ✅ 프롬프트 위계 변경이 spec 의 "코드 = 권위, README = cross-check" 와 일치.
- ✅ 데이터 안전 — 빈 evidence 시 빈 문자열, 추가 GitHub/LLM 호출 0.
- ⚠️ JS/TS regex 는 1차 컷 — `// app.get(...)` 같은 주석도 매칭됨. 큰 false positive 위험은 낮지만 fixture 검증 후 보정 가능.
