"""
apiSpecAutofill 파이프라인 — error_cases / auth 가 빈 SPACK API 노드에 대해 LLM 으로
에러 응답·인증 방식 초안을 자동 생성.

[배경]
PRD 완성도의 "API 에러 응답 명시"·"API 인증 방식 명시" 가 0% 인 프로젝트가 많다
(scorer 의 api_error_cases_ratio / api_auth_specified_ratio). 사용자가 직접 적기
어려운 영역이라, AI 가 각 API 의 method/path/description 을 보고 초안을 채운다.

[자가참조 방지 — 가장 중요]
AI 가 채우고 AI 가 만점 받는 self-reference 를 막기 위해, 생성한 모든 항목에
`source="ai_draft"`, `reviewed=False` 메타를 부착한다. scorer 는 미검토 AI 초안을
0.5 로만 카운트(절반 점수)한다 — 사람이 검토(reviewed=True)해야 1.0 이 된다.

[스테이지 매핑] (skill_trigger_fill_pipeline 패턴을 본뜸)
- Prepare Input → `_split_targets`: error_cases 빈 AND auth(description·required_roles
  모두 빈) API 만 골라낸다. 둘 중 하나라도 채워진 API 는 건너뜀(LLM 호출 0).
- Autofill AI Agent → `call_api_spec_filler`: 대상 API 마다 1 LLM 호출.
  서로 독립적이라 `asyncio.gather` 로 병렬.
- Merge & Save → 원본 순서 유지하며 생성 결과 병합 + 단일 노드 부분 저장.

[건너뜀 정책]
error_cases 가 비어있지 않거나 auth 가 이미 명시(description 또는 required_roles)된
API 는 손대지 않는다. 사용자가 손으로 적은 명세를 덮어쓰지 않기 위함.

[부분 실패 격리]
한 API 의 LLM 호출/파싱/저장 실패가 전체 배치를 깨지 않는다 — 해당 API 만
generated=False 로 떨어뜨리고 나머지는 정상 처리.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.clients.gemini_client import GeminiError
from app.pipelines.base import PipelineContext, generate_json_with_retry
from app.pipelines.design_validator.api_payload import (
    normalize_auth,
    normalize_error_cases,
)

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 생성한 항목 출처 마킹 — scorer 가 미검토 초안을 절반 점수로 인정하는 키.
_AI_DRAFT_SOURCE = "ai_draft"

# [2026-05-29] LLM 동시 호출 상한. API 가 24개면 gather 가 24개를 한 번에 던져
# Gemini rate limit(quota/transient) → GeminiError 로 전체 실패하던 버그 수정.
# 동시성을 제한해 rate limit 을 회피하면서도 직렬보다 훨씬 빠름.
# [2026-06-10] settings.AUTOFILL_LLM_CONCURRENCY(기본 8) 로 이동 — 이 상수는
# 설정 로드 실패 시 보수적 fallback.
_MAX_LLM_CONCURRENCY = 5


def _llm_concurrency() -> int:
    """병렬 LLM 동시성 — env(AUTOFILL_LLM_CONCURRENCY) 로 운영 중 조절."""
    try:
        from app.core.config import settings
        n = int(getattr(settings, "AUTOFILL_LLM_CONCURRENCY", 0) or 0)
        return max(1, n) if n else _MAX_LLM_CONCURRENCY
    except Exception:  # noqa: BLE001 — 설정 로드 실패 시 보수적 기본
        return _MAX_LLM_CONCURRENCY


def resolve_draft_model() -> Optional[str]:
    """autofill 초안 생성 모델 override — env(AUTOFILL_DRAFT_MODEL). 미설정 None.

    초안은 reviewed=False(0.5점)로 사람 검토 전제라 경량 모델(flash-lite, 비-thinking)
    강제가 합리적인 영역 — 운영에서 품질 확인 후 켠다. None 이면 기존 동작(구독 모델).
    """
    try:
        from app.core.config import settings
        return getattr(settings, "AUTOFILL_DRAFT_MODEL", None) or None
    except Exception:  # noqa: BLE001
        return None

# [2026-06-01 fast-fail + 폴백] primary 모델(PRO=gemini-2.5-flash)이 느리거나 quota 면
# 90s×3=270s 매달리지 않고 짧게 끊어 폴백 모델로 넘어가도록 per-call override.
# - 작은 단건 생성(API 1개의 error/auth)은 정상 응답이 보통 <15s. 35s 면 정상은 통과,
#   thinking 폭주/큐잉 같은 비정상만 빠르게 컷.
# - max_retries=1: 백엔드 쪽 추가 재시도 없이 1회만 (프록시가 키 로테이션 담당). 폭주 차단.
_AUTOFILL_LLM_TIMEOUT = 35.0
_AUTOFILL_LLM_MAX_RETRIES = 1


# ─── Structured Output Schema (결정성 강화) ─────────────────────────
# schemas.py 의 _ERROR_CASE_SCHEMA / _AUTH_SCHEMA 와 동일 형태 (status 필수 등).
_API_SPEC_FILL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "error_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "status": {"type": "integer"},
                    "code": {"type": "string"},
                    "condition": {"type": "string"},
                    "message": {"type": "string"},
                    "lineage_quote": {"type": "string"},
                },
                "required": ["status"],
            },
        },
        "auth": {
            "type": "object",
            "properties": {
                "required": {"type": "boolean"},
                "required_roles": {"type": "array", "items": {"type": "string"}},
                "ownership_check": {"type": "string"},
                "description": {"type": "string"},
            },
        },
    },
    "required": ["error_cases", "auth"],
}


# ─── Domain types ───────────────────────────────────────────────


@dataclass(frozen=True)
class ApiSpecInput:
    """autofill 대상 한 API (SPACK API 노드의 부분집합)."""

    id: str
    name: str = ""
    method: str = ""
    endpoint: str = ""
    description: str = ""
    error_cases: List[Dict[str, Any]] = field(default_factory=list)
    auth: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AutofillInput:
    project_name: str
    apis: List[ApiSpecInput]
    # [2026-06 멀티테넌시] design 파이프라인에 편입돼 팀 스코프 프로젝트에 저장할 때
    # 필요. 기본 "" (단일 테넌트/레거시 standalone job 은 그대로 비스코프 저장).
    team_id: str = ""


@dataclass
class FilledApiSpec:
    id: str
    error_cases: List[Dict[str, Any]] = field(default_factory=list)
    auth: Dict[str, Any] = field(default_factory=dict)
    generated: bool = False  # True = LLM 생성 + 메타 부착, False = 기존 유지/건너뜀
    saved: bool = False      # 부분 저장 성공 여부
    degraded: bool = False   # True = primary 모델 실패로 폴백(경량) 모델 초안 사용


@dataclass
class AutofillResult:
    apis: List[FilledApiSpec] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


# ─── Stage 1: split targets ────────────────────────────────────


def _has_error_cases(a: ApiSpecInput) -> bool:
    """error_cases 가 의미있게 채워졌는지 (비빈 list)."""
    return bool(a.error_cases)


def _has_auth_spec(a: ApiSpecInput) -> bool:
    """auth 가 '의도적으로 명시' 됐는지 — scorer 와 동일 기준.

    default auth (required=True) 만으로는 명시로 안 봄. description 이 있거나
    required_roles 가 비어있지 않으면 명시.
    """
    auth = a.auth or {}
    return bool(auth.get("description") or auth.get("required_roles"))


def _needs_fill(a: ApiSpecInput) -> bool:
    """error_cases 비었거나 auth 미명시면 보완 대상."""
    return not (_has_error_cases(a) and _has_auth_spec(a))


def _split_targets(
    apis: List[ApiSpecInput],
) -> Tuple[List[ApiSpecInput], List[ApiSpecInput]]:
    """(보완 대상, 건너뜀) 분리. 원본 순서는 호출자가 유지."""
    targets = [a for a in apis if _needs_fill(a)]
    skipped = [a for a in apis if not _needs_fill(a)]
    return targets, skipped


# ─── Stage 2: LLM call (per API) ───────────────────────────────


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **vars: str) -> str:
    # [2026-05 보안] single-pass 렌더로 통일 (placeholder 주입 방지).
    # 단일 진실원: app.core.prompt_render. 순환 import 회피 위해 함수 로컬 import.
    from app.core.prompt_render import render_template
    return render_template(template, **{k: ("" if v is None else v) for k, v in vars.items()})


def _build_prompt(template: str, api: ApiSpecInput) -> str:
    auth = api.auth or {}
    auth_required = "true" if auth.get("required", True) else "false"
    return _render(
        template,
        name=api.name or "(이름 없음)",
        method=(api.method or "GET").upper(),
        endpoint=api.endpoint or "(경로 미지정)",
        description=api.description or "(설명 없음)",
        auth_required=auth_required,
    )


def _mark_ai_draft_error_cases(cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """각 error_case 에 source=ai_draft, reviewed=False 부착 (정규화 후)."""
    out: List[Dict[str, Any]] = []
    for c in cases:
        item = dict(c)
        item["source"] = _AI_DRAFT_SOURCE
        item["reviewed"] = False
        out.append(item)
    return out


def _mark_ai_draft_auth(auth: Dict[str, Any]) -> Dict[str, Any]:
    """auth 객체에 source=ai_draft, reviewed=False 부착 (정규화 후)."""
    out = dict(auth)
    out["source"] = _AI_DRAFT_SOURCE
    out["reviewed"] = False
    return out


async def call_api_spec_filler(
    ctx: PipelineContext,
    template: str,
    api: ApiSpecInput,
    *,
    fallback_model: Optional[str] = None,
    draft_model: Optional[str] = None,
) -> FilledApiSpec:
    """단일 API 의 error_cases/auth 초안 생성 + AI 초안 메타 부착.

    [2026-06-01 graceful degradation]
    1차는 구독 모델(PRO=gemini-2.5-flash)로 fast-fail 호출 — primary 가 느리거나
    quota 면 90s×3 매달리지 않고 짧게 끊는다. primary 가 GeminiError 면 그 API 한 건만
    폴백 모델(`fallback_model`, 보통 gemini-2.5-flash-lite — 별도 무료 쿼터 + 비-thinking
    이라 빠름)로 재시도해 초안을 만든다. 결과는 사람이 검토하는 AI 초안(reviewed=False,
    0.5점)이므로 폴백 시 모델이 더 가벼워도 허용된다.

    [배치/잡 비실패 — 가장 중요]
    이전엔 GeminiError 를 그대로 전파 → 한 API 실패가 배치 전체를 깨고, 잡까지 실패해
    arq 가 잡 전체를 ARQ_MAX_TRIES 만큼 재시도(모든 API 재호출, 쿼터 재소진)하던
    폭주가 있었다. 이제 polling/transient/quota 도 한 건만 generated=False 로 격리하고
    예외를 올리지 않아 배치·잡이 모두 살아남는다.
    """
    prompt = _build_prompt(template, api)

    async def _attempt(model_override: Optional[str]) -> Dict[str, Any]:
        # fast-fail: primary 가 느려도 짧게 끊고 폴백으로 넘어가도록 per-call override.
        parsed, _ = await generate_json_with_retry(
            ctx.gemini,
            prompt,
            temperature=0.2,
            response_schema=_API_SPEC_FILL_SCHEMA,
            model=model_override,
            timeout=_AUTOFILL_LLM_TIMEOUT,
            max_retries=_AUTOFILL_LLM_MAX_RETRIES,
        )
        return parsed

    parsed: Optional[Dict[str, Any]] = None
    degraded = False
    try:
        # 1차: draft_model 지정 시 그 모델(경량 초안 노브), 아니면 구독 모델
        # (model_override=None → ctx.gemini 인스턴스 기본 = 구독 모델).
        parsed = await _attempt(draft_model)
    except GeminiError as primary_err:
        kind = getattr(primary_err, "kind", "?")
        if fallback_model:
            logger.warning(
                "api_spec_autofill: api=%s primary LLM 실패(kind=%s) — 폴백 모델(%s)로 재시도",
                api.id, kind, fallback_model,
            )
            try:
                parsed = await _attempt(fallback_model)
                degraded = True
            except GeminiError as fb_err:
                logger.warning(
                    "api_spec_autofill: api=%s 폴백 모델도 실패(kind=%s) — 건너뜀",
                    api.id, getattr(fb_err, "kind", "?"),
                )
                return FilledApiSpec(id=api.id, generated=False)
        else:
            logger.warning(
                "api_spec_autofill: api=%s primary LLM 실패(kind=%s), 폴백 없음 — 건너뜀",
                api.id, kind,
            )
            return FilledApiSpec(id=api.id, generated=False)
    except Exception:  # noqa: BLE001 — 그 외 한 API 실패가 배치 전체를 깨지 않게
        logger.exception("api_spec_autofill: api=%s LLM 호출 실패 — 건너뜀", api.id)
        return FilledApiSpec(id=api.id, generated=False)

    if not isinstance(parsed, dict) or not parsed:
        logger.warning("api_spec_autofill: api=%s LLM 결과 비어있음 — 건너뜀", api.id)
        return FilledApiSpec(id=api.id, generated=False)

    # 정규화 — status 범위 검증, 중복 status 제거, default auth 등. 그 다음 메타 부착.
    error_cases = normalize_error_cases(parsed.get("error_cases"))
    auth = normalize_auth(parsed.get("auth"))

    # error_cases 도 auth 도 모두 의미가 없으면 생성 실패로 간주.
    auth_meaningful = bool(auth.get("description") or auth.get("required_roles"))
    if not error_cases and not auth_meaningful:
        logger.warning(
            "api_spec_autofill: api=%s 생성 결과 무의미 — 건너뜀", api.id
        )
        return FilledApiSpec(id=api.id, generated=False)

    return FilledApiSpec(
        id=api.id,
        error_cases=_mark_ai_draft_error_cases(error_cases),
        auth=_mark_ai_draft_auth(auth),
        generated=True,
        degraded=degraded,
    )


# ─── 생성 / 저장 단계 (분리 가능) ────────────────────────────────


def resolve_fallback_model() -> Optional[str]:
    """primary(구독 모델) 실패 시 그 API 만 재시도할 경량 폴백 모델.

    settings.gemini_model_for_free (보통 gemini-2.5-flash-lite): 별도 무료 쿼터 +
    비-thinking 이라 빠르고, primary 가 quota/timeout 일 때 안전망이 된다. 설정을 못
    읽으면 None → 폴백 없이 격리만 (배치/잡은 여전히 안 깨짐).
    """
    try:
        from app.core.config import settings
        return getattr(settings, "gemini_model_for_free", None) or None
    except Exception:  # noqa: BLE001 — 설정 로드 실패해도 폴백 없이 진행
        return None


async def generate_api_spec_fills(
    ctx: PipelineContext,
    apis: List[ApiSpecInput],
    *,
    fallback_model: Optional[str] = None,
    emit_progress: bool = True,
) -> Dict[str, FilledApiSpec]:
    """LLM 생성 단계만 수행 — 저장 없음. {api_id: FilledApiSpec} 반환.

    [2026-06-10 병렬화 — 생성/저장 분리]
    design 파이프라인이 SPACK 확정 직후 이 함수를 DDD/Architecture LLM 과 **병렬**로
    돌릴 수 있도록 분리했다. 저장(merge_and_save_fills)은 design 의 wipe-and-redraw
    트랜잭션 **이후**여야 한다 — 먼저 쓰면 design 저장이 그 노드를 지워버린다.

    emit_progress=False 면 stage 마커를 내보내지 않는다. design 과 병렬로 돌 때
    autofill:generating:k/n 이 design:ddd/architecture 마커와 섞이면 FE 진행바가
    알 수 없는 stage(=idx 0, SPACK)로 후퇴해 보이기 때문.
    """
    targets, _skipped = _split_targets(apis)
    if not targets:
        return {}

    template = _load_prompt("api_spec_autofill.md")
    total = len(targets)
    # [progress] FE 진행바가 경과시간이 아닌 실제 작업량(완료 API 수) 기반으로
    # 차도록, 병렬 LLM 이 각각 끝날 때마다 "autofill:generating:k/n" stage emit.
    if emit_progress:
        await ctx.emit_stage(f"autofill:generating:0/{total}")
    _done = {"n": 0}

    # [2026-06-10] 초안 모델 노브 — 지정 시 1차 시도부터 경량 모델 사용.
    # 폴백과 같은 모델이면 폴백 재시도는 무의미하므로 비활성.
    draft_model = resolve_draft_model()
    if draft_model and draft_model == fallback_model:
        fallback_model = None

    # 각 대상 API 1 LLM 호출 — 독립적이라 병렬. 단 동시성을 _llm_concurrency()
    # 로 제한해 Gemini rate limit 회피 (API 24개를 한꺼번에 던지면 GeminiError).
    sem = asyncio.Semaphore(_llm_concurrency())

    async def _fill_one(a: ApiSpecInput) -> FilledApiSpec:
        async with sem:
            f = await call_api_spec_filler(
                ctx, template, a,
                fallback_model=fallback_model,
                draft_model=draft_model,
            )
        _done["n"] += 1
        if emit_progress:
            await ctx.emit_stage(f"autofill:generating:{_done['n']}/{total}")
        return f

    filled = await asyncio.gather(*(_fill_one(a) for a in targets))
    return {f.id: f for f in filled}


async def merge_and_save_fills(
    project_name: str,
    apis: List[ApiSpecInput],
    generated_map: Dict[str, FilledApiSpec],
    *,
    team_id: str = "",
) -> AutofillResult:
    """생성 결과 저장 + 원본 순서 병합 + meta 계산.

    저장은 query_repository.update_api_error_and_auth (단일 노드 SET, Wipe 미사용).
    한 API 의 저장 실패도 격리. design 병렬 모드에선 design 트랜잭션 커밋 후 호출.
    """
    # 지연 import — pipeline 이 모듈 import 시점에 neo4j 환경 변수 강제 evaluation 회피.
    from app.service import query_repository

    for fid, f in generated_map.items():
        if not f.generated:
            continue
        try:
            f.saved = await query_repository.update_api_error_and_auth(
                project_name, f.id, f.error_cases, f.auth,
                team_id=team_id,
            )
            if not f.saved:
                logger.warning(
                    "api_spec_autofill: api=%s 저장 대상 노드 없음 (id 불일치?)", fid
                )
        except Exception:  # noqa: BLE001 — 한 API 저장 실패가 배치를 깨지 않게
            logger.exception("api_spec_autofill: api=%s 저장 실패", fid)
            f.saved = False

    # 원본 순서 유지하며 병합 — 대상은 생성 결과, 나머지는 기존 값 보존.
    out: List[FilledApiSpec] = []
    for a in apis:
        gen = generated_map.get(a.id)
        if gen is not None:
            out.append(gen)
        else:
            out.append(
                FilledApiSpec(
                    id=a.id,
                    error_cases=a.error_cases or [],
                    auth=a.auth or {},
                    generated=False,
                )
            )

    # targets 는 결정적 재계산 — generate 단계와 같은 _split_targets 기준.
    targets, _skipped = _split_targets(apis)
    generated_count = sum(1 for f in out if f.generated)
    saved_count = sum(1 for f in out if f.saved)
    degraded_count = sum(1 for f in out if f.degraded)
    # 대상이었으나 초안을 못 만든 수 (primary + 폴백 모두 실패/무의미). FE 가 0개
    # 생성 + failedCount>0 이면 "잠시 후 다시 시도 (AI 한도일 수 있어요)" 안내 가능.
    failed_count = len(targets) - generated_count
    return AutofillResult(
        apis=out,
        meta={
            "total": len(apis),
            "targetCount": len(targets),
            "skippedCount": len(apis) - len(targets),
            "generatedCount": generated_count,
            "savedCount": saved_count,
            "degradedCount": degraded_count,
            "failedCount": failed_count,
        },
    )


# ─── End-to-end orchestrator ────────────────────────────────────


async def run_api_spec_autofill_pipeline(
    ctx: PipelineContext, payload: AutofillInput
) -> AutofillResult:
    """
    split → (빈 것만) N개 LLM 병렬 → 메타 부착 → 단일 노드 부분 저장 → 병합.

    standalone 경로("AI로 채우기" 버튼 / autofill_api_specs_job)용 — 생성과 저장을
    연속 실행. design 파이프라인은 generate_api_spec_fills / merge_and_save_fills 를
    직접 호출해 DDD/Architecture LLM 과 생성을 병렬화한다.
    """
    apis = payload.apis or []
    logger.info(
        "api_spec_autofill start: project=%s total=%d key=%s",
        payload.project_name,
        len(apis),
        ctx.idempotency_key,
    )

    generated_map = await generate_api_spec_fills(
        ctx, apis, fallback_model=resolve_fallback_model(), emit_progress=True,
    )
    await ctx.emit_stage("autofill:saving")
    return await merge_and_save_fills(
        payload.project_name, apis, generated_map, team_id=payload.team_id,
    )
