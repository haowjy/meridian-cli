from __future__ import annotations

from pathlib import Path

from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.work_dashboard import (
    WorkShowInput,
    work_show_sync,
)
from meridian.lib.state import work_store


def _setup_project(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    roots = resolve_roots(project_root.as_posix())
    return project_root, roots.project_state_dir


def test_work_show_includes_stored_task_dir(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "feature-a", "", None)
    task_dir = tmp_path / "feature-a-task"
    task_dir.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_task_dir(project_state_dir, item.name, task_dir=task_dir.as_posix())

    output = work_show_sync(WorkShowInput(work_id=item.name, project_root=project_root.as_posix()))

    assert output.task_dir == task_dir.resolve().as_posix()
    formatted = output.format_text()
    assert f"Task dir: {task_dir.resolve().as_posix()}" in formatted
    assert "Worktree" not in formatted


def test_work_show_includes_cleared_task_dir_as_null(tmp_path: Path) -> None:
    project_root, project_state_dir = _setup_project(tmp_path)
    item = work_store.create_work_item(project_state_dir, "feature-b", "", None)
    task_dir = tmp_path / "feature-b-task"
    task_dir.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_task_dir(project_state_dir, item.name, task_dir=task_dir.as_posix())
    work_store.update_work_item_task_dir(project_state_dir, item.name, task_dir=None)

    output = work_show_sync(WorkShowInput(work_id=item.name, project_root=project_root.as_posix()))

    assert output.task_dir is None
