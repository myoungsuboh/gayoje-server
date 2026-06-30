"""
큐 작업 상태 조회 + 멀티테넌트 격리 가드.

[배경 — Sprint 8 P0]
이전엔 `GET /pipelines/*/status/{task_id}` 라우트들이 인증만 검사하고
ownership 미검증 → 공격자가 다른 사용자의 task_id 만 알면 (로그/추측)
결과 전체 조회 가능. multi-tenant 격리 우회.

[해결]
모든 status 라우트가 이 헬퍼를 거치도록 통일:
  1. arq job kwargs 에서 project_name 회수 (모든 enqueue_* 함수가 전달)
  2. ownership_repository.assert_owns(user_email, project_name) 으로 격리 검증
  3. 통과 시 status info 반환

[엣지 케이스]
- job not_found 또는 메타 expire → 404 (정보 누설 방지: ownership 실패와 동일 형태)
- kwargs 에 project_name 없음 (예: 옛 코드로 enqueue 된 잔재) → 404 동일 처리
- assert_owns 가 403 raise → 그대로 전파
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import HTTPException, status

from app.queue.client import get_job_status
from app.service import ownership_repository

logger = logging.getLogger(__name__)


_NOT_FOUND_DETAIL = "작업을 찾을 수 없거나 만료되었습니다."


async def get_job_status_for_user(task_id: str, user_email: str) -> Dict[str, Any]:
    """ownership 검증 후 job status 반환.

    Raises:
        HTTPException(404): task_id 가 없거나 메타 expire, 또는 project_name 미회수.
                            (ownership 실패와 별도 응답 분기 만들면 task_id 유효성
                             누설 — 동일 404 로 통일.)
        HTTPException(403): assert_owns 가 raise — 다른 사용자 task 조회 시도.
    """
    info = await get_job_status(task_id)

    if info.get("status") == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL
        )

    project_name = info.get("project_name")
    if not project_name:
        # 메타 expire 됐거나 옛 enqueue 코드의 잔재 — 동일하게 404 처리.
        logger.warning(
            "status_guard: project_name 회수 실패 task=%s status=%s",
            task_id, info.get("status"),
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL
        )

    # 다른 사용자의 task 조회 시 assert_access 가 403 raise.
    await ownership_repository.assert_access(user_email, project_name)

    return info
