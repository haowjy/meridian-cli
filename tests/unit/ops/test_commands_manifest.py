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


def test_work_set_worktree_command_is_registered() -> None:
    spec = _spec_by_fqid()["meridian.work.set-worktree"]

    assert spec.cli_group == "work"
    assert spec.cli_name == "set-worktree"
    assert spec.agent_default_format == "text"
    assert spec.surfaces == frozenset({ExtensionSurface.CLI, ExtensionSurface.HTTP})
    assert spec.args_schema is WorkSetWorktreeInput
    assert spec.result_schema is WorkSetWorktreeOutput


def test_work_clear_worktree_command_is_registered() -> None:
    spec = _spec_by_fqid()["meridian.work.clear-worktree"]

    assert spec.cli_group == "work"
    assert spec.cli_name == "clear-worktree"
    assert spec.agent_default_format == "text"
    assert spec.surfaces == frozenset({ExtensionSurface.CLI, ExtensionSurface.HTTP})
    assert spec.args_schema is WorkClearWorktreeInput
    assert spec.result_schema is WorkClearWorktreeOutput
