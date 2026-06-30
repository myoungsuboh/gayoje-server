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

from app.pipelines.github_onboard_code_evidence import (
    extract_entry_signals,
    extract_manifest_facts,
    extract_repo_stats,
    format_code_evidence_block,
)


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
    assert "flask" in facts["deps"]
    assert "flask" in facts["framework_hints"]


# ─── Phase B: 다언어 의존성 파싱 (go/rust/gradle/composer/gemfile/pom) ──


def test_manifest_go_mod():
    samples = [{
        "path": "go.mod",
        "content": (
            "module github.com/me/app\n"
            "go 1.21\n"
            "require (\n"
            "    github.com/gin-gonic/gin v1.9.1\n"
            "    github.com/lib/pq v1.10.9\n"
            ")\n"
            "require github.com/foo/bar v1.0.0\n"
        ),
    }]
    facts = extract_manifest_facts(samples)
    assert "go" in facts["languages"]
    assert "gin" in facts["deps"]
    assert "pq" in facts["deps"]
    assert "bar" in facts["deps"]
    assert "gin" in facts["framework_hints"]
    # module/go 지시문은 deps 아님
    assert "app" not in facts["deps"]


def test_manifest_cargo_toml():
    samples = [{
        "path": "Cargo.toml",
        "content": (
            '[package]\nname = "app"\n'
            "[dependencies]\n"
            'axum = "0.7"\n'
            'tokio = { version = "1", features = ["full"] }\n'
        ),
    }]
    facts = extract_manifest_facts(samples)
    assert "rust" in facts["languages"]
    assert "axum" in facts["deps"]
    assert "tokio" in facts["deps"]
    assert "axum" in facts["framework_hints"]


def test_manifest_gradle():
    samples = [{
        "path": "build.gradle",
        "content": (
            "dependencies {\n"
            "    implementation 'org.springframework.boot:spring-boot-starter-web:3.2.0'\n"
            "    implementation 'com.h2database:h2'\n"
            "}\n"
        ),
    }]
    facts = extract_manifest_facts(samples)
    assert "java" in facts["languages"]
    assert "spring-boot-starter-web" in facts["deps"]
    assert "h2" in facts["deps"]


def test_manifest_composer_gemfile_pom():
    samples = [
        {"path": "composer.json", "content": '{"require":{"php":">=8.1","laravel/framework":"^10.0"}}'},
        {"path": "Gemfile", "content": "source 'https://rubygems.org'\ngem 'rails', '~> 7.0'\ngem 'pg'\n"},
        {"path": "pom.xml", "content": "<project><dependencies><dependency><artifactId>spring-boot-starter</artifactId></dependency></dependencies></project>"},
    ]
    facts = extract_manifest_facts(samples)
    assert "laravel/framework" in facts["deps"]   # composer (vendor/package)
    assert "php" not in facts["deps"]             # 버전 키 제외("/" 없음)
    assert "rails" in facts["deps"]               # gemfile
    assert "pg" in facts["deps"]
    assert "spring-boot-starter" in facts["deps"]  # pom


# ─── extract_entry_signals ────────────────────────────────────────


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


def test_entry_signals_python_flask_route():
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


# ─── Phase A: 다언어 route 추출 (Django / NestJS / Go / Spring) ──────


def test_entry_signals_django_urls():
    samples = [{
        "path": "myapp/urls.py",
        "content": (
            "from django.urls import path, re_path\n"
            "urlpatterns = [\n"
            "    path('users/', views.user_list),\n"
            "    path('users/<int:pk>/', views.user_detail),\n"
            "    re_path(r'^legacy/$', views.legacy),\n"
            "]\n"
        ),
    }]
    paths = {s["path"] for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert "users/" in paths
    assert "users/<int:pk>/" in paths
    assert "^legacy/$" in paths


def test_entry_signals_django_path_ignored_without_marker():
    """오탐 방지: urlpatterns/django.urls 단서 없는 파일의 path()는 route 아님."""
    samples = [{
        "path": "utils/helpers.py",
        "content": "import os\np = os.path.join('a', 'b')\n",
    }]
    assert [s for s in extract_entry_signals(samples) if s["kind"] == "route"] == []


def test_entry_signals_nestjs_controller_prefix():
    samples = [{
        "path": "src/users/users.controller.ts",
        "content": (
            "@Controller('users')\n"
            "export class UsersController {\n"
            "  @Get(':id')\n"
            "  findOne() {}\n"
            "  @Post()\n"
            "  create() {}\n"
            "}\n"
        ),
    }]
    mp = {(s["method"], s["path"]) for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert ("GET", "/users/:id") in mp
    assert ("POST", "/users") in mp  # @Post() 빈 인자 → controller prefix


def test_entry_signals_go_gin_and_handlefunc():
    samples = [{
        "path": "main.go",
        "content": (
            'r := gin.Default()\n'
            'r.GET("/ping", handler)\n'
            'r.POST("/users", create)\n'
            'mux.HandleFunc("/legacy", legacyHandler)\n'
        ),
    }]
    mp = {(s["method"], s["path"]) for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert ("GET", "/ping") in mp
    assert ("POST", "/users") in mp
    assert ("", "/legacy") in mp


def test_entry_signals_spring_mappings():
    samples = [{
        "path": "src/UserController.java",
        "content": (
            "@RestController\n"
            "public class UserController {\n"
            '  @GetMapping("/users")\n'
            "  public List<User> list() { return null; }\n"
            '  @PostMapping(value = "/users")\n'
            "  public User create() { return null; }\n"
            "}\n"
        ),
    }]
    mp = {(s["method"], s["path"]) for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert ("GET", "/users") in mp
    assert ("POST", "/users") in mp


def test_entry_signals_dedup_multi_extractor():
    """같은 .ts 에 Express + NestJS 패턴이 같은 route 를 만들어도 중복 제거."""
    samples = [{
        "path": "app.ts",
        "content": "app.get('/health', h)\n@Get('/health')\nfoo() {}\n",
    }]
    health = [
        s for s in extract_entry_signals(samples)
        if s["kind"] == "route" and s["path"] == "/health" and s["method"] == "GET"
    ]
    assert len(health) == 1


def test_entry_signals_go_no_false_positive():
    """오탐 방지: HTTP 메서드 호출이 아닌 일반 .go 코드는 route 0."""
    samples = [{
        "path": "util.go",
        "content": 'package main\nfunc Sum(a, b int) int { return a + b }\n',
    }]
    assert [s for s in extract_entry_signals(samples) if s["kind"] == "route"] == []


# ─── Phase C: Vue Router / Rails / Laravel / Rust / C# ──────


def test_entry_signals_vue_router():
    samples = [{
        "path": "src/router/index.ts",
        "content": (
            "import { createRouter } from 'vue-router'\n"
            "const routes = [\n"
            "  { path: '/home', component: Home },\n"
            "  { path: '/about', component: About },\n"
            "]\n"
            "createRouter({ routes })\n"
        ),
    }]
    paths = {s["path"] for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert "/home" in paths and "/about" in paths


def test_entry_signals_rails_routes():
    samples = [{
        "path": "config/routes.rb",
        "content": (
            "Rails.application.routes.draw do\n"
            "  get '/health', to: 'health#show'\n"
            "  resources :users\n"
            "end\n"
        ),
    }]
    paths = {s["path"] for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert "/health" in paths
    assert "/users" in paths


def test_entry_signals_rails_ignored_without_marker():
    """오탐 방지: routes 단서 없는 .rb 의 obj.get '...' 는 route 아님."""
    samples = [{"path": "lib/foo.rb", "content": "x = obj.get 'key'\n"}]
    assert [s for s in extract_entry_signals(samples) if s["kind"] == "route"] == []


def test_entry_signals_laravel_routes():
    samples = [{
        "path": "routes/web.php",
        "content": (
            "<?php\n"
            "Route::get('/dashboard', [DashboardController::class, 'index']);\n"
            "Route::post('/login', 'Auth@login');\n"
        ),
    }]
    mp = {(s["method"], s["path"]) for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert ("GET", "/dashboard") in mp
    assert ("POST", "/login") in mp


def test_entry_signals_rust_actix():
    samples = [{
        "path": "src/main.rs",
        "content": '#[get("/ping")]\nasync fn ping() -> impl Responder { "ok" }\n',
    }]
    paths = {s["path"] for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert "/ping" in paths


def test_entry_signals_csharp():
    samples = [{
        "path": "Controllers/UsersController.cs",
        "content": (
            '[HttpGet("api/users")]\n'
            "public IActionResult Get() { }\n"
            'app.MapGet("/health", () => "ok");\n'
        ),
    }]
    mp = {(s["method"], s["path"]) for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert ("GET", "api/users") in mp
    assert ("GET", "/health") in mp


# ─── Phase E: export / 함수 시그니처 ──────


def test_entry_signals_python_exports():
    samples = [{
        "path": "lib/core.py",
        "content": "def public_fn():\n    pass\nclass PublicClass:\n    pass\ndef _private():\n    pass\n",
    }]
    names = {s["name"] for s in extract_entry_signals(samples) if s["kind"] == "export"}
    assert "public_fn" in names
    assert "PublicClass" in names
    assert "_private" not in names  # private(_ 시작) 제외


def test_entry_signals_js_exports():
    samples = [{
        "path": "src/api.ts",
        "content": "export function fetchUser() {}\nexport const API_URL = '/x'\nexport class Client {}\n",
    }]
    names = {s["name"] for s in extract_entry_signals(samples) if s["kind"] == "export"}
    assert {"fetchUser", "API_URL", "Client"} <= names


def test_norm_join_root_controller_no_double_slash():
    """[버그수정] @Controller('/') 같은 루트 prefix 가 '//x' 되지 않아야 함."""
    from app.pipelines.github_onboard_code_evidence import _norm_join
    assert _norm_join("/", "users") == "/users"
    assert _norm_join("", "/login") == "/login"
    assert _norm_join("api", "v1") == "/api/v1"
    assert _norm_join("/users/", "/:id") == "/users/:id"


def test_entry_signals_vue_no_filepath_false_positive():
    """[오탐수정] filepath/basepath 의 'path' 부분은 route 로 안 잡음(\\b 경계)."""
    samples = [{
        "path": "src/router/index.ts",
        "content": (
            "import { createRouter } from 'vue-router'\n"
            "const config = { filepath: '/tmp/cache', basepath: '/var' }\n"
            "const routes = [{ path: '/home' }]\n"
            "createRouter({ routes })\n"
        ),
    }]
    paths = {s["path"] for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert "/home" in paths
    assert "/tmp/cache" not in paths
    assert "/var" not in paths


def test_entry_signals_skips_test_and_doc_paths():
    """[오탐수정] 테스트/예제 코드의 가짜 route 는 단서에서 제외, 실제 코드만 남김."""
    samples = [
        {"path": "tests/test_api.py", "content": "@app.get('/fake')\ndef fake(): ..."},
        {"path": "examples/demo.ts", "content": "app.get('/demo', h)"},
        {"path": "src/api/real.py", "content": "@app.get('/real')\ndef real(): ..."},
    ]
    paths = {s["path"] for s in extract_entry_signals(samples) if s["kind"] == "route"}
    assert "/real" in paths
    assert "/fake" not in paths
    assert "/demo" not in paths


def test_format_block_renders_exports():
    """route/manifest 없이 export 만 있어도 단서 표로 렌더."""
    out = format_code_evidence_block(
        {"languages": [], "runtime": None, "deps": [], "dev_deps": [], "framework_hints": []},
        [{"kind": "export", "name": "createApp", "file": "lib/x.py", "method": None, "path": None}],
        {"total_files": 0, "top_dirs": [], "language_breakdown": {}},
    )
    assert "createApp" in out


# ─── extract_repo_stats + format_code_evidence_block ────────────────


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
    assert stats["top_dirs"][0][0] == "src"
    assert stats["top_dirs"][0][1] == 3
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


@pytest.mark.asyncio
async def test_call_onboard_llm_injects_code_evidence():
    """call_onboard_llm 이 code_evidence 를 프롬프트에 주입한다."""
    from app.pipelines import github_onboard_pipeline as pl
    from app.pipelines.base import PipelineContext
    from tests.conftest import FakeGemini

    gemini = FakeGemini(responses=["## 1. 프로젝트 개요\n" + ("내용 " * 100)])
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
    assert "fastapi" in prompt
    assert "/api/health" in prompt
    assert "GET" in prompt


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
    assert "fastapi" in out
    assert "POST" in out and "/login" in out
    assert "app" in out and "30" in out
    assert "42" in out
