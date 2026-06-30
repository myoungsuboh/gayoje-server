"""
Subscription 등급 상수 — pure constants, 의존성 0.

[배경]
이전엔 SUBSCRIPTION_FREE/_PRO 가 `app/service/user_repository.py` 에 있었음.
그러나 user_repository 는 token_encryption → config → settings 평가를 트리거 →
config 의 production validator 가 JWT_SECRET_KEY 검사. worker 같이 JWT 시크릿을
의도적으로 받지 않는 컨텍스트에선 import 자체가 실패함.

[분리 이유]
quota.py 등 등급 상수만 필요한 곳이 user_repository 전체를 import 하면 settings
검증까지 끌고 오게 됨. 상수만 별도 module 로 분리하면 worker 도 이 module 만
import 해서 안전하게 사용.

[backward compat]
`app/service/user_repository.py` 가 이 module 의 상수를 re-export 하므로,
기존 `from app.service.user_repository import SUBSCRIPTION_FREE` 코드는 그대로
동작 (다만 그 경로는 settings 평가를 트리거하므로 lightweight 경로엔 직접
import 권장).
"""
from __future__ import annotations

SUBSCRIPTION_FREE = "free"
SUBSCRIPTION_PRO = "pro"
# 2026-05: Pro 위 단계 2개 추가 — 용량 차등 (Pro의 2배 / 4배), 모델은 Pro와 동일.
SUBSCRIPTION_PRO_PLUS = "pro_plus"
SUBSCRIPTION_PRO_MAX = "pro_max"
SUBSCRIPTION_TYPES = (
    SUBSCRIPTION_FREE,
    SUBSCRIPTION_PRO,
    SUBSCRIPTION_PRO_PLUS,
    SUBSCRIPTION_PRO_MAX,
)

# 유료 등급 집합 — 모델 선택 / 우선 처리 표시 등에서 멤버십 검사용.
PAID_SUBSCRIPTIONS = (SUBSCRIPTION_PRO, SUBSCRIPTION_PRO_PLUS, SUBSCRIPTION_PRO_MAX)
