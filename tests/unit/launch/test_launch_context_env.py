# qa-validated: test-suite-redesign
"""Tests for build_launch_context — environment variable injection and binding."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meridian.lib.core.overrides import RuntimeOverrides
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
from tests.support.launch import stub_bundle_request_and_resolve

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _build_spawn_request(
    prompt: str = "hello",
    extra_args: tuple[str, ...] = (),
    goal: str | None = None,
) -> SpawnRequest:
    return SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        prompt=prompt,
        extra_args=extra_args,
        goal=goal,
    )


def _build_launch_runtime(
    *,
    tmp_path: Path,
    argv_intent: LaunchArgvIntent = LaunchArgvIntent.REQUIRED,
    composition_surface: LaunchCompositionSurface = LaunchCompositionSurface.DIRECT,
    execution_cwd: Path | None = None,
) -> LaunchRuntime:
    resolved_execution_cwd = execution_cwd or tmp_path
    return LaunchRuntime(
        argv_intent=argv_intent,
        composition_surface=composition_surface,
        report_output_path=(tmp_path / "report.md").as_posix(),
        runtime_root=(tmp_path / ".meridian").as_posix(),
        config_root=tmp_path.as_posix(),
        control_root=tmp_path.as_posix(),
        requested_task_cwd=resolved_execution_cwd.as_posix(),
        # Legacy aliases.
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=resolved_execution_cwd.as_posix(),
    )


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


def test_build_launch_context_uses_runtime_override_snapshot_not_live_env(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    ).model_copy(
        update={
            "runtime_override_snapshot": RuntimeOverrides(approval="confirm").model_dump(
                mode="json",
                exclude_none=True,
            )
        }
    )
    monkeypatch.setenv("MERIDIAN_APPROVAL", "yolo")

    runtime_ctx = build_launch_context(
        spawn_id="p-snapshot",
        request=_build_spawn_request(),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.resolved_request.execution_policy.approval == "confirm"
    assert runtime_ctx.binding.environment.runtime_override_env == {"MERIDIAN_APPROVAL": "confirm"}
    assert runtime_ctx.binding.environment.bind_env_overrides["MERIDIAN_APPROVAL"] == "confirm"
    assert runtime_ctx.binding.environment.final_env["MERIDIAN_APPROVAL"] == "confirm"


def test_build_launch_context_explicit_empty_snapshot_blocks_live_policy_env_leak(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    ).model_copy(update={"runtime_override_snapshot": {}})
    monkeypatch.setenv("MERIDIAN_APPROVAL", "yolo")

    runtime_ctx = build_launch_context(
        spawn_id="p-empty-snapshot",
        request=_build_spawn_request(),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.resolved_request.execution_policy.approval is None
    assert runtime_ctx.binding.environment.runtime_override_env == {}
    assert "MERIDIAN_APPROVAL" not in runtime_ctx.binding.environment.bind_env_overrides
    assert "MERIDIAN_APPROVAL" not in runtime_ctx.binding.environment.final_env


@pytest.mark.parametrize(
    ("parent_depth", "expected_depth"),
    [
        pytest.param(None, "0", id="clean-shell-primary-root"),
        pytest.param("2", "2", id="primary-launched-from-existing-depth"),
    ],
)
def test_build_launch_context_primary_preserves_runtime_depth(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    parent_depth: str | None,
    expected_depth: str,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    if parent_depth is not None:
        monkeypatch.setenv("MERIDIAN_DEPTH", parent_depth)
    request = _build_spawn_request()
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.binding.environment.bind_env_overrides["MERIDIAN_DEPTH"] == expected_depth
    assert runtime_ctx.binding.environment.bind_env_overrides["MERIDIAN_SPAWN_ID"] == "p-primary"
    assert "MERIDIAN_PARENT_SPAWN_ID" not in runtime_ctx.binding.environment.bind_env_overrides


def test_build_launch_context_primary_exports_configured_context_dirs(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    (tmp_path / "meridian.local.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "ctx/work"',
                'archive = "ctx/archive/work"',
                "",
                "[context.kb]",
                'path = "ctx/kb"',
                "",
                "[context.strategy]",
                'path = "ctx/strategy"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    request = _build_spawn_request()
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary-context",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    bind_env = runtime_ctx.binding.environment.bind_env_overrides
    assert bind_env["MERIDIAN_CONTEXT_WORK_DIR"] == (tmp_path / "ctx/work").as_posix()
    assert (
        bind_env["MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR"] == (tmp_path / "ctx/archive/work").as_posix()
    )
    assert bind_env["MERIDIAN_CONTEXT_KB_DIR"] == (tmp_path / "ctx/kb").as_posix()
    assert bind_env["MERIDIAN_CONTEXT_STRATEGY_DIR"] == (tmp_path / "ctx/strategy").as_posix()


@pytest.mark.parametrize(
    ("execution_cwd_factory", "expect_task_cwd"),
    [
        pytest.param(
            lambda root: root / ".meridian" / "spawns" / "p-parent",
            True,
            id="distinct-task-cwd",
        ),
        pytest.param(lambda root: root, False, id="task-cwd-matches-control-root"),
    ],
)
def test_build_launch_context_split_root_task_cwd_contract(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    execution_cwd_factory: Callable[[Path], Path],
    expect_task_cwd: bool,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    execution_cwd = execution_cwd_factory(tmp_path)
    execution_cwd.mkdir(parents=True, exist_ok=True)
    request = _build_spawn_request()
    runtime = _build_launch_runtime(tmp_path=tmp_path, execution_cwd=execution_cwd)

    runtime_ctx = build_launch_context(
        spawn_id="p-task-cwd-contract",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    bind_env = runtime_ctx.binding.environment.bind_env_overrides
    final_env = runtime_ctx.binding.environment.final_env
    assert runtime_ctx.execution_cwd == execution_cwd
    assert bind_env["MERIDIAN_PROJECT_DIR"] == tmp_path.as_posix()
    assert bind_env["MERIDIAN_CONTEXT_KB_DIR"] == (tmp_path / ".meridian" / "kb").as_posix()
    assert runtime_ctx.binding.run_params.control_root == tmp_path.as_posix()
    if expect_task_cwd:
        assert runtime_ctx.binding.run_params.task_cwd == execution_cwd.as_posix()
        assert bind_env["MERIDIAN_TASK_CWD"] == execution_cwd.as_posix()
        assert final_env["MERIDIAN_TASK_CWD"] == execution_cwd.as_posix()
        system_prompt = runtime_ctx.binding.run_params.appended_system_prompt or ""
        assert f"Your task working directory is: {execution_cwd.as_posix()}" in system_prompt
        assert "Immediately `cd` into this directory" in system_prompt
    else:
        assert runtime_ctx.binding.run_params.task_cwd is None
        assert "MERIDIAN_TASK_CWD" not in bind_env
        assert "MERIDIAN_TASK_CWD" not in final_env


def test_build_launch_context_primary_does_not_inject_task_cwd_instruction(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "0")
    execution_cwd = tmp_path.parent / f"{tmp_path.name}-primary-task"
    execution_cwd.mkdir(parents=True, exist_ok=True)
    request = _build_spawn_request()
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        execution_cwd=execution_cwd,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary-task-cwd",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.binding.run_params.task_cwd == execution_cwd.as_posix()
    assert "MERIDIAN_TASK_CWD" not in (
        runtime_ctx.binding.run_params.appended_system_prompt or ""
    )

def test_build_launch_context_projects_external_task_cwd_for_active_harness_projection(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    outside_task_cwd = tmp_path.parent / f"{tmp_path.name}-outside-task"
    outside_task_cwd.mkdir(parents=True, exist_ok=True)
    request = _build_spawn_request()
    runtime = _build_launch_runtime(tmp_path=tmp_path, execution_cwd=outside_task_cwd)

    runtime_ctx = build_launch_context(
        spawn_id="p-task-cwd-warning",
        request=request,
        runtime=runtime,
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
    request = _build_spawn_request().model_copy(update={"harness": HarnessId.OPENCODE.value})
    runtime = _build_launch_runtime(tmp_path=tmp_path, execution_cwd=outside_task_cwd)

    runtime_ctx = build_launch_context(
        spawn_id="p-opencode-task-parent",
        request=request,
        runtime=runtime,
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
    runtime = _build_launch_runtime(tmp_path=tmp_path, execution_cwd=outside_task_cwd)

    runtime_ctx = build_launch_context(
        spawn_id="p-task-cwd-warning-pi",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    warning_codes = {warning.code for warning in runtime_ctx.warnings}
    assert "task_cwd_not_projected" in warning_codes
    assert runtime_ctx.binding.run_params.task_cwd == outside_task_cwd.as_posix()
    assert "MERIDIAN_TASK_CWD" in (runtime_ctx.binding.run_params.appended_system_prompt or "")
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
        # Conflicting legacy aliases should be ignored when explicit roots exist.
        project_paths_project_root=legacy_project_root.as_posix(),
        project_paths_execution_cwd=legacy_task_cwd.as_posix(),
    )
    runtime_ctx = build_launch_context(
        spawn_id="p-runtime-root-explicit",
        request=_build_spawn_request(),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.project_root == tmp_path
    assert runtime_ctx.control_root == tmp_path
    assert runtime_ctx.execution_cwd == explicit_task_cwd
    assert runtime_ctx.binding.run_params.task_cwd == explicit_task_cwd.as_posix()


def test_build_launch_context_claude_preflight_uses_control_root_when_task_cwd_differs(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    execution_cwd = tmp_path / ".meridian" / "spawns" / "p-parent"
    execution_cwd.mkdir(parents=True)
    request = SpawnRequest(
        model="claude-sonnet-4-5",
        harness=HarnessId.CLAUDE.value,
        prompt="hello",
        extra_args=("--user-tail", "1"),
    )
    runtime = _build_launch_runtime(tmp_path=tmp_path, execution_cwd=execution_cwd)

    runtime_ctx = build_launch_context(
        spawn_id="p-claude-preflight",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.binding.run_params.control_root == tmp_path.as_posix()
    assert runtime_ctx.binding.run_params.task_cwd == execution_cwd.as_posix()
    assert runtime_ctx.binding.run_params.extra_args == ("--user-tail", "1")


def test_build_launch_context_emits_child_spawn_id(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-parent")
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    request = _build_spawn_request()
    runtime = _build_launch_runtime(tmp_path=tmp_path)

    runtime_ctx = build_launch_context(
        spawn_id="p-child",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    bind_env = runtime_ctx.binding.environment.bind_env_overrides
    assert bind_env["MERIDIAN_SPAWN_ID"] == "p-child"
    assert bind_env["MERIDIAN_PARENT_SPAWN_ID"] == "p-parent"


def test_build_launch_context_opencode_includes_context_paths_in_external_directory(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CONTEXT-PROJ-2: OpenCode projection includes all context paths."""
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gemini-2.5-pro",
        harness=HarnessId.OPENCODE,
    )
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    (tmp_path / "meridian.local.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "ctx/work"',
                'archive = "ctx/archive/work"',
                "",
                "[context.kb]",
                'path = "ctx/kb"',
                "",
                "[context.strategy]",
                'path = "ctx/strategy"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "ctx" / "work").mkdir(parents=True)
    (tmp_path / "ctx" / "archive" / "work").mkdir(parents=True)
    (tmp_path / "ctx" / "kb").mkdir(parents=True)
    (tmp_path / "ctx" / "strategy").mkdir(parents=True)

    request = _build_spawn_request()
    request = request.model_copy(update={"harness": HarnessId.OPENCODE.value})
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-opencode-context-proj",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    bind_env = runtime_ctx.binding.environment.bind_env_overrides
    assert "OPENCODE_CONFIG_CONTENT" in bind_env
    config = json.loads(bind_env["OPENCODE_CONFIG_CONTENT"])
    external_dirs = config.get("permission", {}).get("external_directory", {})

    work_path = (tmp_path / "ctx" / "work").as_posix() + "/**"
    kb_path = (tmp_path / "ctx" / "kb").as_posix() + "/**"
    archive_path = (tmp_path / "ctx" / "archive" / "work").as_posix() + "/**"
    strategy_path = (tmp_path / "ctx" / "strategy").as_posix() + "/**"

    assert work_path in external_dirs
    assert kb_path in external_dirs
    assert archive_path in external_dirs
    assert strategy_path in external_dirs
    assert external_dirs[work_path] == "allow"
    assert external_dirs[kb_path] == "allow"
    assert external_dirs[archive_path] == "allow"
    assert external_dirs[strategy_path] == "allow"
