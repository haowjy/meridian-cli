# qa-validated: test-suite-redesign
"""Tests for build_launch_context — environment variable injection and binding."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meridian.lib.core.child_env import ALLOWED_CHILD_ENV_KEYS
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)

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
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=resolved_execution_cwd.as_posix(),
    )


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude"]\n',
        encoding="utf-8",
    )


def test_build_launch_context_projects_runtime_child_env_paths(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_CHAT_ID", "c-parent")
    monkeypatch.setenv("MERIDIAN_DEPTH", "2")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "work-alpha")
    request = _build_spawn_request()
    runtime = _build_launch_runtime(tmp_path=tmp_path)

    runtime_ctx = build_launch_context(
        spawn_id="p-child-env",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    bind_env = runtime_ctx.binding.environment.bind_env_overrides
    assert bind_env["MERIDIAN_DEPTH"] == "3"
    assert bind_env["MERIDIAN_SPAWN_ID"] == "p-child-env"
    assert bind_env["MERIDIAN_CHAT_ID"] == "c-parent"
    assert bind_env["MERIDIAN_PROJECT_DIR"] == tmp_path.as_posix()
    assert bind_env["MERIDIAN_RUNTIME_DIR"] == (tmp_path / ".meridian").as_posix()
    assert bind_env["MERIDIAN_ACTIVE_WORK_ID"] == "work-alpha"
    assert bind_env["MERIDIAN_ACTIVE_WORK_DIR"] == (
        tmp_path / ".meridian" / "work" / "work-alpha"
    ).as_posix()
    assert bind_env["MERIDIAN_CONTEXT_WORK_DIR"] == (tmp_path / ".meridian" / "work").as_posix()
    assert bind_env["MERIDIAN_CONTEXT_KB_DIR"] == (tmp_path / ".meridian" / "kb").as_posix()
    assert bind_env["MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR"] == (
        tmp_path / ".meridian" / "archive" / "work"
    ).as_posix()
    # MERIDIAN_HARNESS is informational (yield timing), not a policy override.
    assert bind_env["MERIDIAN_HARNESS"] == "codex"
    assert runtime_ctx.binding.environment.final_env["MERIDIAN_HARNESS"] == "codex"
    assert runtime_ctx.binding.environment.child_context_env["MERIDIAN_SPAWN_ID"] == "p-child-env"
    assert runtime_ctx.binding.environment.runtime_override_env == {}
    assert runtime_ctx.binding.environment.bind_env_overrides["MERIDIAN_HARNESS"] == "codex"
    assert runtime_ctx.binding.environment.final_env["MERIDIAN_HARNESS"] == "codex"
    unexpected = {
        key
        for key in bind_env
        if key not in ALLOWED_CHILD_ENV_KEYS
        and not key.startswith("MERIDIAN_CONTEXT_")
        and key != "MERIDIAN_HARNESS"
    }
    assert unexpected == set()


def test_build_launch_context_uses_runtime_override_snapshot_not_live_env(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
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
    assert runtime_ctx.binding.environment.runtime_override_env == {
        "MERIDIAN_APPROVAL": "confirm"
    }
    assert runtime_ctx.binding.environment.bind_env_overrides["MERIDIAN_APPROVAL"] == "confirm"
    assert runtime_ctx.binding.environment.final_env["MERIDIAN_APPROVAL"] == "confirm"


def test_build_launch_context_explicit_empty_snapshot_blocks_live_policy_env_leak(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
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
    assert bind_env["MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR"] == (
        tmp_path / "ctx/archive/work"
    ).as_posix()
    assert bind_env["MERIDIAN_CONTEXT_KB_DIR"] == (tmp_path / "ctx/kb").as_posix()
    assert bind_env["MERIDIAN_CONTEXT_STRATEGY_DIR"] == (tmp_path / "ctx/strategy").as_posix()


def test_build_launch_context_env_keeps_project_root_when_execution_cwd_differs(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    execution_cwd = tmp_path / ".meridian" / "spawns" / "p-parent"
    execution_cwd.mkdir(parents=True)
    request = _build_spawn_request()
    runtime = _build_launch_runtime(tmp_path=tmp_path, execution_cwd=execution_cwd)

    runtime_ctx = build_launch_context(
        spawn_id="p-child-env",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    bind_env = runtime_ctx.binding.environment.bind_env_overrides
    assert runtime_ctx.execution_cwd == execution_cwd
    assert bind_env["MERIDIAN_PROJECT_DIR"] == tmp_path.as_posix()
    assert bind_env["MERIDIAN_CONTEXT_KB_DIR"] == (
        tmp_path / ".meridian" / "kb"
    ).as_posix()


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


def test_build_launch_context_projects_context_paths_to_workspace_roots(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CONTEXT-PROJ-1: Context paths are included in workspace projection."""
    _write_minimal_mars_config(tmp_path)
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
    # Create the directories so they exist for projection
    (tmp_path / "ctx" / "work").mkdir(parents=True)
    (tmp_path / "ctx" / "archive" / "work").mkdir(parents=True)
    (tmp_path / "ctx" / "kb").mkdir(parents=True)
    (tmp_path / "ctx" / "strategy").mkdir(parents=True)

    request = _build_spawn_request()
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary-context-proj",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    # Verify env vars are still exported (existing behavior)
    bind_env = runtime_ctx.binding.environment.bind_env_overrides
    assert bind_env["MERIDIAN_CONTEXT_WORK_DIR"] == (tmp_path / "ctx" / "work").as_posix()
    assert bind_env["MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR"] == (
        tmp_path / "ctx" / "archive" / "work"
    ).as_posix()
    assert bind_env["MERIDIAN_CONTEXT_KB_DIR"] == (tmp_path / "ctx" / "kb").as_posix()
    assert bind_env["MERIDIAN_CONTEXT_STRATEGY_DIR"] == (
        tmp_path / "ctx" / "strategy"
    ).as_posix()

    # Verify workspace projection includes context paths for all harnesses.
    # For OpenCode: check OPENCODE_CONFIG_CONTENT env override.
    if "OPENCODE_CONFIG_CONTENT" in bind_env:
        config = json.loads(bind_env["OPENCODE_CONFIG_CONTENT"])
        external_dirs = config.get("permission", {}).get("external_directory", {})
        assert (tmp_path / "ctx" / "work").as_posix() in external_dirs
        assert (tmp_path / "ctx" / "kb").as_posix() in external_dirs
        assert (tmp_path / "ctx" / "strategy").as_posix() in external_dirs

    projected_roots = {path.as_posix() for path in runtime_ctx.binding.spec.projected_roots}
    assert (tmp_path / "ctx" / "work").as_posix() in projected_roots
    assert (tmp_path / "ctx" / "archive" / "work").as_posix() in projected_roots
    assert (tmp_path / "ctx" / "kb").as_posix() in projected_roots
    assert (tmp_path / "ctx" / "strategy").as_posix() in projected_roots


def test_build_launch_context_opencode_includes_context_paths_in_external_directory(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CONTEXT-PROJ-2: OpenCode projection includes all context paths."""
    _write_minimal_mars_config(tmp_path)
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
    request = request.model_copy(update={"harness": HarnessId.OPENCODE.value, "model": ""})
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
