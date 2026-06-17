# qa-validated: test-suite-redesign
"""Task CWD projection and runtime root resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import context as launch_context_module
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)
from meridian.lib.launch.workspace_projection import ProjectionResult
from tests.support.launch import assert_task_cwd_instruction, stub_bundle_request_and_resolve
from tests.unit.launch.context_env_helpers import (
    build_launch_runtime,
    build_spawn_request,
    write_minimal_mars_config,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_build_launch_context_projects_external_task_cwd_for_active_harness_projection(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    outside_task_cwd = tmp_path.parent / f"{tmp_path.name}-outside-task"
    outside_task_cwd.mkdir(parents=True, exist_ok=True)

    runtime_ctx = build_launch_context(
        spawn_id="p-task-cwd-warning",
        request=build_spawn_request(),
        runtime=build_launch_runtime(tmp_path=tmp_path, execution_cwd=outside_task_cwd),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    projected_roots = {path.resolve() for path in runtime_ctx.binding.spec.projected_roots}
    warning_codes = {warning.code for warning in runtime_ctx.warnings}
    assert "task_cwd_not_projected" not in warning_codes
    assert runtime_ctx.binding.run_params.task_cwd == outside_task_cwd.as_posix()
    assert outside_task_cwd.resolve() in projected_roots


def test_build_launch_context_opencode_projects_external_task_cwd_as_wildcard(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gemini-2.5-pro",
        harness=HarnessId.OPENCODE,
    )
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    outside_task_cwd = tmp_path.parent / f"{tmp_path.name}-outside-opencode-task"
    outside_task_cwd.mkdir(parents=True, exist_ok=True)
    request = build_spawn_request().model_copy(update={"harness": HarnessId.OPENCODE.value})

    runtime_ctx = build_launch_context(
        spawn_id="p-opencode-task-parent",
        request=request,
        runtime=build_launch_runtime(tmp_path=tmp_path, execution_cwd=outside_task_cwd),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    bind_env = runtime_ctx.binding.environment.bind_env_overrides
    config = json.loads(bind_env["OPENCODE_CONFIG_CONTENT"])
    external_dirs = config.get("permission", {}).get("external_directory", {})
    task_cwd_pattern = outside_task_cwd.as_posix() + "/**"

    assert "task_cwd_not_projected" not in {warning.code for warning in runtime_ctx.warnings}
    assert runtime_ctx.binding.run_params.task_cwd == outside_task_cwd.as_posix()
    assert task_cwd_pattern in external_dirs
    assert external_dirs[task_cwd_pattern] == "allow"


def test_build_launch_context_falls_back_for_harness_without_workspace_projection(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    outside_task_cwd = tmp_path.parent / f"{tmp_path.name}-outside-task-pi"
    outside_task_cwd.mkdir(parents=True, exist_ok=True)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.setattr(
        launch_context_module,
        "project_workspace_roots",
        lambda **_kwargs: ProjectionResult(applicability="unsupported:requires_config_generation"),
    )
    request = SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        prompt="hello",
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-task-cwd-warning-pi",
        request=request,
        runtime=build_launch_runtime(tmp_path=tmp_path, execution_cwd=outside_task_cwd),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    warning_codes = {warning.code for warning in runtime_ctx.warnings}
    assert "task_cwd_not_projected" in warning_codes
    assert runtime_ctx.binding.run_params.task_cwd == outside_task_cwd.as_posix()
    assert_task_cwd_instruction(
        runtime_ctx.binding.run_params.appended_system_prompt or "",
        outside_task_cwd,
    )
    assert runtime_ctx.binding.child_cwd == tmp_path


def test_build_launch_context_prefers_explicit_runtime_roots_over_legacy_aliases(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    explicit_task_cwd = tmp_path.parent / f"{tmp_path.name}-explicit-task"
    explicit_task_cwd.mkdir(parents=True, exist_ok=True)
    legacy_project_root = tmp_path.parent / f"{tmp_path.name}-legacy-root"
    legacy_project_root.mkdir(parents=True, exist_ok=True)
    legacy_task_cwd = legacy_project_root / "legacy-task"
    legacy_task_cwd.mkdir(parents=True, exist_ok=True)

    runtime = LaunchRuntime(
        argv_intent=LaunchArgvIntent.REQUIRED,
        composition_surface=LaunchCompositionSurface.DIRECT,
        report_output_path=(tmp_path / "report.md").as_posix(),
        runtime_root=(tmp_path / ".meridian").as_posix(),
        config_root=tmp_path.as_posix(),
        control_root=tmp_path.as_posix(),
        requested_task_cwd=explicit_task_cwd.as_posix(),
        project_paths_project_root=legacy_project_root.as_posix(),
        project_paths_execution_cwd=legacy_task_cwd.as_posix(),
    )
    runtime_ctx = build_launch_context(
        spawn_id="p-runtime-root-explicit",
        request=build_spawn_request(),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.project_root == tmp_path
    assert runtime_ctx.control_root == tmp_path
    assert runtime_ctx.execution_cwd == explicit_task_cwd
    assert runtime_ctx.binding.run_params.task_cwd == explicit_task_cwd.as_posix()
