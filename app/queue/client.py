"""
arq client — FastAPI 프로세스에서 job 을 enqueue / status 조회.

lifespan 에서 pool 을 1회 생성하고 라우트에서 재사용한다.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from arq import create_pool
from arq.connections import ArqRedis
from arq.jobs import Job, JobStatus

from app.queue.settings import FREE_QUEUE_NAME, PRO_QUEUE_NAME, QUEUE_NAME, redis_settings

logger = logging.getLogger(__name__)

_pool: Optional[ArqRedis] = None


async def get_pool() -> ArqRedis:
    """프로세스 수명 동안 단일 ArqRedis pool 재사용."""
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings(), default_queue_name=QUEUE_NAME)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close(close_connection_pool=True)
        _pool = None


async def _select_queue_for_user(user_email: Optional[str]) -> str:
    """
    [2026-05 등급별 큐 분리] user_email 로 tier 조회 후 적절한 큐 결정.

    - paid (pro/pro_plus/pro_max) → PRO_QUEUE_NAME (전용 워커 SLA 보장)
    - free / unknown / None       → FREE_QUEUE_NAME

    user_email 미제공 (system job, e.g. analyze_lineage) → FREE 큐 fallback —
    Pro 전용 워커 자원을 사용자 작업에만 할당. (운영에서 워커 분리 안 하면
    환경변수로 양쪽 다 한 워커가 처리하게 통합 가능.)

    조회 실패 시 보수적으로 FREE — 운영 안정성 우선 (Pro 큐 누설 방지보다 안전).
    """
    if not user_email:
        return FREE_QUEUE_NAME
    try:
        # 지연 import — usage_repository 가 settings/neo4j 를 import 하므로 순환 회피.
        from app.service import usage_repository
        from app.core.subscription import PAID_SUBSCRIPTIONS
        usage = await usage_repository.get_usage(user_email)
        if usage and usage.subscription_type in PAID_SUBSCRIPTIONS:
            return PRO_QUEUE_NAME
    except Exception as e:  # noqa: BLE001
        # Neo4j 일시 장애 / lookup 실패 — Pro 큐 보호 우선. Free 큐로 fallback.
        logger.warning(
            "queue routing: get_usage failed for %s, fallback to FREE queue: %s",
            user_email, e,
        )
    return FREE_QUEUE_NAME


async def _enqueue(function: str, task_id: str, **kwargs: Any) -> str:
    """
    공통 enqueue 헬퍼. task_id 를 _job_id 로 사용 → 같은 task_id 재호출 시 dedup.

    [2026-05] user_email 이 kwargs 에 있으면 등급별 큐로 라우팅. 없으면 default
    queue (FREE 또는 환경 설정 QUEUE_NAME).

    [2026-06 동시성 제한] 무거운 사용자 job(concurrency.HEAVY_JOBS)은 계정당 동시
    실행 수를 제한 — 초과 시 HTTPException(429). job 종료 시 worker 가 슬롯 해제.
    """
    from fastapi import HTTPException, status as http_status
    from app.core import concurrency

    pool = await get_pool()
    # 큐 결정 — user_email 우선 (kwargs 에 있으면), 없으면 default QUEUE_NAME
    user_email = kwargs.get("user_email")

    # 동시성 게이트 — heavy job + 인증 사용자만. enqueue 전에 체크해 워커 자원 낭비 0.
    if function in concurrency.HEAVY_JOBS and user_email:
        if not await concurrency.try_acquire_slot(pool, user_email, task_id):
            raise HTTPException(
                status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "CONCURRENCY_LIMIT",
                    "message": "현재 진행 중인 작업이 있어요. 완료된 후 다시 시도해 주세요.",
                    "limit": concurrency.DEFAULT_CONCURRENCY_LIMIT,
                },
            )

    # [2026-06 멀티디바이스 이중작업] 같은 프로젝트의 master 쓰기 잡이 이미 inflight
    # 면 409 — 웹 배치 중 모바일에서 또 시작하는 케이스를 enqueue 시점에 명확히 안내.
    # 데이터 자체는 워커의 master_lock 이 지키므로 이건 UX 가드. team 프로젝트는
    # scoped key 로 팀 멤버 간 충돌도 잡힘.
    if function in concurrency.MASTER_WRITE_JOBS and user_email:
        from app.core.project_scope import scoped_project
        project_key = scoped_project(
            kwargs.get("project_name") or "", kwargs.get("team_id") or None,
        )
        if not await concurrency.try_acquire_project(pool, project_key, task_id):
            # 위에서 잡은 계정 슬롯 반납 — 누수 시 stale 회수(25분)까지 사용자가 막힘.
            await concurrency.release_slot(pool, user_email, task_id)
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail={
                    "code": "PROJECT_BUSY",
                    "message": "이 프로젝트는 다른 기기 또는 탭에서 처리 중이에요. "
                               "진행 중인 작업이 끝난 뒤 다시 시도해 주세요.",
                    "project_name": kwargs.get("project_name") or "",
                },
            )

    queue = await _select_queue_for_user(user_email) if user_email else QUEUE_NAME
    job = await pool.enqueue_job(function, _job_id=task_id, _queue_name=queue, **kwargs)
    if job is None:
        logger.info(
            "enqueue %s: duplicate task_id=%s (queue=%s) — returning existing",
            function, task_id, queue,
        )
        return task_id
    logger.info("enqueue %s: task_id=%s queue=%s", function, task_id, queue)
    return job.job_id


async def enqueue_cps(
    *,
    task_id: str,
    project_name: str,
    version: str,
    date: str,
    meeting_content: str,
    previous_cps_id: str | None,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> str:
    """cps_pipeline_job 을 큐에 등록 (CPS 단독). user_email 은 quota 토큰 누적용."""
    return await _enqueue(
        "cps_pipeline_job",
        task_id,
        project_name=project_name,
        version=version,
        date=date,
        meeting_content=meeting_content,
        previous_cps_id=previous_cps_id,
        user_email=user_email,
        team_id=team_id,
    )


async def enqueue_post_meeting(
    *,
    task_id: str,
    project_name: str,
    version: str,
    date: str,
    meeting_content: str,
    previous_cps_id: str | None,
    previous_prd_id: str | None,
    user_email: Optional[str] = None,
    next_meeting: Optional[Dict[str, Any]] = None,
    team_id: str = "",
) -> str:
    """post_meeting_pipeline_job (CPS + PRD 체이닝) 을 큐에 등록.

    user_email: quota 토큰 누적용. 라우트에서 current_user.email 전달.
    next_meeting: batch 의 다음 항목 {content, version, previous_cps_id?} — 주어지면
      job 이 merge 단계에서 그 extract 를 선반입(prefetch)해 K+1 처리를 가속. None 이면
      기존 동작 (선반입 없음).
    """
    return await _enqueue(
        "post_meeting_pipeline_job",
        task_id,
        project_name=project_name,
        version=version,
        date=date,
        meeting_content=meeting_content,
        previous_cps_id=previous_cps_id,
        previous_prd_id=previous_prd_id,
        user_email=user_email,
        next_meeting=next_meeting,
        team_id=team_id,
    )


async def enqueue_prefetch_extract(
    *,
    task_id: str,
    project_name: str,
    version: str,
    meeting_content: str,
    previous_cps_id: str | None = None,
    previous_prd_id: str | None = None,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> str:
    """prefetch_extract_job 등록 (batch 파이프라이닝 — 다음 버전 extract 선계산).

    task_id 는 (project, version, content) 결정적으로 — 같은 prefetch 중복 enqueue 를
    arq 가 dedup. user_email 로 본 작업과 같은 큐 tier 로 라우팅.
    """
    return await _enqueue(
        "prefetch_extract_job",
        task_id,
        project_name=project_name,
        version=version,
        meeting_content=meeting_content,
        previous_cps_id=previous_cps_id,
        previous_prd_id=previous_prd_id,
        user_email=user_email,
        team_id=team_id,
    )


async def enqueue_prd(
    *,
    task_id: str,
    project_name: str,
    version: str,
    cps_graph: Dict[str, Any],
    previous_prd_id: str | None,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> str:
    """prd_pipeline_job (PRD 단독 — cps_graph 직접 입력) 을 큐에 등록."""
    return await _enqueue(
        "prd_pipeline_job",
        task_id,
        project_name=project_name,
        version=version,
        cps_graph=cps_graph,
        previous_prd_id=previous_prd_id,
        user_email=user_email,
        team_id=team_id,
    )


async def enqueue_design(
    *, task_id: str, project_name: str, user_email: Optional[str] = None,
    team_id: str = "",
) -> str:
    """design_pipeline_job (createDesign — Spack/DDD/Architecture) 을 큐에 등록."""
    return await _enqueue(
        "design_pipeline_job",
        task_id,
        project_name=project_name,
        user_email=user_email,
        team_id=team_id,
    )


async def enqueue_autofill_api_specs(
    *, task_id: str, project_name: str, user_email: Optional[str] = None,
    team_id: str = "",
) -> str:
    """autofill_api_specs_job (API error_cases/auth AI 초안 병렬 생성) 을 큐에 등록.

    team_id: 프로젝트 게이트(scoped key)와 워커의 SPACK 조회 스코프 일치용 —
    이전엔 안 넘겨 팀 프로젝트에서 빈 그래프를 읽는 버그도 있었다.
    """
    return await _enqueue(
        "autofill_api_specs_job",
        task_id,
        project_name=project_name,
        user_email=user_email,
        team_id=team_id,
    )


async def enqueue_recommend_skills(
    *,
    task_id: str,
    project_name: str,
    skill_catalog: List[Dict[str, Any]],
    allowed_categories: List[str],
    user_email: Optional[str] = None,
    team_id: str = "",
) -> str:
    """recommend_skills_job (recommendSkillsByAI) 을 큐에 등록."""
    return await _enqueue(
        "recommend_skills_job",
        task_id,
        project_name=project_name,
        skill_catalog=skill_catalog,
        allowed_categories=allowed_categories,
        user_email=user_email,
        team_id=team_id,
    )


async def enqueue_run_lint(
    *,
    task_id: str,
    project_name: str,
    github_url: str,
    user_token: Optional[str] = None,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> str:
    """run_lint_job (runLint) 을 큐에 등록.

    user_token: 호출자(인증된 사용자)의 GitHub OAuth access_token. private repo
                접근 가능. 큐 직렬화를 위해 평문으로 함께 enqueue 됨 — 큐(Redis) 에
                일시 보관되므로 환경에서 Redis 자체의 접근 통제 가 전제.
    user_email: quota 토큰 누적용.
    """
    return await _enqueue(
        "run_lint_job",
        task_id,
        project_name=project_name,
        github_url=github_url,
        user_token=user_token,
        user_email=user_email,
        team_id=team_id,
    )


async def enqueue_generate_fix_spec(
    *,
    task_id: str,
    project_name: str,
    github_url: str,
    lint_result: Dict[str, Any],
    user_email: Optional[str] = None,
) -> str:
    """generate_fix_spec_job (generateFixSpec) 을 큐에 등록."""
    return await _enqueue(
        "generate_fix_spec_job",
        task_id,
        project_name=project_name,
        github_url=github_url,
        lint_result=lint_result,
        user_email=user_email,
    )


async def enqueue_analyze_lineage(
    *, task_id: str, project_name: str, user_token: Optional[str] = None,
    team_id: str = "",
) -> str:
    """analyze_lineage_job (analyzeLineage — deterministic 매칭, LLM 미사용).

    user_token: lint 와 동일 — 사용자 OAuth token 으로 private repo tree fetch.
    """
    return await _enqueue(
        "analyze_lineage_job",
        task_id,
        project_name=project_name,
        user_token=user_token,
        team_id=team_id,
    )


async def enqueue_delete_meeting(
    *,
    task_id: str,
    project_name: str,
    version: str,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> str:
    """delete_meeting_job (deleteMeeting + Master rebuild).

    user_email: quota 토큰 누적용 — delete pipeline 도 Master rebuild 시 LLM 호출.
    """
    return await _enqueue(
        "delete_meeting_job",
        task_id,
        project_name=project_name,
        version=version,
        user_email=user_email,
        team_id=team_id,
    )


async def enqueue_github_onboard(
    *,
    task_id: str,
    project_name: str,
    github_url: str,
    user_token: Optional[str] = None,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> str:
    """github_onboard_job (Vibe Coding entry — GitHub URL → V1 + CPS) 을 큐에 등록.

    user_token: 호출자의 GitHub OAuth access_token. private repo 접근 + rate limit
                확장 (5000/hr) 용. None 이면 anonymous (60/hr) 또는 환경 GITHUB_TOKEN.
    user_email: quota 토큰 누적 + 등급별 Gemini 모델 분기.
    """
    return await _enqueue(
        "github_onboard_job",
        task_id,
        project_name=project_name,
        github_url=github_url,
        user_token=user_token,
        user_email=user_email,
        team_id=team_id,
    )


async def enqueue_cleanup_master_prd(
    *,
    task_id: str,
    project_name: str,
    dry_run: bool = True,
    user_email: Optional[str] = None,
    team_id: str = "",
) -> str:
    """cleanup_master_prd_job (V1~Vn 누적 PRD dedupe) — arq 큐 등록.

    [2026-05-26] post_meeting_pipeline_job 끝에서 threshold 검출 시 자동 호출.
    수동 호출 라우트는 admin/debug 전용 — 일반 사용자에게 노출 안 함.
    """
    return await _enqueue(
        "cleanup_master_prd_job",
        task_id,
        project_name=project_name,
        dry_run=dry_run,
        user_email=user_email,
        team_id=team_id,
    )


async def enqueue_create_md(
    *, task_id: str, project_name: str, user_email: Optional[str] = None
) -> str:
    """create_md_job (createMD — Spack/DDD/Arch → MD 3종). user_email 은 quota 토큰 누적용."""
    return await _enqueue(
        "create_md_job",
        task_id,
        project_name=project_name,
        user_email=user_email,
    )


async def get_queue_stats() -> Dict[str, Any]:
    """
    [2026-05 운영 가시성 #2] 큐 깊이 + 헬스 체크 (admin 전용).

    arq 가 사용하는 Redis 키 패턴:
      - {queue_name}              : pending jobs sorted-set (zcard 로 개수).
      - arq:health-check:{queue}  : 워커 health bytes (없으면 워커 미가동).
      - arq:in-progress:*         : 처리 중 (전역, 큐 별 분리 안 됨).

    Returns:
      {
        "queues": {
          "<queue_name>": {
            "pending": int,
            "health": <str | null>,    # 워커가 마지막으로 publish 한 health 라인
          },
          ...
        },
        "default": "<QUEUE_NAME>",
      }

    [실패 모드]
    Redis 일시 장애 → 키별로 None 채워 부분 응답. 전체 500 회피.
    """
    pool = await get_pool()
    # 분리됐을 수 있으니 PRO / FREE / default 셋 다 표시.
    targets = {QUEUE_NAME, PRO_QUEUE_NAME, FREE_QUEUE_NAME}
    out: Dict[str, Any] = {"queues": {}, "default": QUEUE_NAME}

    for q in sorted(targets):
        info: Dict[str, Any] = {"pending": None, "health": None}
        try:
            # arq pending jobs sorted-set 키는 큐 이름 그대로.
            info["pending"] = int(await pool.zcard(q))
        except Exception as e:  # noqa: BLE001
            logger.warning("queue stats: zcard failed for %s: %s", q, e)
        try:
            raw = await pool.get(f"arq:health-check:{q}")
            if raw is not None:
                info["health"] = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("queue stats: health get failed for %s: %s", q, e)
        out["queues"][q] = info

    return out


async def get_job_status(task_id: str) -> Dict[str, Any]:
    """
    arq Job status + 결과 조회.

    [멀티테넌트 격리 — 중요]
    응답에 `project_name` 을 포함한다. 라우트는 이걸로 ownership 검증 후 결과를
    노출해야 한다 — 그러지 않으면 다른 사용자의 task_id 만 알면 결과 조회 가능
    (Sprint 8 P0 픽스).

    Returns:
      {
        "task_id": ...,
        "project_name": <str | None>,     # ← 신규: ownership 검증용
        "status": "queued" | "in_progress" | "complete" | "not_found",
        "result": <dict | None>,
        "error": <str | None>,
        "enqueue_time": <epoch ms | None>,
        "finish_time": <epoch ms | None>,
      }

    project_name 회수 정책:
    - 모든 enqueue_* 함수가 kwargs 로 project_name 을 넘기므로 job.info().kwargs
      에서 그대로 추출.
    - info() 가 None 이거나 kwargs 에 키 없으면 None — 라우트는 이 경우 404 처리
      (정보 누설 방지: ownership 검증 실패와 not_found 를 같은 형태로 응답).
    """
    pool = await get_pool()
    job = Job(task_id, redis=pool, _queue_name=QUEUE_NAME)
    status = await job.status()

    if status is JobStatus.not_found:
        return {"task_id": task_id, "project_name": None, "status": "not_found"}

    info_dict: Dict[str, Any] = {
        "task_id": task_id,
        "project_name": None,
        "status": status.value if hasattr(status, "value") else str(status),
        "result": None,
        "error": None,
        "stage": None,   # [C — 2026-05] worker 가 Redis 에 저장한 진행 단계
    }

    # 진행 단계 (cps_running / prd_running / done) — worker 가 _set_job_stage 로 기록.
    try:
        stage_raw = await pool.get(f"harness:job:{task_id}:stage")
        if stage_raw is not None:
            info_dict["stage"] = (
                stage_raw.decode() if isinstance(stage_raw, (bytes, bytearray)) else str(stage_raw)
            )
    except Exception:  # noqa: BLE001 — stage 누락이 status 응답 자체를 막으면 안 됨
        pass

    try:
        info = await job.info()
        if info is not None:
            # enqueue 시점의 kwargs 에서 project_name 회수 — 모든 enqueue_* 함수가 함께 전달.
            kwargs = info.kwargs or {}
            info_dict["project_name"] = kwargs.get("project_name")
            info_dict["enqueue_time"] = (
                int(info.enqueue_time.timestamp() * 1000) if info.enqueue_time else None
            )
            info_dict["finish_time"] = (
                int(info.finish_time.timestamp() * 1000) if info.finish_time else None
            )
    except Exception:  # noqa: BLE001
        # info() 는 job 메타가 expire 됐을 때 실패 가능 — project_name 은 None 으로 남음.
        pass

    if status is JobStatus.complete:
        try:
            result = await job.result(timeout=0.5)
            info_dict["result"] = result
        except Exception as e:  # noqa: BLE001
            info_dict["error"] = str(e)

    return info_dict


async def flush_queues() -> Dict[str, Any]:
    """
    모든 큐의 pending jobs + in-progress 마커 + stage 키를 제거한다.

    [제거 대상]
      - {queue_name}              : pending sorted-set (ZRANGE 후 arq:job:{id} 해시도 삭제)
      - arq:in-progress:{queue}   : in-progress sorted-set (워커 장애 시 잔류)
      - harness:job:*:stage       : 단계 추적 키 (SCAN + DEL)

    [주의]
      - 워커가 현재 실행 중인 job 은 실행 자체를 중단하지 않는다.
        (worker 프로세스는 살아 있음; Redis 마커만 지움 → status 조회 시 not_found 로 전환)
      - 운영 환경에서는 worker 컨테이너 재시작 후 호출 권장.

    Returns:
      {
        "flushed_queues": [str],
        "pending_removed": int,   # pending sorted-set 에서 제거한 job 수
        "inprogress_removed": int,
        "stage_keys_removed": int,
        "job_hash_removed": int,  # arq:job:{id} 해시 제거 수
      }
    """
    pool = await get_pool()
    targets = {QUEUE_NAME, PRO_QUEUE_NAME, FREE_QUEUE_NAME}

    pending_removed = 0
    inprogress_removed = 0
    job_hash_removed = 0

    for q in targets:
        try:
            job_ids: List[bytes] = await pool.zrange(q, 0, -1)
            if job_ids:
                await pool.delete(q)
                pending_removed += len(job_ids)
                # arq:job:{id} 해시 일괄 삭제
                hash_keys = [f"arq:job:{jid.decode() if isinstance(jid, bytes) else jid}" for jid in job_ids]
                if hash_keys:
                    deleted = await pool.delete(*hash_keys)
                    job_hash_removed += deleted
        except Exception as e:  # noqa: BLE001
            logger.warning("flush_queues: pending flush failed for %s: %s", q, e)

        try:
            ip_key = f"arq:in-progress:{q}"
            ip_count = await pool.zcard(ip_key)
            if ip_count:
                await pool.delete(ip_key)
                inprogress_removed += ip_count
        except Exception as e:  # noqa: BLE001
            logger.warning("flush_queues: in-progress flush failed for %s: %s", q, e)

    # stage 키 SCAN 삭제 (harness:job:*:stage)
    stage_keys_removed = 0
    try:
        cursor = 0
        while True:
            cursor, keys = await pool.scan(cursor, match="harness:job:*:stage", count=200)
            if keys:
                await pool.delete(*keys)
                stage_keys_removed += len(keys)
            if cursor == 0:
                break
    except Exception as e:  # noqa: BLE001
        logger.warning("flush_queues: stage key scan failed: %s", e)

    logger.info(
        "flush_queues: pending=%d inprogress=%d job_hashes=%d stage_keys=%d",
        pending_removed, inprogress_removed, job_hash_removed, stage_keys_removed,
    )
    return {
        "flushed_queues": sorted(targets),
        "pending_removed": pending_removed,
        "inprogress_removed": inprogress_removed,
        "stage_keys_removed": stage_keys_removed,
        "job_hash_removed": job_hash_removed,
    }
