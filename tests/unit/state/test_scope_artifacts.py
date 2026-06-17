from __future__ import annotations

from pathlib import Path

from meridian.lib.state.work_scope import (
    SCOPE_HANDOFFS_DIRNAME,
    SCOPE_PROMPTS_DIRNAME,
    WorkScope,
)


def _ambient_scope(root: Path, *, identifier: str = "p1") -> WorkScope:
    return WorkScope(kind="ambient_spawn", identifier=identifier, root=root)


def _work_item_scope(root: Path, *, work_id: str = "my-feature") -> WorkScope:
    return WorkScope(kind="work_item", identifier=work_id, root=root)


def test_count_artifacts_returns_zero_for_missing_dir(tmp_path: Path) -> None:
    assert _ambient_scope(tmp_path / "missing").count_artifacts() == 0


def test_count_artifacts_ignores_prompts_and_status(tmp_path: Path) -> None:
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    (scope_dir / "__status.json").write_text("{}", encoding="utf-8")
    prompts = scope_dir / SCOPE_PROMPTS_DIRNAME
    prompts.mkdir()
    (prompts / "launch.md").write_text("prompt", encoding="utf-8")

    assert _ambient_scope(scope_dir).count_artifacts() == 0


def test_count_artifacts_counts_top_level_and_handoffs(tmp_path: Path) -> None:
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    (scope_dir / "design.md").write_text("notes", encoding="utf-8")
    handoffs = scope_dir / SCOPE_HANDOFFS_DIRNAME
    handoffs.mkdir()
    (handoffs / "ctx.md").write_text("ctx", encoding="utf-8")
    (handoffs / "summary.md").write_text("sum", encoding="utf-8")
    prompts = scope_dir / SCOPE_PROMPTS_DIRNAME
    prompts.mkdir()
    (prompts / "launch.md").write_text("prompt", encoding="utf-8")

    assert _ambient_scope(scope_dir).count_artifacts() == 3


def test_count_artifacts_counts_handoffs_children_not_nested_files(
    tmp_path: Path,
) -> None:
    """Direct children of handoffs/ count; nested dirs count as one entry each."""

    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    handoffs = scope_dir / SCOPE_HANDOFFS_DIRNAME
    handoffs.mkdir()
    nested = handoffs / "batch-a"
    nested.mkdir()
    (nested / "ctx.md").write_text("nested", encoding="utf-8")
    (handoffs / "top.md").write_text("top", encoding="utf-8")

    assert _ambient_scope(scope_dir).count_artifacts() == 2


def test_count_artifacts_counts_non_dir_handoffs_as_one(tmp_path: Path) -> None:
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    (scope_dir / SCOPE_HANDOFFS_DIRNAME).write_text("not a directory", encoding="utf-8")

    assert _ambient_scope(scope_dir).count_artifacts() == 1


def test_count_artifacts_same_rules_for_durable_work_item(tmp_path: Path) -> None:
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    (scope_dir / "notes.md").write_text("notes", encoding="utf-8")
    prompts = scope_dir / SCOPE_PROMPTS_DIRNAME
    prompts.mkdir()
    (prompts / "launch.md").write_text("prompt", encoding="utf-8")

    assert _work_item_scope(scope_dir).count_artifacts() == 1


def test_scope_label_differs_by_kind() -> None:
    durable = WorkScope(kind="work_item", identifier="feature-a", root=Path("/w/feature-a"))
    ephemeral = WorkScope(kind="ambient_spawn", identifier="p9", root=Path("/w/p9"))

    assert durable.scope_label() == "work item 'feature-a'"
    assert ephemeral.scope_label() == "spawn-local work area (p9)"


def test_format_leave_scope_warning_uses_kind_specific_label(tmp_path: Path) -> None:
    durable_dir = tmp_path / "scope-a"
    durable_dir.mkdir()
    durable = WorkScope(kind="work_item", identifier="scope-a", root=durable_dir)
    ephemeral_dir = tmp_path / "p1"
    ephemeral_dir.mkdir()
    ephemeral = WorkScope(
        kind="ambient_spawn",
        identifier="p1",
        root=ephemeral_dir,
    )

    assert durable.format_leave_scope_warning("scope-b") is None

    (durable_dir / "notes.md").write_text("x", encoding="utf-8")

    warning = durable.format_leave_scope_warning("scope-b")
    assert warning is not None
    assert "work item 'scope-a'" in warning
    assert "scope-b" in warning

    (ephemeral_dir / "scratch.md").write_text("x", encoding="utf-8")

    ambient_warning = ephemeral.format_leave_scope_warning("next-scope")
    assert ambient_warning is not None
    assert "spawn-local work area (p1)" in ambient_warning
    assert "next-scope" in ambient_warning
