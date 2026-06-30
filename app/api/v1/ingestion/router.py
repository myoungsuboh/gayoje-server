"""Ingestion 도메인 라우터 — 수집 트리거/소스 조회 (INGEST-E13).

GET  /api/v1/ingestion/sources — 등록된 소스 어댑터 목록.
POST /api/v1/ingestion/run     — 지정 소스 1회 수집(fetch→정규화→저장).

⚠️ 현재는 요청 스코프 동기 실행(PoC/소규모). 대량/스케줄 수집은 arq 잡으로
   오프로드 예정(INGEST-E1-T3). 라이브 수집엔 DATA_GO_KR_SERVICE_KEY 필요.
레이어 규약: router → service(ingest_records) → repository(upsert). raw fetch 는 adapter.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.ingestion.registry import (
    all_adapters,
    get_adapter,
    list_adapter_keys,
)
from app.api.v1.ingestion.schema import (
    IngestRunRequest,
    IngestRunResponse,
    SourceInfo,
)
from app.api.v1.ingestion.service import ingest_records
from app.common.errors import NotFoundError, ServiceUnavailableError
from app.core.config import settings
from app.infra.db import get_session

router = APIRouter(prefix="/ingestion", tags=["Ingestion"])


@router.get("/sources", response_model=list[SourceInfo])
async def list_sources() -> list[SourceInfo]:
    return [
        SourceInfo(source_key=a.SOURCE_KEY, source_system=a.SOURCE_SYSTEM)
        for a in all_adapters()
    ]


@router.post("/run", response_model=IngestRunResponse)
async def run_ingest(
    req: IngestRunRequest,
    session: AsyncSession = Depends(get_session),
) -> IngestRunResponse:
    try:
        adapter = get_adapter(req.source)
    except KeyError:
        raise NotFoundError(
            f"알 수 없는 소스: {req.source}",
            detail={"available": list_adapter_keys()},
        )

    keys = settings.data_go_kr_service_keys
    if not keys:
        raise ServiceUnavailableError(
            "공공데이터 서비스키(DATA_GO_KR_SERVICE_KEY)가 미설정이라 수집할 수 없습니다."
        )

    records = await adapter.fetch_raw(
        keys[0], num_of_rows=req.num_of_rows, page_no=req.page_no
    )
    counts = await ingest_records(session, adapter, records)
    await session.commit()
    return IngestRunResponse(source=req.source, fetched=len(records), counts=counts)
