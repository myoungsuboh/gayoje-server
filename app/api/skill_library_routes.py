"""
Skill Library API — `/auth/me/skill-library/*` (유저 단위 스킬 보관함).

[흐름]
1. FE 가 모달 열면 GET 으로 전체 라이브러리 조회. 빈 라이브러리는 backend 가 자동 5개 기본 폴더 생성.
2. 폴더 CRUD: POST/PATCH/DELETE /folders/{?id}
3. 스킬 CRUD: POST/PATCH/DELETE /skills/{?id}
4. Import / Export 는 별도 라우트 — BE-3 에서.

[보안]
모든 라우트: get_current_user 의존 + owner_email 매칭 검증을 cypher 안에서.
다른 사용자 폴더 / 스킬 id 로 시도해도 cypher MATCH 가 매칭 안 됨 → 404.

[정책]
- 폴더 이름 / 카테고리 검증: name_validation.validate_name
- 스킬 이름은 자유 (Skill 의 기존 정책과 일관 — `name` 은 더 너그러운 입력 허용)
- 라이브러리 스킬 수 한도: quota.LIMIT_TYPE='library_skills' (Free 100 / Pro 1000)
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core import quota
from app.core.limiter import limiter
from app.core.name_validation import InvalidNameError, validate_name
from app.core.security import get_current_user
from app.service import ownership_repository, skill_library_repository as repo
from app.service.user_repository import UserPublic

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/me/skill-library", tags=["Skill Library"])


# ===== Request DTOs =====


class FolderCreateRequest(BaseModel):
    name: str = Field(..., description="폴더 이름 (한글/영문/숫자/공백/'-'/'_' 1~50자)")
    description: str = Field(default="", max_length=500)
    color: str = Field(default="", description="preset hex 6종 중 하나 또는 빈 값")
    category: str = Field(default="", description="자유 입력, 이름과 동일 검증")


class FolderUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    category: Optional[str] = None


class SkillCreateRequest(BaseModel):
    folder_id: str = Field(..., min_length=1, description="이 스킬이 들어갈 폴더 ID")
    name: str = Field(..., min_length=1, max_length=200)
    scope: str = ""
    priority: str = Field(default="Medium", pattern="^(High|Medium|Low)$")
    trigger_condition: str = ""
    instructions: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)


class SkillUpdateRequest(BaseModel):
    name: Optional[str] = None
    scope: Optional[str] = None
    priority: Optional[str] = Field(default=None, pattern="^(High|Medium|Low)?$")
    trigger_condition: Optional[str] = None
    instructions: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    folder_id: Optional[str] = None  # 폴더 이동


# ===== Response DTOs =====


class FolderResponse(BaseModel):
    id: str
    name: str
    description: str
    color: str
    category: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class LibrarySkillResponse(BaseModel):
    id: str
    name: str
    scope: str
    priority: str
    trigger_condition: str
    instructions: List[str]
    tags: List[str]
    folder_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class LibraryEntryResponse(BaseModel):
    folder: FolderResponse
    skills: List[LibrarySkillResponse]


class LibraryResponse(BaseModel):
    entries: List[LibraryEntryResponse]
    total_skill_count: int
    skill_limit: int
    subscription_type: str


class FolderDeleteResponse(BaseModel):
    mode: str  # 'cascade' | 'moved' | 'not_found'
    deleted_skill_count: Optional[int] = None
    moved_skill_count: Optional[int] = None
    unfiled_folder_id: Optional[str] = None


# ===== Helpers =====


def _validate_folder_name(name: Optional[str], *, field: str) -> Optional[str]:
    """선택적 필드의 이름 검증. None 이면 None 반환."""
    if name is None:
        return None
    try:
        return validate_name(name, field=field)
    except InvalidNameError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


def _validate_category(cat: Optional[str], *, allow_empty: bool = True) -> Optional[str]:
    """category 검증. 빈 값은 허용 (free-form 이라 폴더가 카테고리 안 가져도 OK).

    None → None / "" → "" / 그 외 → validate_name."""
    if cat is None:
        return None
    if cat == "" and allow_empty:
        return ""
    try:
        return validate_name(cat, field="카테고리")
    except InvalidNameError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


def _folder_to_response(f: repo.SkillFolderRow) -> FolderResponse:
    return FolderResponse(
        id=f.id,
        name=f.name,
        description=f.description,
        color=f.color,
        category=f.category,
        created_at=f.created_at,
        updated_at=f.updated_at,
    )


def _skill_to_response(s: repo.LibrarySkillRow) -> LibrarySkillResponse:
    return LibrarySkillResponse(
        id=s.id,
        name=s.name,
        scope=s.scope,
        priority=s.priority,
        trigger_condition=s.trigger_condition,
        instructions=s.instructions,
        tags=s.tags,
        folder_id=s.folder_id,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


async def _build_library_response(current_user: UserPublic) -> LibraryResponse:
    """전체 라이브러리 응답 빌드. 등급별 한도 + 현재 사용량 포함."""
    # 빈 라이브러리면 기본 폴더 5개 자동 생성 (사용자 결정 2026-05)
    await repo.ensure_default_folders_if_empty(current_user.email)

    entries = await repo.list_library(current_user.email)
    total = await repo.count_skills(current_user.email)
    subscription = current_user.subscription_type or "free"
    await quota.ensure_overrides_fresh()   # [2026-06-11] admin 변경 멀티프로세스 전파
    limit = quota.get_limit(subscription, "library_skills")
    return LibraryResponse(
        entries=[
            LibraryEntryResponse(
                folder=_folder_to_response(e.folder),
                skills=[_skill_to_response(s) for s in e.skills],
            )
            for e in entries
        ],
        total_skill_count=total,
        skill_limit=limit,
        subscription_type=subscription,
    )


# ===== Routes =====


@router.get("", response_model=LibraryResponse, summary="내 스킬 라이브러리 전체 조회")
async def list_library_route(
    current_user: UserPublic = Depends(get_current_user),
) -> LibraryResponse:
    """폴더 트리 + 스킬 + 한도 정보. 빈 상태면 기본 폴더 5개 자동 생성."""
    return await _build_library_response(current_user)


@router.post(
    "/folders",
    response_model=FolderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="폴더 생성",
)
async def create_folder_route(
    payload: FolderCreateRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> FolderResponse:
    name = _validate_folder_name(payload.name, field="폴더 이름")
    category = _validate_category(payload.category)
    folder = await repo.create_folder(
        owner_email=current_user.email,
        name=name or "",
        description=payload.description,
        color=payload.color,
        category=category or "",
    )
    if folder is None:
        # 사용자 노드가 없는 비정상 케이스 (인증 통과 + User 없음)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자 정보를 찾을 수 없습니다. 다시 로그인 해주세요.",
        )
    return _folder_to_response(folder)


@router.patch(
    "/folders/{folder_id}",
    response_model=FolderResponse,
    summary="폴더 수정 (이름 / 설명 / 컬러 / 카테고리 부분 변경)",
)
async def update_folder_route(
    folder_id: str,
    payload: FolderUpdateRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> FolderResponse:
    name = _validate_folder_name(payload.name, field="폴더 이름")
    category = _validate_category(payload.category)
    folder = await repo.update_folder(
        owner_email=current_user.email,
        folder_id=folder_id,
        name=name,
        description=payload.description,
        color=payload.color,
        category=category,
    )
    if folder is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="폴더를 찾을 수 없거나 본인 소유가 아닙니다.",
        )
    return _folder_to_response(folder)


@router.delete(
    "/folders/{folder_id}",
    response_model=FolderDeleteResponse,
    summary="폴더 삭제 (cascade=true 면 안 스킬도 삭제, false 면 '미분류' 폴더로 이동)",
)
async def delete_folder_route(
    folder_id: str,
    cascade: bool = Query(
        default=False,
        description="true: 안 스킬까지 삭제. false: 안 스킬을 '미분류' 폴더로 이동",
    ),
    current_user: UserPublic = Depends(get_current_user),
) -> FolderDeleteResponse:
    result = await repo.delete_folder(
        owner_email=current_user.email,
        folder_id=folder_id,
        cascade=cascade,
    )
    if result.get("mode") == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="폴더를 찾을 수 없거나 본인 소유가 아닙니다.",
        )
    return FolderDeleteResponse(**result)


@router.post(
    "/skills",
    response_model=LibrarySkillResponse,
    status_code=status.HTTP_201_CREATED,
    summary="라이브러리 스킬 추가",
)
async def create_skill_route(
    payload: SkillCreateRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> LibrarySkillResponse:
    # 한도 검증 — 라이브러리 스킬 총 개수가 등급 한도 미만이어야.
    current_count = await repo.count_skills(current_user.email)
    subscription = current_user.subscription_type or "free"
    await quota.ensure_overrides_fresh()   # [2026-06-11] admin 변경 멀티프로세스 전파
    limit = quota.get_limit(subscription, "library_skills")
    if current_count >= limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=quota.QuotaExceeded(
                limit_type="library_skills",
                current=current_count,
                limit=limit,
                subscription_type=subscription,
            ).to_dict(),
        )

    skill = await repo.create_skill(
        owner_email=current_user.email,
        folder_id=payload.folder_id,
        name=payload.name,
        scope=payload.scope,
        priority=payload.priority,
        trigger_condition=payload.trigger_condition,
        instructions=payload.instructions,
        tags=payload.tags,
    )
    if skill is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="폴더를 찾을 수 없거나 본인 소유가 아닙니다.",
        )
    return _skill_to_response(skill)


@router.patch(
    "/skills/{skill_id}",
    response_model=LibrarySkillResponse,
    summary="라이브러리 스킬 수정 (필드 부분 변경 + folder_id 변경 시 이동)",
)
async def update_skill_route(
    skill_id: str,
    payload: SkillUpdateRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> LibrarySkillResponse:
    skill = await repo.update_skill(
        owner_email=current_user.email,
        skill_id=skill_id,
        name=payload.name,
        scope=payload.scope,
        priority=payload.priority,
        trigger_condition=payload.trigger_condition,
        instructions=payload.instructions,
        tags=payload.tags,
        folder_id=payload.folder_id,
    )
    if skill is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="스킬을 찾을 수 없거나 본인 소유가 아닙니다.",
        )
    return _skill_to_response(skill)


@router.delete(
    "/skills/{skill_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="라이브러리 스킬 삭제",
)
async def delete_skill_route(
    skill_id: str,
    current_user: UserPublic = Depends(get_current_user),
) -> None:
    ok = await repo.delete_skill(owner_email=current_user.email, skill_id=skill_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="스킬을 찾을 수 없거나 본인 소유가 아닙니다.",
        )
    return None


# ===== Import / Export =====


class ImportFromProjectRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    skill_ids: List[str] = Field(..., min_length=1, description="복사할 프로젝트 Skill id 목록")
    folder_id: str = Field(..., min_length=1, description="대상 라이브러리 폴더 id")


class ImportFromProjectResponse(BaseModel):
    imported: List[dict]  # [{source_skill_id, library_skill_id, name}]
    new_total_skill_count: int
    skill_limit: int


class ExportToProjectRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    library_skill_ids: List[str] = Field(..., min_length=1)
    conflict_strategy: str = Field(
        default="skip",
        pattern="^(overwrite|skip|rename)$",
        description="overwrite: 덮어쓰기 / skip: 충돌 시 건너뛰기 / rename: id 에 -copy suffix",
    )


class ExportToProjectResponse(BaseModel):
    imported_ids: List[str]
    skipped_ids: List[str]
    renamed: List[dict]  # [{old_id, new_id}]


class ConflictCheckRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    team_id: Optional[str] = None
    skill_ids: List[str] = Field(..., min_length=1)


class ConflictCheckResponse(BaseModel):
    conflicting_ids: List[str]


@router.post(
    "/import-from-project",
    response_model=ImportFromProjectResponse,
    summary="현재 프로젝트의 Skill 들을 라이브러리 폴더로 복사",
)
async def import_from_project_route(
    payload: ImportFromProjectRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> ImportFromProjectResponse:
    # ownership 검증 — 본인 소유 프로젝트만 import 가능
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)

    # 한도 검증 — count + import 수 가 limit 초과면 거부
    current_count = await repo.count_skills(current_user.email)
    subscription = current_user.subscription_type or "free"
    await quota.ensure_overrides_fresh()   # [2026-06-11] admin 변경 멀티프로세스 전파
    limit = quota.get_limit(subscription, "library_skills")
    if current_count + len(payload.skill_ids) > limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=quota.QuotaExceeded(
                limit_type="library_skills",
                current=current_count,
                limit=limit,
                subscription_type=subscription,
            ).to_dict(),
        )

    result = await repo.copy_skills_from_project(
        owner_email=current_user.email,
        project_name=payload.project_name,
        skill_ids=payload.skill_ids,
        folder_id=payload.folder_id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="대상 폴더를 찾을 수 없거나 본인 소유가 아닙니다.",
        )
    return ImportFromProjectResponse(
        imported=result.imported,
        new_total_skill_count=result.new_total_skill_count,
        skill_limit=limit,
    )


@router.post(
    "/export-to-project",
    response_model=ExportToProjectResponse,
    summary="라이브러리 스킬들을 현재 프로젝트의 Skill 노드로 복사",
)
async def export_to_project_route(
    payload: ExportToProjectRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> ExportToProjectResponse:
    # ownership 검증
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)

    result = await repo.copy_skills_to_project(
        owner_email=current_user.email,
        project_name=payload.project_name,
        library_skill_ids=payload.library_skill_ids,
        conflict_strategy=payload.conflict_strategy,
    )
    return ExportToProjectResponse(
        imported_ids=result.imported_ids,
        skipped_ids=result.skipped_ids,
        renamed=result.renamed,
    )


@router.post(
    "/check-export-conflicts",
    response_model=ConflictCheckResponse,
    summary="export 전 충돌 ID 미리 검사 (FE 가 다이얼로그 표시 결정용)",
)
async def check_export_conflicts_route(
    payload: ConflictCheckRequest,
    current_user: UserPublic = Depends(get_current_user),
) -> ConflictCheckResponse:
    """FE 가 export 전에 호출 — 충돌 ID 미리 받아서 정책 다이얼로그 표시 여부 결정."""
    await ownership_repository.assert_access(current_user.email, payload.project_name, payload.team_id)
    conflicting = await repo.find_conflicting_skill_ids(
        project_name=payload.project_name,
        skill_ids=payload.skill_ids,
    )
    return ConflictCheckResponse(conflicting_ids=conflicting)
