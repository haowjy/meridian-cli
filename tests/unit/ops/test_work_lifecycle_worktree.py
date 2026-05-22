from __future__ import annotations

from pathlib import Path

from meridian.lib.launch.cwd import resolve_task_cwd
from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.work_lifecycle import (
    WorkClearWorktreeInput,
    WorkSetWorktreeInput,
    work_clear_worktree_sync,
    work_set_worktree_sync,
)
from meridian.lib.state import work_store


def test_work_set_and_clear_worktree(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    roots = resolve_roots(project_root.as_posix())
    work = work_store.create_work_item(roots.project_state_dir, "feature-x", "", None)
    worktree_path = tmp_path / "feature-x-worktree"
    worktree_path.mkdir(parents=True, exist_ok=True)

    set_output = work_set_worktree_sync(
        WorkSetWorktreeInput(
            work_id=work.name,
            path=worktree_path.as_posix(),
            project_root=project_root.as_posix(),
        )
    )
    assert set_output.worktree_path == worktree_path.resolve().as_posix()
    assert (
        work_store.get_work_item(roots.project_state_dir, work.name).worktree_path
        == worktree_path.resolve().as_posix()
    )

    clear_output = work_clear_worktree_sync(
        WorkClearWorktreeInput(work_id=work.name, project_root=project_root.as_posix())
    )
    assert clear_output.work_id == work.name
    assert work_store.get_work_item(roots.project_state_dir, work.name).worktree_path is None


def test_work_clear_worktree_makes_spawn_resolution_fall_back_to_authority_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    roots = resolve_roots(project_root.as_posix())
    work = work_store.create_work_item(roots.project_state_dir, "feature-work", "", None)
    worktree_path = tmp_path / "feature-worktree"
    worktree_path.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_worktree(
        roots.project_state_dir,
        work.name,
        path=worktree_path.as_posix(),
    )

    work_clear_worktree_sync(
        WorkClearWorktreeInput(work_id=work.name, project_root=project_root.as_posix())
    )
    resolved = resolve_task_cwd(
        project_root,
        project_state_dir=roots.project_state_dir,
        explicit_work_id=work.name,
    )

    assert resolved.task_cwd == project_root.resolve()
    assert resolved.source == "authority-root"
