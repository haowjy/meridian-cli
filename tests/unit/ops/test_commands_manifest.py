"""Command manifest registration tests."""

from __future__ import annotations

from meridian.lib.extensions.types import ExtensionCommandSpec, ExtensionSurface
from meridian.lib.ops.commands import get_all_op_specs
from meridian.lib.ops.work_lifecycle import (
    WorkClearWorktreeInput,
    WorkClearWorktreeOutput,
    WorkSetWorktreeInput,
    WorkSetWorktreeOutput,
)


def _spec_by_fqid() -> dict[str, ExtensionCommandSpec]:
    return {spec.fqid: spec for spec in get_all_op_specs()}


def test_work_set_and_clear_worktree_commands_are_registered() -> None:
    specs = _spec_by_fqid()

    set_spec = specs["meridian.work.set-worktree"]
    assert set_spec.cli_group == "work"
    assert set_spec.cli_name == "set-worktree"
    assert set_spec.surfaces == frozenset({ExtensionSurface.CLI, ExtensionSurface.HTTP})
    assert set_spec.args_schema is WorkSetWorktreeInput
    assert set_spec.result_schema is WorkSetWorktreeOutput

    clear_spec = specs["meridian.work.clear-worktree"]
    assert clear_spec.cli_group == "work"
    assert clear_spec.cli_name == "clear-worktree"
    assert clear_spec.surfaces == frozenset({ExtensionSurface.CLI, ExtensionSurface.HTTP})
    assert clear_spec.args_schema is WorkClearWorktreeInput
    assert clear_spec.result_schema is WorkClearWorktreeOutput
