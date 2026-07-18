"""Integration coverage for inherited MERIDIAN_TASK_DIR launch resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.composition_spawn import bind_spawn_launch_context
from meridian.lib.launch.context import RuntimeBindings
from meridian.lib.launch.plan import build_spawn_mars_runtime
from meridian.lib.launch.request import LaunchArgvIntent
from meridian.lib.ops.runtime import build_runtime
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.ops.spawn.prepare import build_create_payload
from meridian.lib.state.spawn_scope import write_spawn_scope_task_dir
from tests.support.launch import stub_bundle_request_and_resolve

pytestmark = pytest.mark.slow


def _seed_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (project_root / ".meridian").mkdir(parents=True)
    return project_root


def test_inherited_task_dir_round_trips_through_dry_run_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    inherited = tmp_path / "parent-worktree"
    inherited.mkdir(parents=True)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.setenv("MERIDIAN_TASK_DIR", inherited.as_posix())
    runtime = build_runtime(project_root)

    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="task",
            model="gpt-5.4",
            harness="codex",
            project_root=str(project_root),
            dry_run=True,
        ),
        runtime=runtime,
    )

    assert artifacts.request.task_cwd == inherited.resolve().as_posix()
    assert artifacts.request.task_cwd_source == "inherited-task-dir"


def test_bind_child_env_uses_resolved_task_dir_not_stale_parent_inherited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    parent_inherited = tmp_path / "parent-worktree"
    child_task_dir = tmp_path / "child-worktree"
    parent_inherited.mkdir(parents=True)
    child_task_dir.mkdir(parents=True)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.setenv("MERIDIAN_TASK_DIR", parent_inherited.as_posix())
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-parent")
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    runtime = build_runtime(project_root)

    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="task",
            model="gpt-5.4",
            harness="codex",
            project_root=str(project_root),
            task_dir=child_task_dir.as_posix(),
            dry_run=True,
        ),
        runtime=runtime,
    )
    launch_runtime = build_spawn_mars_runtime(
        runtime=runtime,
        runtime_root=project_root / ".meridian",
        control_root=project_root,
        execution_cwd=artifacts.request.task_cwd,
        argv_intent=LaunchArgvIntent.REQUIRED,
    )
    bound = bind_spawn_launch_context(
        prepared=artifacts.prepared,
        bindings=RuntimeBindings(
            spawn_id="p-child",
            dry_run=True,
        ),
        runtime=launch_runtime,
        harness_registry=get_default_harness_registry(),
    )

    child_env = bound.binding.environment.child_context_env
    assert child_env["MERIDIAN_TASK_DIR"] == child_task_dir.resolve().as_posix()
    assert child_env["MERIDIAN_TASK_DIR"] != parent_inherited.as_posix()


def test_parent_scope_file_task_dir_is_inherited_by_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent's mid-session ``task-dir set`` (scope file) propagates to children.

    Regression: prepare must inherit the parent's scope/env provenance without
    folding work-item or project-root defaults into the inherited tier, which
    would shadow resolve_task_cwd's lower tiers.
    """

    project_root = _seed_project(tmp_path)
    scope_dir = tmp_path / "scope-worktree"
    scope_dir.mkdir(parents=True)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-parent")
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    runtime = build_runtime(project_root)
    # Establish the project (runtime root) before writing the scope file so the
    # write and prepare's read resolve the same spawn-log dir.
    write_spawn_scope_task_dir(runtime.project_root, "p-parent", scope_dir)

    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="task",
            model="gpt-5.4",
            harness="codex",
            project_root=str(project_root),
            dry_run=True,
        ),
        runtime=runtime,
    )

    assert artifacts.request.task_cwd == scope_dir.resolve().as_posix()
    assert artifacts.request.task_cwd_source == "inherited-task-dir"


def test_stale_inherited_task_dir_raises_through_dry_run_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _seed_project(tmp_path)
    stale_inherited = tmp_path / "deleted-worktree"
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.setenv("MERIDIAN_TASK_DIR", stale_inherited.as_posix())
    runtime = build_runtime(project_root)

    with pytest.raises(ValueError, match="Inherited MERIDIAN_TASK_DIR does not exist"):
        build_create_payload(
            SpawnCreateInput(
                prompt="task",
                model="gpt-5.4",
                harness="codex",
                project_root=str(project_root),
                dry_run=True,
            ),
            runtime=runtime,
        )
