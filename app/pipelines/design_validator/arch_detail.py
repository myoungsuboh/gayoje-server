"""
Architecture 노드의 detail 정규화.

[D-2 — 2026-05-25 Architecture detail 격상]
- Service.deployment: {port, replicas, health_check_path, env_vars[], scaling_policy}
- Service.external_dependencies: [{name, type, purpose}, ...]
- Connection.auth: "mTLS" | "bearer" | "basic" | "api-key" | "none"

설계는 A 단계와 동일 (객체 list/dict ↔ JSON string ↔ legacy 형태 모두 흡수).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_AUTH_ENUM = {"mTLS", "bearer", "basic", "api-key", "none"}
_SCALING_ENUM = {"manual", "auto-cpu", "auto-memory"}


def _coerce_int_default(value: Any, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def normalize_deployment(raw: Any) -> Dict[str, Any]:
    """Service.deployment 정규화. 4가지 입력 흡수, 안전 default."""
    if raw is None:
        return {"port": 0, "replicas": 1, "health_check_path": "",
                "env_vars": [], "scaling_policy": "manual"}
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return normalize_deployment(None)
        try:
            raw = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return normalize_deployment(None)
    if not isinstance(raw, dict):
        return normalize_deployment(None)

    env_vars_raw = raw.get("env_vars") or []
    if not isinstance(env_vars_raw, list):
        env_vars_raw = []
    # 중복 제거 + 순서 안정
    seen: set = set()
    env_vars: List[str] = []
    for v in env_vars_raw:
        if isinstance(v, str) and v.strip() and v not in seen:
            seen.add(v)
            env_vars.append(v.strip())

    scaling = str(raw.get("scaling_policy") or "manual").strip()
    if scaling not in _SCALING_ENUM:
        scaling = "manual"

    return {
        "port": _coerce_int_default(raw.get("port"), 0),
        "replicas": max(1, _coerce_int_default(raw.get("replicas"), 1)),
        "health_check_path": str(raw.get("health_check_path") or "").strip(),
        "env_vars": env_vars,
        "scaling_policy": scaling,
    }


def _coerce_external_dependency(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return {
        "name": name.strip(),
        "type": str(raw.get("type") or "").strip(),
        "purpose": str(raw.get("purpose") or "").strip(),
    }


def normalize_external_dependencies(raw: Any) -> List[Dict[str, Any]]:
    """external_dependencies list 정규화."""
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            raw = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    seen: set = set()
    out: List[Dict[str, Any]] = []
    for item in raw:
        coerced = _coerce_external_dependency(item)
        if coerced is None:
            continue
        if coerced["name"] in seen:
            continue
        seen.add(coerced["name"])
        out.append(coerced)
    return out


def normalize_connection_auth(raw: Any) -> str:
    """Connection.auth 정규화. enum 외 값은 'none' 으로 fallback."""
    if not isinstance(raw, str):
        return "none"
    s = raw.strip()
    # case-insensitive fallback (예: "MTLS" 또는 "Bearer" 등)
    for valid in _AUTH_ENUM:
        if s.lower() == valid.lower():
            return valid
    return "none"


def serialize_deployment_for_neo4j(raw: Any) -> str:
    return json.dumps(normalize_deployment(raw), ensure_ascii=False)


def serialize_external_dependencies_for_neo4j(raw: Any) -> str:
    return json.dumps(normalize_external_dependencies(raw), ensure_ascii=False)


def decode_services_detail(
    services: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Service list 의 deployment / external_dependencies 복원."""
    out: List[Dict[str, Any]] = []
    for s in services or []:
        if not isinstance(s, dict):
            continue
        copy = dict(s)
        copy["deployment"] = normalize_deployment(s.get("deployment"))
        copy["external_dependencies"] = normalize_external_dependencies(
            s.get("external_dependencies")
        )
        out.append(copy)
    return out


def decode_connections_auth(
    connections: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Connection list 의 auth 정규화. 원본 mutate 회피."""
    out: List[Dict[str, Any]] = []
    for c in connections or []:
        if not isinstance(c, dict):
            continue
        copy = dict(c)
        copy["auth"] = normalize_connection_auth(c.get("auth"))
        out.append(copy)
    return out
