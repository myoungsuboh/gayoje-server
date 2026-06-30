from __future__ import annotations

from typing import Any, Dict


CPS_AGENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "_harness_metadata": {
            "type": "object",
            "properties": {
                "state": {"type": "string"},
                "verification_passed": {"type": "boolean"},
                "journey": {"type": "string"},
            },
        },
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "properties": {"type": "object"},
                },
                "required": ["id", "label"],
            },
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "type": {"type": "string"},
                    "properties": {"type": "object"},
                },
                "required": ["source", "target", "type"],
            },
        },
    },
    "required": ["nodes", "relationships"],
}

CPS_IMPACT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "affected_sections": {"type": "array", "items": {"type": "string"}},
        "removed_prb_ids": {"type": "array", "items": {"type": "string"}},
        "removed_res_ids": {"type": "array", "items": {"type": "string"}},
        "analysis": {"type": "string"},
    },
}

_TEMPERATURE = 0.1
