from __future__ import annotations

from pathlib import Path

from meridian.lib.state.work_store import (
    SCOPE_HANDOFFS_DIRNAME,
    SCOPE_PROMPTS_DIRNAME,
    count_scope_artifacts,
)


def test_count_scope_artifacts_returns_zero_for_missing_dir(tmp_path: Path) -> None:
    assert count_scope_artifacts(tmp_path / "missing") == 0


def test_count_scope_artifacts_ignores_prompts_and_status(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "__status.json").write_text("{}", encoding="utf-8")
    prompts = scope / SCOPE_PROMPTS_DIRNAME
    prompts.mkdir()
    (prompts / "launch.md").write_text("prompt", encoding="utf-8")

    assert count_scope_artifacts(scope) == 0


def test_count_scope_artifacts_counts_top_level_and_handoffs(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "design.md").write_text("notes", encoding="utf-8")
    handoffs = scope / SCOPE_HANDOFFS_DIRNAME
    handoffs.mkdir()
    (handoffs / "ctx.md").write_text("ctx", encoding="utf-8")
    (handoffs / "summary.md").write_text("sum", encoding="utf-8")
    prompts = scope / SCOPE_PROMPTS_DIRNAME
    prompts.mkdir()
    (prompts / "launch.md").write_text("prompt", encoding="utf-8")

    assert count_scope_artifacts(scope) == 3
