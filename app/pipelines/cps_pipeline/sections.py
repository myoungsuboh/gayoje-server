from __future__ import annotations

import re
from typing import Any, Dict, List


_SECTION_HEADER_RE = re.compile(r"^###\s+(?:\d+\.\s*)?(.+?)\s*$")


def split_master_sections(master_content: str) -> tuple[Dict[str, str], List[str]]:
    """
    Mirrors `CPS Section Filter1` section split.
    Returns: (section_map keyed by raw header, ordered list of keys)
    """
    section_map: Dict[str, str] = {}
    section_order: List[str] = []
    current_key = "__header__"
    current_lines: List[str] = []
    for line in master_content.split("\n"):
        m = _SECTION_HEADER_RE.match(line)
        if m:
            if current_lines:
                section_map[current_key] = "\n".join(current_lines)
                section_order.append(current_key)
            current_key = m.group(1).strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        section_map[current_key] = "\n".join(current_lines)
        section_order.append(current_key)
    return section_map, section_order


def filter_affected_sections(
    master_content: str,
    latest_content: str,
    impact: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Stage: `CPS Section Filter1`.
    Returns a payload (key names) to pass to Merge CPS Agent.
    """
    if not master_content.strip():
        return {
            "affected_sections_content": "",
            "full_section_map": {},
            "section_order": [],
            "affected_section_keys": [],
            "latest_content": latest_content,
            "impact": impact,
            "master_content": "",
            "is_first_run": True,
            "_diagnostic": {
                "mode": "FIRST_RUN",
                "master_size": 0,
                "affected_size": 0,
                "latest_size": len(latest_content),
            },
        }

    section_map, section_order = split_master_sections(master_content)

    candidates: List[str] = list(impact.get("affected_sections") or [])
    if not candidates and len(latest_content.strip()) > 50:
        candidates = ["Problem", "Solution", "Pending"]

    affected_keys: List[str] = []
    affected_content = ""
    section_keys = [k for k in section_map.keys() if k != "__header__"]

    for target in candidates:
        matched = next(
            (
                k
                for k in section_keys
                if k.lower().find(target.lower()) >= 0
                or target.lower().find(k.lower()) >= 0
            ),
            None,
        )
        if matched and matched not in affected_keys:
            affected_content += ("\n\n" if affected_content else "") + section_map[matched]
            affected_keys.append(matched)

    if not affected_content.strip():
        for fb in ("Problem", "Solution"):
            found = next(
                (k for k in section_keys if k.lower().find(fb.lower()) >= 0),
                None,
            )
            if found and found not in affected_keys:
                affected_content += (
                    "\n\n" if affected_content else ""
                ) + section_map[found]
                affected_keys.append(found)

    return {
        "affected_sections_content": affected_content.strip(),
        "full_section_map": section_map,
        "section_order": section_order,
        "affected_section_keys": affected_keys,
        "latest_content": latest_content,
        "impact": impact,
        "master_content": master_content,
        "is_first_run": False,
        "_diagnostic": {
            "mode": "INCREMENTAL",
            "master_size": len(master_content),
            "affected_size": len(affected_content),
            "latest_size": len(latest_content),
            "reduction_pct": (
                round((1 - len(affected_content) / len(master_content)) * 100)
                if master_content
                else 0
            ),
            "affected_sections": affected_keys,
            "analysis": impact.get("analysis", ""),
        },
    }
