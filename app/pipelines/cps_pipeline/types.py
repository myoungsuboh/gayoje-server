from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class CpsInput:
    """프론트엔드 postMeeting payload.

    [멀티테넌시] project_name 은 **사람이 읽는 깨끗한 이름** (LLM 프롬프트/마크다운
    표시용). DB 노드의 project property / id 는 team_id 를 합성한 *스코프 키*
    (project_key())로 만들어 동명 팀/개인 프로젝트를 격리한다.
    개인(team_id="")은 스코프 키 = 이름 그대로 → 기존 id 형식 100% 보존.
    """

    project_name: str
    version: str
    date: str
    meeting_content: str
    previous_cps_id: Optional[str] = None
    team_id: str = ""

    def normalized_version(self) -> str:
        return self.version.replace(".", "_")

    def project_key(self) -> str:
        """도메인 노드 project property/id 용 스코프 키 (개인=이름, 팀=sentinel 합성)."""
        from app.core.project_scope import scoped_project
        return scoped_project(self.project_name, self.team_id)

    def derived_cps_id(self) -> str:
        from app.core.project_scope import cps_delta_id
        return self.previous_cps_id or cps_delta_id(self.project_key(), self.version)

    def log_id(self) -> str:
        from app.core.project_scope import meeting_log_id
        return meeting_log_id(self.project_key(), self.version)


@dataclass
class CpsResult:
    """End-to-end 결과. 호출자(API)에 그대로 반환."""

    meeting_log_id: str
    delta_cps_id: str
    master_cps_id: str
    mode: str  # 'first_run' | 'incremental'
    diagnostic: Dict[str, Any] = field(default_factory=dict)
    cps_graph: Dict[str, Any] = field(default_factory=dict)
    # [2026-05-25] CPS Agent 의 추출 모드 — FE 가 사용자에게 표시.
    # 'strict' = 명시적 추출 (기존, 배지 없음)
    # 'lenient' = AI 적극 추측 (검토 권장 배지)
    # 'skip'   = spec 변동 없음 (정보성 배지)
    extraction_mode: str = "strict"
    extraction_warning: Optional[str] = None
