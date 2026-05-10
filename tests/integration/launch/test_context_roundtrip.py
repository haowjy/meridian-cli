from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import LaunchArgvIntent, LaunchRuntime, SpawnRequest


def test_launch_context_child_env_roundtrips_through_resolved_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_CHAT_ID", "chat-parent")
    monkeypatch.setenv("MERIDIAN_DEPTH", "2")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "work-parent")

    launch_context = build_launch_context(
        spawn_id="p-roundtrip",
        request=SpawnRequest(
            prompt="roundtrip",
            model="gpt-5.4",
            harness=HarnessId.CODEX.value,
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            report_output_path=(tmp_path / "report.md").as_posix(),
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    # MERIDIAN_HARNESS is informational (yield timing), not a policy override.
    assert launch_context.binding.environment.final_env["MERIDIAN_HARNESS"] == HarnessId.CODEX.value
    bind_env = launch_context.binding.environment.bind_env_overrides
    assert bind_env["MERIDIAN_HARNESS"] == HarnessId.CODEX.value

    for key, value in bind_env.items():
        monkeypatch.setenv(key, value)

    child_context = ResolvedContext.from_environment()

    assert child_context.depth == 3
    assert child_context.project_root == tmp_path
    assert child_context.runtime_root == tmp_path / ".meridian"
    assert child_context.chat_id == "chat-parent"
    assert child_context.work_id == "work-parent"
    assert child_context.work_dir == tmp_path / ".meridian" / "work" / "work-parent"
    assert child_context.kb_dir == tmp_path / ".meridian" / "kb"


def test_launch_context_child_env_roundtrips_spawn_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-parent")
    monkeypatch.setenv("MERIDIAN_DEPTH", "4")

    launch_context = build_launch_context(
        spawn_id="p-child",
        request=SpawnRequest(
            prompt="identity",
            model="gpt-5.4",
            harness=HarnessId.CODEX.value,
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            report_output_path=(tmp_path / "report.md").as_posix(),
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    for key, value in launch_context.binding.environment.bind_env_overrides.items():
        monkeypatch.setenv(key, value)

    child_context = ResolvedContext.from_environment()

    assert child_context.spawn_id == "p-child"
    assert child_context.parent_spawn_id == "p-parent"
