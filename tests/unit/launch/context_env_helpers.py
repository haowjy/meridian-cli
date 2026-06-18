"""Shared helpers for launch context-env contract tests."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.types import HarnessId
from meridian.lib.launch.prompt_context import CONTEXT_PROMPT_HEADER
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)


def build_spawn_request(
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


def build_launch_runtime(
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
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=resolved_execution_cwd.as_posix(),
    )


def write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


def assert_materialized_work_dir_parity(
    *,
    run_params_prompt: str,
    run_params_appended_system_prompt: str,
    child_env: dict[str, str],
    expected_work_dir: Path,
    parent_ambient: Path | None = None,
) -> None:
    """Child env work dir matches injected prompt when context-env is present."""
    resolved = expected_work_dir.as_posix()
    assert child_env["MERIDIAN_ACTIVE_WORK_DIR"] == resolved

    materialized_channels = (
        run_params_prompt,
        run_params_appended_system_prompt,
    )
    if any(CONTEXT_PROMPT_HEADER in channel for channel in materialized_channels):
        assert any(resolved in channel for channel in materialized_channels)
    if parent_ambient is not None:
        parent_resolved = parent_ambient.as_posix()
        assert parent_resolved not in run_params_prompt
        assert parent_resolved not in run_params_appended_system_prompt


def write_codex_subagent_profile(project_root: Path) -> None:
    agents_dir = project_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "meridian-subagent.md").write_text(
        "---\n"
        "name: meridian-subagent\n"
        "description: Test subagent profile\n"
        "model: gpt-5.3-codex\n"
        "---\n"
        "\n"
        "NATIVE_AGENT_PROFILE_BODY_FOR_BIND_REFRESH.\n",
        encoding="utf-8",
    )
