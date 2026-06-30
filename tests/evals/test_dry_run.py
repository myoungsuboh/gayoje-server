"""
evals.dry_run — LLM 호출 없이 prompt 조합 확인.

prompt template (design_spack.md) 와 PRD fixture 가 깨지지 않고 합쳐지는지
회귀 보호.
"""
from __future__ import annotations

import pytest

from evals.dry_run import main, render


def test_render_plant_substitutes_prd_input():
    """plant 시나리오 — <<spack_input>> placeholder 가 PRD 로 치환."""
    rendered = render("plant")
    assert "<<spack_input>>" not in rendered, "placeholder 가 치환되지 않음"
    # PRD 의 핵심 키워드가 포함됐는지
    assert "plant — Product Overview" in rendered
    assert "Story 3.2" in rendered
    assert "leafCount" in rendered
    # design_spack.md 의 지시사항도 포함
    assert "API PAYLOAD" in rendered
    assert "ENTITY ATTRIBUTES" in rendered
    assert "API ERROR HANDLING" in rendered


def test_render_unknown_scenario_raises():
    with pytest.raises(FileNotFoundError):
        render("nonexistent-scenario")


def test_main_stats_only(capsys):
    rc = main(["plant", "--stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "bytes" in out
    assert "tokens" in out


def test_main_outputs_full_prompt_to_stdout(capsys):
    rc = main(["plant"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "plant — Product Overview" in captured.out
    # 통계는 stderr 로
    assert "dry-run" in captured.err


def test_main_save_writes_rendered_prompt_file(tmp_path, monkeypatch):
    """--save 가 rendered_prompt.txt 생성 (임시 디렉토리로 redirect)."""
    from evals import dry_run

    # 실제 plant 디렉토리에 파일 만들지 않도록 임시 redirect
    monkeypatch.setattr(dry_run, "_SCENARIOS_DIR", tmp_path)
    plant_dir = tmp_path / "plant"
    plant_dir.mkdir()
    (plant_dir / "prd_input.md").write_text("# tiny prd", encoding="utf-8")

    rc = main(["plant", "--save", "--stats"])
    assert rc == 0
    assert (plant_dir / "rendered_prompt.txt").exists()


def test_main_unknown_returns_nonzero(capsys):
    rc = main(["nonexistent-scenario", "--stats"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "파일 부재" in err
