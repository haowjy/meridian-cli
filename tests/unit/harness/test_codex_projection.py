"""Codex subprocess projection and report path scoping tests."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.cursor import CursorAdapter
from meridian.lib.harness.projections.project_codex_subprocess import (
    project_codex_spec_to_cli_args,
)
from meridian.lib.harness.projections.project_cursor import project_cursor_spec_to_cli_args
from meridian.lib.launch.constants import (
    BASE_COMMAND_CODEX_SUBPROCESS,
    BASE_COMMAND_CURSOR_SUBPROCESS,
)
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver


def test_codex_resolve_launch_spec_populates_report_output_path_from_artifact_path(
    tmp_path: Path,
) -> None:
    report_path = str(tmp_path / "spawns" / "abc" / "report.md")
    adapter = CodexAdapter()

    spec = adapter.resolve_launch_spec(
        SpawnParams(prompt="do work", report_artifact_path=report_path),
        TieredPermissionResolver(config=PermissionConfig()),
    )

    assert spec.report_output_path == report_path


def test_codex_subprocess_projection_emits_o_flag_for_report_output_path(
    tmp_path: Path,
) -> None:
    report_path = str(tmp_path / "spawns" / "abc" / "report.md")
    adapter = CodexAdapter()
    spec = adapter.resolve_launch_spec(
        SpawnParams(prompt="do work", report_artifact_path=report_path),
        TieredPermissionResolver(config=PermissionConfig()),
    )

    command = project_codex_spec_to_cli_args(
        spec,
        base_command=BASE_COMMAND_CODEX_SUBPROCESS,
    )

    o_index = command.index("-o")
    assert command[o_index + 1] == report_path


def test_cursor_resolve_launch_spec_leaves_report_output_path_none(
    tmp_path: Path,
) -> None:
    report_path = str(tmp_path / "spawns" / "abc" / "report.md")
    adapter = CursorAdapter()

    spec = adapter.resolve_launch_spec(
        SpawnParams(
            prompt="do work",
            report_artifact_path=report_path,
            task_cwd=str(tmp_path / "task"),
        ),
        TieredPermissionResolver(config=PermissionConfig()),
    )

    assert spec.report_output_path is None


def test_cursor_subprocess_projection_emits_no_o_flag() -> None:
    adapter = CursorAdapter()
    spec = adapter.resolve_launch_spec(
        SpawnParams(prompt="do work", task_cwd="/tmp/task"),
        TieredPermissionResolver(config=PermissionConfig()),
    )

    command = project_cursor_spec_to_cli_args(
        spec,
        base_command=BASE_COMMAND_CURSOR_SUBPROCESS,
    )

    assert "-o" not in command
