"""Instructors 도메인 라우터.

레이어 규약: router → service → repository (router 는 service 만 호출, repository 직접
호출 금지). 엔드포인트는 후속 task 에서 추가한다. 빈 라우터여도 build_v1_router() 가
도메인 추가만으로 자동 등록한다 (BE-E01-T01).
"""
from fastapi import APIRouter

router = APIRouter(prefix="/instructors", tags=["Instructors"])
