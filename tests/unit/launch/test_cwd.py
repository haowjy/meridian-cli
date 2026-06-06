from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.launch.cwd import LaunchDirectoryContext, TaskCwdResolution, resolve_task_cwd
from meridian.lib.state import work_store


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    authority_root = tmp_path / "authority"
    state_dir = tmp_path / "state"
    authority_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return authority_root, state_dir


def _work_with_task_dir(state_dir: Path, tmp_path: Path, name: str) -> str:
    item = work_store.create_work_item(state_dir, name, "", None)
    task_dir = tmp_path / f"{name}-task-dir"
    task_dir.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_task_dir(state_dir, item.name, task_dir=task_dir.as_posix())
    return item.name


def test_resolve_task_cwd_uses_explicit_task_dir_override_first(tmp_path: Path) -> None:
    authority_root, state_dir = _roots(tmp_path)
    explicit = tmp_path / "explicit-task"
    explicit.mkdir(parents=True, exist_ok=True)

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        explicit_task_dir=explicit.as_posix(),
    )

    assert resolved.task_cwd == explicit.resolve()
    assert resolved.source == "explicit-task-dir"


def test_resolve_task_cwd_rejects_missing_explicit_task_dir(tmp_path: Path) -> None:
    authority_root, state_dir = _roots(tmp_path)

    with pytest.raises(ValueError, match="task_dir does not exist"):
        resolve_task_cwd(
            authority_root,
            project_state_dir=state_dir,
            explicit_task_dir=(tmp_path / "missing-task-dir").as_posix(),
        )


def test_resolve_task_cwd_explicit_work_uses_work_item_task_dir(tmp_path: Path) -> None:
    authority_root, state_dir = _roots(tmp_path)
    work_id = _work_with_task_dir(state_dir, tmp_path, "feature-work")

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        explicit_work_id=work_id,
    )

    assert resolved.task_cwd == (tmp_path / "feature-work-task-dir").resolve()
    assert resolved.source == "explicit-work-task-dir"
    assert resolved.work_item == work_id


def test_resolve_task_cwd_explicit_work_is_boundary_over_ambient(tmp_path: Path) -> None:
    authority_root, state_dir = _roots(tmp_path)
    explicit = work_store.create_work_item(state_dir, "explicit-work", "", None)
    ambient_id = _work_with_task_dir(state_dir, tmp_path, "ambient-work")

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        explicit_work_id=explicit.name,
        ambient_work_id=ambient_id,
    )

    assert resolved.task_cwd == authority_root.resolve()
    assert resolved.source == "explicit-work-authority-root"
    assert resolved.work_item == explicit.name


def test_resolve_task_cwd_ambient_work_uses_task_dir(tmp_path: Path) -> None:
    authority_root, state_dir = _roots(tmp_path)
    ambient_id = _work_with_task_dir(state_dir, tmp_path, "ambient-work")

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        ambient_work_id=ambient_id,
    )

    assert resolved.task_cwd == (tmp_path / "ambient-work-task-dir").resolve()
    assert resolved.source == "ambient-work-task-dir"
    assert resolved.work_item == ambient_id


def test_launch_directory_context_keeps_subprocess_cwd_at_authority_root(tmp_path: Path) -> None:
    authority_root, _ = _roots(tmp_path)
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)

    context = LaunchDirectoryContext.from_task_cwd_resolution(
        authority_root=authority_root,
        task_cwd_resolution=TaskCwdResolution(
            task_cwd=task_dir,
            source="explicit-task-dir",
            work_item="feature",
        ),
    )

    assert context.logical_task_cwd == task_dir.resolve()
    assert context.reference_anchor == task_dir.resolve()
    assert context.actual_process_cwd == authority_root.resolve()


def test_resolve_task_cwd_uses_caller_cwd_outside_project_tree(tmp_path: Path) -> None:
    authority_root, state_dir = _roots(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        caller_cwd=worktree.as_posix(),
    )

    assert resolved.task_cwd == worktree.resolve()
    assert resolved.source == "ambient-cwd"
    assert resolved.work_item is None


def test_resolve_task_cwd_ignores_caller_cwd_inside_project_tree(tmp_path: Path) -> None:
    authority_root, state_dir = _roots(tmp_path)
    inside = authority_root / "src" / "meridian"
    inside.mkdir(parents=True, exist_ok=True)

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        caller_cwd=inside.as_posix(),
    )

    assert resolved.task_cwd == authority_root.resolve()
    assert resolved.source == "authority-root"


def test_resolve_task_cwd_explicit_task_dir_wins_over_caller_cwd(tmp_path: Path) -> None:
    authority_root, state_dir = _roots(tmp_path)
    explicit = tmp_path / "explicit-task"
    explicit.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        explicit_task_dir=explicit.as_posix(),
        caller_cwd=worktree.as_posix(),
    )

    assert resolved.task_cwd == explicit.resolve()
    assert resolved.source == "explicit-task-dir"


def test_resolve_task_cwd_work_item_task_dir_wins_over_caller_cwd(tmp_path: Path) -> None:
    authority_root, state_dir = _roots(tmp_path)
    work_id = _work_with_task_dir(state_dir, tmp_path, "feature-work")
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        ambient_work_id=work_id,
        caller_cwd=worktree.as_posix(),
    )

    assert resolved.task_cwd == (tmp_path / "feature-work-task-dir").resolve()
    assert resolved.source == "ambient-work-task-dir"


def test_resolve_task_cwd_caller_cwd_keeps_actual_process_cwd_at_authority_root(
    tmp_path: Path,
) -> None:
    authority_root, state_dir = _roots(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)

    resolved = resolve_task_cwd(
        authority_root,
        project_state_dir=state_dir,
        caller_cwd=worktree.as_posix(),
    )
    context = LaunchDirectoryContext.from_task_cwd_resolution(
        authority_root=authority_root,
        task_cwd_resolution=resolved,
    )

    assert context.logical_task_cwd == worktree.resolve()
    assert context.actual_process_cwd == authority_root.resolve()
