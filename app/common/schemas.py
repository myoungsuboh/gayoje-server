"""API 스키마 베이스 — camelCase 직렬화 규약 (BE-E01-T01).

CamelModel 을 상속하면:
- 응답 JSON 키가 camelCase(alias)로 직렬화된다. FastAPI 는 response_model 을
  by_alias 로 직렬화하므로 별도 설정 없이 camelCase 응답이 된다.
- 서버 코드는 snake_case 로 다룬다(populate_by_name=True → 두 이름 모두 허용).
- ORM/dataclass 객체로부터 직접 검증 가능(from_attributes=True).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """camelCase 직렬화 + snake_case 입력 허용 베이스 모델."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        ser_json_by_alias=True,
    )
