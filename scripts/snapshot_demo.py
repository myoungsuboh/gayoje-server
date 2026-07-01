"""데모용 정적 스냅샷 — gayoje_dev.db 를 API와 동일 포맷(camelCase)으로 직렬화.

공개 배포(백엔드 없이 폰에서 열리는 데모)용. GET /festivals(목록) + GET /festivals/{id}(상세)
응답을 그대로 재현해 gayoje-client/public/demo/festivals.json 으로 저장.
    실행: PYTHONPATH=. python scripts/snapshot_demo.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import app.api.v1.festivals.models  # noqa: F401 — 테이블 등록
from app.api.v1.festivals.schema import (
    FestivalDetail,
    FestivalListItem,
    FestivalListResponse,
)
from app.api.v1.festivals.service import get_festival, list_festivals
from app.infra import db

OUT = Path("C:/project/gayoje-client/public/demo/festivals.json")


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    async with db.session_scope() as s:
        rows, total = await list_festivals(s, limit=200, offset=0)
        list_resp = FestivalListResponse(
            items=[FestivalListItem.model_validate(r) for r in rows],
            total=total, limit=200, offset=0,
        )
        detail: dict[str, dict] = {}
        for r in rows:
            ev = await get_festival(s, r.id)
            detail[str(r.id)] = FestivalDetail.model_validate(ev).model_dump(
                by_alias=True, mode="json"
            )
    payload = {
        "list": list_resp.model_dump(by_alias=True, mode="json"),
        "detail": detail,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}  (목록 {len(rows)}건, 상세 {len(detail)}건)")
    await db.dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
