from __future__ import annotations

from pathlib import Path

from meridian.lib.launch.request import LaunchRuntime
from meridian.lib.ops.spawn.execute_bg import _resolve_background_execution_cwd


def test_background_execution_cwd_prefers_explicit_requested_task_cwd(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    explicit_task_cwd = tmp_path / "explicit-task"
    legacy_task_cwd = tmp_path / "legacy-task"
    project_root.mkdir(parents=True, exist_ok=True)
    explicit_task_cwd.mkdir(parents=True, exist_ok=True)
    legacy_task_cwd.mkdir(parents=True, exist_ok=True)

    runtime_request = LaunchRuntime(
        runtime_root=(tmp_path / ".meridian").as_posix(),
        config_root=project_root.as_posix(),
        control_root=project_root.as_posix(),
        requested_task_cwd=explicit_task_cwd.as_posix(),
        project_paths_project_root=project_root.as_posix(),
        project_paths_execution_cwd=legacy_task_cwd.as_posix(),
    )

    resolved = _resolve_background_execution_cwd(
        runtime_request=runtime_request,
        project_root=project_root,
        work_id=None,
    )

    assert resolved == explicit_task_cwd.as_posix()


def test_background_execution_cwd_falls_back_to_legacy_task_alias(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    legacy_task_cwd = tmp_path / "legacy-task"
    project_root.mkdir(parents=True, exist_ok=True)
    legacy_task_cwd.mkdir(parents=True, exist_ok=True)

    runtime_request = LaunchRuntime(
        runtime_root=(tmp_path / ".meridian").as_posix(),
        config_root=project_root.as_posix(),
        control_root=project_root.as_posix(),
        requested_task_cwd=None,
        project_paths_project_root=project_root.as_posix(),
        project_paths_execution_cwd=legacy_task_cwd.as_posix(),
    )

    resolved = _resolve_background_execution_cwd(
        runtime_request=runtime_request,
        project_root=project_root,
        work_id=None,
    )

    assert resolved == legacy_task_cwd.as_posix()
