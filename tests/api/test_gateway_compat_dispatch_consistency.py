"""
gateway_compat_routes 의 dispatch ↔ ownership 분류 정합성 회귀 가드.

[배경]
gateway_compat 의 _DISPATCH 에 신규 action 추가 시 _OWNERSHIP_CREATE /
_OWNERSHIP_ACCESS / _OWNERSHIP_FREE 셋 중 한 곳에도 등록 안 되면 silently
가드 우회됐던 회귀 표면이었음 (2026-05 보안 감사 발견).

이제 dispatcher 가 "분류 안 된 action" 만나면 500 raise. 이 테스트가 그
회귀 발생을 빌드 단계에서 잡는다.

[정합성 규칙]
모든 _DISPATCH 키는 다음 정확히 한 집합에 속해야:
  - _OWNERSHIP_CREATE  (claim 호출)
  - _OWNERSHIP_READ    (can_access — 비소유는 200-empty, 핸들러 미실행)
  - _OWNERSHIP_ACCESS  (assert_access — write/LLM, 비소유 403)
  - _OWNERSHIP_FREE    (의도적 우회 — setup 등 시스템 라우트)
"""
from __future__ import annotations

import pytest

from app.api import gateway_compat_routes as gw


def test_every_dispatch_action_is_classified():
    """[정합성 가드] _DISPATCH 의 모든 키가 ownership set 셋 중 정확히 1개에 속함."""
    dispatch_keys = set(gw._DISPATCH.keys())
    create = gw._OWNERSHIP_CREATE
    read = gw._OWNERSHIP_READ
    access = gw._OWNERSHIP_ACCESS
    free = gw._OWNERSHIP_FREE

    all_classified = create | read | access | free

    # 1. _DISPATCH 키가 한 집합에 속하지 않음 — 누락
    unclassified = dispatch_keys - all_classified
    assert not unclassified, (
        f"_DISPATCH 에 있지만 ownership 분류 안 된 action: {unclassified}. "
        f"_OWNERSHIP_CREATE / _OWNERSHIP_READ / _OWNERSHIP_ACCESS / _OWNERSHIP_FREE 중 한 곳에 추가하세요."
    )

    # 2. ownership set 에 있는데 _DISPATCH 에는 없음 — 죽은 등록
    orphans = all_classified - dispatch_keys
    assert not orphans, (
        f"ownership set 에 등록됐지만 _DISPATCH 에 없는 action: {orphans}. "
        "이전 PR 잔재이거나 오타. 정리 필요."
    )

    # 3. 두 set 에 동시 등록 — 정책 충돌 (4개 집합 쌍별 disjoint)
    sets = {"CREATE": create, "READ": read, "ACCESS": access, "FREE": free}
    names = list(sets)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            overlap = sets[a] & sets[b]
            assert not overlap, f"{a} 와 {b} 양쪽에 등록된 action: {overlap}"


def test_ownership_free_is_minimal():
    """[보수성] FREE 집합은 system 라우트 / 내부 검증 위임 라우트만. 임의 확장 방지."""
    # 정책상 FREE 는 적어야 함. 새로 추가될 때 일부러 이 테스트 깨뜨려서 의식적 결정하게 함.
    # - setupUserConstraints: project 무관한 시스템 라우트
    # - getJobStatus: task_id 만 받음 → dispatcher 가 project 추출 불가. 핸들러 내부의
    #   status_guard.get_job_status_for_user 가 ownership 직접 검증.
    # - cancelJob: [2026-05-27] getJobStatus 와 동일 — task_id 만 받고 핸들러 내부의
    #   get_job_status_for_user 로 ownership 검증 후 Redis cancel flag set.
    # - improveSkill: [2026-06] 편집 중인 규칙 1개의 초안(name/instructions/tags)을 LLM 으로
    #   다듬을 뿐 프로젝트(Neo4j) 데이터 미접근 · project_name 도 안 받음. 인증 + 토큰 quota
    #   (_LLM_HANDLERS) 로 충분.
    expected = {"setupUserConstraints", "getJobStatus", "cancelJob", "improveSkill"}
    assert gw._OWNERSHIP_FREE == expected, (
        f"_OWNERSHIP_FREE 가 의도와 다름. 기대: {expected}, 실제: {gw._OWNERSHIP_FREE}. "
        "FREE 에 새 action 을 의도적으로 추가하려면 이 테스트도 함께 갱신하세요."
    )
