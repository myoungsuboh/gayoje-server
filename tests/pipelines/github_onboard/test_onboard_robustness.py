"""
GitHub Onboard 코드 단서 추출 — 강건성 / 대량 테스트.

[목적] 사용자가 임의의 public repo 를 던지므로:
  (1) 어떤 험한 입력에도 **절대 크래시하지 않는다** (silent skip).
  (2) route 가 아닌 일반 코드를 route 로 **오탐하지 않는다**.
  (3) 현실적 프레임워크 패턴은 잡는다.
단위 테스트(test_code_evidence)를 넘어 대량/엣지/퍼징 수준으로 검증.
"""
from __future__ import annotations

import pytest

from app.pipelines.github_onboard_code_evidence import (
    extract_entry_signals,
    extract_manifest_facts,
    format_code_evidence_block,
)
from app.pipelines.github_onboard_pipeline import select_onboard_files


# ════════════════ A. 크래시 절대 없음 ════════════════

# {짧은 id: 험한 content}. 거대 문자열을 parametrize '값'으로 쓰면 pytest 가 그 문자열을
# 그대로 test id 로 만들며 setup 단계에서 터진다 → 키로 parametrize 하고 값은 lookup.
_HOSTILE = {
    "empty": "", "space": " ", "newline": "\n", "tabs": "\t\t\n",
    "null": "\x00", "binary": "\x00\x01\x02binary\xff",
    "unicode": "한글 ünïcödé 🎉 emoji 中文 العربية नमस्ते",
    "huge_x": "x" * 200_000,
    "unbalanced_parens": "(" * 5_000,
    "big_signature": "def f(" + "a," * 3_000 + "): pass",
    "at_bomb": "@" * 5_000,
    "brace_bomb": "{" * 2_000 + "}" * 2_000,
    "vue_bomb": "path: '" * 5_000,
    "go_bomb": '.GET("' * 3_000,
    "rails_bomb": "Route::get(" * 3_000,
    "nest_bomb": "@Get('" * 3_000,
    "many_imports": "import os\n" * 10_000,
    "backslashes": "\\" * 1_000,
    "quote_bomb": "'" * 5_000 + '"' * 5_000,
    "big_comment": "// " + "a" * 100_000,
    "none": None,
}

_PATHS = [
    "a.py", "a.js", "a.ts", "a.tsx", "a.jsx", "a.vue", "a.go",
    "a.java", "a.kt", "a.rb", "a.php", "a.rs", "a.cs",
    "weird_no_ext", "a.PY", "deeply/nested/dir/file.py", "a.b.c.ts", "urls.py",
]


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("cname", list(_HOSTILE))
def test_entry_signals_never_crashes(path, cname):
    out = extract_entry_signals([{"path": path, "content": _HOSTILE[cname]}])
    assert isinstance(out, list)


@pytest.mark.parametrize("cname", list(_HOSTILE))
def test_manifest_never_crashes(cname):
    content = _HOSTILE[cname]
    for base in ("package.json", "pyproject.toml", "requirements.txt", "go.mod",
                 "Cargo.toml", "build.gradle", "composer.json", "Gemfile", "pom.xml"):
        facts = extract_manifest_facts([{"path": base, "content": content}])
        assert isinstance(facts["deps"], list)


def test_many_mixed_files_no_crash():
    samples = []
    for i, c in enumerate(_HOSTILE.values()):
        for ext in (".py", ".go", ".ts", ".rb", ".cs"):
            samples.append({"path": f"src/f{i}{ext}", "content": c})
    out = extract_entry_signals(samples)
    assert isinstance(out, list)


# ════════════════ B. 정탐 — 현실 프레임워크 패턴 ════════════════

def test_real_fastapi_apirouter():
    samples = [{"path": "app/routers/users.py", "content": (
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/users')\n"
        "@router.get('/')\nasync def list_users(): ...\n"
        "@router.get('/{uid}')\nasync def get_user(uid): ...\n"
        "@router.delete('/{uid}')\nasync def del_user(uid): ...\n"
    )}]
    routes = [s for s in extract_entry_signals(samples) if s["kind"] == "route"]
    paths = {r["path"] for r in routes}
    methods = {r["method"] for r in routes}
    assert "/" in paths and "/{uid}" in paths
    assert "GET" in methods and "DELETE" in methods


def test_real_express_router():
    samples = [{"path": "routes/api.js", "content": (
        "const router = require('express').Router()\n"
        "router.get('/items', list)\n"
        "router.post('/items', create)\n"
        "app.put('/items/:id', update)\n"
    )}]
    mp = {(s["method"], s["path"]) for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert ("GET", "/items") in mp and ("POST", "/items") in mp and ("PUT", "/items/:id") in mp


def test_real_spring_class_and_method():
    samples = [{"path": "src/UserController.java", "content": (
        "@RestController\n@RequestMapping(\"/api/users\")\n"
        "public class UserController {\n"
        "  @GetMapping(\"/{id}\")\n  public User one() { return null; }\n"
        "  @PostMapping(\"/\")\n  public User create() { return null; }\n"
        "}\n"
    )}]
    paths = {s["path"] for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert "/api/users" in paths   # class @RequestMapping
    assert "/{id}" in paths        # method @GetMapping


def test_real_gin_group():
    samples = [{"path": "main.go", "content": (
        'r := gin.Default()\n'
        'v1 := r.Group("/v1")\n'
        'v1.GET("/health", h)\n'
        'r.POST("/login", login)\n'
    )}]
    mp = {(s["method"], s["path"]) for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert ("GET", "/health") in mp and ("POST", "/login") in mp


# ════════════════ C. 오탐 음성 — route 아닌 코드 ════════════════

@pytest.mark.parametrize("path,content", [
    ("svc.py", "result = obj.get('key')\nval = d.post('x')"),     # dict-ish 메서드
    ("util.go", 'v := m.Get("k")\n'),                              # .Get 카멜(대문자 GET 아님)
    ("a.ts", "const x = arr.map(y => y)\nlist.filter(z)\n"),       # .map (MapGet 아님)
    ("a.rb", "config = { get: 'val' }\nx.post_process\n"),         # routes 단서 없음
    ("a.py", "import pathlib\np = pathlib.Path('/x')\n"),          # path() 아님(django 단서 없음)
    ("a.cs", 'var s = str.Replace("a","b")\n'),                    # HttpGet/MapGet 아님
    ("a.rs", "let p = config.route_table;\n"),                     # .route( 아님
    ("a.php", "$x = $arr['route'];\n"),                            # Route:: 아님
    ("router/index.ts", "const x = { filepath: '/tmp', basepath: '/v' }\n"),  # vue 단서 없음
])
def test_no_false_positive_common_code(path, content):
    routes = [s for s in extract_entry_signals([{"path": path, "content": content}]) if s["kind"] == "route"]
    assert routes == [], f"오탐 발생: {routes}"


# ════════════════ D. 엣지 — 잘림/유니코드/테스트경로 ════════════════

def test_truncated_python_silent():
    samples = [{"path": "app/api.py", "content": "@app.get('/ok')\ndef ok():\n    return foo(  # 잘림"}]
    assert isinstance(extract_entry_signals(samples), list)


def test_unicode_path_and_route():
    samples = [{"path": "src/안녕/main.py", "content": "@app.get('/한글')\ndef f(): ..."}]
    routes = [s for s in extract_entry_signals(samples) if s["kind"] == "route"]
    assert any(r["path"] == "/한글" for r in routes)


def test_test_and_doc_paths_excluded_bulk():
    """다양한 테스트/문서 경로의 가짜 route 가 전부 제외되는지."""
    fake_paths = [
        "tests/test_x.py", "spec/foo_spec.rb", "examples/demo.go",
        "docs/guide.md.py", "__tests__/a.ts", "src/__mocks__/m.ts",
        "pkg/handler_test.go", "test_main.py", "a.spec.ts", "fixtures/data.py",
    ]
    samples = [{"path": p, "content": "@app.get('/fake')\ndef f(): ...\napp.get('/fake2', h)"} for p in fake_paths]
    routes = [s for s in extract_entry_signals(samples) if s["kind"] == "route"]
    assert routes == [], f"테스트/문서 경로 오탐: {routes}"


# ════════════════ E. manifest 강건성 ════════════════

@pytest.mark.parametrize("content", [
    "fastapi", "fastapi==1.0", "fastapi>=1,<2", "fastapi[all]>=0.1",
    "# comment\n\n  \nflask  ", "-e .\ngit+https://x\nflask", "Flask==3 # inline",
])
def test_requirements_robust(content):
    facts = extract_manifest_facts([{"path": "requirements.txt", "content": content}])
    assert isinstance(facts["deps"], list)


@pytest.mark.parametrize("content", [
    '{"dependencies":{"a":"1"}}', '{"dependencies":null}', '{}', '{"x":', 'not json', '[]', 'null', '   ',
])
def test_package_json_robust(content):
    facts = extract_manifest_facts([{"path": "package.json", "content": content}])
    assert isinstance(facts["deps"], list)


@pytest.mark.parametrize("base,content", [
    ("pyproject.toml", "project = []"),                  # project 가 dict 아님
    ("pyproject.toml", "[tool.poetry]\nname = 'x'"),     # project 섹션 없음
    ("pyproject.toml", "[project]\ndependencies = 'oops'"),  # deps 가 list 아님
    ("Cargo.toml", "dependencies = 'oops'"),             # deps 가 table 아님
    ("Cargo.toml", "[package]\nname = 'x'"),             # deps 섹션 없음
])
def test_toml_manifests_robust(base, content):
    """유효하지만 비정상 구조의 TOML 매니페스트에도 크래시 없이 list 반환."""
    facts = extract_manifest_facts([{"path": base, "content": content}])
    assert isinstance(facts["deps"], list)


# ════════════════ F. select_onboard_files 강건성 ════════════════

def test_select_malformed_tree():
    blobs = [
        {"type": "blob"},                                   # path/sha 없음
        {"path": "a.py"},                                   # sha 없음
        {"path": "b.py", "sha": "x", "size": None},         # size None
        {"path": "c.py", "sha": "y", "type": "blob", "size": 100},
        {"path": "", "sha": "", "type": "blob"},            # 빈 path
    ]
    selected = select_onboard_files(blobs)
    assert isinstance(selected, list)
    assert all(s.get("path") for s in selected)


def test_select_large_tree_caps():
    blobs = [{"path": f"src/services/f{i}.py", "sha": str(i), "type": "blob", "size": i} for i in range(500)]
    assert len(select_onboard_files(blobs)) <= 40


# ════════════════ G. format / 캡 강건성 ════════════════

def test_format_caps_huge_signal_lists():
    routes = [{"kind": "route", "method": "GET", "path": f"/r{i}", "file": "a", "name": ""} for i in range(1000)]
    exports = [{"kind": "export", "name": f"fn{i}", "file": "a", "method": None, "path": None} for i in range(1000)]
    out = format_code_evidence_block(
        {"languages": ["python"], "deps": ["x"] * 100, "dev_deps": [], "framework_hints": [], "runtime": None},
        routes + exports,
        {"total_files": 1000, "top_dirs": [("a", 1000)], "language_breakdown": {".py": 1000}},
    )
    assert "외" in out          # "외 N개" 캡 표기 존재
    assert len(out) < 20_000    # 무한정 커지지 않음
