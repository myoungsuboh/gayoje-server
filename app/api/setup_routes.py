"""
Setup 라우트 — DB 초기 셋업 (Neo4j 제약 등).

[설계]
- 운영 부팅 직후 또는 새 환경 구성 시 한 번씩 호출.
- Idempotent (`IF NOT EXISTS` 사용 — 여러 번 호출해도 안전).
- 인증 미요구 (최초 셋업 시 회원이 없을 수 있음).
- 참고: App 부팅 시 lifespan 에서도 자동 호출되므로 보통 수동 호출 불필요.
"""
from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

from app.service import user_repository

router = APIRouter(prefix="/setup", tags=["Setup"])


class SetupResponse(BaseModel):
    status: str
    data: Dict[str, Any]


@router.post("/user-constraints", response_model=SetupResponse)
async def setup_user_constraints() -> SetupResponse:
    """
    Neo4j 에 `User.email UNIQUE` 제약을 idempotent 생성.

    `ensure_user_constraints` 는 실패해도 예외를 던지지 않으므로 (warning 만 찍음),
    응답은 항상 success. 실제 적용 여부는 Neo4j 직접 확인.
    """
    await user_repository.ensure_user_constraints()
    return SetupResponse(
        status="success",
        data={"constraint": "user_email_unique", "message": "User.email UNIQUE 제약 ensure 완료"},
    )
