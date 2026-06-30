"""
Notion API client — Internal Integration token 검증 + 페이지 조회 (Phase 2 예정).

[배경 — 2026-05-18 pivot]
원래 Public OAuth 로 구현했으나 (notion_oauth.py) 노션이 셀프서비스 등록을 막아
실질적으로 사용 불가. Internal Integration token 입력 방식으로 전환.

사용자가 노션 워크스페이스에서 Internal Integration 만들고 secret token (ntn_*)
발급 후 Harness 에 직접 붙여넣기.

[API 호출]
- Authorization: Bearer {token}
- Notion-Version: 2022-06-28

[현재 모듈에서 제공]
- get_me(token) : 토큰 유효성 검증 + workspace 정보 조회 (저장 직전 검증)

Phase 2 예정:
- search_pages(token, query) : 페이지 검색
- get_page_blocks(token, page_id) : 페이지 → blocks 변환
- blocks_to_markdown(blocks) : 회의록 markdown 변환
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


# ===== 예외 =====


class NotionTokenInvalid(Exception):
    """토큰 검증 실패 — 잘못된 토큰 / 만료 / 권한 부족."""


class NotionAPIError(Exception):
    """노션 API 호출 일반 실패."""


# ===== API: 토큰 검증 =====


async def get_me(token: str) -> Dict[str, Any]:
    """
    노션 GET /v1/users/me 호출 — 토큰 유효성 + workspace 정보 확인.

    응답 예 (Internal Integration):
        {
            "object": "user",
            "id": "abc-123",
            "type": "bot",
            "bot": {
                "owner": { "type": "workspace", "workspace": true },
                "workspace_name": "내 워크스페이스"
            }
        }

    Raises:
        NotionTokenInvalid: 401/403 (잘못된 토큰)
        NotionAPIError: 그 외 (네트워크 / 5xx)
    """
    if not token or not token.strip():
        raise NotionTokenInvalid("token_empty")

    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Notion-Version": NOTION_VERSION,
    }
    url = f"{NOTION_API_BASE}/users/me"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        logger.warning("notion get_me network error: %s", e)
        raise NotionAPIError("notion_network_error") from e

    if res.status_code in (401, 403):
        # 토큰 잘못됨 — 사용자에게 명확히 안내
        body = res.text[:200]
        logger.info("notion token invalid: status=%s body=%s", res.status_code, body)
        raise NotionTokenInvalid(f"notion_unauthorized_{res.status_code}")

    if res.status_code != 200:
        logger.warning(
            "notion get_me unexpected status=%s body=%s",
            res.status_code, res.text[:200],
        )
        raise NotionAPIError(f"notion_status_{res.status_code}")

    try:
        return res.json()
    except Exception as e:  # noqa: BLE001
        raise NotionAPIError("notion_invalid_json") from e


def extract_workspace_info(me_response: Dict[str, Any]) -> Dict[str, str]:
    """get_me 응답에서 저장용 정보 추출.

    Returns:
        {
            "bot_id": str,        # 노션 bot 식별자
            "workspace_id": str,  # 노션이 직접 안 주므로 '' (필요시 owner 분석)
            "workspace_name": str # 사용자 노출용 라벨
        }
    """
    bot = (me_response or {}).get("bot") or {}
    return {
        "bot_id": (me_response or {}).get("id") or "",
        "workspace_id": "",  # Internal Integration 응답엔 workspace_id 없음
        "workspace_name": bot.get("workspace_name") or "노션 워크스페이스",
    }
