"""
user_repository 단위 테스트 — Neo4j 직접 호출 경로 (PR5 변경).

neo4j_client.run_cypher 를 monkeypatch 로 fake 응답 주입.
외부 의존성 제거 검증 + Cypher 호환성 회귀 추적.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.service import user_repository
from app.service.user_repository import UserInDB, UserPublic


pytestmark = pytest.mark.asyncio


# ─── Fake neo4j_client.run_cypher ───────────────────────────────


class _FakeRunCypher:
    """neo4j_client.run_cypher 를 대체. 호출 기록 + 미리 큐잉된 응답 반환."""

    def __init__(self, responses: Optional[List[List[Dict[str, Any]]]] = None):
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses or [])

    async def __call__(
        self,
        cypher: str,
        params: Optional[Dict[str, Any]] = None,
        database: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self.calls.append(
            {"cypher": cypher, "params": params or {}, "database": database}
        )
        if self._responses:
            return self._responses.pop(0)
        return []


@pytest.fixture
def fake_run(monkeypatch):
    """user_repository 가 import 한 neo4j_client.run_cypher 를 fake 로 치환."""

    def _setup(responses: Optional[List[List[Dict[str, Any]]]] = None) -> _FakeRunCypher:
        fake = _FakeRunCypher(responses=responses)
        monkeypatch.setattr(
            "app.service.user_repository.neo4j_client.run_cypher", fake
        )
        return fake

    return _setup


# ─── ensure_user_constraints ────────────────────────────────────


async def test_ensure_user_constraints_runs_cypher(fake_run):
    """제약 + 인덱스 일괄 ensure. PR3 (Library), PR-OAuth (github_id UNIQUE) 등이
    호출 개수를 늘리는 추세이므로, 정확한 count 보다는 핵심 statement 가 포함됐는지로 검증."""
    fake = fake_run([[]] * 10)  # 여러 호출 대비 넉넉히
    await user_repository.ensure_user_constraints()
    assert len(fake.calls) >= 1
    all_cypher = "\n".join(c["cypher"] for c in fake.calls)
    assert "CREATE CONSTRAINT user_email_unique IF NOT EXISTS" in all_cypher
    assert "REQUIRE u.email IS UNIQUE" in all_cypher


async def test_ensure_user_constraints_swallows_errors(monkeypatch, caplog):
    """Neo4j 미연결이어도 부팅 막지 않음 — warning 로그만."""

    async def _raise(*a, **kw):
        raise RuntimeError("NEO4J_URI not set")

    monkeypatch.setattr(
        "app.service.user_repository.neo4j_client.run_cypher", _raise
    )
    # 예외 안 던져야 함
    await user_repository.ensure_user_constraints()


# ─── get_user_by_email ──────────────────────────────────────────


async def test_get_user_by_email_returns_user_in_db(fake_run):
    fake = fake_run(
        [
            [
                {
                    "user": {
                        "id": "uid-7",
                        "email": "x@y.com",
                        "name": "xena",
                        "hashed_password": "$2b$12$...",
                        "created_at": "2026-05-12T00:00:00Z",
                        "updated_at": "2026-05-13T00:00:00Z",
                    }
                }
            ]
        ]
    )
    out = await user_repository.get_user_by_email("x@y.com")
    assert isinstance(out, UserInDB)
    assert out.hashed_password == "$2b$12$..."
    assert out.id == "uid-7"
    # 파라미터 바인딩
    assert fake.calls[0]["params"] == {"email": "x@y.com"}


async def test_get_user_by_email_returns_none_when_not_found(fake_run):
    # Neo4j MATCH 가 0건 → records = []
    fake_run([[]])
    out = await user_repository.get_user_by_email("nobody@nowhere.com")
    assert out is None


async def test_get_user_by_email_handles_null_user_field(fake_run):
    """row 는 있지만 user 가 null (방어적)."""
    fake_run([[{"user": None}]])
    out = await user_repository.get_user_by_email("x@y.com")
    assert out is None


# ─── update_user ────────────────────────────────────────────────


async def test_update_user_returns_userpublic(fake_run):
    fake = fake_run(
        [
            [
                {
                    "user": {
                        "id": "uid-7",
                        "email": "x@y.com",
                        "name": "renamed",
                        "updated_at": "2026-05-13T01:00:00Z",
                    }
                }
            ]
        ]
    )
    out = await user_repository.update_user("x@y.com", "renamed")
    assert isinstance(out, UserPublic)
    assert out.name == "renamed"
    # 파라미터 바인딩 — github_username / auto_progress / locale 안 넘기면 None (선택적 인자)
    assert fake.calls[0]["params"] == {
        "email": "x@y.com",
        "name": "renamed",
        "github_username": None,
        "auto_progress": None,
        "locale": None,
    }
    # Cypher 가 SET u.name = ... 포함
    assert "SET u.name =" in fake.calls[0]["cypher"]


async def test_update_user_returns_none_when_not_found(fake_run):
    fake_run([[]])
    out = await user_repository.update_user("x@y.com", "renamed")
    assert out is None


# ─── GitHub OAuth 토큰 암호화 회귀 ────────────────────────────


async def test_link_github_encrypts_access_token(fake_run, monkeypatch):
    """link_github 가 access_token 을 Neo4j 에 넣기 전 token_encryption.encrypt 통과."""
    from cryptography.fernet import Fernet
    from app.core import token_encryption
    from app.core.config import settings

    # 캐시 리셋 + 키 설정
    token_encryption._fernet_cache = None
    monkeypatch.setattr(
        settings, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )

    fake = fake_run(
        [
            [
                {
                    "user": {
                        "id": "u1",
                        "email": "x@y.com",
                        "name": "x",
                        "github_username": "octocat",
                        "created_at": "2026-05-13T00:00:00Z",
                    }
                }
            ]
        ]
    )
    await user_repository.link_github(
        email="x@y.com",
        github_id=42,
        github_username="octocat",
        github_access_token="gho_plain_secret",
        github_scopes="repo",
    )
    stored = fake.calls[0]["params"]["github_access_token"]
    # 평문이 그대로 들어가면 안 됨
    assert stored != "gho_plain_secret"
    assert stored.startswith(token_encryption.ENCRYPTED_PREFIX)
    # 복호화하면 원본 동일
    assert token_encryption.decrypt(stored) == "gho_plain_secret"

    token_encryption._fernet_cache = None


async def test_link_github_falls_back_to_plaintext_without_key(fake_run, monkeypatch):
    """키 미설정 환경 (개발) 에서는 평문 그대로 — graceful degrade."""
    from app.core import token_encryption
    from app.core.config import settings

    token_encryption._fernet_cache = None
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", None)

    fake = fake_run(
        [[{"user": {"id": "u1", "email": "x@y.com", "name": "x", "github_username": "u"}}]]
    )
    await user_repository.link_github(
        email="x@y.com",
        github_id=1,
        github_username="u",
        github_access_token="plain_dev_token",
        github_scopes="",
    )
    assert fake.calls[0]["params"]["github_access_token"] == "plain_dev_token"


# ─── set_password ───────────────────────────────────────────────


async def test_set_password_returns_true_on_success(fake_run):
    fake = fake_run([[{"email": "x@y.com"}]])
    ok = await user_repository.set_password("x@y.com", "hashed_pw_value")
    assert ok is True
    # bcrypt 해시는 호출자(라우트) 책임 — repository 는 평문/해시를 그대로 저장
    assert fake.calls[0]["params"] == {
        "email": "x@y.com",
        "hashed_password": "hashed_pw_value",
    }
    assert "SET u.hashed_password = $hashed_password" in fake.calls[0]["cypher"]


async def test_set_password_returns_false_when_user_missing(fake_run):
    fake_run([[]])
    ok = await user_repository.set_password("ghost@y.com", "h")
    assert ok is False


# ─── delete_user ────────────────────────────────────────────────


async def test_delete_user_returns_deleted_status(fake_run):
    fake = fake_run([[{"result": {"status": "deleted", "email": "x@y.com"}}]])
    result = await user_repository.delete_user("x@y.com")
    assert result["status"] == "deleted"
    assert result["email"] == "x@y.com"
    # DETACH DELETE 사용 확인
    assert "DETACH DELETE u" in fake.calls[0]["cypher"]
    assert fake.calls[0]["params"] == {"email": "x@y.com"}


async def test_delete_user_returns_not_found_when_missing(fake_run):
    fake_run([[]])
    result = await user_repository.delete_user("x@y.com")
    assert result["status"] == "not_found"


async def test_delete_user_blocks_last_admin(fake_run):
    fake_run([[{"result": {"status": "last_admin", "message": "마지막 관리자입니다."}}]])
    result = await user_repository.delete_user("admin@y.com")
    assert result["status"] == "last_admin"
    assert "관리자" in (result.get("message") or "")


async def test_delete_user_cypher_cascades_history(fake_run):
    """탈퇴 cypher 가 SubscriptionChange 까지 함께 정리하는지 회귀 가드."""
    fake = fake_run([[{"result": {"status": "deleted", "email": "x@y.com"}}]])
    await user_repository.delete_user("x@y.com")
    cypher = fake.calls[0]["cypher"]
    # cascade 대상 3종 모두 cypher 안에 명시되어 있어야 함
    assert "HAS_VIBE_REPO" in cypher
    assert "SUBSCRIPTION_HISTORY" in cypher
    assert "SubscriptionChange" in cypher
    # last-admin 보호 분기도 cypher 안에서 처리
    assert "would_orphan" in cypher


# ─── Cypher injection safety regression ─────────────────────────


async def test_email_with_special_chars_is_parameterized(fake_run):
    """Cypher injection 가능성이 있는 입력이 그대로 $email 로 바인딩되는지."""
    fake = fake_run([[{"user": None}]])
    dangerous = "x@y.com'} ) DETACH DELETE u //"
    await user_repository.get_user_by_email(dangerous)
    # 입력이 Cypher 문자열 안에 보간되지 않고 params 로만 전달돼야 함
    assert dangerous not in fake.calls[0]["cypher"]
    assert fake.calls[0]["params"]["email"] == dangerous
