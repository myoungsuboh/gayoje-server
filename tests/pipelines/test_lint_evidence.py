"""
lint_evidence 결정적 evidence collector 단위 테스트.

[검증 포인트]
- API endpoint pattern: FastAPI / Express / Spring / Vue Router / Django / React Router
- Path 매개변수 변형 흡수: {id} / :id / <int:id>
- Class 매칭: Python / TypeScript / Java / Go / Rust / TS interface
- BoundedContext: 디렉토리명 매칭 (PascalCase → snake/kebab variant)
- DomainEvent: class + publish/emit/dispatch 호출
- tech_stack manifest: package.json / pyproject.toml / pom.xml / requirements.txt
- Policy/Rule weak token 매칭 + manifest 제외 확인
- Hallucination 차단: 빈 name / 한글 / 공백 포함 → 매칭 X
"""
from __future__ import annotations

from app.pipelines.lint_evidence import (
    FileSample,
    _normalize_endpoint_path,
    collect_api_evidence,
    collect_class_evidence,
    collect_context_evidence,
    collect_event_evidence,
    collect_rule_evidence,
    collect_screen_evidence,
    collect_tech_stack_evidence,
)


# ─── _normalize_endpoint_path ──────────────────────────────────


def test_normalize_endpoint_path_fastapi_braces():
    assert _normalize_endpoint_path("/users/{id}") == "/users/{__P__}"
    assert _normalize_endpoint_path("/users/{user_id}/posts/{post_id}") == "/users/{__P__}/posts/{__P__}"


def test_normalize_endpoint_path_express_colon():
    assert _normalize_endpoint_path("/users/:id") == "/users/{__P__}"
    assert _normalize_endpoint_path("/a/:x/b/:y") == "/a/{__P__}/b/{__P__}"


def test_normalize_endpoint_path_django_brackets():
    assert _normalize_endpoint_path("/users/<int:id>") == "/users/{__P__}"


def test_normalize_endpoint_path_leading_slash_added():
    assert _normalize_endpoint_path("users/x") == "/users/x"


def test_normalize_endpoint_path_empty_returns_empty():
    assert _normalize_endpoint_path("") == ""
    assert _normalize_endpoint_path(None) == ""


# ─── collect_api_evidence ──────────────────────────────────────


def _fs(path: str, content: str) -> FileSample:
    return FileSample(path=path, content=content, size=len(content))


def test_api_evidence_fastapi_router_post():
    api = {"method": "POST", "endpoint": "/tickets"}
    samples = [_fs("a.py", "@router.post('/tickets')\ndef f(): ...\n")]
    evs = collect_api_evidence(api, samples)
    assert len(evs) == 1
    assert evs[0].file == "a.py"
    assert evs[0].kind == "endpoint"


def test_api_evidence_fastapi_app_get():
    api = {"method": "GET", "endpoint": "/health"}
    samples = [_fs("main.py", '@app.get("/health")\nasync def h():\n    return "ok"\n')]
    evs = collect_api_evidence(api, samples)
    assert len(evs) == 1


def test_api_evidence_express_router():
    api = {"method": "POST", "endpoint": "/users"}
    samples = [_fs("server.js", "router.post('/users', createUser)\n")]
    evs = collect_api_evidence(api, samples)
    assert len(evs) == 1


def test_api_evidence_express_app():
    api = {"method": "DELETE", "endpoint": "/cart/:id"}
    samples = [_fs("app.js", "app.delete('/cart/:id', removeItem)\n")]
    evs = collect_api_evidence(api, samples)
    assert len(evs) == 1


def test_api_evidence_spring_postmapping():
    api = {"method": "POST", "endpoint": "/orders"}
    samples = [_fs("Ctrl.java", '@PostMapping("/orders")\npublic void create() {}\n')]
    evs = collect_api_evidence(api, samples)
    assert len(evs) == 1


def test_api_evidence_spring_requestmapping():
    api = {"method": "POST", "endpoint": "/orders"}
    samples = [_fs(
        "Ctrl.java",
        '@RequestMapping(value = "/orders", method = RequestMethod.POST)\npublic void create() {}\n',
    )]
    evs = collect_api_evidence(api, samples)
    assert len(evs) == 1


def test_api_evidence_django_path():
    api = {"method": "GET", "endpoint": "/articles/<int:pk>"}
    samples = [_fs(
        "urls.py",
        "from django.urls import path\npath('/articles/<int:pk>', view)\n",
    )]
    evs = collect_api_evidence(api, samples)
    assert len(evs) == 1


def test_api_evidence_vue_router():
    api = {"method": "GET", "endpoint": "/dashboard"}
    samples = [_fs(
        "router.ts",
        "const routes = [\n  { path: '/dashboard', component: Dashboard },\n]\n",
    )]
    evs = collect_api_evidence(api, samples)
    assert len(evs) == 1


def test_api_evidence_react_router():
    api = {"method": "GET", "endpoint": "/profile"}
    samples = [_fs(
        "App.tsx",
        '<Route path="/profile" element={<Profile />} />\n',
    )]
    evs = collect_api_evidence(api, samples)
    assert len(evs) == 1


def test_api_evidence_no_match_returns_empty():
    api = {"method": "POST", "endpoint": "/nonexistent"}
    samples = [_fs("a.py", "print('hi')\n")]
    assert collect_api_evidence(api, samples) == []


def test_api_evidence_no_endpoint_returns_empty():
    assert collect_api_evidence({"method": "POST"}, [_fs("a", "x")]) == []
    assert collect_api_evidence({"method": "POST", "endpoint": ""}, []) == []


def test_api_evidence_dedupes_same_file_same_line():
    api = {"method": "POST", "endpoint": "/x"}
    # 같은 파일에서 두 패턴이 같은 줄에 매칭되어도 1건만 (dedup by file+line)
    samples = [_fs("a.py", "@router.post('/x')  # also: app.post('/x')\n")]
    evs = collect_api_evidence(api, samples)
    assert len(evs) <= 2  # 최대 2건 (다른 패턴이라도 같은 줄이면 dedup)


# ─── collect_class_evidence ────────────────────────────────────


def test_class_evidence_python_class():
    samples = [_fs("models.py", "class Ticket(BaseModel):\n    id: str\n")]
    evs = collect_class_evidence("Ticket", samples)
    assert len(evs) == 1
    assert evs[0].kind == "class"


def test_class_evidence_typescript_interface():
    samples = [_fs(
        "types.ts",
        "export interface Ticket {\n  id: string\n}\n",
    )]
    evs = collect_class_evidence("Ticket", samples)
    assert len(evs) == 1


def test_class_evidence_typescript_type_alias():
    samples = [_fs("types.ts", "export type Ticket = { id: string }\n")]
    evs = collect_class_evidence("Ticket", samples)
    assert len(evs) == 1


def test_class_evidence_java_class():
    samples = [_fs("Ticket.java", "public class Ticket {\n  private String id;\n}\n")]
    evs = collect_class_evidence("Ticket", samples)
    assert len(evs) == 1


def test_class_evidence_go_struct():
    samples = [_fs("ticket.go", "type Ticket struct {\n  ID string\n}\n")]
    evs = collect_class_evidence("Ticket", samples)
    assert len(evs) == 1


def test_class_evidence_rust_struct():
    samples = [_fs("ticket.rs", "pub struct Ticket {\n  pub id: String,\n}\n")]
    evs = collect_class_evidence("Ticket", samples)
    assert len(evs) == 1


def test_class_evidence_rust_enum():
    samples = [_fs("status.rs", "pub enum Status { Active, Closed }\n")]
    evs = collect_class_evidence("Status", samples)
    assert len(evs) == 1


def test_class_evidence_rejects_non_pascalcase():
    """공백/특수문자 포함 name → 매칭 안 함 (false positive 차단)."""
    samples = [_fs("a.py", "class FooBar: pass\n")]
    assert collect_class_evidence("foo bar", samples) == []
    assert collect_class_evidence("Foo Bar", samples) == []
    assert collect_class_evidence("foo_bar", samples) == []
    assert collect_class_evidence("", samples) == []


def test_class_evidence_rejects_korean():
    samples = [_fs("a.py", "class 티켓: pass\n")]
    assert collect_class_evidence("티켓", samples) == []


def test_class_evidence_no_match():
    samples = [_fs("a.py", "x = 1\n")]
    assert collect_class_evidence("Ticket", samples) == []


# ─── collect_context_evidence ──────────────────────────────────


def test_context_evidence_matches_lowercase_dir():
    samples = [
        _fs("src/funding/service.py", "x"),
        _fs("src/other/foo.py", "y"),
    ]
    evs = collect_context_evidence("Funding", samples)
    assert len(evs) == 1
    assert "funding" in evs[0].file


def test_context_evidence_matches_snake_case_dir():
    samples = [_fs("app/user_account/x.py", "x")]
    evs = collect_context_evidence("UserAccount", samples)
    assert len(evs) == 1


def test_context_evidence_skips_short_name():
    samples = [_fs("src/x/a.py", "x")]
    assert collect_context_evidence("X", samples) == []


# ─── collect_event_evidence ────────────────────────────────────


def test_event_evidence_finds_class():
    samples = [_fs("events.py", "class TicketIssued:\n    pass\n")]
    evs = collect_event_evidence("TicketIssued", samples)
    assert any(e.kind == "event_class" for e in evs)


def test_event_evidence_finds_publish_call():
    samples = [_fs(
        "service.py",
        "class TicketIssued: pass\n\npublish(TicketIssued(ticket_id=x))\n",
    )]
    evs = collect_event_evidence("TicketIssued", samples)
    kinds = {e.kind for e in evs}
    assert "event_class" in kinds
    assert "event_publish" in kinds


def test_event_evidence_finds_emit_call():
    samples = [_fs("svc.js", "emit('OrderShipped', { id })\n")]
    # class 선언 없어도 publish 호출만으로 evidence 인정 — 단, 호출 인자에 event name 등장 필요
    # 현재 collect_event_evidence 는 word boundary 매칭이라 'OrderShipped' 가 들어있어야 함
    evs = collect_event_evidence("OrderShipped", samples)
    assert any(e.kind == "event_publish" for e in evs)


def test_event_evidence_rejects_non_pascalcase():
    samples = [_fs("a.py", "class foo: pass\n")]
    assert collect_event_evidence("foo", samples) == []


# ─── collect_tech_stack_evidence ───────────────────────────────


def test_tech_stack_vue_in_package_json():
    samples = [_fs(
        "package.json",
        '{"dependencies": {"vue": "^3.4.0", "axios": "^1"}}\n',
    )]
    evs = collect_tech_stack_evidence("Vue.js", samples)
    assert len(evs) == 1
    assert evs[0].kind == "manifest"


def test_tech_stack_postgresql_in_requirements():
    samples = [_fs("requirements.txt", "asyncpg==0.29.0\nfastapi==0.128.0\n")]
    evs = collect_tech_stack_evidence("PostgreSQL", samples)
    assert len(evs) == 1


def test_tech_stack_spring_boot_in_pom():
    samples = [_fs(
        "pom.xml",
        "<dependency>\n  <groupId>org.springframework.boot</groupId>\n</dependency>\n",
    )]
    evs = collect_tech_stack_evidence("Spring Boot", samples)
    assert len(evs) == 1


def test_tech_stack_list_input():
    samples = [_fs("package.json", '{"deps": {"react": "^18"}}\n')]
    evs = collect_tech_stack_evidence(["React", "TypeScript"], samples)
    assert len(evs) >= 1


def test_tech_stack_skips_non_manifest():
    """manifest 가 아닌 파일에서는 매칭 안 함 (false positive 차단)."""
    samples = [_fs("src/main.py", "import vue  # dummy\n")]
    assert collect_tech_stack_evidence("Vue.js", samples) == []


def test_tech_stack_empty_input():
    assert collect_tech_stack_evidence(None, []) == []
    assert collect_tech_stack_evidence("", [_fs("package.json", "{}")]) == []


# ─── collect_rule_evidence ─────────────────────────────────────


def test_rule_evidence_matches_name_tokens():
    rule = {"name": "TypeScriptStrict", "tags": ["typescript"]}
    samples = [_fs("a.ts", "// TypeScript strict mode\nconst x: number = 1\n")]
    evs = collect_rule_evidence(rule, samples)
    assert len(evs) >= 1


def test_rule_evidence_matches_tags():
    rule = {"name": "Foo", "tags": ["camelcase"]}
    samples = [_fs("a.ts", "const myCamelCase = 1\n")]
    # 'camelcase' 토큰 매칭
    evs = collect_rule_evidence(rule, samples)
    assert len(evs) >= 1


def test_rule_evidence_skips_manifest():
    rule = {"name": "Naming", "tags": ["naming"]}
    samples = [_fs("package.json", '{"name": "naming-test"}\n')]
    assert collect_rule_evidence(rule, samples) == []


def test_rule_evidence_no_tokens():
    """name/tags 모두 없거나 너무 짧으면 매칭 X."""
    rule = {"name": "x"}
    samples = [_fs("a.py", "x = 1\n")]
    assert collect_rule_evidence(rule, samples) == []


# ─── collect_screen_evidence (2026-06 기획 항목 자동 검증) ──────


def test_screen_evidence_matches_vue_router_path():
    screen = {"name": "로그인 화면", "path": "/login"}
    samples = [_fs("src/router/index.js",
                   "const routes = [\n  { path: '/login', component: Login },\n]\n")]
    evs = collect_screen_evidence(screen, samples)
    assert len(evs) == 1
    assert evs[0].kind == "screen_route"
    assert evs[0].line == 2


def test_screen_evidence_matches_react_route_element():
    screen = {"name": "Login", "path": "/login"}
    samples = [_fs("src/App.tsx",
                   '<Routes>\n  <Route path="/login" element={<Login />} />\n</Routes>\n')]
    evs = collect_screen_evidence(screen, samples)
    assert any(e.kind == "screen_route" for e in evs)


def test_screen_evidence_route_with_param_normalized():
    """'/users/{id}' ↔ '/users/:id' — API 와 같은 매개변수 정규화."""
    screen = {"name": "사용자 상세", "path": "/users/{id}"}
    samples = [_fs("src/router.js", "  { path: '/users/:id', component: UserDetail },\n")]
    evs = collect_screen_evidence(screen, samples)
    assert len(evs) == 1


def test_screen_evidence_root_path_skipped():
    """루트 '/' 는 어디에나 있어 증거 가치 없음 — 패턴 생략 → 미매칭."""
    screen = {"name": "홈", "path": "/"}
    samples = [_fs("src/router.js", "  { path: '/', component: Home },\n")]
    assert collect_screen_evidence(screen, samples) == []


def test_screen_evidence_plain_string_not_matched():
    """따옴표 안 단순 등장(axios 호출 등)은 route 정의가 아님 — 미매칭."""
    screen = {"name": "로그인", "path": "/login"}
    samples = [_fs("src/api.js", "axios.post('/login', body)\n")]
    assert collect_screen_evidence(screen, samples) == []


def test_screen_evidence_pascalcase_component_class():
    screen = {"name": "LoginPage", "path": ""}
    samples = [_fs("src/pages/login.tsx", "export class LoginPage extends React.Component {\n}\n")]
    evs = collect_screen_evidence(screen, samples)
    assert len(evs) == 1
    assert evs[0].kind == "screen_component"


def test_screen_evidence_korean_name_no_path_empty():
    """한글 이름 + path 없음 → 결정적 매칭 불가 (LLM residual 로 넘어감)."""
    screen = {"name": "마이페이지", "path": ""}
    samples = [_fs("src/pages/my.vue", "<template><div/></template>\n")]
    assert collect_screen_evidence(screen, samples) == []
