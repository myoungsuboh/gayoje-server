"""
라우트 간 공용 Pydantic 스키마.

[배경]
Sprint 8 P0 까지 StatusResponse 가 5개 라우트 파일에 동일하게 정의되어 있었음.
project_name 필드 추가 같은 변경 시 5곳을 동시에 손대야 했고, 어긋날 경우 라우트
별로 응답 shape 가 달라지는 정합성 문제 발생.

이 모듈에 한 곳에 정의 → import 로 통일.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel


class PipelineStatusResponse(BaseModel):
    """arq Job status — 모든 status 라우트 응답의 공용 shape.

    project_name 은 Sprint 8 P0 멀티테넌트 격리 가드의 일부로 추가.
    stage 는 C — 2026-05 진행 단계 UI 위해 추가 (worker 가 Redis 에 기록).
    """

    task_id: str
    project_name: Optional[str] = None
    status: str  # 'queued' | 'in_progress' | 'complete' | 'not_found' | 'deferred' ...
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    enqueue_time: Optional[int] = None
    finish_time: Optional[int] = None
    stage: Optional[str] = None   # 'cps_running' | 'prd_running' | 'done' | None
