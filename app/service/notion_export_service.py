"""
Notion export 오케스트레이션 — CPS/PRD/설계 → 허브 페이지 + 하위 3페이지 (멱등).

흐름:
1. (user, project) 매핑 조회. 허브 없으면 parent_page_id 아래 생성(없으면 need_parent).
2. 각 문서: 소스 fetch → 마크다운 렌더 → 블록 변환.
   - 하위 페이지 없으면 생성, 있으면 기존 자식 archive 후 새로 append (멱등 갱신).
3. 매핑/synced_at 저장. 부분 실패는 문서별 status 로 보고(401/429 는 전체 전파).

하위 페이지는 parent=허브 로 생성되므로 허브에 child_page 로 자동 링크됨.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.clients.notion_client import NotionError, NotionRateLimited, NotionUnauthorized
from app.core.design_to_markdown import design_to_markdown
from app.core.markdown_to_notion_blocks import chunk_blocks_by_weight, markdown_to_blocks
from app.service import query_repository, user_repository

logger = logging.getLogger(__name__)

_HUB_ICON = "📦"
_VALID_DOCS = ("cps", "prd", "design")
_DOC_META = {
    "cps": ("🎯 핵심 정리 (CPS)", "🎯"),
    "prd": ("📋 기획서(PRD)", "📋"),
    "design": ("🏗️ 시스템 설계", "🏗️"),
}


def _map_key(project_name: str, team_id: str) -> str:
    return f"{team_id}::{project_name}" if team_id else project_name


def _page_url(page_id: str) -> str:
    return f"https://www.notion.so/{str(page_id or '').replace('-', '')}"


def _callout(text: str, emoji: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
            "icon": {"type": "emoji", "emoji": emoji},
            "color": "brown_background",
        },
    }


def _hub_children(project_name: str) -> List[Dict[str, Any]]:
    return [
        _callout("Harness 가 회의록에서 자동 정리한 기획 · 설계입니다.", _HUB_ICON),
        {"object": "block", "type": "divider", "divider": {}},
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": "아래 하위 페이지에서 CPS · PRD · 시스템 설계를 확인하세요. "
                            "다시 공유하면 이 페이지들이 최신 내용으로 자동 갱신됩니다."
                        },
                    }
                ]
            },
        },
    ]


def _footer_md() -> str:
    return "\n\n---\n\n_🔗 Harness 에서 자동 생성 · 다시 공유 시 갱신됩니다._"


def _design_empty(spack: Any, ddd: Any, arch: Any) -> bool:
    return not any(
        [
            getattr(spack, "apis", None),
            getattr(spack, "entities", None),
            getattr(spack, "policies", None),
            getattr(spack, "screens", None),
            getattr(ddd, "contexts", None),
            getattr(ddd, "aggregates", None),
            getattr(ddd, "domain_entities", None),
            getattr(ddd, "domain_events", None),
            getattr(arch, "services", None),
            getattr(arch, "databases", None),
        ]
    )


async def _doc_markdown(doc: str, project_name: str, team_id: str) -> Optional[str]:
    """문서별 마크다운. 내용 없으면 None (skip)."""
    if doc == "cps":
        cps = await query_repository.get_master_cps(project_name, team_id)
        return (getattr(cps, "content", None) if cps else None) or None
    if doc == "prd":
        prd = await query_repository.get_master_prd(project_name, team_id)
        return (getattr(prd, "prd_content", None) if prd else None) or None
    if doc == "design":
        spack = await query_repository.get_spack_graph(project_name, team_id)
        ddd = await query_repository.get_ddd_graph(project_name, team_id)
        arch = await query_repository.get_architecture_graph(project_name, team_id)
        if _design_empty(spack, ddd, arch):
            return None
        return design_to_markdown(spack, ddd, arch)
    return None


async def export_project_to_notion(
    *,
    email: str,
    project_name: str,
    team_id: str,
    docs: List[str],
    parent_page_id: Optional[str],
    client: Any,
) -> Dict[str, Any]:
    """CPS/PRD/설계를 Notion 허브+하위페이지로 멱등 export. NotionClient 는 주입."""
    team_id = team_id or ""
    key = _map_key(project_name, team_id)
    existing = await user_repository.get_notion_export_map(email, key) or {}
    hub_page_id = existing.get("hub_page_id")
    results: List[Dict[str, Any]] = []

    # 0) 저장된 허브가 Notion 에서 삭제됐는지 확인 — 삭제됐으면(404) 스테일 매핑
    #    전체를 폐기하고 아래에서 재생성. (401/429 등은 전파)
    if hub_page_id:
        try:
            await client.get_page(hub_page_id)
        except (NotionUnauthorized, NotionRateLimited):
            raise
        except NotionError as e:
            if getattr(e, "status", None) == 404:
                hub_page_id = None
                existing = {}
            else:
                raise

    # 1) 허브 페이지 보장
    if not hub_page_id:
        if not parent_page_id:
            return {
                "hub_url": None,
                "results": [
                    {
                        "doc": "hub",
                        "status": "need_parent",
                        "error": "처음 공유할 때는 Notion 상위 페이지를 선택해야 합니다.",
                    }
                ],
            }
        hub = await client.create_page(
            parent_page_id=parent_page_id,
            title=f"📦 {project_name} — 기획 · 설계 (by Harness)",
            icon_emoji=_HUB_ICON,
            children=_hub_children(project_name),
        )
        hub_page_id = hub.get("id")
        await user_repository.save_notion_export_map(email, key, hub_page_id=hub_page_id)

    # 2) 문서별 업서트
    requested = [d for d in (docs or list(_VALID_DOCS)) if d in _VALID_DOCS]
    for doc in requested:
        title, icon = _DOC_META[doc]
        try:
            md = await _doc_markdown(doc, project_name, team_id)
            if not md:
                results.append({"doc": doc, "status": "skipped"})
                continue
            blocks = markdown_to_blocks(md + _footer_md())
            sub_id = existing.get(f"{doc}_page_id")
            recreate = not sub_id
            if sub_id:
                # 기존 하위 페이지 내용 비우기 — 삭제됐으면(404) 재생성으로 폴백.
                try:
                    await client.archive_block_children(block_id=sub_id)
                except (NotionUnauthorized, NotionRateLimited):
                    raise
                except NotionError as e:
                    if getattr(e, "status", None) == 404:
                        recreate = True
                    else:
                        raise
            if recreate:
                # [2026-06-05] 내용을 페이지 생성과 함께(첫 100블록) 넣는다 — 빈 페이지
                # 잔존 방지. 이전엔 children=[] 로 만들고 별도 append 라, append 가
                # 실패하면 빈 자식 페이지가 남았다. 나머지(>100)는 append 로 이어붙임.
                # [2026-06] 첫 청크는 가중치 기준(table 자식 포함)으로 — table 이 첫
                # 100블록 안에 있어도 자식 합산이 노션 한도를 넘지 않도록.
                _first_chunks = chunk_blocks_by_weight(blocks)
                first = _first_chunks[0] if _first_chunks else []
                sub = await client.create_page(
                    parent_page_id=hub_page_id, title=title, icon_emoji=icon,
                    children=first,
                )
                sub_id = sub.get("id")
                existing[f"{doc}_page_id"] = sub_id
                await user_repository.save_notion_export_map(
                    email, key, **{f"{doc}_page_id": sub_id}
                )
                status = "created"
                rest = blocks[len(first):]
            else:
                # 갱신 경로 — 위에서 기존 자식 블록을 archive 했으니 전체를 다시 append.
                status = "updated"
                rest = blocks
            if rest:
                await client.append_block_children(block_id=sub_id, children=rest)
            results.append({"doc": doc, "status": status, "url": _page_url(sub_id)})
        except (NotionUnauthorized, NotionRateLimited):
            raise  # 전체 요청 차원 오류 → 라우트가 401/429 로 매핑
        except NotionError as e:
            logger.warning("notion export doc=%s failed: %s", doc, e)
            results.append({"doc": doc, "status": "failed", "error": str(e)})

    # 3) synced_at 갱신
    await user_repository.save_notion_export_map(email, key)
    return {"hub_url": _page_url(hub_page_id), "results": results}
