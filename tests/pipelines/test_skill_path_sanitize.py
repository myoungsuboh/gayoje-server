"""get_skill_path — 스킬 id/태그의 경로 구분자 sanitize (FE skillFileBase 와 동일 규칙).

'/'·'\\' 가 파일명에 남으면 zip 에서 하위 폴더로 풀려 skills/{cat}/ 평탄 구조가
깨지고, 오케스트레이터(Target Skill 경로)와 실제 zip 경로가 어긋난다.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.pipelines.create_md_pipeline import get_skill_path


def _s(id_, tags):
    return SimpleNamespace(id=id_, tags=tags)


def test_slash_in_id_is_sanitized():
    p = get_skill_path(_s("SKL-SRC/COMPONENTS-PROJECT-ARCHITECTURE", ["frontEnd"]))
    assert p == "skills/frontEnd/frontEnd-src-components-project-architecture.md"
    # skills/{category}/{file} — 정확히 2개의 '/' 만 (하위 폴더 없음)
    assert p.count("/") == 2


def test_backslash_is_sanitized():
    p = get_skill_path(_s("SKL-A\\B", ["backEnd"]))
    assert p == "skills/backEnd/backEnd-a-b.md"


def test_normal_id_unchanged():
    assert get_skill_path(_s("SKL-3", ["backEnd"])) == "skills/backEnd/backEnd-3.md"
    assert get_skill_path(_s("SKL-Login", [])) == "skills/etc/login.md"


# ── 신규 카테고리(5종) 인식 테스트 ──────────────────────────────────────────
# FE KNOWN_CATEGORIES 와 BE known_categories 가 동기화되지 않으면 'etc' 로 폴백해
# orchestrator 참조 경로와 zip 내 실제 경로가 어긋난다.

def test_ai_category_resolved():
    p = get_skill_path(_s("SKL-VIBE-CODING-WORKFLOW", ["vibe-coding", "ai"]))
    assert p.startswith("skills/ai/"), f"Expected skills/ai/..., got {p}"
    assert p.count("/") == 2


def test_design_category_resolved():
    p = get_skill_path(_s("SKL-FORM-UX", ["form", "design"]))
    assert p.startswith("skills/design/")
    assert p.count("/") == 2


def test_security_category_resolved():
    p = get_skill_path(_s("SKL-OWASP", ["owasp", "security"]))
    assert p.startswith("skills/security/")


def test_devops_category_resolved():
    p = get_skill_path(_s("SKL-CI-CD", ["ci", "devops"]))
    assert p.startswith("skills/devops/")


def test_testing_category_resolved():
    p = get_skill_path(_s("SKL-E2E", ["e2e", "testing"]))
    assert p.startswith("skills/testing/")


# ── Windows 무효 문자 sanitize 테스트 ────────────────────────────────────────

def test_angle_brackets_sanitized():
    p = get_skill_path(_s("SKL-SCRIPT-SETUP", ["<script setup>", "frontEnd"]))
    assert "<" not in p and ">" not in p
    assert p.count("/") == 2


def test_space_in_tag_sanitized():
    p = get_skill_path(_s("SKL-NPM-AUDIT", ["npm audit", "security"]))
    assert " " not in p
    assert p.count("/") == 2
