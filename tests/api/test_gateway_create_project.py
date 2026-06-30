"""
[2026-06] createProject — 미팅 로그 없이 빈 프로젝트를 즉시 등록(claim).

dispatcher 의 _OWNERSHIP_CREATE 분기가 ownership_repository.claim 을 호출(OWNS 등록 +
max_projects 쿼터 402 + 동명 타 유저 409). 핸들러는 확인 응답만. 이 테스트는 분류
정합성(createProject 가 CREATE 로 가야 claim 됨) + 핸들러 계약을 핀 고정한다.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import gateway_compat_routes as gw


def test_create_project_classified_as_create():
    """createProject 가 _DISPATCH + _OWNERSHIP_CREATE 에 있고 read/access 로 새지 않음.

    CREATE 분기에 있어야 dispatcher 가 claim(등록+쿼터+409)을 수행한다. READ/ACCESS 로
    잘못 분류되면 claim 이 안 돼 등록이 일어나지 않는다.
    """
    assert "createProject" in gw._DISPATCH
    assert gw._DISPATCH["createProject"] is gw._h_create_project
    assert "createProject" in gw._OWNERSHIP_CREATE
    assert "createProject" not in gw._OWNERSHIP_READ
    assert "createProject" not in gw._OWNERSHIP_ACCESS
    assert "createProject" not in gw._OWNERSHIP_FREE


@pytest.mark.asyncio
async def test_create_project_handler_returns_created():
    """핸들러는 확인 응답만 — claim 은 dispatcher 가 이미 수행."""
    out = await gw._h_create_project({"projectName": "myproj"}, {})
    assert out == {"result": {"project_name": "myproj", "created": True}}


@pytest.mark.asyncio
async def test_create_project_handler_reads_snake_case_and_query():
    assert (await gw._h_create_project({"project_name": "snake"}, {}))["result"]["project_name"] == "snake"
    assert (await gw._h_create_project({}, {"projectName": "viaquery"}))["result"]["project_name"] == "viaquery"


@pytest.mark.asyncio
async def test_create_project_handler_requires_name():
    with pytest.raises(HTTPException) as ei:
        await gw._h_create_project({}, {})
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException):
        await gw._h_create_project({"projectName": "   "}, {})
