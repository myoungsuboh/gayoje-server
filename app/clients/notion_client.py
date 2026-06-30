"""
Notion REST API 얇은 async 래퍼.

[설계 원칙]
- OAuth access_token 은 호출자가 주입 (user_repository.get_notion_info 로 복호화된 값).
- Stateless — 매 호출마다 httpx client 생성. GitHub 클라이언트 패턴과 동일.
- Notion-Version 헤더 통일 ('2022-06-28' — notion_oauth.py 와 동일).
- 401 / 429 / 5xx 명시적 예외 → 라우트가 사용자에게 친절한 메시지 변환.

[사용 예]
    info = await users.get_notion_info(email)  # {access_token, workspace_id, ...}
    client = NotionClient(user_token=info["access_token"])
    result = await client.search_pages(query="회의록", page_size=20)
    blocks = await client.get_page_blocks(page_id)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"

# 페이지 검색 안전 한도 — Notion 권장 100, 보수적으로 50.
_DEFAULT_PAGE_SIZE = 25
_MAX_PAGE_SIZE = 100

# 블록 재귀 안전 한도 — toggle 안의 toggle 처럼 깊어질 수 있어 차단.
_MAX_BLOCK_DEPTH = 8
# 한 페이지의 총 블록 수 상한 — 비정상적으로 거대한 페이지로 메모리 폭발 방지.
_MAX_BLOCKS_PER_PAGE = 5000
# archive 동시 처리 한도 — Notion 공식 rate limit 평균 ~3 req/s. 보수적 3.
_ARCHIVE_CONCURRENCY = 3


class NotionError(RuntimeError):
    """Notion API 비복구 실패."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class NotionUnauthorized(NotionError):
    """401 — 토큰 만료/취소. 라우트가 unlink 안내."""

    def __init__(self, message: str = "notion_unauthorized") -> None:
        super().__init__(message, status=401)


class NotionRateLimited(NotionError):
    """429 — Notion rate limit. retry_after_seconds 힌트 제공."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


def _headers(user_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {user_token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
        "User-Agent": "harness-backend/1.0",
    }


class NotionClient:
    """
    Notion REST 얇은 래퍼. user_token (OAuth access_token) 필수.

    호출 단위:
    - search_pages: workspace 전체 페이지 검색 (필터 page only)
    - get_page: 페이지 메타 (제목/icon/last_edited_time)
    - get_page_blocks: 블록 트리 (children 재귀 + 페이지네이션 자동 따라가기)
    """

    def __init__(self, *, user_token: str, timeout: float = 15.0) -> None:
        if not user_token:
            raise NotionError("user_token is required", status=400)
        self._user_token = user_token
        self._timeout = timeout

    # ===== Public API =====

    async def search_pages(
        self,
        *,
        query: str = "",
        page_size: int = _DEFAULT_PAGE_SIZE,
        start_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        POST /v1/search — 워크스페이스에서 페이지 검색.

        filter: page 만 (database 제외). sort: last_edited_time desc.

        Returns: Notion 원본 페이로드
            {
                "results": [ { "id", "object": "page", "properties", "icon",
                              "last_edited_time", "url", ... }, ... ],
                "has_more": bool,
                "next_cursor": str | None,
            }
        """
        page_size = max(1, min(page_size, _MAX_PAGE_SIZE))
        body: Dict[str, Any] = {
            "filter": {"value": "page", "property": "object"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": page_size,
        }
        # 빈 query 면 워크스페이스의 최근 페이지 목록이 됨 (Notion 기본 동작).
        if query:
            body["query"] = query
        if start_cursor:
            body["start_cursor"] = start_cursor
        return await self._request(
            "POST", f"{_API_BASE}/search", json=body, context="search_pages"
        )

    async def get_page(self, page_id: str) -> Dict[str, Any]:
        """GET /v1/pages/{id} — 페이지 메타데이터 (properties 포함)."""
        pid = _normalize_id(page_id)
        return await self._request(
            "GET", f"{_API_BASE}/pages/{pid}", context=f"get_page {pid[:8]}"
        )

    async def get_page_blocks(self, page_id: str) -> List[Dict[str, Any]]:
        """
        페이지의 전체 블록 트리 반환 — children 재귀 + page_size 페이지네이션 자동.

        각 블록은 Notion 원본 형식을 유지하되, `has_children=True` 인 블록은 동기 fetch
        후 `_children: List[block]` 키 주입 (Notion 응답에는 없는 우리 확장).

        depth/total 안전 한도 초과 시 NotionError(413).
        """
        pid = _normalize_id(page_id)
        counter = {"total": 0}
        return await self._fetch_children(pid, depth=0, counter=counter)

    # ===== Write (export) =====

    async def create_page(
        self,
        *,
        parent_page_id: str,
        title: str,
        icon_emoji: Optional[str] = None,
        children: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        POST /v1/pages — parent_page_id 아래 새 페이지 생성.

        children 은 페이지 생성 시 최대 100개 (Notion 제약) — 초과분은 호출자가
        append_block_children 로 추가. parent/title/icon 은 우리 export 용 고정 형식.
        Returns: 생성된 페이지 객체 ({id, url, ...}).
        """
        body: Dict[str, Any] = {
            "parent": {"page_id": parent_page_id},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
            "children": (children or [])[:100],
        }
        if icon_emoji:
            body["icon"] = {"type": "emoji", "emoji": icon_emoji}
        return await self._request(
            "POST", f"{_API_BASE}/pages", json=body, context="create_page"
        )

    async def append_block_children(
        self, *, block_id: str, children: List[Dict[str, Any]]
    ) -> None:
        """PATCH /v1/blocks/{id}/children — 100개씩 분할 append (Notion 한 콜 100 제한).

        [2026-06-05 버그픽스] Notion 의 'append block children' 은 PATCH 다.
        이전엔 POST 라 405 로 매번 실패 → 자식 페이지가 빈 채로 생성되고 export 는
        '실패' 로 떴다(허브는 POST /pages 로 정상이라 더 헷갈렸음).
        """
        # [2026-06] 가중치 분할 — table 은 table_row 자식도 100 제한에 합산되므로
        # 단순 100개 슬라이스로는 큰 표가 들어간 요청이 한도를 초과할 수 있다.
        from app.core.markdown_to_notion_blocks import chunk_blocks_by_weight
        for chunk in chunk_blocks_by_weight(children or []):
            await self._request(
                "PATCH",
                f"{_API_BASE}/blocks/{block_id}/children",
                json={"children": chunk},
                context=f"append {block_id[:8]} +{len(chunk)}",
            )

    async def archive_block_children(self, *, block_id: str) -> None:
        """
        블록의 직속(1-레벨) 자식 블록 전부 archive — 재공유 시 기존 내용 비우기.

        부모 블록을 archive 하면 하위 트리도 함께 사라지므로 재귀하지 않는다.
        페이지네이션으로 id 만 수집 후 동시 PATCH (Semaphore 로 Notion rate limit 존중).

        [2026-06-12 hang fix] 이전엔 자식 id 별로 직렬 PATCH — 큰 문서(수백~수천
        블록)의 두 번째 공유에서 archive 만 수십~수백 초 걸려 사용자는 "공유 중..."
        무한 로딩으로 체감. Notion API 가 ~3 req/s 라 동시 3개로 묶어도 안전하다.
        """
        child_ids: List[str] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = await self._request(
                "GET",
                f"{_API_BASE}/blocks/{block_id}/children",
                params=params,
                context=f"list-for-archive {block_id[:8]}",
            )
            for blk in data.get("results") or []:
                cid = blk.get("id")
                if cid:
                    child_ids.append(cid)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        if not child_ids:
            return
        sem = asyncio.Semaphore(_ARCHIVE_CONCURRENCY)

        async def _archive_one(cid: str) -> None:
            async with sem:
                await self._request(
                    "PATCH",
                    f"{_API_BASE}/blocks/{cid}",
                    json={"archived": True},
                    context=f"archive {cid[:8]}",
                )

        await asyncio.gather(*[_archive_one(cid) for cid in child_ids])

    # ===== 내부 =====

    async def _fetch_children(
        self, block_id: str, *, depth: int, counter: Dict[str, int]
    ) -> List[Dict[str, Any]]:
        if depth > _MAX_BLOCK_DEPTH:
            logger.warning(
                "notion blocks too deep — truncating at depth=%s block=%s",
                depth,
                block_id[:8],
            )
            return []
        results: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = await self._request(
                "GET",
                f"{_API_BASE}/blocks/{block_id}/children",
                params=params,
                context=f"blocks {block_id[:8]} d={depth}",
            )
            page_blocks = data.get("results") or []
            for block in page_blocks:
                counter["total"] += 1
                if counter["total"] > _MAX_BLOCKS_PER_PAGE:
                    logger.warning(
                        "notion page exceeds %s blocks — truncating",
                        _MAX_BLOCKS_PER_PAGE,
                    )
                    return results
                if block.get("has_children"):
                    child_id = block.get("id") or ""
                    if child_id:
                        block["_children"] = await self._fetch_children(
                            child_id, depth=depth + 1, counter=counter
                        )
                results.append(block)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return results

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        context: str,
    ) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.request(
                    method,
                    url,
                    headers=_headers(self._user_token),
                    json=json,
                    params=params,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                raise NotionError(f"notion_network_error ({context}): {e}") from e

        if resp.status_code == 401:
            raise NotionUnauthorized()
        if resp.status_code == 429:
            retry_after_raw = resp.headers.get("Retry-After")
            retry_after: Optional[float] = None
            if retry_after_raw:
                try:
                    retry_after = float(retry_after_raw)
                except ValueError:
                    retry_after = None
            raise NotionRateLimited(
                f"notion_rate_limited ({context})", retry_after=retry_after
            )
        if resp.status_code == 404:
            raise NotionError(
                f"notion_not_found ({context})", status=404
            )
        if resp.status_code >= 400:
            raise NotionError(
                f"notion_{resp.status_code} ({context}): {resp.text[:200]}",
                status=resp.status_code,
            )
        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001
            raise NotionError(
                f"notion_json_decode_failed ({context}): {e}", status=500
            ) from e


# ===== Public 헬퍼 =====


def _normalize_id(raw: str) -> str:
    """
    Notion 페이지 ID 정규화. 사용자가 URL 통째로 줘도 동작하게.

    허용 입력:
      - 'abcd1234abcd1234abcd1234abcd1234'  (32 hex)
      - 'abcd1234-abcd-1234-abcd-1234abcd1234' (UUID)
      - 'https://www.notion.so/.../My-Page-abcd1234abcd1234abcd1234abcd1234'
    """
    s = (raw or "").strip()
    if not s:
        raise NotionError("notion_page_id_empty", status=400)
    # UUID-with-dashes 또는 32 hex 연속 — 둘 다 매칭. URL 안에서도 추출.
    # 단순 `[0-9a-f]{32}` 는 dashed-id 의 dash 를 미리 제거하면 URL 다른 부분의
    # hex 와 경계가 섞여 잘못된 위치를 잡을 수 있어, dash-옵션 패턴으로 직접 매칭.
    import re

    m = re.search(
        r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})",
        s,
        re.IGNORECASE,
    )
    if not m:
        raise NotionError(f"notion_page_id_invalid: {raw[:80]}", status=400)
    hex32 = m.group(1).replace("-", "").lower()
    # UUID 형식으로 변환 — Notion API 가 둘 다 받지만 UUID 가 안전
    return f"{hex32[0:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"


def extract_page_title(page: Dict[str, Any]) -> str:
    """
    페이지 객체에서 제목 추출. 데이터베이스 페이지 vs 일반 페이지 모두 처리.

    일반 페이지: properties.title.title[*].plain_text
    DB 페이지: properties[<title_col>].title[*].plain_text (title 타입 컬럼)
    """
    props = (page or {}).get("properties") or {}
    # 1) properties.title (일반 페이지)
    title_prop = props.get("title")
    if isinstance(title_prop, dict) and title_prop.get("type") == "title":
        parts = title_prop.get("title") or []
        joined = "".join(p.get("plain_text") or "" for p in parts).strip()
        if joined:
            return joined
    # 2) DB 페이지 — title 타입 컬럼 찾기
    for value in props.values():
        if isinstance(value, dict) and value.get("type") == "title":
            parts = value.get("title") or []
            joined = "".join(p.get("plain_text") or "" for p in parts).strip()
            if joined:
                return joined
    return "(제목 없음)"


def extract_page_icon(page: Dict[str, Any]) -> Optional[str]:
    """페이지 icon (emoji 또는 외부 URL) 추출. 없으면 None."""
    icon = (page or {}).get("icon")
    if not isinstance(icon, dict):
        return None
    kind = icon.get("type")
    if kind == "emoji":
        return icon.get("emoji")
    if kind == "external":
        ext = icon.get("external") or {}
        return ext.get("url")
    if kind == "file":
        f = icon.get("file") or {}
        return f.get("url")
    return None
