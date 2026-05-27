"""Command manifest registration tests."""

from __future__ import annotations

from meridian.lib.extensions.types import ExtensionCommandSpec, ExtensionSurface
from meridian.lib.ops.commands import get_all_op_specs
from meridian.lib.ops.spawn.models import SpawnDetailOutput, SpawnStatusInput
from meridian.lib.ops.work_lifecycle import WorkTaskDirInput, WorkTaskDirOutput


def _spec_by_fqid() -> dict[str, ExtensionCommandSpec]:
    return {spec.fqid: spec for spec in get_all_op_specs()}


def test_work_task_dir_command_is_registered() -> None:
    specs = _spec_by_fqid()

    task_dir_spec = specs["meridian.work.task-dir"]
    assert task_dir_spec.cli_group == "work"
    assert task_dir_spec.cli_name == "task-dir"
    assert task_dir_spec.surfaces == frozenset({ExtensionSurface.CLI, ExtensionSurface.HTTP})
    assert task_dir_spec.args_schema is WorkTaskDirInput
    assert task_dir_spec.result_schema is WorkTaskDirOutput


def test_spawn_status_command_is_registered_for_cli() -> None:
    specs = _spec_by_fqid()

    status_spec = specs["meridian.spawn.status"]
    assert status_spec.cli_group == "spawn"
    assert status_spec.cli_name == "status"
    assert status_spec.surfaces == frozenset({ExtensionSurface.CLI})
    assert status_spec.args_schema is SpawnStatusInput
    assert status_spec.result_schema is SpawnDetailOutput
    parsed = status_spec.args_schema(spawn_id="p123")
    assert parsed.include_report_body is False
